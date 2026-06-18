"""
对账单 & 明细账 预处理脚本
========================
将原始 Excel 文件转换为 bank_ledger_match 工具所需的标准格式。

用法：
    python preprocess_bank_data.py

输入：
    - 对账单.xlsx （银行流水）
    - 明细账.xlsx （序时账/明细账）

输出：
    - processed/对账单_clean.csv   （标准银行流水）
    - processed/明细账_clean.csv   （标准序时账）
    - processed/对账单_formatted.xlsx （可读的格式化 Excel）
    - processed/明细账_formatted.xlsx （可读的格式化 Excel）
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Windows 控制台 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

# ── 路径配置 ──────────────────────────────────────────────
INPUT_DIR = Path(r"C:\Users\DengKaina\Downloads\sample-16050")
OUTPUT_DIR = INPUT_DIR / "processed"
OUTPUT_DIR.mkdir(exist_ok=True)

BANK_FILE = INPUT_DIR / "对账单.xlsx"
LEDGER_FILE = INPUT_DIR / "明细账.xlsx"

# ── 标准列名（与 cleaners.py 中 BANK_COLUMNS / LEDGER_COLUMNS 对齐） ──
BANK_COLUMNS = [
    "txn_id", "source_id", "source_file", "source_sheet", "row_no",
    "bank_name", "account_no", "transaction_date", "flow", "amount",
    "bank_debit", "bank_credit", "counterparty_name", "counterparty_account",
    "summary", "description", "balance", "raw_text",
]

LEDGER_COLUMNS = [
    "entry_id", "source_id", "source_file", "source_sheet", "row_no",
    "bank_name", "account_no", "transaction_date", "flow", "amount",
    "ledger_debit", "ledger_credit", "voucher_no", "voucher_type",
    "summary", "subject", "counterparty_name", "raw_text",
]


def _make_id(prefix: str, row_no: int, *parts) -> str:
    """生成稳定的行 ID。"""
    key = f"{prefix}:{row_no}:" + ":".join(str(p) for p in parts if p)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _amount_to_float(val) -> float:
    """安全地将金额转为 float，无效值返回 0.0。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("，", "")
    if s in ("", "-", "—", "nan", "NaN"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ═══════════════════════════════════════════════════════════
#  银行对账单处理
# ═══════════════════════════════════════════════════════════

def process_bank_statement(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    读取对账单 Excel，返回 (clean_df, display_df)。

    clean_df:   标准 BANK_COLUMNS 格式，供匹配引擎使用
    display_df: 中文列名，便于人工查阅
    """
    # 1. 读取：第 3 行是表头（0-indexed header=2），前两行是标题和账户信息
    raw = pd.read_excel(path, header=2, dtype=str)

    # 2. 提取账户元数据（从第 2 行）
    meta_row = pd.read_excel(path, header=None, nrows=2, dtype=str)
    account_no = ""
    for val in meta_row.iloc[1]:
        if val and "账号" in str(val):
            account_no = str(val).replace("账号:", "").replace("账号：", "").strip()
            break

    bank_name = "农村商业银行"  # 根据实际银行修改

    # 3. 标准化列名映射
    col_map = {
        "交易时间": "transaction_date",
        "收入金额": "bank_credit",
        "支出金额": "bank_debit",
        "账户余额": "balance",
        "对方账号": "counterparty_account",
        "对方户名": "counterparty_name",
        "对方开户行": "description",
        "摘要": "summary",
    }
    df = raw.rename(columns=col_map)

    # 4. 逐行处理
    records = []
    for idx, row in df.iterrows():
        row_no = idx + 4  # Excel 行号（含前 3 行标题/表头）
        txn_date = str(row.get("transaction_date", "")).strip()

        # 解析金额
        credit = _amount_to_float(row.get("bank_credit"))
        debit = _amount_to_float(row.get("bank_debit"))

        if credit > 0:
            flow = "in"
            amount = credit
        elif debit > 0:
            flow = "out"
            amount = debit
        else:
            continue  # 跳过无金额的行

        balance = _amount_to_float(row.get("balance"))

        # 构建原始文本
        raw_parts = [str(row.get(c, "")) for c in df.columns if pd.notna(row.get(c))]
        raw_text = " | ".join(raw_parts)

        records.append({
            "txn_id": _make_id("bank", row_no, txn_date, amount),
            "source_id": "bank_01",
            "source_file": path.name,
            "source_sheet": "Sheet1",
            "row_no": row_no,
            "bank_name": bank_name,
            "account_no": account_no,
            "transaction_date": txn_date,
            "flow": flow,
            "amount": amount,
            "bank_debit": debit,
            "bank_credit": credit,
            "counterparty_name": str(row.get("counterparty_name", "")).strip() if pd.notna(row.get("counterparty_name")) else "",
            "counterparty_account": str(row.get("counterparty_account", "")).strip() if pd.notna(row.get("counterparty_account")) else "",
            "summary": str(row.get("summary", "")).strip() if pd.notna(row.get("summary")) else "",
            "description": str(row.get("description", "")).strip() if pd.notna(row.get("description")) else "",
            "balance": balance,
            "raw_text": raw_text,
        })

    clean_df = pd.DataFrame(records, columns=BANK_COLUMNS)

    # 5. 构建可读的显示表
    display_cols = {
        "row_no": "行号",
        "transaction_date": "交易时间",
        "flow": "收支方向",
        "amount": "金额",
        "bank_debit": "支出",
        "bank_credit": "收入",
        "balance": "余额",
        "counterparty_name": "对方户名",
        "counterparty_account": "对方账号",
        "summary": "摘要",
        "description": "对方开户行",
    }
    display_df = clean_df[list(display_cols.keys())].rename(columns=display_cols)
    display_df["收支方向"] = display_df["收支方向"].map({"in": "收入", "out": "支出"})

    return clean_df, display_df


# ═══════════════════════════════════════════════════════════
#  明细账（序时账）处理
# ═══════════════════════════════════════════════════════════

def process_ledger(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    读取明细账 Excel，返回 (clean_df, display_df)。

    clean_df:   标准 LEDGER_COLUMNS 格式
    display_df: 中文列名，便于人工查阅
    """
    # 1. 读取：第 2 行是表头（0-indexed header=1），第 1 行是标题
    raw = pd.read_excel(path, header=1, dtype=str)

    # 2. 过滤掉 "承前" 行（opening balance）和空行
    raw = raw[raw["凭证日期"].notna() & (raw["凭证日期"] != "nan")].copy()

    bank_name = "农村商业银行"

    records = []
    for idx, row in raw.iterrows():
        row_no = idx + 3  # Excel 行号
        voucher_date = str(row.get("凭证日期", "")).strip()

        # 格式化日期：20200115 → 2020-01-15
        if len(voucher_date) == 8 and voucher_date.isdigit():
            voucher_date = f"{voucher_date[:4]}-{voucher_date[4:6]}-{voucher_date[6:]}"

        debit = _amount_to_float(row.get("借方"))
        credit = _amount_to_float(row.get("贷方"))

        if debit > 0:
            flow = "in"
            amount = debit
        elif credit > 0:
            flow = "out"
            amount = credit
        else:
            continue

        voucher_no = str(row.get("编号", "")).strip() if pd.notna(row.get("编号")) else ""
        voucher_type = str(row.get("类型", "")).strip() if pd.notna(row.get("类型")) else ""

        raw_parts = [str(row.get(c, "")) for c in raw.columns if pd.notna(row.get(c)) and str(row.get(c, "")) != "nan"]
        raw_text = " | ".join(raw_parts)

        records.append({
            "entry_id": _make_id("ledger", row_no, voucher_date, amount),
            "source_id": "ledger_01",
            "source_file": path.name,
            "source_sheet": "Sheet1",
            "row_no": row_no,
            "bank_name": bank_name,
            "account_no": "",
            "transaction_date": voucher_date,
            "flow": flow,
            "amount": amount,
            "ledger_debit": debit,
            "ledger_credit": credit,
            "voucher_no": voucher_no,
            "voucher_type": voucher_type,
            "summary": str(row.get("摘要", "")).strip() if pd.notna(row.get("摘要")) else "",
            "subject": str(row.get("科目", "")).strip() if pd.notna(row.get("科目")) else "",
            "counterparty_name": str(row.get("供应商名称", "")).strip() if pd.notna(row.get("供应商名称")) else "",
            "raw_text": raw_text,
        })

    clean_df = pd.DataFrame(records, columns=LEDGER_COLUMNS)

    # 3. 可读显示表
    display_cols = {
        "row_no": "行号",
        "transaction_date": "凭证日期",
        "voucher_type": "类型",
        "voucher_no": "编号",
        "flow": "借贷方向",
        "amount": "金额",
        "ledger_debit": "借方",
        "ledger_credit": "贷方",
        "subject": "科目",
        "counterparty_name": "供应商名称",
        "summary": "摘要",
    }
    display_df = clean_df[list(display_cols.keys())].rename(columns=display_cols)
    display_df["借贷方向"] = display_df["借贷方向"].map({"in": "借", "out": "贷"})

    return clean_df, display_df


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  银行对账数据预处理")
    print("=" * 60)

    # ── 处理银行对账单 ──
    print(f"\n[1/4] 读取对账单: {BANK_FILE.name}")
    bank_clean, bank_display = process_bank_statement(BANK_FILE)
    print(f"      → {len(bank_clean)} 条交易记录")

    # 统计
    income_count = len(bank_clean[bank_clean["flow"] == "in"])
    expense_count = len(bank_clean[bank_clean["flow"] == "out"])
    total_in = bank_clean[bank_clean["flow"] == "in"]["amount"].sum()
    total_out = bank_clean[bank_clean["flow"] == "out"]["amount"].sum()
    print(f"      收入: {income_count} 笔, 合计 {total_in:,.2f}")
    print(f"      支出: {expense_count} 笔, 合计 {total_out:,.2f}")

    # ── 处理明细账 ──
    print(f"\n[2/4] 读取明细账: {LEDGER_FILE.name}")
    ledger_clean, ledger_display = process_ledger(LEDGER_FILE)
    print(f"      → {len(ledger_clean)} 条分录记录")

    debit_count = len(ledger_clean[ledger_clean["flow"] == "in"])
    credit_count = len(ledger_clean[ledger_clean["flow"] == "out"])
    total_debit = ledger_clean[ledger_clean["flow"] == "in"]["amount"].sum()
    total_credit = ledger_clean[ledger_clean["flow"] == "out"]["amount"].sum()
    print(f"      借方: {debit_count} 笔, 合计 {total_debit:,.2f}")
    print(f"      贷方: {credit_count} 笔, 合计 {total_credit:,.2f}")

    # ── 保存 CSV（供匹配引擎使用） ──
    bank_csv = OUTPUT_DIR / "对账单_clean.csv"
    ledger_csv = OUTPUT_DIR / "明细账_clean.csv"
    bank_clean.to_csv(bank_csv, index=False, encoding="utf-8-sig")
    ledger_clean.to_csv(ledger_csv, index=False, encoding="utf-8-sig")
    print(f"\n[3/4] CSV 已保存:")
    print(f"      → {bank_csv}")
    print(f"      → {ledger_csv}")

    # ── 保存格式化 Excel（供人工查阅） ──
    bank_xlsx = OUTPUT_DIR / "对账单_formatted.xlsx"
    ledger_xlsx = OUTPUT_DIR / "明细账_formatted.xlsx"
    with pd.ExcelWriter(bank_xlsx, engine="openpyxl") as writer:
        bank_display.to_excel(writer, sheet_name="银行流水", index=False)
    with pd.ExcelWriter(ledger_xlsx, engine="openpyxl") as writer:
        ledger_display.to_excel(writer, sheet_name="序时账", index=False)
    print(f"\n[4/4] Excel 已保存:")
    print(f"      → {bank_xlsx}")
    print(f"      → {ledger_xlsx}")

    # ── 数据质量检查 ──
    print("\n" + "=" * 60)
    print("  数据质量检查")
    print("=" * 60)

    # 检查空值
    bank_null_name = bank_clean["counterparty_name"].isna().sum() + (bank_clean["counterparty_name"] == "").sum()
    ledger_null_name = ledger_clean["counterparty_name"].isna().sum() + (ledger_clean["counterparty_name"] == "").sum()

    print(f"\n  对账单 - 对方户名为空: {bank_null_name}/{len(bank_clean)} 行")
    print(f"  明细账 - 供应商名称为空: {ledger_null_name}/{len(ledger_clean)} 行")

    # 检查日期格式
    bank_bad_dates = bank_clean[~bank_clean["transaction_date"].str.match(r"\d{4}-\d{2}-\d{2}", na=False)]
    if len(bank_bad_dates) > 0:
        print(f"\n  ⚠ 对账单有 {len(bank_bad_dates)} 行日期格式异常（非 YYYY-MM-DD）")
        print(f"    示例: {bank_bad_dates['transaction_date'].iloc[0]}")
    else:
        print("\n  ✓ 对账单日期格式正常")

    ledger_bad_dates = ledger_clean[~ledger_clean["transaction_date"].str.match(r"\d{4}-\d{2}-\d{2}", na=False)]
    if len(ledger_bad_dates) > 0:
        print(f"  ⚠ 明细账有 {len(ledger_bad_dates)} 行日期格式异常")
    else:
        print("  ✓ 明细账日期格式正常")

    # 金额交叉验证
    print(f"\n  对账单净额: {total_in - total_out:,.2f}")
    print(f"  明细账净额: {total_debit - total_credit:,.2f}")
    diff = abs((total_in - total_out) - (total_debit - total_credit))
    if diff < 0.01:
        print("  ✓ 两侧净额一致")
    else:
        print(f"  ⚠ 两侧净额差异: {diff:,.2f}")

    print("\n  处理完成！")


if __name__ == "__main__":
    main()
