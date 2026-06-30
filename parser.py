"""
PDF parsing engine for Form 990.
Automatic text-layer vs OCR detection, then delegates extraction.
"""

import io
import json
import logging
import os
from pathlib import Path

from extractor import Form990Data, extract_fields

log = logging.getLogger("parser")

# ── Tesseract / poppler fallback for scoop installs ────────────────────────
# Add scoop shims to PATH if not already present (so tesseract + pdftoppm
# are found without manual $env:Path setup).
_SCOOP_SHM = "C:/Users/12285/scoop/shims"
if _SCOOP_SHM not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_SCOOP_SHM};{os.environ.get('PATH', '')}"

# If TESSDATA_PREFIX is not set, try scoop's tessdata path.
_TESSDATA_PATH = Path("C:/Users/12285/scoop/persist/tesseract/tessdata")
if not os.environ.get("TESSDATA_PREFIX") and _TESSDATA_PATH.is_dir():
    os.environ["TESSDATA_PREFIX"] = str(_TESSDATA_PATH)


# --------------------------------------------------------------------------
# Text extraction: digital layer (pdfplumber) / OCR (Tesseract / Textract)
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


def ocr_tesseract(pdf_bytes: bytes, dpi: int = 300, psm: int = 6) -> str:
    from pdf2image import convert_from_bytes
    import pytesseract
    cfg = f"--psm {psm}"
    pages = convert_from_bytes(pdf_bytes, dpi=dpi)
    return "\n".join(pytesseract.image_to_string(p, config=cfg) for p in pages)


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
# Orchestration
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
            "Text layer only %d chars / %d pages -> OCR (%s)",
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
