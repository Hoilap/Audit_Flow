from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

def strip_code_fence(value: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", value, flags=re.S)
    return match.group(1).strip() if match else value.strip()

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


def safe_read_excel_sheet(
    path: str | Path,
    sheet_hint: str | int | None = None,
    engine: str | None = None,
    header: int | list[int] | None = None,
    dtype: object = object,
    *,
    keywords: list[str] | None = None,
) -> tuple[str, pd.DataFrame]:
    """安全读取 Excel 工作表，始终返回单个 DataFrame（不会返回 dict）。

    工作表选择逻辑：
    1. 如果 sheet_hint 是有效字符串且存在于工作簿中 → 使用该表
    2. 如果 sheet_hint 是整数 → 直接传给 pd.read_excel
    3. 否则读取所有工作表：
       a. 仅一个非空表 → 使用它
       b. 多个非空表 → 按 keywords 对表头打分，选最高分
       c. 全部为空 → 使用第一个表

    Args:
        path: Excel 文件路径
        sheet_hint: 建议的工作表名或索引（来自 config["sheet"]）
        engine: pandas 引擎，None 则按后缀自动选择
        header: 传给 pd.read_excel 的 header 参数
        dtype: 传给 pd.read_excel 的 dtype 参数
        keywords: 用于多表评分的关键词列表，默认银行流水相关词
    """
    path = Path(path)
    if engine is None:
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

    if keywords is None:
        keywords = [
            "日期", "交易日期", "记账日期", "入账日期",
            "金额", "借方", "贷方", "收入", "支出", "余额",
            "摘要", "用途", "备注", "对方", "户名", "账号",
        ]

    # --- sheet_hint 是整数，直接透传 ---
    if isinstance(sheet_hint, int):
        df = pd.read_excel(path, sheet_name=sheet_hint, header=header, dtype=dtype, engine=engine)
        return str(sheet_hint), df

    # --- sheet_hint 是非空字符串，验证是否存在 ---
    if sheet_hint and isinstance(sheet_hint, str):
        xls = pd.ExcelFile(path, engine=engine)
        if sheet_hint in xls.sheet_names:
            df = pd.read_excel(
                path, sheet_name=sheet_hint, header=header, dtype=dtype, engine=engine
            )
            return sheet_hint, df
        # 指定的工作表不存在，回退到自动检测

    # --- 读取所有工作表，智能选择 ---
    all_sheets: dict[str, pd.DataFrame] = pd.read_excel(
        path, sheet_name=None, header=header, dtype=dtype, engine=engine
    )
    if not all_sheets:
        raise ValueError(f"Excel 文件中没有工作表：{path}")

    non_empty = {
        name: df for name, df in all_sheets.items()
        if not df.dropna(how="all").empty
    }

    # 仅一个非空表
    if len(non_empty) == 1:
        name, df = next(iter(non_empty.items()))
        return str(name), df

    # 多个非空表 → 按表头关键词打分
    if len(non_empty) > 1:
        best_name = ""
        best_score = -1
        for name, df in non_empty.items():
            score = 0
            # 检查表头行（前 5 行的所有文本）
            head_text = " ".join(
                str(v) for v in df.head(5).values.flatten() if pd.notna(v)
            ).lower()
            for kw in keywords:
                if kw in head_text:
                    score += 1
            if score > best_score:
                best_score = score
                best_name = name
        return str(best_name), non_empty[best_name]

    # 全部为空，返回第一个
    first_name = next(iter(all_sheets))
    return str(first_name), all_sheets[first_name]


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
