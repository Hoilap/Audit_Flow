"""
audit_workflow 公共工具函数 —— 供 BLM / OSM 等多个工作流共享使用。
"""

from __future__ import annotations

import warnings as _warnings
from typing import Any

import pandas as pd


def build_smart_sample(
    df: pd.DataFrame,
    *,
    header_min_cells: int = 8,
    data_rows: int = 8,
) -> list[dict[str, Any]]:
    """构建发送给 LLM 的样本数据。

    策略：先找到表头行（第一个拥有 >= header_min_cells 个非空值的行），
    然后取从文件开头到表头行 + 其后 data_rows 行数据行作为样本。
    这样 LLM 能看到标题/元信息行 + 列名表头 + 真实数据，理解完整的文件结构。

    如果找不到表头行，回退到前 30 行。
    """
    header_idx = None
    for i in range(min(20, len(df))):
        non_empty = sum(1 for v in df.iloc[i] if pd.notna(v) and str(v).strip())
        if non_empty >= header_min_cells:
            header_idx = i
            break

    if header_idx is not None:
        end = min(header_idx + 1 + data_rows, len(df))
        sample_df = df.iloc[:end]
    else:
        sample_df = df.head(30)

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", FutureWarning)
        sample_df = sample_df.fillna("")
    return sample_df.astype(str).to_dict(orient="records")


def read_excel_for_sample(
    path: str,
    *,
    sheet: str | int | None = None,
    nrows: int = 30,
) -> pd.DataFrame:
    """读取 Excel 文件的前 nrows 行（header=None），用于构建 LLM 样本。

    自动根据扩展名选择引擎：.xls → xlrd, .xlsx/.xlsm → openpyxl。
    """
    from pathlib import Path as _Path

    p = _Path(path)
    suffix = p.suffix.lower()
    if suffix == ".xls":
        engine = "xlrd"
    elif suffix in {".xlsx", ".xlsm"}:
        engine = "openpyxl"
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")

    kwargs: dict[str, Any] = {
        "header": None,
        "nrows": nrows,
        "engine": engine,
        "dtype": object,
    }
    if sheet is not None:
        kwargs["sheet_name"] = sheet

    return pd.read_excel(path, **kwargs)
