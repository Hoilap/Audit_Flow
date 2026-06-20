"""Shared utility functions for outbound_settlement_match."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

import pandas as pd


# ── Text helpers ─────────────────────────────────────────────

def text(value: Any) -> str:
    """Convert any value to a stripped string; None/NaN → ''."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


# ── Amount helpers ───────────────────────────────────────────

def parse_amount(value: Any) -> float:
    """Parse a numeric amount from a string, handling commas/currency symbols.

    Returns 0.0 for unparseable values.
    """
    s = text(value)
    if not s:
        return 0.0
    # Remove currency symbols and whitespace
    s = re.sub(r"[¥￥$€£元,\s]", "", s)
    # Handle parenthesised negatives: (123.45) → -123.45
    m = re.match(r"^\((.+)\)$", s)
    if m:
        s = "-" + m.group(1)
    try:
        return float(s)
    except ValueError:
        # Try extracting first number
        m2 = re.search(r"[-+]?\d+\.?\d*", s)
        if m2:
            return float(m2.group())
    return 0.0


# ── Date helpers ─────────────────────────────────────────────

def parse_date(value: Any) -> date | None:
    """Parse a date from various formats. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = text(value)
    if not s:
        return None
    try:
        dt = pd.to_datetime(s, dayfirst=False)
        if pd.notna(dt):
            return dt.date() if isinstance(dt, datetime) else dt
    except Exception:
        pass
    return None


def extract_month_key(file_path: str) -> str:
    """Extract a month key like '2022-01' from a file path.

    Handles directory names like '01-22', '02-22', '12' and
    filenames containing date patterns.
    """
    parts = str(file_path).replace("\\", "/").split("/")
    for part in reversed(parts):
        # Match "MM-YY" pattern (e.g. "01-22")
        m = re.match(r"^(\d{1,2})-(\d{2})$", part)
        if m:
            month, year = int(m.group(1)), int(m.group(2))
            full_year = 2000 + year if year < 100 else year
            return f"{full_year:04d}-{month:02d}"
        # Match "YYYYMM" in filename
        m2 = re.search(r"(\d{4})(\d{2})", part)
        if m2:
            y, mo = int(m2.group(1)), int(m2.group(2))
            if 2000 <= y <= 2099 and 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}"
        # Match bare month number (e.g. "12" for December)
        m3 = re.match(r"^(\d{1,2})$", part)
        if m3:
            month = int(m3.group(1))
            if 1 <= month <= 12:
                return f"2022-{month:02d}"  # Default year
    return "unknown"


def safe_read_csv(path: str, **kwargs) -> pd.DataFrame:
    """Read CSV trying multiple encodings."""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, **kwargs)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort
    return pd.read_csv(path, encoding="latin-1", **kwargs)


def safe_write_csv(df: pd.DataFrame, path: str):
    """Write CSV with utf-8-sig encoding (BOM for Excel compatibility)."""
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
