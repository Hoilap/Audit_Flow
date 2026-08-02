from __future__ import annotations

import re
import shutil
from copy import copy as copy_style
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import column_index_from_string, get_column_letter

from .config import output_dir, resolve_path
from .utils import parse_amount, parse_date, text


# ── public entry ──────────────────────────────────────────────────────────

def fill_working_paper(config: dict[str, Any]) -> Path:
    wp_config = config.get("working_paper", {})
    if not wp_config.get("enabled", True):
        raise RuntimeError("working_paper.enabled=false，跳过底稿填报。")

    template = resolve_path(config, config.get("project", {}).get("template_path"))
    if template is None or not template.exists():
        raise FileNotFoundError(f"找不到底稿模板：{template}")

    output_file = resolve_path(config, wp_config.get("output_file"))
    assert output_file is not None
    output_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, output_file)

    wb = load_workbook(output_file, keep_vba=output_file.suffix.lower() == ".xlsm")
    if wp_config.get("wp03", {}).get("enabled", True):
        _fill_wp03_multi(wb, config)
    if wp_config.get("wp02", {}).get("enabled", True):
        _fill_wp02_multi(wb, config)
    wb.save(output_file)
    return output_file


# ══════════════════════════════════════════════════════════════════════════
#  WP-03  银行存款/其他货币资金收支完整性检查表（多账户 · 纯净模板）
# ══════════════════════════════════════════════════════════════════════════

# ── 样式常量 ─────────────────────────────────────────────────────────────

_THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
_HEADER_FONT = Font(name="宋体", bold=True, size=10)
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_DATA_ALIGN = Alignment(vertical="center", wrap_text=False)
_ACCOUNT_FONT = Font(name="宋体", bold=True, size=11)
_ACCOUNT_ALIGN = Alignment(horizontal="left", vertical="center")
_SUBHEADER_FONT = Font(name="宋体", bold=True, size=10)
_SUBHEADER_ALIGN = Alignment(horizontal="center", vertical="center")

_WP03_COLUMNS = [
    # (col_letter, header_text)
    ("B", "主体名称"),
    ("C", "银行对账单\n日期"),
    ("D", "银行对账单\n借方发生额"),
    ("E", "银行对账单\n贷方发生额"),
    ("F", "银行对账单\n对方户名"),
    ("G", "记账日期"),
    ("H", "凭证字号"),
    ("I", "摘要"),
    ("J", "对方公司名称"),
    ("K", "借方金额"),
    ("L", "贷方金额"),
]

_WP03_MERGE_COLS = 12  # A..L, 合计 12 列


def _fill_wp03_multi(wb, config: dict[str, Any]) -> None:
    """按 account_no 分组，在纯净 WP-03 模板上从头写入所有账户数据。"""
    wp = config.get("working_paper", {}).get("wp03", {})
    matches_path = output_dir(config) / "matches" / "matches.csv"
    if not matches_path.exists():
        return
    matches = pd.read_csv(matches_path, dtype=str).fillna("")
    if matches.empty:
        return

    sheet = wp.get("sheet", "WP-03")
    ws = wb[sheet]
    min_amount = parse_amount(wp.get("min_amount", 70000))
    project = config.get("project", {})
    entity = project.get("client_short_name") or project.get("client_name", "")

    # 过滤 & 按 account_no 分组
    filtered = matches[matches["amount"].apply(parse_amount) >= min_amount].copy()
    if filtered.empty:
        return
    filtered = filtered.sort_values(["flow", "amount"], ascending=[True, False])
    by_account = _group_by_account(filtered)
    if not by_account:
        return

    # 从模板中读取起始行（可通过 task.yml 指定，默认 8）
    start_row = int(wp.get("start_row", 8))

    # 取消该区域所有合并单元格
    _unmerge_region(ws, start_row, ws.max_row, 1, _WP03_MERGE_COLS)

    cursor = start_row

    for account_no, rows in sorted(by_account.items()):
        if rows.empty:
            continue
        first = rows.iloc[0]
        bank_name = text(first.get("bank_name"))
        account_label = f"{bank_name}  {account_no}".strip()

        # 插入本账户所需行数（不够则插入）
        needed = _wp03_account_rows(len(rows))
        available = ws.max_row - cursor + 1
        if needed > available:
            ws.insert_rows(cursor, needed - available)

        # ── 账户信息行 ──
        _write_account_row(ws, cursor, account_label)
        cursor += 1

        # ── 子标题行（银行流水 | 账面记录）──
        _write_subheader_row(ws, cursor)
        cursor += 1

        # ── 列标题行 ──
        _write_column_header_row(ws, cursor)
        cursor += 1

        # ── 数据行 ──
        for offset, (_, match) in enumerate(rows.iterrows()):
            _write_data_row(ws, cursor + offset, entity, match)

        cursor += len(rows)
        # 账户间空一行
        cursor += 1

    # 清除末尾多余旧内容
    _clear_below(ws, cursor)


# ── 辅助函数 ──────────────────────────────────────────────────────────────

def _wp03_account_rows(n_data: int) -> int:
    """计算一个账户区块需要的总行数：account + subheader + colheader + data + 1(空行)"""
    return 3 + n_data + 1


def _unmerge_region(ws, start_row: int, end_row: int, start_col: int, end_col: int) -> None:
    """取消与指定区域有任何重叠的合并单元格。"""
    merged = list(ws.merged_cells.ranges)
    for mr in merged:
        # 检查是否有重叠：两个区间 [a,b] 与 [c,d] 重叠当 a<=d 且 c<=b
        if (mr.min_row <= end_row and mr.max_row >= start_row and
            mr.min_col <= end_col and mr.max_col >= start_col):
            ws.unmerge_cells(str(mr))


def _apply_border(ws, row: int, start_col: int, end_col: int) -> None:
    for c in range(start_col, end_col + 1):
        ws.cell(row=row, column=c).border = _THIN_BORDER


def _write_account_row(ws, row: int, account_label: str) -> None:
    """写入「开户行名称：XXX」行，合并 B..L。"""
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=_WP03_MERGE_COLS)
    cell = ws.cell(row=row, column=2)
    cell.value = f"开户行名称：{account_label}"
    cell.font = _ACCOUNT_FONT
    cell.alignment = _ACCOUNT_ALIGN


def _write_subheader_row(ws, row: int) -> None:
    """写入「银行流水 | 账面记录」分隔行。"""
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    ws.cell(row=row, column=2).value = "银行流水"
    ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=_WP03_MERGE_COLS)
    ws.cell(row=row, column=7).value = "账面记录"
    for col in range(2, _WP03_MERGE_COLS + 1):
        c = ws.cell(row=row, column=col)
        c.font = _SUBHEADER_FONT
        c.alignment = _SUBHEADER_ALIGN
        c.border = _THIN_BORDER


def _write_column_header_row(ws, row: int) -> None:
    """写入列标题行。"""
    for col_letter, header_text in _WP03_COLUMNS:
        col_idx = column_index_from_string(col_letter)
        c = ws.cell(row=row, column=col_idx)
        c.value = header_text
        c.font = _HEADER_FONT
        c.alignment = _HEADER_ALIGN
        c.border = _THIN_BORDER


def _write_data_row(ws, row: int, entity: str, match: pd.Series) -> None:
    """写入一行匹配数据，所有值均为文本格式。"""
    data = {
        "B": entity,
        "C": _fmt_date(match.get("bank_date")),
        "D": _fmt_amount(match.get("bank_debit")),
        "E": _fmt_amount(match.get("bank_credit")),
        "F": text(match.get("counterparty_name")),
        "G": _fmt_date(match.get("ledger_date")),
        "H": text(match.get("voucher_no")),
        "I": text(match.get("ledger_summary")),
        "J": text(match.get("counterparty_name")),
        "K": _fmt_amount(match.get("ledger_debit")),
        "L": _fmt_amount(match.get("ledger_credit")),
    }
    for col_letter, value in data.items():
        col_idx = column_index_from_string(col_letter)
        c = ws.cell(row=row, column=col_idx)
        c.value = value
        c.font = Font(name="宋体", size=10)
        c.alignment = _DATA_ALIGN
        c.border = _THIN_BORDER
        c.number_format = "@"  # 文本格式


def _fmt_date(value: object) -> str:
    """将日期转为 yyyy-mm-dd 文本，避免 Excel 自动格式化。"""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    d = parse_date(s)
    if d:
        return d.strftime("%Y-%m-%d")
    return s


def _fmt_amount(value: object) -> str:
    """金额转为文本：0 或空 → ''，否则保留两位小数。"""
    if value is None:
        return ""
    amount = parse_amount(value)
    if amount == 0:
        return ""
    return f"{amount:.2f}"


def _clear_below(ws, start_row: int) -> None:
    """清除 start_row 及以下所有行的 B..L 列内容（保留 A 列公式）。"""
    for row in range(start_row, ws.max_row + 1):
        for col in range(2, _WP03_MERGE_COLS + 1):
            c = ws.cell(row=row, column=col)
            if isinstance(c.value, str) and c.value.startswith("="):
                continue
            c.value = None
            c.font = Font(name="宋体", size=10)
            c.border = Border()


# ══════════════════════════════════════════════════════════════════════════
#  WP-02  现金流核查总体情况表（多账户支持）
# ══════════════════════════════════════════════════════════════════════════

def _group_by_account(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """按 account_no 分组。若无 account_no 列，统一放入 '' 组。"""
    if "account_no" not in df.columns:
        return {"": df}
    groups: dict[str, pd.DataFrame] = {}
    for key, group in df.groupby("account_no"):
        groups[str(key).strip()] = group
    return groups


def _fill_wp02_multi(wb, config: dict[str, Any]) -> None:
    """自动检测 WP-02 中所有账户区块，按 account_no 分组填入月度汇总。"""
    wp = config.get("working_paper", {}).get("wp02", {})
    clean_dir = output_dir(config) / "clean"
    bank_path = clean_dir / "bank_transactions.csv"
    ledger_path = clean_dir / "ledger_entries.csv"
    if not bank_path.exists() or not ledger_path.exists():
        return

    bank = pd.read_csv(bank_path, dtype=str).fillna("")
    ledger = pd.read_csv(ledger_path, dtype=str).fillna("")
    if bank.empty and ledger.empty:
        return

    sheet = wp.get("sheet", "WP-02")
    ws = wb[sheet]

    # 按 account_no 分组
    bank_groups = _group_by_account(bank)
    ledger_groups = _group_by_account(ledger)
    all_accounts = sorted(set(list(bank_groups.keys()) + list(ledger_groups.keys())))

    if not all_accounts:
        return

    # 检测模板中的区块
    sections = _detect_wp02_sections(ws)

    # 为每个账户填入对应区块
    # 从上到下处理，插入行后动态更新下游区块坐标
    for i, account_no in enumerate(all_accounts):
        bank_df = bank_groups.get(account_no, pd.DataFrame())
        ledger_df = ledger_groups.get(account_no, pd.DataFrame())
        bank_name = _first_value(bank_df, "bank_name") or _first_value(ledger_df, "bank_name")

        if i < len(sections):
            section = sections[i]
            inserted = _fill_one_wp02_section(ws, section, bank_df, ledger_df, bank_name, account_no)
            # 插入行后，更新后续区块坐标
            if inserted > 0:
                for s in sections[i + 1:]:
                    s["data_start"] += inserted
                    s["data_end"] += inserted
                    s["header_row"] += inserted
        else:
            # 需要新建区块
            if sections:
                _create_and_fill_wp02_section(ws, sections[-1], bank_df, ledger_df, bank_name, account_no)


def _detect_wp02_sections(ws) -> list[dict[str, Any]]:
    """检测 WP-02 中「月份」行来定位区块。"""
    sections: list[dict[str, Any]] = []
    month_rows: list[int] = []

    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=3), start=1):
        b_val = str(row[1].value or "").strip()
        if b_val == "月份":
            month_rows.append(r_idx)

    for idx, mr in enumerate(month_rows):
        data_start = mr + 2  # skip 期初
        # 找到「余额」和「差异合计」行
        data_end = data_start + 11  # 默认 12 个月
        for r in range(data_start, ws.max_row + 1):
            b_val = str(ws.cell(row=r, column=2).value or "").strip()
            if b_val in ("余额", "差异合计"):
                data_end = r - 1
                break
        sections.append({
            "index": idx,
            "header_row": mr,
            "data_start": data_start,
            "data_end": data_end,
        })

    return sections


def _fill_one_wp02_section(
    ws, section: dict, bank: pd.DataFrame, ledger: pd.DataFrame,
    bank_name: str, account_no: str,
) -> int:
    """填入一个 WP-02 区块的月度数据。返回插入的行数。"""
    # 列映射：header_row 的 C=银行, D=账户号, E=账面借方, F=流水贷方, G=差异(公式), H=账面贷方, I=流水借方, J=差异(公式)
    header_row = section["header_row"]
    # 银行名和账号列
    for month in range(1, 13):
        row_no = section["data_start"] + month - 1
        if row_no > section["data_end"]:
            break
        _set(ws, "C", row_no, bank_name)
        _set(ws, "D", row_no, account_no)
        _set(ws, "E", row_no, _monthly_sum(ledger, "ledger_debit", month))
        _set(ws, "F", row_no, _monthly_sum(bank, "bank_credit", month))
        _set(ws, "H", row_no, _monthly_sum(ledger, "ledger_credit", month))
        _set(ws, "I", row_no, _monthly_sum(bank, "bank_debit", month))
        # G 和 J 列保留公式，不写入

    return 0  # WP-02 固定 12 行，不需要插入


def _create_and_fill_wp02_section(
    ws, reference_section: dict, bank: pd.DataFrame, ledger: pd.DataFrame,
    bank_name: str, account_no: str,
) -> None:
    """以 reference_section 为模板新建 WP-02 区块。"""
    # 在 reference_section 末尾之后插入
    insert_after = reference_section["data_end"]
    # 找到下一个区块的开始或结束
    next_start = None
    for r in range(insert_after + 1, ws.max_row + 1):
        b_val = str(ws.cell(row=r, column=2).value or "").strip()
        if b_val == "月份":
            next_start = r
            break

    # 新区块需要 2(期初+余额) + 12(月) + 1(差异合计) = 15 行
    new_rows = 15
    if next_start:
        _insert_rows_preserve_formulas(ws, next_start, new_rows, reference_section["header_row"])
    else:
        _insert_rows_preserve_formulas(ws, insert_after + 1, new_rows, reference_section["header_row"])

    new_header_row = insert_after + 1
    new_data_start = new_header_row + 2

    # 复制 header 样式并写入内容
    for col in range(1, ws.max_column + 1):
        _copy_cell_style(ws, reference_section["header_row"], col, new_header_row, col)

    ws.cell(row=new_header_row, column=2).value = "月份"
    ws.cell(row=new_header_row, column=3).value = "银行"
    ws.cell(row=new_header_row, column=4).value = "账户号"
    ws.cell(row=new_header_row, column=5).value = "账面借方"
    ws.cell(row=new_header_row, column=6).value = "流水贷方"
    ws.cell(row=new_header_row, column=7).value = "差异"
    ws.cell(row=new_header_row, column=8).value = "账面贷方"
    ws.cell(row=new_header_row, column=9).value = "流水借方"
    ws.cell(row=new_header_row, column=10).value = "差异"
    ws.cell(row=new_header_row, column=11).value = "备注(有差异或异常的应解释原因)"

    # 期初行
    ws.cell(row=new_header_row + 1, column=2).value = "期初"
    # 月份标签
    months = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
    for mi, m in enumerate(months):
        ws.cell(row=new_data_start + mi, column=2).value = m

    new_section = {
        "header_row": new_header_row,
        "data_start": new_data_start,
        "data_end": new_data_start + 11,
    }
    _fill_one_wp02_section(ws, new_section, bank, ledger, bank_name, account_no)


# ══════════════════════════════════════════════════════════════════════════
#  helpers
# ══════════════════════════════════════════════════════════════════════════

def _insert_rows_preserve_formulas(ws, insert_at: int, count: int, template_row: int) -> None:
    """在指定位置插入行，保留下方公式列（O 列/差额列）的公式引用自动调整。"""
    ws.insert_rows(insert_at, count)
    for offset in range(count):
        dst_row = insert_at + offset
        if dst_row != template_row:
            _copy_row_style(ws, template_row, dst_row)


def _set(ws, col: str | int | None, row_no: int, value: Any) -> None:
    if col is None:
        return
    if isinstance(col, str):
        col_idx = column_index_from_string(col)
    else:
        col_idx = col
    cell = ws.cell(row=row_no, column=col_idx)
    for merged_range in ws.merged_cells.ranges:
        if cell.coordinate in merged_range:
            cell = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
            break
    # 不覆盖已有公式
    if isinstance(cell.value, str) and cell.value.startswith("="):
        return
    cell.value = value


def _clear_row(ws, row_no: int, cols) -> None:
    for col in cols:
        if col:
            col_idx = column_index_from_string(col) if isinstance(col, str) else col
            cell = ws.cell(row=row_no, column=col_idx)
            # 不清理公式
            if isinstance(cell.value, str) and cell.value.startswith("="):
                continue
            cell.value = None


def _copy_row_style(ws, src_row: int, dst_row: int) -> None:
    if src_row == dst_row:
        return
    for col_idx in range(1, ws.max_column + 1):
        _copy_cell_style(ws, src_row, col_idx, dst_row, col_idx)


def _copy_cell_style(ws, src_row: int, src_col: int, dst_row: int, dst_col: int) -> None:
    src = ws.cell(src_row, src_col)
    dst = ws.cell(dst_row, dst_col)
    if src.has_style:
        dst._style = copy_style(src._style)
    if src.number_format:
        dst.number_format = src.number_format
    if src.alignment:
        dst.alignment = copy_style(src.alignment)
    if src.font:
        dst.font = copy_style(src.font)
    if src.fill:
        dst.fill = copy_style(src.fill)
    if src.border:
        dst.border = copy_style(src.border)


def _date_or_text(value: object) -> object:
    raw = text(value)
    if "|" in raw:
        return raw
    return parse_date(raw) or raw


def _blank_if_zero(value: object, zero_value: object | None = None) -> object:
    amount = parse_amount(value)
    if amount == 0:
        return zero_value
    return amount


def _monthly_sum(df: pd.DataFrame, col: str, month: int) -> float:
    if df.empty or col not in df or "transaction_date" not in df:
        return 0.0
    total = 0.0
    for _, row in df.iterrows():
        trans_date = parse_date(row.get("transaction_date"))
        if trans_date and trans_date.month == month:
            total += parse_amount(row.get(col))
    return round(total, 2)


def _first_value(df: pd.DataFrame, col: str) -> str:
    if df.empty or col not in df:
        return ""
    for value in df[col]:
        item = text(value)
        if item:
            return item
    return ""
