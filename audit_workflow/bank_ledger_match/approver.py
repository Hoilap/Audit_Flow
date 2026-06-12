from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir
from .matcher import Record, _group_score, _groups_same_month_flow_account, _make_group, _safe_to_csv
from .utils import amount_to_cents, parse_amount, parse_date, text


def apply_manual_approvals(config: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    out = output_dir(config) / "matches"
    review_path = out / text(
        config.get("matching", {}).get("llm", {}).get("manual_review_file") or "manual_review_candidates.csv"
    )
    matches_path = out / "matches.csv"
    unmatched_bank_path = out / "unmatched_bank.csv"
    unmatched_ledger_path = out / "unmatched_ledger.csv"

    review_df = _load_csv(review_path)
    matches_df = _load_csv(matches_path)
    unmatched_bank_df = _load_csv(unmatched_bank_path)
    unmatched_ledger_df = _load_csv(unmatched_ledger_path)
    if review_df.empty:
        return matches_path, unmatched_bank_path, unmatched_ledger_path, review_path

    bank_records = _records_by_id(unmatched_bank_df, "bank", "txn_id")
    ledger_records = _records_by_id(unmatched_ledger_df, "ledger", "entry_id")
    used_bank = _used_ids(matches_df, "bank_txn_ids")
    used_ledger = _used_ids(matches_df, "ledger_entry_ids")
    amount_tol = amount_to_cents(config.get("matching", {}).get("amount_tolerance", 0.01))
    date_tol = int(config.get("matching", {}).get("date_tolerance_days", 30))
    groups: list[dict[str, Any]] = []
    next_serial = _next_match_serial(matches_df)

    for idx, row in review_df.iterrows():
        if not _as_bool(row.get("approve")):
            continue
        bank_ids = _split_ids(row.get("bank_ids"))
        ledger_ids = _split_ids(row.get("ledger_ids"))
        bank_group = [bank_records[item] for item in bank_ids if item in bank_records]
        ledger_group = [ledger_records[item] for item in ledger_ids if item in ledger_records]
        if (
            len(bank_group) != len(bank_ids)
            or len(ledger_group) != len(ledger_ids)
            or any(item in used_bank for item in bank_ids)
            or any(item in used_ledger for item in ledger_ids)
            or abs(_sum_cents(bank_group) - _sum_cents(ledger_group)) > amount_tol
        ):
            review_df.loc[idx, "status"] = "blocked_conflict_or_amount_diff"
            continue
        if not _groups_same_month_flow_account(bank_group, ledger_group, config):
            review_df.loc[idx, "status"] = "blocked_cross_month_or_flow_account"
            continue

        score = _approval_score(config, row, bank_group, ledger_group, amount_tol, date_tol)
        group = _make_group(
            config,
            text(row.get("match_type")) or "manual_review",
            bank_group,
            ledger_group,
            score,
            next_serial,
        )
        next_serial += 1
        group["llm_candidate_id"] = text(row.get("candidate_id"))
        group["llm_reason"] = text(row.get("llm_reason"))
        group["manual_review_signature"] = text(row.get("candidate_signature"))
        group["manual_note"] = text(row.get("manual_note"))
        groups.append(group)
        used_bank.update(bank_ids)
        used_ledger.update(ledger_ids)
        review_df.loc[idx, "status"] = "approved_applied"

    if groups:
        appended = pd.DataFrame(groups)
        matches_df = pd.concat([matches_df, appended], ignore_index=True) if not matches_df.empty else appended
        unmatched_bank_df = _drop_ids(unmatched_bank_df, "txn_id", used_bank)
        unmatched_ledger_df = _drop_ids(unmatched_ledger_df, "entry_id", used_ledger)

    matches_path = _safe_to_csv(matches_df, matches_path)
    unmatched_bank_path = _safe_to_csv(unmatched_bank_df, unmatched_bank_path)
    unmatched_ledger_path = _safe_to_csv(unmatched_ledger_df, unmatched_ledger_path)
    review_path = _safe_to_csv(review_df, review_path)
    return matches_path, unmatched_bank_path, unmatched_ledger_path, review_path


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def _records_by_id(df: pd.DataFrame, kind: str, id_col: str) -> dict[str, Record]:
    records: dict[str, Record] = {}
    if df.empty or id_col not in df:
        return records
    for _, row in df.iterrows():
        trans_date = parse_date(row.get("transaction_date"))
        amount_cents = amount_to_cents(row.get("amount", 0))
        record_id = text(row.get(id_col))
        if not trans_date or not record_id:
            continue
        records[record_id] = Record(record_id, kind, row.to_dict(), amount_cents, trans_date)
    return records


def _used_ids(df: pd.DataFrame, col: str) -> set[str]:
    used: set[str] = set()
    if df.empty or col not in df:
        return used
    for value in df[col]:
        used.update(_split_ids(value))
    return used


def _split_ids(value: object) -> list[str]:
    return [item.strip() for item in text(value).split("|") if item.strip()]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return text(value).lower() in {"true", "1", "yes", "y", "是", "通过"}


def _sum_cents(records: list[Record]) -> int:
    return sum(record.amount_cents for record in records)


def _approval_score(
    config: dict[str, Any],
    row: pd.Series,
    bank_group: list[Record],
    ledger_group: list[Record],
    amount_tol: int,
    date_tol: int,
) -> float:
    try:
        llm_confidence = float(row.get("llm_confidence") or 0)
    except (TypeError, ValueError):
        llm_confidence = 0.0
    group_score = _group_score(bank_group, ledger_group, amount_tol, max(date_tol, 30))
    manual_floor = float(config.get("matching", {}).get("manual_approval_score_floor", 0.75))
    return round(max(llm_confidence, group_score, manual_floor), 4)


def _drop_ids(df: pd.DataFrame, id_col: str, used: set[str]) -> pd.DataFrame:
    if df.empty or id_col not in df:
        return df
    return df[~df[id_col].isin(used)].copy()


def _next_match_serial(matches_df: pd.DataFrame) -> int:
    if matches_df.empty or "match_id" not in matches_df:
        return 1
    serials: list[int] = []
    for value in matches_df["match_id"]:
        raw = text(value)
        if raw.startswith("M") and raw[1:].isdigit():
            serials.append(int(raw[1:]))
    return max(serials, default=0) + 1
