"""
本地 FastAPI 服務 —— 配合 ngrok 讓 Zoho Creator 調用本機解析。

流程(後台非同步, 因為多頁 OCR 較慢, 會超過 Deluge 的 invokeurl 超時):
  Zoho 提交 -> Deluge POST /zoho/webhook -> 本服務立刻回 202
  後台: Processing_Stage 依次寫 Parsing -> Extracting -> Writing Back -> Done/Failed

本地測試(不接 Zoho): 啟動後打開 http://127.0.0.1:8000/docs , 用 /parse-file 直接傳 PDF。
日誌: 同時打印到終端並寫入 app.log。

依賴: pip install fastapi uvicorn[standard] requests python-multipart pdf2image pytesseract
啟動: uvicorn app:app --reload --port 8000
內網穿透: ngrok http 8000
"""

import os
import time
import base64
import logging

# 日誌: 同時輸出到終端和 app.log。force=True 確保覆蓋 uvicorn/其他模組可能已設的 root handler。
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
# 配置 (用環境變數, 不要把金鑰寫進程式碼)
# ⚠️ Zoho 分數據中心, 網域名稱不同。中國大陸多半是 .com.cn:
#    US: accounts.zoho.com / www.zohoapis.com      CN: accounts.zoho.com.cn / www.zohoapis.com.cn
# ----------------------------------------------------------------------
ZOHO_ACCOUNTS = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
ZOHO_API = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.com")
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")
ZOHO_OWNER = os.getenv("ZOHO_ACCOUNT_OWNER", "")
OCR_BACKEND = os.getenv("OCR_BACKEND", "tesseract")

# ↓↓↓ OCR 效能優化配置 ↓↓↓
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "30"))  # 限制最大解析頁數，防止記憶體溢出
OCR_DPI = int(os.getenv("OCR_DPI", "200"))            # 300->200 省記憶體/提速; 這類表 200 夠清晰

# ↓↓↓ Zoho 欄位 link name ↓↓↓
#  已核實: Processing_Stage / Total_Revenue / Rich_Text1(=你的 Error Log, 富文本) 確實存在。
#  待你核實: 下面三個目前【不在報表 Form_990_Parser_Report 裡】, 需先在 Zoho 把它們加進該
#           報表的列, 再用 GET 記錄看到真實 link name, 然後改這裡。改之前回寫會自動跳過它們。
ZOHO_FIELDS = {
    "stage":               "Processing_Stage",        # ✅ 已確認
    "revenue":             "Total_Revenue",            # ✅ 已確認
    "expenses_or_assets":  "Total_Expenses_Assets",    # ❓ 待確認(且需加進報表)
    "liabilities":         "Liabilities",              # ❓ 待確認(且需加進報表)
    "exec_comp":           "Executive_Compensation",   # ❓ 待確認(且需加進報表)
    "error_log":           "Rich_Text1",               # ✅ 實測就是 Rich_Text1
}


def ocr_tesseract(pdf_bytes, dpi=OCR_DPI, psm=6, max_pages=MAX_OCR_PAGES):
    """逐頁渲染+OCR, 每頁用完即釋放 -> 峰值記憶體≈單頁, 適配 Render 等小記憶體環境。"""
    from pdf2image import convert_from_path, pdfinfo_from_path
    import pytesseract, tempfile
    cfg = f"--psm {psm}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes); tmp = tf.name
    texts = []
    try:
        total = pdfinfo_from_path(tmp)["Pages"]
        for i in range(1, min(max_pages, total) + 1):
            imgs = convert_from_path(tmp, dpi=dpi, first_page=i, last_page=i)
            texts.append(pytesseract.image_to_string(imgs[0], config=cfg))
            imgs[0].close(); del imgs
        log.info("OCR 完成: %d/%d 頁 (dpi=%d)", len(texts), total, dpi)
    finally:
        os.unlink(tmp)
    return "\n".join(texts)


def to_zoho_payload(data: Form990Data, stage: str) -> dict:
    """
    解析結果 -> Zoho 欄位。
    ⚠️ "Total Expenses / Assets" 含義不明確(英文 Expenses, 中文注 總資產):
       預設回寫【總資產】data.total_assets_eoy; 要總支出改成 data.total_expenses。
    """
    return {
        ZOHO_FIELDS["stage"]:              stage,
        ZOHO_FIELDS["revenue"]:            data.total_revenue,
        ZOHO_FIELDS["expenses_or_assets"]: data.total_assets_eoy,   # ← 預設總資產; 要總支出改這裡
        ZOHO_FIELDS["liabilities"]:        data.total_liabilities_eoy,
        ZOHO_FIELDS["exec_comp"]:          data.executive_compensation,
        ZOHO_FIELDS["error_log"]:          "\n".join(data.warnings) if data.warnings else "",
    }


# ----------------------------------------------------------------------
# Zoho Creator 客戶端 (v2.1)
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
    """從 File Upload 欄位下載 PDF。"""
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
        # 把 Zoho 的錯誤正文帶出來 —— 它通常會指明哪個欄位 link name 不對
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)
    return r.json()


def zoho_get_record(token, app_link, report_link, record_id) -> dict:
    """GET 一條記錄, 返回其 data(key 即該報表裡實際存在的欄位 link name)。"""
    url = (f"{ZOHO_API}/creator/v2.1/data/{ZOHO_OWNER}/{app_link}"
           f"/report/{report_link}/{record_id}")
    r = requests.get(url, headers={"Authorization": f"Zoho-oauthtoken {token}"}, timeout=30)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)
    return r.json().get("data", {})


def filter_existing(payload: dict, existing_keys: set):
    """拆成 (報表裡存在的部分, 不存在被跳過的 key 列表)。"""
    kept = {k: v for k, v in payload.items() if k in existing_keys}
    dropped = [k for k in payload if k not in existing_keys]
    return kept, dropped


def _set_stage(token, app_link, report_link, record_id, stage, error=""):
    """只更新狀態(和錯誤日誌)。"""
    payload = {ZOHO_FIELDS["stage"]: stage}
    if error:
        payload[ZOHO_FIELDS["error_log"]] = error
    try:
        zoho_update(token, app_link, report_link, record_id, payload)
    except Exception as e:
        log.warning("更新狀態失敗: %s", e)
        if error:  # Error_Log 欄位名也可能不對 -> 退而只更新狀態, 至少讓 Failed 顯示
            try:
                zoho_update(token, app_link, report_link, record_id, {ZOHO_FIELDS["stage"]: stage})
            except Exception as e2:
                log.warning("僅更新狀態也失敗: %s", e2)


# ----------------------------------------------------------------------
# 後台處理: 下載 -> 解析(計時) -> 回寫(只寫報表裡存在的欄位)
# ----------------------------------------------------------------------
def process_record(app_link, report_link, record_id, pdf_field,
                   pdf_b64=None, pdf_bytes=None):
    token = None
    try:
        token = zoho_token()
    except Exception as e:
        log.warning("拿 Zoho token 失敗 -> 回寫會跳過, 但仍會解析並列印結果: %s", e)

    if token:
        _set_stage(token, app_link, report_link, record_id, "Parsing")

    # 1) 取 PDF 位元組
    if pdf_bytes is None:
        try:
            if pdf_b64:
                pdf_bytes = base64.b64decode(pdf_b64)
            elif token:
                pdf_bytes = zoho_download_pdf(token, app_link, report_link, record_id, pdf_field)
            else:
                log.error("既沒有直傳文件, 也沒有 token 可下載, 中止")
                return
        except Exception as e:
            log.exception("獲取 PDF 失敗")
            if token:
                _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))
            return

    # 2) 解析(計時)
    if token:
        _set_stage(token, app_link, report_link, record_id, "Extracting")
    try:
        t0 = time.time()
        data = parse_pdf(pdf_bytes, ocr_backend=OCR_BACKEND)
        log.info("解析(含 OCR)耗時 %.1fs", time.time() - t0)
    except Exception as e:
        log.exception("解析失敗")
        if token:
            _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))
        return
    log.info("記錄 %s 解析結果: %s", record_id, data.to_dict())

    # 3) 回寫 Zoho —— 只寫"該報表裡實際存在"的欄位, 缺的跳過並記錄(避免一個壞欄位拖垮整次寫入)
    if token:
        try:
            _set_stage(token, app_link, report_link, record_id, "Writing Back")
            existing = set(zoho_get_record(token, app_link, report_link, record_id).keys())
            full = to_zoho_payload(data, "Done")
            payload, dropped = filter_existing(full, existing)
            if dropped:
                msg = "未寫入(欄位不在報表中, 請加進報表並核對 link name): " + ", ".join(dropped)
                log.warning("[報表=%s] %s", report_link, msg)
                # 把"缺哪些欄位"也寫進 Error Log, 讓你在 Zoho 裡直接看到
                if ZOHO_FIELDS["error_log"] in payload:
                    payload[ZOHO_FIELDS["error_log"]] = msg
            zoho_update(token, app_link, report_link, record_id, payload)
            log.info("回寫成功, 已寫入欄位: %s", list(payload.keys()))
        except Exception as e:
            log.warning("回寫 Zoho 失敗: %s", e)
            _set_stage(token, app_link, report_link, record_id, "Failed", error=str(e))


# ----------------------------------------------------------------------
# 工具: 從請求裡把"文本參數 + 文件"都收齊(query / multipart / JSON 都兼容)
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
                log.info("收到文件: 欄位名='%s' filename='%s' (%d bytes)",
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
# 端點
# ----------------------------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok", "ocr_backend": OCR_BACKEND}


@app.post("/parse-file")
async def parse_file(file: UploadFile = File(...)):
    """本地測試用: 直接上傳 PDF, 同步返回解析 JSON (不碰 Zoho)。"""
    t0 = time.time()
    data = parse_pdf(await file.read(), oauthtoken=None)  # 備註：此處如需整合 ocr_tesseract 可於 parser 內調用
    log.info("/parse-file 解析耗時 %.1fs", time.time() - t0)
    return data.to_dict()


@app.post("/zoho/debug")
async def debug_webhook(request: Request):
    """調試用: 回顯 query 和表單兩邊收到的所有欄位(含文件欄位名+大小), 不解析、秒回。"""
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
    Zoho Deluge 調用這裡。文本參數無論在 query 還是 multipart 表單體都能收到,
    文件按"有 filename 的那項"自動識別。立即返回 202, OCR 在後台跑。
    """
    merged, query_fields, form_fields, pdf_bytes, pdf_name, file_key = await collect_request(request)

    # 保險: 收到的"文件"過小(如 Deluge 推來的占位)說明不是真 PDF, 忽略它, 改用 Zoho API 下載。
    if pdf_bytes is not None and len(pdf_bytes) < 1000:
        log.warning("收到的文件僅 %d 位元組, 不像真 PDF -> 忽略, 改用 Zoho API 下載", len(pdf_bytes))
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