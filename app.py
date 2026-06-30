"""
本地 FastAPI 服务 —— 配合 ngrok 让 Zoho Creator 调用本机解析。

流程(后台异步, 因为多页 OCR 较慢, 会超过 Deluge 的 invokeurl 超时):
  Zoho 提交 -> Deluge POST /zoho/webhook -> 本服务立刻回 202
  后台: Processing_Stage 依次写 Parsing -> Extracting -> Writing Back -> Done/Failed

本地测试(不接 Zoho): 启动后打开 http://127.0.0.1:8000/docs , 用 /parse-file 直接传 PDF。
日志: 同时打印到终端并写入 app.log。

依赖: pip install fastapi uvicorn[standard] requests python-multipart
启动: uvicorn app:app --reload --port 8000
内网穿透: ngrok http 8000
"""

import os
import time
import base64
import logging

# 日志: 同时输出到终端和 app.log。force=True 确保覆盖 uvicorn/其他模块可能已设的 root handler。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("app.log", encoding="utf-8")],
    force=True,
)
log = logging.getLogger("app")

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import requests

from parser import parse_pdf
from extractor import Form990Data

app = FastAPI(title="Form 990 Parser")

# ----------------------------------------------------------------------
# 配置 (用环境变量, 不要把密钥写进代码)
# ⚠️ Zoho 分数据中心, 域名不同。中国大陆多半是 .com.cn:
#    US: accounts.zoho.com / www.zohoapis.com      CN: accounts.zoho.com.cn / www.zohoapis.com.cn
# ----------------------------------------------------------------------
ZOHO_ACCOUNTS = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
ZOHO_API = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.com")
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")
ZOHO_OWNER = os.getenv("ZOHO_ACCOUNT_OWNER", "")
OCR_BACKEND = os.getenv("OCR_BACKEND", "tesseract")

# ↓↓↓ Zoho 字段 link name ↓↓↓
#  已核实: Processing_Stage / Total_Revenue / Rich_Text1(=你的 Error Log, 富文本) 确实存在。
#  待你核实: 下面三个目前【不在报表 Form_990_Parser_Report 里】, 需先在 Zoho 把它们加进该
#           报表的列, 再用 GET 记录看到真实 link name, 然后改这里。改之前回写会自动跳过它们。
ZOHO_FIELDS = {
    "stage":               "Processing_Stage",        # ✅ 已确认
    "revenue":             "Total_Revenue",            # ✅ 已确认
    "expenses_or_assets":  "Total_Expenses_Assets",    # ❓ 待确认(且需加进报表)
    "liabilities":         "Liabilities",              # ❓ 待确认(且需加进报表)
    "exec_comp":           "Executive_Compensation",   # ❓ 待确认(且需加进报表)
    "error_log":           "Rich_Text1",               # ✅ 实测就是 Rich_Text1
}


def to_zoho_payload(data: Form990Data, stage: str) -> dict:
    """
    解析结果 -> Zoho 字段。
    ⚠️ "Total Expenses / Assets" 含义不明确(英文 Expenses, 中文注 总资产):
       默认回写【总资产】data.total_assets_eoy; 要总支出改成 data.total_expenses。
    """
    return {
        ZOHO_FIELDS["stage"]:              stage,
        ZOHO_FIELDS["revenue"]:            data.total_revenue,
        ZOHO_FIELDS["expenses_or_assets"]: data.total_assets_eoy,   # ← 默认总资产; 要总支出改这里
        ZOHO_FIELDS["liabilities"]:        data.total_liabilities_eoy,
        ZOHO_FIELDS["exec_comp"]:          data.executive_compensation,
        ZOHO_FIELDS["error_log"]:          "\n".join(data.warnings) if data.warnings else "",
    }


# ----------------------------------------------------------------------
# Zoho Creator 客户端 (v2.1)
# ----------------------------------------------------------------------
def zoho_token() -> str:
    r = requests.post(f"{ZOHO_ACCOUNTS}/oauth/v2/token", params={
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def zoho_download_pdf(token, app_link, report_link, record_id, field_link) -> bytes:
    """从 File Upload 字段下载 PDF。"""
    url = (f"{ZOHO_API}/creator/v2.1/data/{ZOHO_OWNER}/{app_link}"
           f"/report/{report_link}/{record_id}/{field_link}/download")
    r = requests.get(url, headers={"Authorization": f"Zoho-oauthtoken {token}"}, timeout=60)
    r.raise_for_status()
    return r.content


def zoho_update(token, app_link, report_link, record_id, payload: dict):
    url = (f"{ZOHO_API}/creator/v2.1/data/{ZOHO_OWNER}/{app_link}"
           f"/report/{report_link}/{record_id}")
    r = requests.patch(url,
                       headers={"Authorization": f"Zoho-oauthtoken {token}",
                                "Content-Type": "application/json"},
                       json={"data": payload}, timeout=30)
    if not r.ok:
        # 把 Zoho 的错误正文带出来 —— 它通常会指明哪个字段 link name 不对
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)
    return r.json()


def zoho_get_record(token, app_link, report_link, record_id) -> dict:
    """GET 一条记录, 返回其 data(key 即该报表里实际存在的字段 link name)。"""
    url = (f"{ZOHO_API}/creator/v2.1/data/{ZOHO_OWNER}/{app_link}"
           f"/report/{report_link}/{record_id}")
    r = requests.get(url, headers={"Authorization": f"Zoho-oauthtoken {token}"}, timeout=30)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)
    return r.json().get("data", {})


def filter_existing(payload: dict, existing_keys: set):
    """拆成 (报表里存在的部分, 不存在被跳过的 key 列表)。"""
    kept = {k: v for k, v in payload.items() if k in existing_keys}
    dropped = [k for k in payload if k not in existing_keys]
    return kept, dropped


def _set_stage(token, app_link, report_link, record_id, stage, error=""):
    """只更新状态(和错误日志)。"""
    payload = {ZOHO_FIELDS["stage"]: stage}
    if error:
        payload[ZOHO_FIELDS["error_log"]] = error
    try:
        zoho_update(token, app_link, report_link, record_id, payload)
    except Exception as e:
        log.warning("更新状态失败: %s", e)
        if error:  # Error_Log 字段名也可能不对 -> 退而只更新状态, 至少让 Failed 显示
            try:
                zoho_update(token, app_link, report_link, record_id, {ZOHO_FIELDS["stage"]: stage})
            except Exception as e2:
                log.warning("仅更新状态也失败: %s", e2)


# ----------------------------------------------------------------------
# 后台处理: 下载 -> 解析(计时) -> 回写(只写报表里存在的字段)
# ----------------------------------------------------------------------
def process_record(app_link, report_link, record_id, pdf_field,
                   pdf_b64=None, pdf_bytes=None):
    token = None
    try:
        token = zoho_token()
    except Exception as e:
        log.warning("拿 Zoho token 失败 -> 回写会跳过, 但仍会解析并打印结果: %s", e)

    if token:
        _set_stage(token, app_link, report_link, record_id, "Parsing")

    # 1) 取 PDF 字节
    if pdf_bytes is None:
        try:
            if pdf_b64:
                pdf_bytes = base64.b64decode(pdf_b64)
            elif token:
                pdf_bytes = zoho_download_pdf(token, app_link, report_link, record_id, pdf_field)
            else:
                log.error("既没有直传文件, 也没有 token 可下载, 中止")
                return
        except Exception as e:
            log.exception("获取 PDF 失败")
            if token:
                _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))
            return

    # 2) 解析(计时)
    if token:
        _set_stage(token, app_link, report_link, record_id, "Extracting")
    try:
        t0 = time.time()
        data = parse_pdf(pdf_bytes, ocr_backend=OCR_BACKEND)
        log.info("解析(含 OCR)耗时 %.1fs", time.time() - t0)
    except Exception as e:
        log.exception("解析失败")
        if token:
            _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))
        return
    log.info("记录 %s 解析结果: %s", record_id, data.to_dict())

    # 3) 回写 Zoho —— 只写"该报表里实际存在"的字段, 缺的跳过并记录(避免一个坏字段拖垮整次写入)
    if token:
        try:
            _set_stage(token, app_link, report_link, record_id, "Writing Back")
            existing = set(zoho_get_record(token, app_link, report_link, record_id).keys())
            full = to_zoho_payload(data, "Done")
            payload, dropped = filter_existing(full, existing)
            if dropped:
                msg = "未写入(字段不在报表中, 请加进报表并核对 link name): " + ", ".join(dropped)
                log.warning("[报表=%s] %s", report_link, msg)
                # 把"缺哪些字段"也写进 Error Log, 让你在 Zoho 里直接看到
                if ZOHO_FIELDS["error_log"] in payload:
                    payload[ZOHO_FIELDS["error_log"]] = msg
            zoho_update(token, app_link, report_link, record_id, payload)
            log.info("回写成功, 已写入字段: %s", list(payload.keys()))
        except Exception as e:
            log.warning("回写 Zoho 失败: %s", e)
            _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))


# ----------------------------------------------------------------------
# 工具: 从请求里把"文本参数 + 文件"都收齐(query / multipart / JSON 都兼容)
# ----------------------------------------------------------------------
async def collect_request(request: Request):
    query_fields = dict(request.query_params)
    form_fields, pdf_bytes, pdf_name, file_key = {}, None, None, None

    ctype = request.headers.get("content-type", "")
    if "multipart/form-data" in ctype or "x-www-form-urlencoded" in ctype:
        form = await request.form()
        for key, value in form.items():
            if hasattr(value, "filename") and value.filename is not None:
                pdf_bytes = await value.read()
                pdf_name, file_key = value.filename, key
                log.info("收到文件: 字段名='%s' filename='%s' (%d bytes)",
                         key, pdf_name, len(pdf_bytes))
            else:
                form_fields[key] = value
    elif "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            if body.get("pdf_base64"):
                pdf_bytes = base64.b64decode(body["pdf_base64"])
            form_fields.update({k: v for k, v in body.items() if k != "pdf_base64"})

    merged = {**query_fields, **form_fields}
    return merged, query_fields, form_fields, pdf_bytes, pdf_name, file_key


# ----------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok", "ocr_backend": OCR_BACKEND}


@app.post("/parse-file")
async def parse_file(file: UploadFile = File(...)):
    """本地测试用: 直接上传 PDF, 同步返回解析 JSON (不碰 Zoho)。"""
    t0 = time.time()
    data = parse_pdf(await file.read(), ocr_backend=OCR_BACKEND)
    log.info("/parse-file 解析耗时 %.1fs", time.time() - t0)
    return data.to_dict()


@app.post("/zoho/debug")
async def debug_webhook(request: Request):
    """调试用: 回显 query 和表单两边收到的所有字段(含文件字段名+大小), 不解析、秒回。"""
    merged, query_fields, form_fields, pdf_bytes, pdf_name, file_key = await collect_request(request)
    out = {
        "content_type": request.headers.get("content-type", ""),
        "query_fields": query_fields,
        "form_text_fields": form_fields,
        "file_field": file_key,
        "filename": pdf_name,
        "file_size": (len(pdf_bytes) if pdf_bytes else 0),
    }
    log.info("=== DEBUG ===\n%s", out)
    return out


@app.post("/zoho/webhook")
async def zoho_webhook(request: Request, bg: BackgroundTasks):
    """
    Zoho Deluge 调用这里。文本参数无论在 query 还是 multipart 表单体都能收到,
    文件按"有 filename 的那项"自动识别。立即返回 202, OCR 在后台跑。
    """
    merged, query_fields, form_fields, pdf_bytes, pdf_name, file_key = await collect_request(request)

    # 保险: 收到的"文件"过小(如 Deluge 推来的占位)说明不是真 PDF, 忽略它, 改用 Zoho API 下载。
    if pdf_bytes is not None and len(pdf_bytes) < 1000:
        log.warning("收到的文件仅 %d 字节, 不像真 PDF -> 忽略, 改用 Zoho API 下载", len(pdf_bytes))
        pdf_bytes = file_key = pdf_name = None

    record_id = merged.get("record_id")
    app_link = merged.get("app_link_name")
    report_link = merged.get("report_link_name")
    pdf_field = merged.get("pdf_field", "IRS_PDF")

    if not (record_id and app_link and report_link):
        raise HTTPException(status_code=400, detail={
            "error": "缺少 record_id / app_link_name / report_link_name",
            "content_type": request.headers.get("content-type", ""),
            "query_fields": list(query_fields),
            "form_text_fields": list(form_fields),
            "file_field": file_key,
        })

    bg.add_task(process_record, app_link, report_link, record_id, pdf_field, None, pdf_bytes)
    return JSONResponse(status_code=202, content={
        "status": "accepted",
        "record_id": record_id,
        "received_file_field": file_key,
        "received_filename": pdf_name,
        "query_fields": list(query_fields),
        "form_text_fields": list(form_fields),
    })
