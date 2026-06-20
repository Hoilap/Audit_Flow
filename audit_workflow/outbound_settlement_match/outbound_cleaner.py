"""Outbound report cleaner.

Reads monthly revenue report Excel files (sellout/refund/return reports),
classifies each worksheet by type, and extracts data into standardized CSVs.

Sheet types:
- sellout: 出库 (outbound shipments)
- refund: 仅退款/资损 (refund only / loss)
- return: 货损 (physical returns)
- transfer: 退回保税仓 (return to bonded warehouse for re-shelving)
- recap: 汇总 (summary — skipped for data extraction)
- collect: collect 结算流水 (pre-collected settlement — skipped)
- other: unknown sheets — skipped

IMPORTANT: Pre-processed sheets like "sellout filter" / "sellout filtered" /
"sellout without refund" are SKIPPED. The filtering is done programmatically
in the matcher step using the raw refund/return/transfer data.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir, inputs_dir
from .utils import parse_amount, safe_write_csv

logger = logging.getLogger(__name__)

# ── Sheet type classification keywords ──────────────────────

_SKIP_KEYWORDS = [
    "filter", "filtered", "without refund",
    "recap", "汇总",
    "collect",
    "sheet",           # Generic "Sheet1", "Sheet4" etc.
    "to be invoiced",
    "to be credited",
    "出库总表",
    "税费",
]

_TYPE_KEYWORDS = {
    "sellout": ["sellout", "出库"],
    "refund": ["refund only", "refund only系统", "仅退款", "资损"],
    "return": ["return wh", "rongqing return", "货损"],
    "transfer": ["return to bonded", "退回保税仓", "interim退回"],
}


def classify_sheet(sheet_name: str) -> str:
    """Classify a worksheet name into a sheet type.

    Returns one of: 'sellout', 'refund', 'return', 'transfer', 'skip'.

    Pre-processed sheets (sellout filter, etc.) are classified as 'skip'.
    """
    name_lower = sheet_name.lower().strip()

    # Check skip keywords first (pre-processed sheets)
    for kw in _SKIP_KEYWORDS:
        if kw in name_lower:
            return "skip"

    # Check type keywords
    for stype, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return stype

    return "skip"


# ── Standardized column schemas ──────────────────────────────

# Unified sellout columns (mapped from both early and late schemas)
SELLOUT_COLUMNS = [
    "order_id",          # Primary order identifier (for matching)
    "shop_name",         # 店铺名称
    "warehouse_name",    # 仓库名称
    "order_time",        # 下单时间 / 订单创建时间
    "payment_time",      # 支付时间 / 订单付款时间
    "ship_time",         # 发货时间
    "status",            # 订单状态
    "amount",            # 订单金额 / 总金额 / 买家实际支付金额
    "freight",           # 运费 / 买家应付邮费
    "quantity",          # 商品数量 / 宝贝总数量
    "product_name",      # 商品名称 / 宝贝标题
    "product_code",      # 商品编码 / 商家编码
    "nsv",               # NSV (net sales value)
    "buyer_name",        # 买家昵称 / 买家会员名
    "recipient_name",    # 收件人 / 收货人姓名
    "logistics_no",      # 配送运单号 / 物流单号
    "logistics_company", # 配送公司 / 物流公司
    "source_file",       # Original filename
    "source_sheet",      # Original sheet name
    "month",             # Month key
]

# Unified refund/return columns
REFUND_COLUMNS = [
    "order_id",          # Related order ID (退款编号 or 交易订单号)
    "refund_id",         # Refund/return specific ID
    "refund_time",       # 退款完结时间
    "refund_amount",     # 买家退款金额
    "refund_type",       # 手工退款/系统退款
    "need_return",       # 是否需要退货
    "product_name",      # 宝贝标题 / 货品名称
    "buyer_name",        # 买家会员昵称
    "status",            # 状态
    "source_file",
    "source_sheet",
    "month",
]

# Unified transfer columns
TRANSFER_COLUMNS = [
    "order_id",          # 交易订单号
    "erp_order_id",      # ERP订单号
    "transfer_id",       # 单号
    "lp_id",             # LP单号
    "product_code",      # 货品编码
    "product_name",      # 货品名称
    "warehouse_name",    # 仓库名称
    "transfer_time",     # 出入库时间
    "doc_type",          # 单据类型
    "stock_type",        # 库存类型
    "merchant_code",     # 商家编码
    "quantity",          # 出入数量
    "nsv",               # NSV
    "source_file",
    "source_sheet",
    "month",
]

# ── Column name mapping for sellout sheets ───────────────────

# Maps raw Chinese column names → standardized English names
# Covers both early (4月-5月, TMF platform) and late (6月+, WMS) schemas
_SELLOUT_COLUMN_MAP = {
    # Order identifiers
    "订单编号": "order_id",
    "交易订单号": "order_id",
    "支付单号": "payment_id",
    "子交易单号": "sub_order_id",
    "仓库订单号": "warehouse_order_id",
    "外部订单号": "external_order_id",
    "物流订单号": "logistics_order_id",
    # Shop / warehouse
    "店铺名称": "shop_name",
    "店铺Id": "shop_id",
    "仓库名称": "warehouse_name",
    "仓库编码": "warehouse_code",
    # Timestamps
    "下单时间": "order_time",
    "订单创建时间": "order_time",
    "支付时间": "payment_time",
    "订单付款时间": "payment_time",
    "发货时间": "ship_time",
    "出库时间": "ship_time",
    "出库日期": "ship_time",
    "确认收货时间": "confirm_time",
    # Status
    "订单状态": "status",
    # Amount
    "总金额": "amount",
    "买家应付货款": "amount",
    "买家实际支付金额": "amount",
    "订单金额": "amount",
    "运费": "freight",
    "买家应付邮费": "freight",
    # Quantity
    "商品数量": "quantity",
    "宝贝总数量": "quantity",
    # Product
    "宝贝标题": "product_name",
    "商品名称": "product_name",
    "货品名称": "product_name",
    "商家编码": "product_code",
    "商品编码": "product_code",
    "货品编码": "product_code",
    # NSV
    "NSV": "nsv",
    # Buyer
    "买家会员名": "buyer_name",
    "买家会员昵称": "buyer_name",
    "买家昵称": "buyer_name",
    "收货人姓名": "recipient_name",
    "收件人": "recipient_name",
    # Logistics
    "物流单号": "logistics_no",
    "配送运单号": "logistics_no",
    "物流公司": "logistics_company",
    "配送公司": "logistics_company",
    # Other
    "订单备注": "order_remark",
    "商家备忘": "merchant_memo",
    "订单关闭原因": "close_reason",
    "宝贝种类": "product_varieties",
    "货品id": "product_id",
    "商品id": "item_id",
    "手机": "phone",
    "税费": "tax",
    "均摊金额": "allocated_amount",
    "均摊税费": "allocated_tax",
    "match": "match",
    "LSCODE": "lscode",
    "条码": "barcode",
    "SCP单号": "scp_no",
    "规格": "specification",
    "买家留言": "buyer_message",
    "运送方式": "shipping_method",
    "返点积分": "reward_points",
    "买家支付积分": "buyer_points",
    "买家实际支付积分": "buyer_actual_points",
    "商品包税金额": "tax_inclusive_amount",
    "订单税费": "order_tax",
}

# Maps for refund/return sheets
_REFUND_COLUMN_MAP = {
    "退款编号": "order_id",
    "退款完结时间": "refund_time",
    "买家退款金额": "refund_amount",
    "手工退款/系统退款": "refund_type",
    "是否需要退货": "need_return",
    "宝贝标题": "product_name",
    "货品名称": "product_name",
    "买家会员昵称": "buyer_name",
    "买家昵称": "buyer_name",
    "状态": "status",
    "交易订单号": "order_id",
    "退款原因": "refund_reason",
    "退款原因备注": "refund_reason_memo",
    "退款金额": "refund_amount",
    "商家退款金额": "merchant_refund_amount",
    "退款创建时间": "refund_create_time",
    "商品名称": "product_name",
}

# Maps for transfer sheets
_TRANSFER_COLUMN_MAP = {
    "交易订单号": "order_id",
    "ERP订单号": "erp_order_id",
    "单号": "transfer_id",
    "LP单号": "lp_id",
    "货品编码": "product_code",
    "货品名称": "product_name",
    "仓库名称": "warehouse_name",
    "出入库时间": "transfer_time",
    "单据类型": "doc_type",
    "库存类型": "stock_type",
    "商家编码": "merchant_code",
    "出入数量": "quantity",
    "NSV": "nsv",
}


# ── Public API ───────────────────────────────────────────────

def clean_outbound(config: dict[str, Any]) -> dict[str, Path]:
    """Clean outbound Excel files (sellout/refund/return reports).

    Steps:
    1. Scan outbound directory for Excel files
    2. Read each file, classify sheets by type
    3. Extract and standardize data from each sheet
    4. Write unified CSVs by type

    Returns:
        dict with keys: 'sellout', 'refund', 'return', 'transfer'
        Values are paths to the cleaned CSV files.
    """
    inp = inputs_dir(config)
    outbound_dir = inp / "outbound"
    if not outbound_dir.exists():
        raise FileNotFoundError(f"Outbound directory not found: {outbound_dir}")

    out = output_dir(config)
    clean_dir = out / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)

    # Find all Excel files
    xlsx_files = sorted(
        f for f in outbound_dir.iterdir()
        if f.suffix.lower() in (".xlsx", ".xls") and not f.name.startswith("~")
    )
    if not xlsx_files:
        raise FileNotFoundError(f"No Excel files found in {outbound_dir}")

    logger.info("Found %d outbound Excel files", len(xlsx_files))

    # Classify and extract all sheets
    sellout_frames: list[pd.DataFrame] = []
    refund_frames: list[pd.DataFrame] = []
    return_frames: list[pd.DataFrame] = []
    transfer_frames: list[pd.DataFrame] = []
    sheet_index: list[dict] = []

    for xlsx_path in xlsx_files:
        month = _extract_month_from_filename(xlsx_path.name)
        logger.info("Processing: %s (month=%s)", xlsx_path.name, month)

        try:
            xl = pd.ExcelFile(str(xlsx_path), engine="openpyxl")
        except Exception as e:
            logger.warning("Failed to open %s: %s", xlsx_path.name, e)
            continue

        for sheet_name in xl.sheet_names:
            stype = classify_sheet(sheet_name)
            sheet_index.append({
                "file": xlsx_path.name,
                "sheet": sheet_name,
                "type": stype,
                "month": month,
            })

            if stype == "skip":
                continue

            try:
                df = _read_sheet_auto_header(xl, sheet_name)
            except Exception as e:
                logger.warning("  Failed to read sheet '%s': %s", sheet_name, e)
                continue

            if df.empty:
                continue

            # Clean column names
            df.columns = [str(c).strip() for c in df.columns]

            # Add source metadata (using standard column names)
            df["source_file"] = xlsx_path.name
            df["source_sheet"] = sheet_name
            df["month"] = month

            if stype == "sellout":
                cleaned = _clean_sellout_sheet(df)
                if not cleaned.empty:
                    sellout_frames.append(cleaned)
                    sheet_index[-1]["rows"] = len(cleaned)
            elif stype == "refund":
                cleaned = _clean_refund_sheet(df)
                if not cleaned.empty:
                    refund_frames.append(cleaned)
                    sheet_index[-1]["rows"] = len(cleaned)
            elif stype == "return":
                cleaned = _clean_refund_sheet(df)  # Same structure as refund
                if not cleaned.empty:
                    return_frames.append(cleaned)
                    sheet_index[-1]["rows"] = len(cleaned)
            elif stype == "transfer":
                cleaned = _clean_transfer_sheet(df)
                if not cleaned.empty:
                    transfer_frames.append(cleaned)
                    sheet_index[-1]["rows"] = len(cleaned)

        xl.close()

    # Write outputs
    result = {}
    for name, frames, columns in [
        ("sellout", sellout_frames, SELLOUT_COLUMNS),
        ("refund", refund_frames, REFUND_COLUMNS),
        ("return", return_frames, REFUND_COLUMNS),
        ("transfer", transfer_frames, TRANSFER_COLUMNS),
    ]:
        if frames:
            combined = pd.concat(frames, ignore_index=True)
            # Ensure all standard columns exist
            for col in columns:
                if col not in combined.columns:
                    combined[col] = ""
            combined = combined[columns + [c for c in combined.columns if c not in columns and not c.startswith("_")]]
            path = clean_dir / f"{name}.csv"
            safe_write_csv(combined, str(path))
            result[name] = path
            logger.info("  %s: %d records", name, len(combined))
        else:
            result[name] = None
            logger.info("  %s: no records found", name)

    # Save sheet classification index
    idx_df = pd.DataFrame(sheet_index)
    safe_write_csv(idx_df, str(clean_dir / "outbound_sheet_index.csv"))

    return result


# ── Internal helpers ─────────────────────────────────────────

def _read_sheet_auto_header(xl: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    """Read a sheet and auto-detect the header row.

    Some sheets have a summary row at the top, pushing the real headers
    to row 1. We read with header=0 first; if most columns are 'Unnamed'
    or numeric, we promote the first data row to be the real header.
    """
    try:
        df = xl.parse(sheet_name, header=0, dtype=str)
    except Exception:
        return pd.DataFrame()

    if df.empty or len(df) == 0:
        return df

    # Check if the current header row looks valid
    bad_count = 0
    for col in df.columns:
        col_str = str(col).strip()
        if col_str.startswith("Unnamed") or _is_numeric_str(col_str):
            bad_count += 1

    # If more than half the columns are bad, the real header is probably in row 0
    if bad_count > len(df.columns) / 2 and len(df) > 0:
        # Promote first data row to header
        new_header = df.iloc[0].tolist()
        df = df.iloc[1:].reset_index(drop=True)
        df.columns = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(new_header)]

    # Deduplicate column names (keep first occurrence)
    seen: dict[str, int] = {}
    new_cols = []
    for col in df.columns:
        col_str = str(col)
        if col_str in seen:
            seen[col_str] += 1
            new_cols.append(f"{col_str}_dup{seen[col_str]}")
        else:
            seen[col_str] = 0
            new_cols.append(col_str)
    df.columns = new_cols

    return df


def _is_numeric_str(s: str) -> bool:
    """Check if a string looks like a number (not a real column header)."""
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _deduplicate_rename(raw_columns, column_map: dict) -> dict:
    """Build a rename dict that avoids mapping multiple source columns
    to the same target name. Keeps the first occurrence only.

    Args:
        raw_columns: list of actual column names from the DataFrame
        column_map: full mapping dict (Chinese → English)
    Returns:
        dict suitable for DataFrame.rename()
    """
    rename = {}
    used_targets: set[str] = set()
    for col in raw_columns:
        if col.startswith("_"):
            continue
        mapped = column_map.get(col)
        if mapped and mapped not in used_targets:
            rename[col] = mapped
            used_targets.add(mapped)
    return rename


def _clean_sellout_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """Clean a sellout (出库) sheet by mapping columns to standardized schema."""
    rename = _deduplicate_rename(df.columns, _SELLOUT_COLUMN_MAP)
    df = df.rename(columns=rename)

    # Convert numeric columns
    for col in ("amount", "freight", "quantity", "nsv"):
        if col in df.columns:
            df[col] = df[col].apply(parse_amount)

    # Select standard columns (only those that exist)
    available = [c for c in SELLOUT_COLUMNS if c in df.columns]
    return df[available].copy()


def _clean_refund_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """Clean a refund or return sheet.

    Refund/return sheets may use either:
    1. The early refund-specific schema (退款编号, 退款完结时间, etc.)
    2. The same WMS column structure as sellout (交易订单号, 店铺名称, etc.)

    This function tries both mappings, preferring the sellout map for
    common columns and the refund map for refund-specific columns.
    """
    # Merge: sellout map first (for order_id, shop_name, etc.),
    # then refund map overrides for refund-specific columns
    combined_map = {**_SELLOUT_COLUMN_MAP, **_REFUND_COLUMN_MAP}
    rename = _deduplicate_rename(df.columns, combined_map)
    df = df.rename(columns=rename)

    # Convert numeric columns
    for col in ("refund_amount", "amount", "quantity", "nsv"):
        if col in df.columns:
            df[col] = df[col].apply(parse_amount)

    # Select standard columns: use refund columns if available, else sellout columns
    # (WMS-format refund sheets will have sellout-like columns)
    standard_cols = list(dict.fromkeys(REFUND_COLUMNS + SELLOUT_COLUMNS))  # dedup, preserve order
    available = [c for c in standard_cols if c in df.columns]
    return df[available].copy()


def _clean_transfer_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """Clean a transfer (退回保税仓) sheet."""
    rename = _deduplicate_rename(df.columns, _TRANSFER_COLUMN_MAP)
    df = df.rename(columns=rename)

    # Convert numeric columns
    for col in ("quantity", "nsv"):
        if col in df.columns:
            df[col] = df[col].apply(parse_amount)

    available = [c for c in TRANSFER_COLUMNS if c in df.columns]
    return df[available].copy()


def _extract_month_from_filename(filename: str) -> str:
    """Extract month key from filename like 'Health FSS 4月收入报告.xlsx' → '2022-04'."""
    import re
    # Try Chinese month pattern: "4月", "10月"
    m = re.search(r"(\d{1,2})月", filename)
    if m:
        month = int(m.group(1))
        # Default year is 2022 (from context); could be parameterized
        return f"2022-{month:02d}"
    # Try YYYYMM pattern
    m2 = re.search(r"(\d{4})(\d{2})", filename)
    if m2:
        y, mo = int(m2.group(1)), int(m2.group(2))
        if 2000 <= y <= 2099 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    return "unknown"
