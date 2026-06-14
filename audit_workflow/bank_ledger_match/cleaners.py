from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir, resolve_path
from .utils import (
    csv_date,
    date_from_parts,
    extract_counterparty,
    first_nonblank,
    join_unique,
    parse_amount,
    parse_date,
    parse_int,
    read_excel_headerless,
    safe_get,
    text,
)


BANK_COLUMNS = [
    "txn_id",
    "source_id",
    "source_file",
    "source_sheet",
    "row_no",
    "bank_name",
    "account_no",
    "transaction_date",
    "flow",
    "amount",
    "bank_debit",
    "bank_credit",
    "counterparty_name",
    "counterparty_account",
    "summary",
    "description",
    "balance",
    "raw_text",
]

LEDGER_COLUMNS = [
    "entry_id",
    "source_id",
    "source_file",
    "source_sheet",
    "row_no",
    "bank_name",
    "account_no",
    "transaction_date",
    "flow",
    "amount",
    "ledger_debit",
    "ledger_credit",
    "voucher_no",
    "voucher_type",
    "summary",
    "subject",
    "counterparty_name",
    "raw_text",
]


def enabled_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("enabled", True)]


def clean_to_csv(config: dict[str, Any]) -> tuple[Path, Path]:
    out = output_dir(config) / "clean"
    out.mkdir(parents=True, exist_ok=True)
    bank_df = parse_bank_statements(config)
    ledger_df = parse_ledgers(config)
    bank_path = out / "bank_transactions.csv"
    ledger_path = out / "ledger_entries.csv"
    bank_df.to_csv(bank_path, index=False, encoding="utf-8-sig")
    ledger_df.to_csv(ledger_path, index=False, encoding="utf-8-sig")
    return bank_path, ledger_path


def parse_bank_statements(config: dict[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item in enabled_items(config.get("inputs", {}).get("bank_statements", [])):
        parser = item.get("parser")
        if parser == "icbc_historydetail":
            frames.append(parse_icbc_historydetail(config, item))
        elif parser == "llm_bank":
            frames.append(parse_llm_bank(config, item))
        elif parser == "generated_bank":
            frames.append(parse_generated_bank(config, item))
        else:
            raise ValueError(f"未知银行流水解析器：{parser}")
    if not frames:
        return pd.DataFrame(columns=BANK_COLUMNS)
    return pd.concat(frames, ignore_index=True)[BANK_COLUMNS]


def parse_ledgers(config: dict[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for item in enabled_items(config.get("inputs", {}).get("ledgers", [])):
        parser = item.get("parser")
        if parser == "xinjiyuan_bank_ledger":
            frames.append(parse_xinjiyuan_bank_ledger(config, item))
        else:
            raise ValueError(f"未知序时账解析器：{parser}")
    if not frames:
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    return pd.concat(frames, ignore_index=True)[LEDGER_COLUMNS]


def parse_icbc_historydetail(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    path = resolve_path(config, item["path"])
    assert path is not None
    sheet, df = read_excel_headerless(path, item.get("sheet"))
    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        trans_date = parse_date(safe_get(row, 2))
        direction = text(safe_get(row, 3))
        amount = parse_amount(safe_get(row, 10))
        if not trans_date or amount == 0 or direction not in {"借", "贷"}:
            continue
        flow = "out" if direction == "借" else "in"
        summary = join_unique([safe_get(row, 7), safe_get(row, 9), safe_get(row, 4), safe_get(row, 8)])
        counterparty_name = first_nonblank([safe_get(row, 4), safe_get(row, 5), safe_get(row, 6)])
        if not counterparty_name:
            counterparty_name = extract_counterparty(summary)
        row_no = int(idx) + 1
        records.append(
            {
                "txn_id": f"{item['id']}:r{row_no}",
                "source_id": item["id"],
                "source_file": path.name,
                "source_sheet": sheet,
                "row_no": row_no,
                "bank_name": item.get("bank_name", ""),
                "account_no": item.get("account_no", ""),
                "transaction_date": csv_date(trans_date),
                "flow": flow,
                "amount": amount,
                "bank_debit": amount if flow == "out" else 0,
                "bank_credit": amount if flow == "in" else 0,
                "counterparty_name": counterparty_name,
                "counterparty_account": text(safe_get(row, 1)),
                "summary": text(safe_get(row, 7)),
                "description": text(safe_get(row, 9)),
                "balance": parse_amount(safe_get(row, 11)),
                "raw_text": join_unique([safe_get(row, col) for col in range(len(row))]),
            }
        )
    return pd.DataFrame(records, columns=BANK_COLUMNS)


def parse_xinjiyuan_bank_ledger(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    path = resolve_path(config, item["path"])
    assert path is not None
    sheet, df = read_excel_headerless(path, item.get("sheet"))
    project_year = item.get("year") or config.get("project", {}).get("audit_year")
    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        account_no = text(safe_get(row, 0))
        if not account_no or not any(ch.isdigit() for ch in account_no):
            continue

        second = parse_int(safe_get(row, 1))
        if second and second >= 1900:
            year = second
            month = safe_get(row, 2)
            day = safe_get(row, 3)
            voucher_type = text(safe_get(row, 5))
            voucher_no = text(safe_get(row, 6))
            summary = text(safe_get(row, 7))
            subject = text(safe_get(row, 8))
            debit = parse_amount(safe_get(row, 9))
            credit = parse_amount(safe_get(row, 10))
        else:
            year = project_year
            month = safe_get(row, 1)
            day = safe_get(row, 2)
            voucher_type = text(safe_get(row, 4))
            voucher_no = text(safe_get(row, 5))
            summary = text(safe_get(row, 6))
            subject = ""
            debit = parse_amount(safe_get(row, 7))
            credit = parse_amount(safe_get(row, 8))

        trans_date = date_from_parts(year, month, day)
        if not trans_date or (debit == 0 and credit == 0):
            continue

        net_increase = debit - credit
        flow = "in" if net_increase > 0 else "out"
        amount = abs(net_increase)
        normalized_debit = amount if flow == "in" else 0.0
        normalized_credit = amount if flow == "out" else 0.0
        row_no = int(idx) + 1
        records.append(
            {
                "entry_id": f"{item['id']}:r{row_no}",
                "source_id": item["id"],
                "source_file": path.name,
                "source_sheet": sheet,
                "row_no": row_no,
                "bank_name": item.get("bank_name", ""),
                "account_no": item.get("account_no") or account_no,
                "transaction_date": csv_date(trans_date),
                "flow": flow,
                "amount": amount,
                "ledger_debit": normalized_debit,
                "ledger_credit": normalized_credit,
                "voucher_no": f"{voucher_type} {voucher_no}".strip(),
                "voucher_type": voucher_type,
                "summary": summary,
                "subject": subject,
                "counterparty_name": extract_counterparty(summary),
                "raw_text": join_unique([safe_get(row, col) for col in range(len(row))]),
            }
        )
    return pd.DataFrame(records, columns=LEDGER_COLUMNS)


def parse_llm_bank(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    from .llm_cleaner import ensure_llm_bank_parser

    parser_path = ensure_llm_bank_parser(config, item)
    item = {**item, "generated_parser": str(parser_path)}
    return parse_generated_bank(config, item)


def parse_generated_bank(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    parser_path = resolve_path(config, item.get("generated_parser"))
    if parser_path is None:
        raise ValueError(f"{item['id']} 缺少 generated_parser")
    module_name = f"generated_bank_parser_{item['id']}"
    spec = importlib.util.spec_from_file_location(module_name, parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载生成解析器：{parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.parse(str(resolve_path(config, item["path"])), {"bank_input": item})
    df = pd.DataFrame(rows)
    for col in BANK_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["source_id"] = item["id"]
    df["source_file"] = Path(item["path"]).name
    df["bank_name"] = item.get("bank_name", df.get("bank_name", ""))
    df["account_no"] = item.get("account_no", df.get("account_no", ""))
    return df[BANK_COLUMNS]
