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

import logging

_log = logging.getLogger(__name__)


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


def _validate_data_quality(
    df: pd.DataFrame, source_id: str, source_file: str, data_type: str,
) -> list[dict[str, Any]]:
    """检查解析后的 DataFrame 中关键字段为空的行，返回问题列表。

    检查的关键字段：
    - amount: 金额为 0 或 NaN（可能是解析器遗漏或数据异常）
    - transaction_date: 日期为空（无法参与时间维度匹配）
    - account_no: 账号为空（无法按账户维度匹配）

    每个 issue 包含 bad_rows（1-based 数据行号列表）便于前端定位。
    """
    if df is None or len(df) == 0:
        return []
    issues: list[dict[str, Any]] = []
    total = len(df)

    def _row_label(bad_mask) -> tuple[int, list[int]]:
        """返回 (bad_count, 1-based 行号列表)。"""
        bad_indices = df.index[bad_mask].tolist()
        # 转成 1-based 数据行号（假设 DataFrame 从 0 开始连续索引）
        rows = [int(i) + 1 for i in bad_indices]
        return len(rows), rows

    def _fmt_rows(rows: list[int], total_bad: int) -> str:
        """格式化行号片段：最多展示 10 个，超出部分用 ... 表示。"""
        if total_bad <= 10:
            return f"行号: {', '.join(str(r) for r in rows)}"
        shown = rows[:10]
        return f"行号: {', '.join(str(r) for r in shown)} ... 等共 {total_bad} 行"

    # 金额为 0 或 NaN
    if "amount" in df.columns:
        amt = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        bad_mask = amt == 0
        bad, rows = _row_label(bad_mask)
        if bad > 0:
            issues.append({
                "field": "amount",
                "count": bad,
                "total": total,
                "bad_rows": rows,
                "message": f"{bad}/{total} 条记录金额为 0 或为空（{_fmt_rows(rows, bad)}）",
            })

    # 金额为负数（不符合约定：amount 应始终为正，flow 标记方向）
    for col in ("amount", "bank_debit", "bank_credit", "ledger_debit", "ledger_credit"):
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce").fillna(0)
            bad_mask = vals < 0
            bad, rows = _row_label(bad_mask)
            if bad > 0:
                issues.append({
                    "field": col,
                    "count": bad,
                    "total": total,
                    "bad_rows": rows,
                    "message": f"{bad}/{total} 条记录 {col} 为负数（应始终为正数）（{_fmt_rows(rows, bad)}）",
                })

    # 日期为空
    if "transaction_date" in df.columns:
        bad_mask = df["transaction_date"].isna() | (
            df["transaction_date"].astype(str).str.strip() == ""
        )
        bad, rows = _row_label(bad_mask)
        if bad > 0:
            issues.append({
                "field": "transaction_date",
                "count": bad,
                "total": total,
                "bad_rows": rows,
                "message": f"{bad}/{total} 条记录日期为空（{_fmt_rows(rows, bad)}）",
            })

    # 账号为空
    if "account_no" in df.columns:
        bad_mask = df["account_no"].isna() | (
            df["account_no"].astype(str).str.strip() == ""
        )
        bad, rows = _row_label(bad_mask)
        if bad > 0:
            issues.append({
                "field": "account_no",
                "count": bad,
                "total": total,
                "bad_rows": rows,
                "message": f"{bad}/{total} 条记录账号为空（{_fmt_rows(rows, bad)}）",
            })

    if issues:
        summary = "; ".join(i["message"] for i in issues)
        _log.warning(
            "数据质量问题 [%s] %s (%s): %s", source_id, source_file, data_type, summary,
        )
        # 通过 SSE 实时通知前端
        try:
            from desktop.common import notify_frontend
            notify_frontend("data_quality_warning", {
                "source_id": source_id,
                "source_file": source_file,
                "type": data_type,
                "issues": issues,
            })
        except Exception:
            pass

    return issues


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


def clean_bank_to_csv(config: dict[str, Any], parser: str | None = None) -> Path:
    """仅清洗银行流水，返回输出 CSV 路径。parser 可覆盖 task.yml 中的解析器。"""
    out = output_dir(config) / "clean"
    out.mkdir(parents=True, exist_ok=True)
    bank_df = parse_bank_statements(config, parser_override=parser)
    bank_path = out / "bank_transactions.csv"
    bank_df.to_csv(bank_path, index=False, encoding="utf-8-sig")
    return bank_path


def clean_ledger_to_csv(config: dict[str, Any], parser: str | None = None) -> Path:
    """仅清洗序时账，返回输出 CSV 路径。parser 可覆盖 task.yml 中的解析器。"""
    out = output_dir(config) / "clean"
    out.mkdir(parents=True, exist_ok=True)
    ledger_df = parse_ledgers(config, parser_override=parser)
    ledger_path = out / "ledger_entries.csv"
    ledger_df.to_csv(ledger_path, index=False, encoding="utf-8-sig")
    return ledger_path


def parse_bank_statements(config: dict[str, Any], parser_override: str | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    items = enabled_items(config.get("inputs", {}).get("bank_statements", []))
    # 初始化清洗元数据（sideband），供路由层读取
    cleaning_info = config.setdefault("_cleaning", {})
    warnings_list = cleaning_info.setdefault("warnings", [])
    _log.info("parse_bank_statements: 共 %d 个银行流水源待处理", len(items))
    for item in items:
        item_id = item.get("id", "?")
        parser = parser_override or item.get("parser")
        _log.info("  [%s] 开始解析，解析器=%s，文件=%s", item_id, parser, item.get("path", "?"))
        try:
            if parser == "icbc_historydetail":
                df = parse_icbc_historydetail(config, item)
            elif parser == "llm_bank":
                df = parse_llm_bank(config, item)
            elif parser == "generated_bank":
                df = parse_generated_bank(config, item)
            else:
                raise ValueError(f"未知银行流水解析器：{parser}")
            _log.info("  [%s] 解析成功：%d 条记录", item_id, len(df))
            # 数据质量校验
            file_warnings = _validate_data_quality(
                df, item_id, item.get("path", "?"), "bank",
            )
            for w in file_warnings:
                warnings_list.append({
                    "source_id": item_id,
                    "source_file": Path(item.get("path", "?")).name,
                    "type": "bank",
                    **w,
                })
            frames.append(df)
        except Exception as exc:
            _log.error(
                "  [%s] 解析失败：%s\n  解析器=%s，文件=%s\n  错误详情：%s",
                item_id, type(exc).__name__, parser, item.get("path", "?"), exc,
            )
            raise
    if not frames:
        _log.warning("parse_bank_statements: 没有成功解析任何银行流水文件")
        return pd.DataFrame(columns=BANK_COLUMNS)
    result = pd.concat(frames, ignore_index=True)[BANK_COLUMNS]
    _log.info("parse_bank_statements: 合计 %d 条记录（%d 个文件）", len(result), len(frames))
    return result


def parse_ledgers(config: dict[str, Any], parser_override: str | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    items = enabled_items(config.get("inputs", {}).get("ledgers", []))
    # 初始化清洗元数据（sideband），供路由层读取
    cleaning_info = config.setdefault("_cleaning", {})
    warnings_list = cleaning_info.setdefault("warnings", [])
    _log.info("parse_ledgers: 共 %d 个序时账源待处理", len(items))
    for item in items:
        item_id = item.get("id", "?")
        parser = parser_override or item.get("parser")
        _log.info("  [%s] 开始解析，解析器=%s，文件=%s", item_id, parser, item.get("path", "?"))
        try:
            if parser == "xinjiyuan_bank_ledger":
                df = parse_xinjiyuan_bank_ledger(config, item)
            elif parser == "llm_ledger":
                df = parse_llm_ledger(config, item)
            else:
                raise ValueError(f"未知序时账解析器：{parser}")
            _log.info("  [%s] 解析成功：%d 条记录", item_id, len(df))
            # 数据质量校验
            file_warnings = _validate_data_quality(
                df, item_id, item.get("path", "?"), "ledger",
            )
            for w in file_warnings:
                warnings_list.append({
                    "source_id": item_id,
                    "source_file": Path(item.get("path", "?")).name,
                    "type": "ledger",
                    **w,
                })
            frames.append(df)
        except Exception as exc:
            _log.error(
                "  [%s] 解析失败：%s\n  解析器=%s，文件=%s\n  错误详情：%s",
                item_id, type(exc).__name__, parser, item.get("path", "?"), exc,
            )
            raise
    if not frames:
        _log.warning("parse_ledgers: 没有成功解析任何序时账文件")
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    result = pd.concat(frames, ignore_index=True)[LEDGER_COLUMNS]
    _log.info("parse_ledgers: 合计 %d 条记录（%d 个文件）", len(result), len(frames))
    if len(result) == 0 and items:
        _log.warning(
            "parse_ledgers: 配置了 %d 个序时账源但解析结果为 0 条记录，"
            "请检查序时账文件格式是否与解析器匹配。",
            len(items),
        )
    return result


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
    result = parse_generated_bank(config, item)

    # # 后处理: 确保金额始终为正数（flow 标记方向，amount 为正）。
    # # LLM 生成的解析器有时会把 out 的金额存为负数。
    # if result is not None and len(result) > 0 and "amount" in result.columns:
    #     amt = pd.to_numeric(result["amount"], errors="coerce").fillna(0)
    #     neg_mask = amt < 0
    #     if neg_mask.any():
    #         _log.info(
    #             "%s: 自动校正 %d 条记录的金额为正数（LLM 解析器输出了负数金额）",
    #             item["id"], neg_mask.sum(),
    #         )
    #         result = result.copy()
    #         result.loc[neg_mask, "amount"] = amt[neg_mask].abs()
    #         if "bank_debit" in result.columns:
    #             result["bank_debit"] = pd.to_numeric(
    #                 result["bank_debit"], errors="coerce"
    #             ).fillna(0).abs()
    #         if "bank_credit" in result.columns:
    #             result["bank_credit"] = pd.to_numeric(
    #                 result["bank_credit"], errors="coerce"
    #             ).fillna(0).abs()

    return result


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
    _log.info("  [%s] 加载解析器: %s", item["id"], parser_path.name)
    # 规范化: 空 sheet "" → 0（第一个 sheet），None 会让 pd.read_excel 读取所有 sheet 返回 dict
    parser_item = {**item}
    if not parser_item.get("sheet"):
        parser_item["sheet"] = 0
    try:
        rows = module.parse(str(resolve_path(config, item["path"])), parser_item)
    except AttributeError as exc:
        if "'dict' object has no attribute" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器在处理多工作表时出错：解析器将 sheet_name=None "
                f"传给 pd.read_excel 导致返回 dict。请删除缓存的解析器文件以重新生成。"
                f"解析器路径: {parser_path}"
            ) from exc
        raise
    except TypeError as exc:
        if "expected str instance" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器在拼接字符串时出错：{exc}。"
                f"请在 .join() 调用前用 str() 转换每个元素。"
                f"解析器路径: {parser_path}，请删除该文件后重新生成。"
            ) from exc
        if "not iterable" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器对非字符串值使用了 in 操作符：{exc}。"
                f"请在 in 检查前用 str() 转换单元格值（如 '关键词' in str(val)）。"
                f"解析器路径: {parser_path}，请删除该文件后重新生成。"
            ) from exc
        raise
    except Exception as exc:
        _log.error(
            "  [%s] 解析器 %s 执行出错：%s: %s\n  文件: %s",
            item["id"], parser_path.name, type(exc).__name__, exc, item.get("path", "?"),
        )
        raise
    row_count = len(rows) if isinstance(rows, (list, dict, pd.DataFrame)) else "?"
    _log.info("  [%s] 解析器返回 %s 条原始记录", item["id"], row_count)
    # 安全网: 处理生成的 parse() 返回意外类型的情况
    if isinstance(rows, dict):
        # 解析器可能将 pd.read_excel(sheet_name=None) 返回的 dict 直接透传
        first_df = next(iter(rows.values()))
        rows = first_df.to_dict("records") if isinstance(first_df, pd.DataFrame) else []
    elif isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    df = pd.DataFrame(rows)
    for col in BANK_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["source_id"] = item["id"]
    df["source_file"] = Path(item["path"]).name
    # 配置值非空时优先采用；为空时保留解析器从数据中提取的值。
    # 注意：.get(key, default) 只在键缺失时返回 default，空字符串会误清空解析器结果。
    if item.get("bank_name"):
        df["bank_name"] = item["bank_name"]
    if item.get("account_no"):
        df["account_no"] = item["account_no"]
    # 覆写 LLM 解析器生成的 txn_id，使用 {source_id}_{source_file}_{source_sheet}_{row_no}
    # 确保跨多个 source file/sheet 时 ID 唯一，防止 bank_by_id dict 键碰撞导致记录幽灵消耗
    df["txn_id"] = df.apply(
        lambda row: f"{row['source_id']}_{row['source_file']}_{row['source_sheet']}_{row['row_no']}", axis=1
    )
    return df[BANK_COLUMNS]


def parse_llm_ledger(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    from .llm_cleaner import ensure_llm_ledger_parser

    parser_path = ensure_llm_ledger_parser(config, item)
    item_with_parser = {**item, "generated_parser": str(parser_path)}
    result = parse_generated_ledger(config, item_with_parser)

    # 安全网: LLM 生成的解析器返回 0 行时，回退到硬编码的新纪元序时账解析器
    if len(result) == 0:
        _log.warning(
            "LLM 解析器 %s 为 %s 返回 0 条记录，回退到 xinjiyuan_bank_ledger 硬编码解析器",
            parser_path.name, item["id"],
        )
        result = parse_xinjiyuan_bank_ledger(config, item)
        if len(result) > 0:
            _log.info(
                "xinjiyuan_bank_ledger 成功解析 %s: %d 条记录",
                item["id"], len(result),
            )

    # # 后处理: 根据 ledger_debit/ledger_credit 自动校正 flow 方向。
    # # LLM 生成的解析器经常搞反借贷→flow 的映射，这里做兜底修正。
    # # 公司账簿中银行存款是资产类科目：借方增加 = in，贷方减少 = out。
    # if len(result) > 0 and "ledger_debit" in result.columns and "ledger_credit" in result.columns:
    #     debit = pd.to_numeric(result["ledger_debit"], errors="coerce").fillna(0)
    #     credit = pd.to_numeric(result["ledger_credit"], errors="coerce").fillna(0)
    #     corrected = pd.Series("", index=result.index)
    #     corrected[debit > credit] = "in"
    #     corrected[credit > debit] = "out"
    #     corrected[(debit == 0) & (credit == 0)] = ""
    #     mask = corrected != ""
    #     wrong = mask & (result["flow"] != corrected)
    #     if wrong.any():
    #         _log.info(
    #             "%s: 自动校正 %d 条记录的 flow 方向（LLM 解析器借贷映射有误）",
    #             item["id"], wrong.sum(),
    #         )
    #         result = result.copy()
    #         result.loc[mask, "flow"] = corrected[mask]
    #         # 同步校正 amount（取非零的 debit 或 credit）
    #         result.loc[mask, "amount"] = result.loc[mask, ["ledger_debit", "ledger_credit"]].apply(
    #             lambda r: max(abs(r.get("ledger_debit", 0)), abs(r.get("ledger_credit", 0))), axis=1
    #         )

    return result


def parse_generated_ledger(config: dict[str, Any], item: dict[str, Any]) -> pd.DataFrame:
    parser_path = resolve_path(config, item.get("generated_parser"))
    if parser_path is None:
        raise ValueError(f"{item['id']} 缺少 generated_parser")
    module_name = f"generated_ledger_parser_{item['id']}"
    spec = importlib.util.spec_from_file_location(module_name, parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载生成解析器：{parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _log.info("  [%s] 加载解析器: %s", item["id"], parser_path.name)
    # 规范化: 空 sheet "" → 0（第一个 sheet），None 会让 pd.read_excel 读取所有 sheet 返回 dict
    parser_item = {**item}
    if not parser_item.get("sheet"):
        parser_item["sheet"] = 0
    try:
        rows = module.parse(str(resolve_path(config, item["path"])), parser_item)
    except AttributeError as exc:
        if "'dict' object has no attribute" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器在处理多工作表时出错：解析器将 sheet_name=None "
                f"传给 pd.read_excel 导致返回 dict。请删除缓存的解析器文件以重新生成。"
                f"解析器路径: {parser_path}"
            ) from exc
        raise
    except TypeError as exc:
        if "expected str instance" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器在拼接字符串时出错：{exc}。"
                f"请在 .join() 调用前用 str() 转换每个元素。"
                f"解析器路径: {parser_path}，请删除该文件后重新生成。"
            ) from exc
        if "not iterable" in str(exc):
            raise RuntimeError(
                f"{item['id']} 的生成解析器对非字符串值使用了 in 操作符：{exc}。"
                f"请在 in 检查前用 str() 转换单元格值（如 '关键词' in str(val)）。"
                f"解析器路径: {parser_path}，请删除该文件后重新生成。"
            ) from exc
        raise
    except Exception as exc:
        _log.error(
            "  [%s] 解析器 %s 执行出错：%s: %s\n  文件: %s",
            item["id"], parser_path.name, type(exc).__name__, exc, item.get("path", "?"),
        )
        raise
    row_count = len(rows) if isinstance(rows, (list, dict, pd.DataFrame)) else "?"
    _log.info("  [%s] 解析器返回 %s 条原始记录", item["id"], row_count)
    # 安全网: 处理生成的 parse() 返回意外类型的情况
    if isinstance(rows, dict):
        first_df = next(iter(rows.values()))
        rows = first_df.to_dict("records") if isinstance(first_df, pd.DataFrame) else []
    elif isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    df = pd.DataFrame(rows)
    for col in LEDGER_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["source_id"] = item["id"]
    df["source_file"] = Path(item["path"]).name
    # 配置值非空时优先采用；为空时保留解析器填充的原始银行信息（Phase 1 宁多勿缺规则）。
    # 注意：.get(key, default) 只在键缺失时返回 default，空字符串会误清空解析器结果。
    if item.get("bank_name"):
        df["bank_name"] = item["bank_name"]
    if item.get("account_no"):
        df["account_no"] = item["account_no"]
    # 覆写 LLM 解析器生成的 entry_id，使用 {source_id}_{source_file}_{source_sheet}_{row_no}
    # 与 parse_generated_bank 保持一致的唯一 ID 策略
    df["entry_id"] = df.apply(
        lambda row: f"{row['source_id']}_{row['source_file']}_{row['source_sheet']}_{row['row_no']}", axis=1
    )
    return df[LEDGER_COLUMNS]
