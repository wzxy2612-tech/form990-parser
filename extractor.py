"""
Field extraction logic for Form 990.
Data model, regex anchors, money parsing, extract_fields().
"""

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("extractor")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Form990Data:
    ein: Optional[str] = None
    org_name: Optional[str] = None
    tax_year: Optional[str] = None
    period_begin: Optional[str] = None
    period_end: Optional[str] = None

    total_revenue: Optional[float] = None
    total_expenses: Optional[float] = None
    total_assets_eoy: Optional[float] = None
    total_liabilities_eoy: Optional[float] = None
    net_assets_eoy: Optional[float] = None
    executive_compensation: Optional[float] = None

    source_type: Optional[str] = None
    raw_text_chars: int = 0
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------
# Money parsing
# --------------------------------------------------------------------------
MONEY_RE = re.compile(
    r"-?\(?\$?\s?[\d]{1,3}(?:,[\d]{3})+(?:\.\d{2})?\)?"
    r"|-?\(?\$?\s?\d{4,}(?:\.\d{2})?\)?"
)


def amounts_in(line: str):
    return [m.group(0) for m in MONEY_RE.finditer(line)]


def parse_amount(s):
    if s is None:
        return None
    s = s.strip()
    if not s or s in {"-", "—", "N/A"}:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.lstrip("$ ").startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    if s == "":
        return None
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Field anchors
#   take: "last" = rightmost column (current year / year-end)
#         "first" = leftmost column (col A total)
#         int    = Nth amount
# --------------------------------------------------------------------------
FIELD_ANCHORS = {
    "total_revenue":          ([r"(?<!\d)12\b.*total revenue"], "last"),
    "total_expenses":         ([r"(?<!\d)18\b.*total expenses"], "last"),
    "total_assets_eoy":       ([r"(?<!\d)20\b.*total assets"], "last"),
    "total_liabilities_eoy":  ([r"(?<!\d)21\b.*total liabilities"], "last"),
    "net_assets_eoy":         ([r"(?<!\d)22\b.*net assets"], "last"),
    "executive_compensation": ([r"(?<!\d)5\b.*compensation of current officers"], "first"),
}


def _pick(amts, take):
    if not amts:
        return None
    if take == "last":
        return amts[-1]
    if take == "first":
        return amts[0]
    return amts[min(int(take), len(amts) - 1)]


def extract_fields(lines):
    data = Form990Data()
    joined = "\n".join(lines)

    m = re.search(r"\b(\d{2}-\d{7})\b", joined)
    if m:
        data.ein = m.group(1)
    m = re.search(r"For the\s+(\d{4})\s+calendar year", joined, re.I)
    if m:
        data.tax_year = m.group(1)
    m = re.search(
        r"tax year beginning\s+([\d/\-]+)\s*,?\s*and ending\s+([\d/\-]+)",
        joined, re.I,
    )
    if m:
        data.period_begin, data.period_end = m.group(1).strip(), m.group(2).strip()

    for fname, (patterns, take) in FIELD_ANCHORS.items():
        for pat in patterns:
            for line in lines:
                if re.search(pat, line, re.I):
                    val = parse_amount(_pick(amounts_in(line), take))
                    if val is not None:
                        setattr(data, fname, val)
                    break
            if getattr(data, fname) is not None:
                break
    return data
