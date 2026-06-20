"""Platform settlement flow cleaner.

Reads raw settlement CSV files (one per settlement date, organized by month),
standardizes columns, aggregates into a unified dataset, and produces
monthly summaries.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir, inputs_dir
from .utils import extract_month_key, parse_amount, safe_write_csv

logger = logging.getLogger(__name__)

# ── Standardized column schema for transaction-level settlement records ──
SETTLEMENT_COLUMNS = [
    "partner_txn_id",       # Platform transaction ID (order-level)
    "original_txn_id",      # Original partner transaction ID (for refunds)
    "amount",               # Transaction amount (original currency)
    "rmb_amount",           # RMB equivalent of transaction amount
    "fee",                  # Platform fee
    "settlement",           # Settlement amount (original currency)
    "rmb_settlement",       # RMB settlement amount
    "currency",             # Original currency code
    "rate",                 # Exchange rate
    "payment_time",         # Payment timestamp
    "settlement_time",      # Settlement timestamp
    "type",                 # Transaction type (P=payment, R=refund)
    "status",               # Transaction status
    "stem_from",            # Source/stem
    "remarks",              # Remarks (product description)
    "month",                # Month key (e.g., "2022-01")
    "source_file",          # Original filename
]

# Column name mapping: raw CSV column (lowered+stripped) → standard name
_COLUMN_MAP = {
    "partner_transaction_id": "partner_txn_id",
    "original_partner_transaction_id": "original_txn_id",
    "amount": "amount",
    "rmb_amount": "rmb_amount",
    "fee": "fee",
    "settlement": "settlement",
    "rmb_settlement": "rmb_settlement",
    "currency": "currency",
    "rate": "rate",
    "payment_time": "payment_time",
    "settlement_time": "settlement_time",
    "type": "type",
    "status": "status",
    "stem_from": "stem_from",
    "remarks": "remarks",
}

# Batch settlement columns
BATCH_COLUMNS = [
    "settle_batch_no",
    "settle_date",
    "amount",
    "fee",
    "settlement",
    "currency",
    "month",
    "source_file",
]


def clean_settlement(config: dict[str, Any]) -> tuple[Path, Path]:
    """Clean and aggregate platform settlement CSV files.

    Steps:
    1. Scan settlement directory for CSV files
    2. Read each file, classify as transaction or batch
    3. Standardize columns
    4. Aggregate all transaction records
    5. Generate monthly summary
    6. Write outputs to output_dir/clean/

    Returns:
        (settlement_csv_path, monthly_summary_path)
    """
    inp = inputs_dir(config)
    settle_dir = inp / "settlement"
    if not settle_dir.exists():
        raise FileNotFoundError(f"Settlement directory not found: {settle_dir}")

    out = output_dir(config)
    clean_dir = out / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)

    # Collect all CSV files
    csv_files = sorted(settle_dir.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {settle_dir}")

    logger.info("Found %d settlement CSV files in %s", len(csv_files), settle_dir)

    # Read and classify each file
    txn_frames: list[pd.DataFrame] = []
    batch_frames: list[pd.DataFrame] = []
    file_index: list[dict] = []

    for csv_path in csv_files:
        month_key = extract_month_key(str(csv_path))
        try:
            df = _read_settlement_csv(csv_path)
            if df.empty:
                continue

            file_type = _classify_settlement_file(df)
            df["month"] = month_key
            df["source_file"] = csv_path.name

            if file_type == "transaction":
                df = _standardize_transaction(df)
                txn_frames.append(df)
            elif file_type == "batch":
                df = _standardize_batch(df)
                batch_frames.append(df)

            file_index.append({
                "file": str(csv_path.relative_to(settle_dir)),
                "month": month_key,
                "type": file_type,
                "rows": len(df),
            })
        except Exception as e:
            logger.warning("Failed to read %s: %s", csv_path, e)
            file_index.append({
                "file": str(csv_path.relative_to(settle_dir)),
                "month": month_key,
                "type": "error",
                "rows": 0,
                "error": str(e),
            })

    if not txn_frames:
        raise ValueError("No transaction-level settlement records found")

    # Aggregate all transactions
    all_txn = pd.concat(txn_frames, ignore_index=True)
    settlement_path = clean_dir / "settlement_all.csv"
    safe_write_csv(all_txn, str(settlement_path))
    logger.info(
        "Settlement cleaned: %d records from %d files",
        len(all_txn), len(txn_frames),
    )

    # Generate monthly summary
    summary = _generate_monthly_summary(all_txn)
    summary_path = clean_dir / "settlement_monthly_summary.csv"
    safe_write_csv(summary, str(summary_path))
    logger.info("Monthly summary: %d months", len(summary))

    # Save batch data separately if present
    if batch_frames:
        all_batch = pd.concat(batch_frames, ignore_index=True)
        batch_path = clean_dir / "settlement_batch.csv"
        safe_write_csv(all_batch, str(batch_path))
        logger.info("Batch settlement: %d records", len(all_batch))

    # Save file index
    index_df = pd.DataFrame(file_index)
    safe_write_csv(index_df, str(clean_dir / "settlement_file_index.csv"))

    return settlement_path, summary_path


# ── Internal helpers ─────────────────────────────────────────

def _read_settlement_csv(path: Path) -> pd.DataFrame:
    """Read a settlement CSV, trying multiple encodings.
    Strips whitespace from column names and cell values.
    """
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            df = pd.read_csv(str(path), encoding=enc, dtype=str)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        return pd.DataFrame()

    # Clean column names: lowercase, strip whitespace
    df.columns = [c.lower().strip().replace(" ", "_") for c in df.columns]

    # Strip whitespace from all string values
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
        df[col] = df[col].replace({"nan": "", "None": "", "": ""})

    return df


def _classify_settlement_file(df: pd.DataFrame) -> str:
    """Classify a settlement CSV as 'transaction' or 'batch'.

    Transaction files have 'partner_transaction_id'.
    Batch files have 'settle_batch_no'.
    """
    cols = set(df.columns)
    if "settle_batch_no" in cols:
        return "batch"
    if "partner_transaction_id" in cols:
        return "transaction"
    # Heuristic: if it has many rows and 'amount' column, assume transaction
    if "amount" in cols and len(df) > 5:
        return "transaction"
    return "unknown"


def _standardize_transaction(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw columns to standardized SETTLEMENT_COLUMNS."""
    rename = {}
    for raw_col in df.columns:
        clean_col = raw_col.lower().strip().replace(" ", "_")
        if clean_col in _COLUMN_MAP:
            rename[raw_col] = _COLUMN_MAP[clean_col]
        elif raw_col != clean_col:
            rename[raw_col] = clean_col

    df = df.rename(columns=rename)

    # Convert numeric columns
    for col in ("amount", "rmb_amount", "fee", "settlement", "rmb_settlement", "rate"):
        if col in df.columns:
            df[col] = df[col].apply(parse_amount)

    # Ensure all standard columns exist
    for col in SETTLEMENT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df[SETTLEMENT_COLUMNS]


def _standardize_batch(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw columns to standardized BATCH_COLUMNS."""
    rename_map = {}
    for raw_col in df.columns:
        clean_col = raw_col.lower().strip().replace(" ", "_")
        if clean_col != raw_col:
            rename_map[raw_col] = clean_col
    if rename_map:
        df = df.rename(columns=rename_map)

    # Convert numeric columns
    for col in ("amount", "fee", "settlement"):
        if col in df.columns:
            df[col] = df[col].apply(parse_amount)

    for col in BATCH_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df[BATCH_COLUMNS]


def _generate_monthly_summary(all_txn: pd.DataFrame) -> pd.DataFrame:
    """Generate monthly summary statistics from transaction-level settlement data."""

    def _agg(group: pd.DataFrame) -> dict:
        amount = group["amount"].apply(parse_amount).sum() if group["amount"].dtype == object else group["amount"].sum()
        rmb_amount = group["rmb_amount"].apply(parse_amount).sum() if group["rmb_amount"].dtype == object else group["rmb_amount"].sum()
        fee = group["fee"].apply(parse_amount).sum() if group["fee"].dtype == object else group["fee"].sum()
        rmb_settlement = group["rmb_settlement"].apply(parse_amount).sum() if group["rmb_settlement"].dtype == object else group["rmb_settlement"].sum()

        type_col = group.get("type", pd.Series(dtype=str))
        payment_count = int((type_col.str.upper() == "P").sum()) if type_col.dtype == object else 0
        refund_count = int((type_col.str.upper() == "R").sum()) if type_col.dtype == object else 0

        return {
            "txn_count": len(group),
            "payment_count": payment_count,
            "refund_count": refund_count,
            "total_amount": round(float(amount), 2),
            "total_rmb_amount": round(float(rmb_amount), 2),
            "total_fee": round(float(fee), 2),
            "total_rmb_settlement": round(float(rmb_settlement), 2),
            "currency": group["currency"].mode().iloc[0] if len(group["currency"].mode()) > 0 else "",
        }

    # If numeric conversion already happened, use simpler aggregation
    summary = all_txn.groupby("month").apply(_agg, include_groups=False).reset_index()

    # Flatten the dict column
    if len(summary.columns) == 2 and summary.columns[1] == 0:
        # apply returned a Series of dicts
        detail = pd.json_normalize(summary[0])
        summary = pd.concat([summary[["month"]], detail], axis=1)

    # Sort by month
    summary = summary.sort_values("month").reset_index(drop=True)

    # Add annual total row
    totals = {
        "month": "TOTAL",
        "txn_count": summary["txn_count"].sum(),
        "payment_count": summary["payment_count"].sum(),
        "refund_count": summary["refund_count"].sum(),
        "total_amount": round(summary["total_amount"].sum(), 2),
        "total_rmb_amount": round(summary["total_rmb_amount"].sum(), 2),
        "total_fee": round(summary["total_fee"].sum(), 2),
        "total_rmb_settlement": round(summary["total_rmb_settlement"].sum(), 2),
        "currency": "",
    }
    summary = pd.concat([summary, pd.DataFrame([totals])], ignore_index=True)

    return summary
