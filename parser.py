"""
Form 990 PDF 解析引擎。
自動檢測文字層（電子檔）與 OCR（掃描檔），然後交由欄位擷取器處理。
"""

import io
import json
import logging
import os
from pathlib import Path

from extractor import Form990Data, extract_fields

log = logging.getLogger("parser")

# ── Tesseract / poppler 備用路徑 (適用於 scoop 安裝) ────────────────────────
# 如果 PATH 中沒有 scoop shims，則自動加入 (以便無需手動設定 $env:Path 就能找到 tesseract 與 pdftoppm)。
_SCOOP_SHM = "C:/Users/12285/scoop/shims"
if _SCOOP_SHM not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_SCOOP_SHM};{os.environ.get('PATH', '')}"

# 如果未設定 TESSDATA_PREFIX，嘗試使用 scoop 的 tessdata 路徑。
_TESSDATA_PATH = Path("C:/Users/12285/scoop/persist/tesseract/tessdata")
if not os.environ.get("TESSDATA_PREFIX") and _TESSDATA_PATH.is_dir():
    os.environ["TESSDATA_PREFIX"] = str(_TESSDATA_PATH)

# ── OCR 效能優化配置 ──────────────────────────────────────────────────────
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "30"))  # 限制最大解析頁數，防止記憶體溢出
OCR_DPI = int(os.getenv("OCR_DPI", "200"))            # 300->200 省記憶體/提速; 這類表 200 夠清晰


# --------------------------------------------------------------------------
# 文字擷取：電子層 (pdfplumber) / OCR (Tesseract / Textract)
# --------------------------------------------------------------------------
def extract_text_layer(pdf_bytes: bytes):
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
    full = "\n".join(pages)
    return full, len(full.strip()), full.splitlines(), page_count


def ocr_tesseract(pdf_bytes: bytes, dpi: int = OCR_DPI, psm: int = 6, max_pages: int = MAX_OCR_PAGES) -> str:
    """逐頁渲染+OCR, 每頁用完即釋放 -> 峰值記憶體≈單頁, 適配 Render 等小記憶體環境。"""
    from pdf2image import convert_from_path, pdfinfo_from_path
    import pytesseract, tempfile
    cfg = f"--psm {psm}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp = tf.name
    texts = []
    try:
        total = pdfinfo_from_path(tmp)["Pages"]
        for i in range(1, min(max_pages, total) + 1):
            imgs = convert_from_path(tmp, dpi=dpi, first_page=i, last_page=i)
            texts.append(pytesseract.image_to_string(imgs[0], config=cfg))
            imgs[0].close()
            del imgs
        log.info("OCR 完成: %d/%d 頁 (dpi=%d)", len(texts), total, dpi)
    finally:
        os.unlink(tmp)
    return "\n".join(texts)


def ocr_textract(pdf_bytes: bytes, region: str = "us-east-1") -> str:
    import boto3
    client = boto3.client("textract", region_name=region)
    resp = client.analyze_document(
        Document={"Bytes": pdf_bytes},
        FeatureTypes=["FORMS", "TABLES"],
    )
    return "\n".join(
        b["Text"] for b in resp["Blocks"]
        if b["BlockType"] == "LINE" and "Text" in b
    )


# --------------------------------------------------------------------------
# 排程與核心調度
# --------------------------------------------------------------------------
def parse_pdf(
    pdf_bytes: bytes,
    ocr_backend: str = "tesseract",
    textract_region: str = "us-east-1",
) -> Form990Data:
    full, char_count, lines, page_count = extract_text_layer(pdf_bytes)

    if char_count >= 100 * max(page_count, 1):
        source_type = "digital"
    else:
        source_type = "scanned"
        log.info(
            "文字層僅有 %d 字元 / %d 頁 -> 啟動 OCR (%s)",
            char_count, page_count, ocr_backend,
        )
        text = (
            ocr_textract(pdf_bytes, textract_region)
            if ocr_backend == "textract"
            else ocr_tesseract(pdf_bytes)
        )
        lines = text.splitlines()

    data = extract_fields(lines)
    data.source_type = source_type
    data.raw_text_chars = char_count

    for fld, label in [
        ("total_revenue", "Total Revenue"),
        ("total_expenses", "Total Expenses"),
        ("total_assets_eoy", "Total Assets"),
        ("total_liabilities_eoy", "Total Liabilities"),
        ("executive_compensation", "Executive Compensation"),
    ]:
        if getattr(data, fld) is None:
            data.warnings.append(f"Could not extract {label} ({fld})")
    if data.ein is None:
        data.warnings.append("EIN not found")
    return data


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "pdf/Mozilla Foundation.pdf"
    with open(path, "rb") as f:
        result = parse_pdf(f.read(), ocr_backend="tesseract")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))