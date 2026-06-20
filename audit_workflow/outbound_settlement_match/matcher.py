"""Net outbound filtering and settlement matching.

Workflow:
1. Filter sellout: remove orders that appear in refund/return/transfer sheets
   → net outbound (orders actually shipped and kept by customers)
2. Match net outbound against platform settlement records by order/transaction ID
3. Generate matched/unmatched reports and monthly summaries

Matching key:
  outbound.order_id  ←→  settlement.partner_txn_id

The matching is purely by ID (not by amount scoring like bank_ledger_match),
because e-commerce platforms use the same order ID for both outbound
shipment records and payment settlement records.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir
from .utils import parse_amount, safe_write_csv

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────

def match_outbound_settlement(
    config: dict[str, Any],
    outbound_paths: dict[str, Path | None],
    settlement_path: Path,
) -> dict[str, Any]:
    """Filter net outbound and match against settlement.

    Args:
        config: Pipeline configuration dict.
        outbound_paths: Dict with keys 'sellout', 'refund', 'return', 'transfer',
                        values are paths to cleaned CSVs (or None).
        settlement_path: Path to the cleaned settlement_all.csv.

    Returns:
        dict with keys:
            'net_outbound': Path to net outbound CSV
            'matched': Path to matched records CSV
            'unmatched_outbound': Path to unmatched outbound CSV
            'unmatched_settlement': Path to unmatched settlement CSV
            'monthly_summary': Path to monthly match summary CSV
            'summary': dict with match statistics
    """
    out = output_dir(config)
    match_dir = out / "matches"
    match_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load data ─────────────────────────────────────────
    sellout_df = _load_csv(outbound_paths.get("sellout"))
    refund_df = _load_csv(outbound_paths.get("refund"))
    return_df = _load_csv(outbound_paths.get("return"))
    transfer_df = _load_csv(outbound_paths.get("transfer"))
    settlement_df = pd.read_csv(str(settlement_path), dtype=str, encoding="utf-8-sig")

    logger.info(
        "Loaded: sellout=%d, refund=%d, return=%d, transfer=%d, settlement=%d",
        len(sellout_df), len(refund_df), len(return_df), len(transfer_df), len(settlement_df),
    )

    if sellout_df.empty:
        raise ValueError("Sellout data is empty, cannot proceed with matching")

    # ── 2. Filter net outbound ────────────────────────────────
    net_outbound = _filter_net_outbound(sellout_df, refund_df, return_df, transfer_df)
    net_path = match_dir / "net_outbound.csv"
    safe_write_csv(net_outbound, str(net_path))
    logger.info(
        "Net outbound: %d records (filtered %d from %d sellout)",
        len(net_outbound), len(sellout_df) - len(net_outbound), len(sellout_df),
    )

    # ── 3. Match by ID ────────────────────────────────────────
    matched_df, unmatched_outbound_df, unmatched_settlement_df, summary = _match_by_id(
        net_outbound, settlement_df, config,
    )

    matched_path = match_dir / "matched.csv"
    unmatched_outbound_path = match_dir / "unmatched_outbound.csv"
    unmatched_settlement_path = match_dir / "unmatched_settlement.csv"

    safe_write_csv(matched_df, str(matched_path))
    safe_write_csv(unmatched_outbound_df, str(unmatched_outbound_path))
    safe_write_csv(unmatched_settlement_df, str(unmatched_settlement_path))

    # ── 4. Monthly summary ────────────────────────────────────
    monthly_path = _write_monthly_summary(
        matched_df, unmatched_outbound_df, unmatched_settlement_df, match_dir,
    )

    # ── 5. Summary dict ──────────────────────────────────────
    summary.update({
        "sellout_total": len(sellout_df),
        "refund_count": len(refund_df),
        "return_count": len(return_df),
        "transfer_count": len(transfer_df),
        "net_outbound_count": len(net_outbound),
        "filtered_count": len(sellout_df) - len(net_outbound),
    })

    logger.info(
        "Match summary: matched=%d, unmatched_outbound=%d, unmatched_settlement=%d",
        summary["matched_count"], summary["unmatched_outbound_count"],
        summary["unmatched_settlement_count"],
    )

    return {
        "net_outbound": net_path,
        "matched": matched_path,
        "unmatched_outbound": unmatched_outbound_path,
        "unmatched_settlement": unmatched_settlement_path,
        "monthly_summary": monthly_path,
        "summary": summary,
    }


# ── Internal: Net Outbound Filter ────────────────────────────

def _filter_net_outbound(
    sellout_df: pd.DataFrame,
    refund_df: pd.DataFrame,
    return_df: pd.DataFrame,
    transfer_df: pd.DataFrame,
) -> pd.DataFrame:
    """Filter sellout to get net outbound by removing orders in refund/return/transfer.

    Uses 'order_id' as the unique identifier. An order is excluded from net
    outbound if its order_id appears in any of the refund/return/transfer datasets.
    """
    # Collect order IDs to exclude
    exclude_ids: set[str] = set()

    for label, df in [("refund", refund_df), ("return", return_df), ("transfer", transfer_df)]:
        if df.empty:
            continue
        if "order_id" in df.columns:
            ids = df["order_id"].dropna().astype(str).str.strip().unique()
            exclude_ids.update(ids)
            logger.info("  %s: %d unique order IDs to exclude", label, len(ids))

    # Normalize sellout order IDs
    sellout_df = sellout_df.copy()
    sellout_df["_order_id_norm"] = sellout_df["order_id"].astype(str).str.strip()

    # Mark excluded orders
    sellout_df["is_excluded"] = sellout_df["_order_id_norm"].isin(exclude_ids)

    # Add exclusion reason
    def _get_reason(row):
        if not row["is_excluded"]:
            return ""
        oid = row["_order_id_norm"]
        reasons = []
        if not refund_df.empty and "order_id" in refund_df.columns:
            if oid in set(refund_df["order_id"].astype(str).str.strip()):
                reasons.append("refund")
        if not return_df.empty and "order_id" in return_df.columns:
            if oid in set(return_df["order_id"].astype(str).str.strip()):
                reasons.append("return")
        if not transfer_df.empty and "order_id" in transfer_df.columns:
            if oid in set(transfer_df["order_id"].astype(str).str.strip()):
                reasons.append("transfer")
        return "|".join(reasons) if reasons else "unknown"

    sellout_df["exclusion_reason"] = sellout_df.apply(_get_reason, axis=1)

    # Filter to net outbound
    net = sellout_df[~sellout_df["is_excluded"]].copy()
    net = net.drop(columns=["_order_id_norm", "is_excluded", "exclusion_reason"], errors="ignore")

    return net


# ── Internal: ID Matching ────────────────────────────────────

def _match_by_id(
    net_outbound: pd.DataFrame,
    settlement: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Match net outbound against settlement by order/transaction ID.

    Matching key:
        outbound.order_id  ←→  settlement.partner_txn_id

    Returns:
        (matched_df, unmatched_outbound_df, unmatched_settlement_df, summary_dict)
    """
    match_cfg = config.get("matching", {})

    # Normalize matching keys
    outbound_key = match_cfg.get("outbound_order_id_key", "order_id")
    settlement_key = match_cfg.get("settlement_txn_id_key", "partner_txn_id")

    # Validate keys exist
    if outbound_key not in net_outbound.columns:
        # Try fallback keys
        fallback_keys = ["order_id", "交易订单号", "订单编号"]
        for fk in fallback_keys:
            if fk in net_outbound.columns:
                outbound_key = fk
                break
        else:
            raise ValueError(
                f"Outbound key '{outbound_key}' not found. "
                f"Available: {list(net_outbound.columns)}"
            )

    if settlement_key not in settlement.columns:
        # Try fallback keys
        fallback_keys = ["partner_txn_id", "Partner_transaction_id"]
        for fk in fallback_keys:
            if fk in settlement.columns:
                settlement_key = fk
                break
        else:
            raise ValueError(
                f"Settlement key '{settlement_key}' not found. "
                f"Available: {list(settlement.columns)}"
            )

    logger.info("Matching: outbound.%s ←→ settlement.%s", outbound_key, settlement_key)

    # Normalize IDs for matching
    net_outbound = net_outbound.copy()
    settlement = settlement.copy()
    net_outbound["_match_id"] = net_outbound[outbound_key].astype(str).str.strip()
    settlement["_match_id"] = settlement[settlement_key].astype(str).str.strip()

    # Remove empty match IDs
    net_outbound = net_outbound[net_outbound["_match_id"].notna() & (net_outbound["_match_id"] != "")]
    settlement = settlement[settlement["_match_id"].notna() & (settlement["_match_id"] != "")]

    outbound_ids = set(net_outbound["_match_id"].unique())
    settlement_ids = set(settlement["_match_id"].unique())

    matched_ids = outbound_ids & settlement_ids
    unmatched_outbound_ids = outbound_ids - settlement_ids
    unmatched_settlement_ids = settlement_ids - outbound_ids

    # Build matched DataFrame (join on match_id)
    matched_outbound = net_outbound[net_outbound["_match_id"].isin(matched_ids)].copy()
    matched_settlement = settlement[settlement["_match_id"].isin(matched_ids)].copy()

    # Merge: one row per (outbound_order, settlement_txn) pair
    matched_df = pd.merge(
        matched_outbound,
        matched_settlement,
        on="_match_id",
        how="inner",
        suffixes=("_outbound", "_settlement"),
    )

    # Add match metadata
    matched_df["match_id"] = matched_df["_match_id"]

    # Compute amount comparison if both sides have amount fields
    _add_amount_comparison(matched_df)

    # Build unmatched DataFrames
    unmatched_outbound_df = net_outbound[
        net_outbound["_match_id"].isin(unmatched_outbound_ids)
    ].copy()
    unmatched_settlement_df = settlement[
        settlement["_match_id"].isin(unmatched_settlement_ids)
    ].copy()

    # Clean up temp columns
    for df in (matched_df, unmatched_outbound_df, unmatched_settlement_df):
        df.drop(columns=["_match_id"], inplace=True, errors="ignore")

    # Summary statistics
    summary = {
        "matched_count": len(matched_df),
        "matched_outbound_orders": len(matched_ids),
        "unmatched_outbound_count": len(unmatched_outbound_df),
        "unmatched_settlement_count": len(unmatched_settlement_df),
        "total_outbound_ids": len(outbound_ids),
        "total_settlement_ids": len(settlement_ids),
        "match_rate": round(len(matched_ids) / len(outbound_ids) * 100, 2) if outbound_ids else 0,
        "outbound_key": outbound_key,
        "settlement_key": settlement_key,
    }

    # Add amount totals if available
    if "amount_outbound" in matched_df.columns:
        summary["matched_outbound_amount"] = round(
            matched_df["amount_outbound"].apply(parse_amount).sum(), 2
        )
    if "rmb_settlement" in matched_df.columns:
        summary["matched_settlement_amount"] = round(
            matched_df["rmb_settlement"].apply(parse_amount).sum(), 2
        )
    if "amount" in unmatched_outbound_df.columns:
        summary["unmatched_outbound_amount"] = round(
            unmatched_outbound_df["amount"].apply(parse_amount).sum(), 2
        )
    if "rmb_settlement" in unmatched_settlement_df.columns:
        summary["unmatched_settlement_amount"] = round(
            unmatched_settlement_df["rmb_settlement"].apply(parse_amount).sum(), 2
        )

    return matched_df, unmatched_outbound_df, unmatched_settlement_df, summary


def _add_amount_comparison(matched_df: pd.DataFrame):
    """Add amount comparison columns to matched DataFrame."""
    # Try to find outbound amount column
    for col in ("amount_outbound", "amount", "nsv"):
        if col in matched_df.columns:
            matched_df["outbound_amount"] = matched_df[col].apply(parse_amount)
            break

    # Try to find settlement amount column
    for col in ("rmb_settlement", "settlement", "rmb_amount"):
        if col in matched_df.columns:
            matched_df["settlement_amount"] = matched_df[col].apply(parse_amount)
            break

    # Compute difference if both sides have amounts
    if "outbound_amount" in matched_df.columns and "settlement_amount" in matched_df.columns:
        matched_df["amount_diff"] = (
            matched_df["outbound_amount"] - matched_df["settlement_amount"]
        ).round(2)
        matched_df["amount_match"] = matched_df["amount_diff"].abs() < 0.01


# ── Internal: Monthly Summary ────────────────────────────────

def _write_monthly_summary(
    matched_df: pd.DataFrame,
    unmatched_outbound_df: pd.DataFrame,
    unmatched_settlement_df: pd.DataFrame,
    match_dir: Path,
) -> Path:
    """Generate a monthly match summary and write to CSV."""
    rows = []

    # Determine month column — prefer the one with actual data
    month_col = None
    for col in ("month_settlement", "month_outbound", "month", "_month"):
        if col in matched_df.columns and matched_df[col].notna().any():
            month_col = col
            break
    if month_col is None and "month" in unmatched_outbound_df.columns:
        month_col = "month"

    # Build summary per month from matched data
    if month_col and not matched_df.empty:
        for month, group in matched_df.groupby(month_col):
            row = {
                "month": month,
                "matched_count": len(group),
                "unmatched_outbound": 0,
                "unmatched_settlement": 0,
            }
            # Add amount info if available
            if "outbound_amount" in group.columns:
                row["matched_outbound_amount"] = round(group["outbound_amount"].sum(), 2)
            if "settlement_amount" in group.columns:
                row["matched_settlement_amount"] = round(group["settlement_amount"].sum(), 2)
            rows.append(row)

    # Add unmatched counts per month
    if not unmatched_outbound_df.empty and "month" in unmatched_outbound_df.columns:
        for month, group in unmatched_outbound_df.groupby("month"):
            existing = next((r for r in rows if r["month"] == month), None)
            if existing:
                existing["unmatched_outbound"] = len(group)
            else:
                rows.append({
                    "month": month,
                    "matched_count": 0,
                    "unmatched_outbound": len(group),
                    "unmatched_settlement": 0,
                })

    if not unmatched_settlement_df.empty and "month" in unmatched_settlement_df.columns:
        for month, group in unmatched_settlement_df.groupby("month"):
            existing = next((r for r in rows if r["month"] == month), None)
            if existing:
                existing["unmatched_settlement"] = len(group)
            else:
                rows.append({
                    "month": month,
                    "matched_count": 0,
                    "unmatched_outbound": 0,
                    "unmatched_settlement": len(group),
                })

    if not rows:
        rows = [{"month": "N/A", "matched_count": 0, "unmatched_outbound": 0, "unmatched_settlement": 0}]

    summary_df = pd.DataFrame(rows).sort_values("month").reset_index(drop=True)

    # Add total row
    totals = {
        "month": "TOTAL",
        "matched_count": summary_df["matched_count"].sum(),
        "unmatched_outbound": summary_df["unmatched_outbound"].sum(),
        "unmatched_settlement": summary_df["unmatched_settlement"].sum(),
    }
    for col in summary_df.columns:
        if col not in totals and summary_df[col].dtype in ("float64", "int64"):
            totals[col] = round(summary_df[col].sum(), 2)

    summary_df = pd.concat([summary_df, pd.DataFrame([totals])], ignore_index=True)

    path = match_dir / "monthly_match_summary.csv"
    safe_write_csv(summary_df, str(path))
    return path


# ── Helpers ──────────────────────────────────────────────────

def _load_csv(path: Path | None) -> pd.DataFrame:
    """Load a CSV file, returning empty DataFrame if path is None or doesn't exist."""
    if path is None or not Path(path).exists():
        return pd.DataFrame()
    return pd.read_csv(str(path), dtype=str, encoding="utf-8-sig")
