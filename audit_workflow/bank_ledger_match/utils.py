from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd


def text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def parse_amount(value: object) -> float:
    raw = text(value)
    if raw == "":
        return 0.0
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = (
        raw.replace(",", "")
        .replace("￥", "")
        .replace("¥", "")
        .replace("元", "")
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
    )
    if cleaned in {"-", "--"}:
        return 0.0
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if not match:
            return 0.0
        amount = Decimal(match.group(0))
    if negative:
        amount = -amount
    return float(amount)


def amount_to_cents(value: object) -> int:
    amount = Decimal(str(parse_amount(value)))
    cents = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def parse_int(value: object) -> int | None:
    raw = text(value)
    if raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        match = re.search(r"\d+", raw)
        return int(match.group(0)) if match else None


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = text(value)
    if raw == "":
        return None
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def date_from_parts(year: object, month: object, day: object) -> date | None:
    y = parse_int(year)
    m = parse_int(month)
    d = parse_int(day)
    if not (y and m and d):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def normalize_text(value: object) -> str:
    raw = text(value).lower()
    return re.sub(r"[\s,，。；;：:/\\()\[\]（）【】\-_=+]+", "", raw)


def text_similarity(left: object, right: object) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(a, b) / 100
    except Exception:
        return SequenceMatcher(None, a, b).ratio()


def join_unique(values: Iterable[object], sep: str = " | ") -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = text(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return sep.join(result)


def first_nonblank(values: Sequence[object]) -> str:
    for value in values:
        item = text(value)
        if item:
            return item
    return ""


def read_excel_headerless(path: Path, sheet_name: str | None = None) -> tuple[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix == ".xls":
        try:
            import xlrd  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "读取 .xls 需要安装 xlrd：python -m pip install xlrd"
            ) from exc
        engine = "xlrd"
    elif suffix in {".xlsx", ".xlsm"}:
        engine = "openpyxl"
    else:
        raise ValueError(f"不支持的 Excel 格式：{path}")

    if sheet_name:
        df = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object, engine=engine)
        return sheet_name, df

    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine=engine)
    for name, df in sheets.items():
        if not df.dropna(how="all").empty:
            return str(name), df
    first_name = next(iter(sheets))
    return str(first_name), sheets[first_name]


def safe_get(row: pd.Series, idx: int) -> object:
    if idx >= len(row):
        return ""
    return row.iloc[idx]


def extract_counterparty(summary: object) -> str:
    raw = text(summary)
    if not raw:
        return ""
    cleaned = raw
    for prefix in ("收到", "收", "付给", "支付", "付"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.split("/")[0]
    cleaned = cleaned.split("的")[0]
    cleaned = cleaned.split("关于")[0]
    cleaned = cleaned.strip(" ，,。；;")
    return cleaned[:80]


def csv_date(value: object) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else text(value)
