"""LLM-driven cleaning scripts for outbound_settlement_match.

Generates Python cleaning scripts via LLM for settlement CSVs and
outbound Excel sheets.  Each generated script defines a ``clean()``
function that reads a file and returns standardised records.

Generated scripts are cached by column-layout fingerprint so that
files with identical headers share one script (no redundant LLM calls).

Falls back gracefully when LLM is disabled or generation fails.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from .llm_agent import run_osm_cleaner_agent
from .utils import strip_code_fence
from audit_workflow.llm_agent import _extract_token_usage

logger = logging.getLogger(__name__)

# ── Standard column schemas (referenced in prompts) ─────────────

_SETTLEMENT_SCHEMA = [
    {"name": "partner_txn_id", "desc": "平台交易ID（订单级）"},
    {"name": "original_txn_id", "desc": "原始交易ID（退款时引用）"},
    {"name": "amount", "desc": "交易金额（原币种）", "type": "float"},
    {"name": "rmb_amount", "desc": "人民币交易金额", "type": "float"},
    {"name": "fee", "desc": "平台手续费", "type": "float"},
    {"name": "settlement", "desc": "结算金额（原币种）", "type": "float"},
    {"name": "rmb_settlement", "desc": "人民币结算金额", "type": "float"},
    {"name": "currency", "desc": "原币种代码"},
    {"name": "rate", "desc": "汇率", "type": "float"},
    {"name": "payment_time", "desc": "支付时间"},
    {"name": "settlement_time", "desc": "结算时间"},
    {"name": "type", "desc": "交易类型（P=支付, R=退款）"},
    {"name": "status", "desc": "交易状态"},
    {"name": "stem_from", "desc": "来源"},
    {"name": "remarks", "desc": "备注（商品描述等）"},
]

_OUTBOUND_SCHEMAS = {
    "sellout": [
        {"name": "order_id", "desc": "订单编号 / 交易订单号 / 平台订单号（匹配关键字段）"},
        {"name": "shop_name", "desc": "店铺名称"},
        {"name": "warehouse_name", "desc": "仓库名称"},
        {"name": "order_time", "desc": "下单时间 / 订单创建时间"},
        {"name": "payment_time", "desc": "支付时间 / 订单付款时间"},
        {"name": "ship_time", "desc": "发货时间 / 出库时间 / 出库日期"},
        {"name": "status", "desc": "订单状态"},
        {"name": "amount", "desc": "订单金额 / 总金额 / 买家实际支付金额（注意：不是 NSV）", "type": "float"},
        {"name": "freight", "desc": "运费 / 买家应付邮费", "type": "float"},
        {"name": "quantity", "desc": "商品数量 / 宝贝总数量 / QTY", "type": "float"},
        {"name": "product_name", "desc": "商品名称 / 宝贝标题 / 货品名称"},
        {"name": "product_code", "desc": "商品编码 / 商家编码 / 货品编码"},
        {"name": "nsv", "desc": "NSV（净销售额，仅映射列名为 NSV 的列）", "type": "float"},
        {"name": "buyer_name", "desc": "买家昵称 / 买家会员名"},
        {"name": "recipient_name", "desc": "收件人 / 收货人姓名"},
        {"name": "logistics_no", "desc": "物流单号 / 配送运单号"},
        {"name": "logistics_company", "desc": "物流公司 / 配送公司"},
    ],
    "refund": [
        {"name": "order_id", "desc": "退款编号 / 交易订单号（关联订单号）"},
        {"name": "refund_id", "desc": "退款ID"},
        {"name": "refund_time", "desc": "退款完结时间 / 退款成功时间"},
        {"name": "refund_amount", "desc": "买家退款金额 / 退款金额", "type": "float"},
        {"name": "refund_type", "desc": "退款类型（手工退款/系统退款）"},
        {"name": "need_return", "desc": "是否需要退货"},
        {"name": "product_name", "desc": "商品名称 / 宝贝标题 / 货品名称"},
        {"name": "buyer_name", "desc": "买家昵称 / 买家会员昵称"},
        {"name": "status", "desc": "状态"},
    ],
    "transfer": [
        {"name": "order_id", "desc": "交易订单号 / 平台订单号"},
        {"name": "erp_order_id", "desc": "ERP订单号"},
        {"name": "transfer_id", "desc": "单号"},
        {"name": "lp_id", "desc": "LP单号"},
        {"name": "product_code", "desc": "货品编码"},
        {"name": "product_name", "desc": "货品名称"},
        {"name": "warehouse_name", "desc": "仓库名称"},
        {"name": "transfer_time", "desc": "出入库时间"},
        {"name": "doc_type", "desc": "单据类型"},
        {"name": "stock_type", "desc": "库存类型"},
        {"name": "merchant_code", "desc": "商家编码 / 物料编码"},
        {"name": "quantity", "desc": "出入数量 / QTY", "type": "float"},
        {"name": "nsv", "desc": "NSV（仅映射列名为 NSV 的列，禁止映射订单金额/总金额到 nsv）", "type": "float"},
    ],
}

# ── System prompts ──────────────────────────────────────────────

SETTLEMENT_SYSTEM_PROMPT = """\
你是审计数据清洗助手。你的任务是为平台结算流水 CSV 文件生成一个 Python 清洗脚本。
脚本必须定义 def clean(path: str, month: str, source_file: str) -> list[dict]。
- path: CSV 文件路径
- month: 月份键（如 "2022-01"），由调用方传入，直接写入每条记录
- source_file: 原始文件名，由调用方传入，直接写入每条记录

返回的每条记录必须包含以下标准字段（缺失的填空字符串，金额缺失填 0.0）：
{schema}

重要要求：
1. 使用 pandas 读取 CSV，依次尝试编码 utf-8, utf-8-sig, gbk, gb18030, latin-1。
2. CSV 列名可能是英文或中文，你需要根据 raw_first_rows 和 sample_rows 推断列名映射。
3. 金额字段（amount, rmb_amount, fee, settlement, rmb_settlement, rate）必须转为 float，去掉逗号、货币符号。
4. month 和 source_file 直接使用传入参数，不要自行推断。
5. **重复列名处理**：rename 之后必须检查并去除重复列名：df = df.loc[:, ~df.columns.duplicated(keep='first')]。
6. 只输出 Python 代码，不要输出解释，不要用 ``` 代码块包裹。"""

OUTBOUND_SYSTEM_PROMPT = """\
你是审计数据清洗助手。你的任务是为出库报告 Excel 工作表生成一个 Python 清洗脚本。
脚本必须定义 def clean(path: str, sheet_name: str, source_file: str, month: str, sheet_type: str) -> list[dict]。
- path: Excel 文件路径
- sheet_name: 工作表名称
- source_file: 原始文件名，由调用方传入
- month: 月份键（如 "2022-04"），由调用方传入
- sheet_type: 工作表类型（sellout/refund/return/transfer）

根据 sheet_type 返回对应标准字段的记录：
{schema}


⚠️ 重要区分：
- **amount（订单金额）**：对应"订单金额""总金额""买家实际支付金额"等，表示买家应付或实际支付的金额。
- **nsv（净销售额）**：仅对应列名明确为"NSV"的列。NSV 是 Net Sales Value 的缩写，与订单金额含义不同。
- **绝对不要**将"订单金额"映射到 nsv，即使该工作表没有名为"NSV"的列。如果工作表没有 NSV 列，nsv 字段填空或填 0.0。
- 每个标准字段只能对应一个源列，禁止将多个源列映射到同一标准字段。

重要要求：
1. 使用 pandas + openpyxl 读取 Excel 工作表，先用 header=None 读取前 5 行来检测表头位置。
2. 表头检测：查看 raw_first_rows，找到包含最多中文列名（如"订单""商品""金额"等）的行作为真正的表头行。表头行之前可能有汇总行（多数值为空或数字），必须跳过。确定表头行号后用 header=行号 读取。
3. Excel 列名可能是英文或中文，根据上面的映射参考和样本数据推断列名映射。
4. 金额字段必须转为 float，去掉逗号、货币符号。
5. source_file 和 month 直接使用传入参数。
6. **重复列名处理**：rename 之后必须检查并去除重复列名：df = df.loc[:, ~df.columns.duplicated(keep='first')]。
7. 只输出 Python 代码，不要输出解释，不要用 ``` 代码块包裹。"""

# 常见列名映射参考（中文/英文 → 标准字段名）：
#   订单编号 / 交易订单号 / 平台订单号 → order_id
#   ERP订单号 → erp_order_id
#   店铺名称 / 店铺Id → shop_name
#   仓库名称 / 仓库编码 → warehouse_name
#   下单时间 / 订单创建时间 → order_time
#   支付时间 / 订单付款时间 → payment_time
#   发货时间 / 出库时间 / 出库日期 → ship_time
#   出入库时间 → transfer_time
#   订单状态 → status
#   总金额 / 买家应付货款 / 买家实际支付金额 / 订单金额 → amount（订单金额，不是 NSV）
#   运费 / 买家应付邮费 → freight
#   商品数量 / 宝贝总数量 / QTY → quantity
#   宝贝标题 / 商品名称 / 货品名称 → product_name
#   商家编码 / 商品编码 / 货品编码 → product_code
#   NSV → nsv（净销售额，与"订单金额"是不同的字段，不可混用）
#   单号 → transfer_id
#   LP单号 → lp_id
#   单据类型 → doc_type
#   库存类型 → stock_type
#   物料编码 / 商家编码 → merchant_code
#   退款编号 → order_id（退款表中）
#   退款完结时间 → refund_time
#   买家退款金额 / 退款金额 → refund_amount
#   手工退款/系统退款 → refund_type
#   是否需要退货 → need_return
#   买家会员名 / 买家昵称 → buyer_name
#   收件人 / 收货人姓名 → recipient_name
#   物流单号 / 配送运单号 → logistics_no
#   物流公司 / 配送公司 → logistics_company

_MAX_RETRIES = 3


# ── Public API ──────────────────────────────────────────────────

def ensure_llm_settlement_cleaner(
    config: dict[str, Any],
    csv_path: Path,
    column_signature: str,
) -> tuple[Path, dict]:
    """确保存在一个 LLM 生成的结算流水清洗脚本。

    如果 ``generated_parsers/osm/settlement_{sig}.py`` 已存在则直接返回，
    否则读取样本数据、调用 LLM 生成、校验并保存。

    Returns:
        (script_path, usage) — usage 为 dict(total_tokens, prompt_tokens, completion_tokens)。
        缓存命中时 usage 各字段均为 0。
    """
    _zero_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    script_dir = _parser_dir(config)
    script_path = script_dir / f"settlement_{column_signature}.py"
    force_regenerate = config.get("_force_regenerate", False)
    if not force_regenerate and script_path.exists():
        logger.info("Reusing cached settlement cleaner: %s", script_path.name)
        return script_path, _zero_usage

    llm_config = config.get("llm", {})
    if not llm_config.get("enabled", False):
        raise RuntimeError(
            f"需要 LLM 生成结算清洗脚本，但 llm.enabled=false。"
            f"请启用 LLM 或提供手动编写的清洗脚本。"
        )

    # Read sample rows
    sample_df = _read_csv_sample(csv_path, nrows=30)
    sample_rows = sample_df.fillna("").astype(str).to_dict(orient="records")

    # Read raw first rows (unprocessed) so LLM can see the actual CSV structure
    raw_first_rows = _read_csv_raw_rows(csv_path, nrows=5)

    schema_text = "\n".join(
        f"  - {f['name']}: {f['desc']}"
        + (f" (类型: {f.get('type', 'string')})" if f.get("type") else "")
        for f in _SETTLEMENT_SCHEMA
    )
    system_prompt = SETTLEMENT_SYSTEM_PROMPT.format(schema=schema_text)

    user_prompt = {
        "file_info": {
            "path": csv_path.name,
            "columns": list(sample_df.columns),
        },
        "raw_first_rows": raw_first_rows,
        "sample_rows": sample_rows,
        "requirements": [
            "使用 pandas 读取 CSV，依次尝试编码 utf-8, utf-8-sig, gbk, gb18030, latin-1",
            "根据样本数据的列名推断到标准字段的映射",
            "金额字段转为 float（去掉逗号和货币符号）",
            "month 和 source_file 由参数传入，直接写入每条记录",
            "缺失的标准字段填空字符串（金额填 0.0）",
            "自动识别并去除重复列名：df = df.loc[:, ~df.columns.duplicated(keep='first')]", 
        ],
    }
    extra = config.get("_user_requirement", "")
    if extra:
        user_prompt["requirements"].append(f"【用户额外要求】{extra}")

    return _generate_with_retry(
        config=config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        script_path=script_path,
    )


def ensure_llm_outbound_cleaner(
    config: dict[str, Any],
    xlsx_path: Path,
    sheet_name: str,
    sheet_type: str,
    column_signature: str,
) -> tuple[Path, dict]:
    """确保存在一个 LLM 生成的出库表清洗脚本。

    如果 ``generated_parsers/osm/outbound_{type}_{sig}.py`` 已存在则直接返回。

    Returns:
        (script_path, usage) — 缓存命中时 usage 各字段均为 0。
    """
    _zero_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    script_dir = _parser_dir(config)
    script_path = script_dir / f"outbound_{sheet_type}_{column_signature}.py"
    force_regenerate = config.get("_force_regenerate", False)
    if not force_regenerate and script_path.exists():
        logger.info("Reusing cached outbound cleaner: %s", script_path.name)
        return script_path, _zero_usage

    llm_config = config.get("llm", {})
    if not llm_config.get("enabled", False):
        raise RuntimeError(
            f"需要 LLM 生成出库清洗脚本，但 llm.enabled=false。"
            f"请启用 LLM 或提供手动编写的清洗脚本。"
        )

    # Read sample rows from sheet
    sample_df = _read_excel_sample(xlsx_path, sheet_name, nrows=30)
    sample_rows = sample_df.fillna("").astype(str).to_dict(orient="records")

    # Read raw first rows (unprocessed) so LLM can see summary rows, real header position, etc.
    raw_first_rows = _read_raw_rows(xlsx_path, sheet_name, nrows=5)

    # Build schema text for the specific sheet type (and "return" shares refund schema)
    effective_type = "refund" if sheet_type == "return" else sheet_type
    schema_fields = _OUTBOUND_SCHEMAS.get(effective_type, [])
    schema_text = "\n".join(
        f"  - {f['name']}: {f['desc']}"
        + (f" (类型: {f.get('type', 'string')})" if f.get("type") else "")
        for f in schema_fields
    )
    system_prompt = OUTBOUND_SYSTEM_PROMPT.format(schema=schema_text)

    user_prompt = {
        "file_info": {
            "path": xlsx_path.name,
            "sheet_name": sheet_name,
            "sheet_type": sheet_type,
            "columns": list(sample_df.columns),
        },
        "raw_first_rows": raw_first_rows,
        "sample_rows": sample_rows,
        "requirements": [
            "使用 pandas + openpyxl 读取指定工作表",
            "自动检测表头行（header=0，若多数列名异常则提升第一行为表头）",
            "根据样本数据的列名推断到标准字段的映射",
            "金额字段转为 float（去掉逗号和货币符号）",
            "source_file 和 month 由参数传入",
            "缺失的标准字段填空字符串（金额填 0.0）",
            "自动识别并去除重复列名：df = df.loc[:, ~df.columns.duplicated(keep='first')]",
        ],
    }
    extra = config.get("_user_requirement", "")
    if extra:
        user_prompt["requirements"].append(f"【用户额外要求】{extra}")

    return _generate_with_retry(
        config=config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        script_path=script_path,
    )


def load_and_run_cleaner(script_path: Path, *args: Any) -> list[dict]:
    """动态加载生成的清洗脚本并调用其 clean() 函数。"""
    module_name = script_path.stem
    spec = importlib.util.spec_from_file_location(
        f"generated_osm_{module_name}", str(script_path)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本模块: {script_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "clean"):
        raise RuntimeError(f"生成的脚本 {script_path.name} 中没有定义 clean() 函数")
    return mod.clean(*args)


def safe_run_cleaner(script_path: Path, *args: Any) -> list[dict]:
    """安全包装：运行清洗脚本后自动去除重复列名。

    LLM 生成的脚本可能在 rename 时将多个源列映射到同一目标名
    （如 '订单金额'→'amount' + '总金额'→'amount'），导致后续
    DataFrame 操作失败。此函数在运行后检测并去除重复列。
    """
    records = load_and_run_cleaner(script_path, *args)
    if not records:
        return records
    df = pd.DataFrame(records)
    if df.columns.duplicated().any():
        dup_cols = df.columns[df.columns.duplicated()].tolist()
        logger.warning(
            "脚本 %s 产生了重复列名 %s，自动去重（保留首个）",
            script_path.name, dup_cols,
        )
        df = df.loc[:, ~df.columns.duplicated(keep='first')]
        records = df.to_dict('records')
    return records


def patch_rename_dedup(code: str) -> str:
    """在生成的脚本中，找到 df.rename(...) 调用后注入去重行。

    LLM 生成的清洗脚本可能在 rename 时将多个源列映射到同一目标名
    （如 '订单金额'→'nsv' + 'NSV'→'nsv'），导致后续操作失败。
    此函数在 rename 调用之后插入一行：
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

    如果脚本中已经存在该去重行（在 rename 之后），则返回原始代码。
    """
    import re as _re

    dedup_line = "df = df.loc[:, ~df.columns.duplicated(keep='first')]"

    # Find all df.rename(...) call lines
    rename_pattern = _re.compile(
        r'^(\s*)(df\.rename\s*\(|df\s*=\s*df\.rename\s*\()', _re.MULTILINE
    )
    matches = list(rename_pattern.finditer(code))
    if not matches:
        return code

    # Use the last rename call
    last_match = matches[-1]
    indent = last_match.group(1)

    # Find the end of the rename statement (handle multi-line parenthesised calls)
    pos = last_match.end()
    paren_depth = code.count('(', last_match.start(), pos) - code.count(')', last_match.start(), pos)
    while paren_depth > 0 and pos < len(code):
        if code[pos] == '(':
            paren_depth += 1
        elif code[pos] == ')':
            paren_depth -= 1
        pos += 1

    # Find end of that line
    eol = code.find('\n', pos)
    if eol == -1:
        eol = len(code)

    # Check if dedup already exists shortly after rename (within ~300 chars)
    after_snippet = code[eol:eol + 300]
    if "duplicated(keep=" in after_snippet:
        return code  # Already has dedup after rename

    # Insert dedup line after the rename statement
    insert = f"\n{indent}{dedup_line}"
    return code[:eol] + insert + code[eol:]


def compute_column_signature(columns: list[str]) -> str:
    """计算列名列表的指纹（排序后取 md5 前 8 位）。"""
    key = "|".join(sorted(str(c).strip() for c in columns))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


# ── Internal helpers ────────────────────────────────────────────

def _parser_dir(config: dict[str, Any]) -> Path:
    """获取生成脚本的存储目录。"""
    from .config import output_dir
    out = output_dir(config)
    parser_dir = out / "generated_parsers" / "osm"
    parser_dir.mkdir(parents=True, exist_ok=True)
    return parser_dir


def _read_csv_sample(path: Path, nrows: int = 30) -> pd.DataFrame:
    """读取 CSV 文件的前 N 行样本（尝试多种编码）。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            df = pd.read_csv(str(path), encoding=enc, dtype=str, nrows=nrows)
            # Normalise column names for display
            df.columns = [str(c).strip() for c in df.columns]
            return df
        except (UnicodeDecodeError, UnicodeError):
            continue
    return pd.DataFrame()


def _read_csv_raw_rows(path: Path, nrows: int = 5) -> list[list[str]]:
    """读取 CSV 文件的前 N 行原始数据（无表头解析），用于传给 LLM 查看原始结构。"""
    import csv
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            rows = []
            with open(str(path), encoding=enc, newline="") as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    if i >= nrows:
                        break
                    rows.append([cell.strip() for cell in row])
            return rows
        except (UnicodeDecodeError, UnicodeError):
            continue
    return []


def _read_excel_sample(path: Path, sheet_name: str, nrows: int = 30) -> pd.DataFrame:
    """读取 Excel 工作表的前 N 行样本（自动检测表头行）。"""
    try:
        xl = pd.ExcelFile(str(path), engine="openpyxl")
        df = xl.parse(sheet_name, header=0, dtype=str, nrows=nrows + 1)
        xl.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    # Auto-detect header: if most column names look bad, promote row 0
    bad_count = sum(
        1 for c in df.columns
        if str(c).startswith("Unnamed") or _is_numeric_str(str(c))
    )
    if bad_count > len(df.columns) / 2 and len(df) > 0:
        new_header = df.iloc[0].tolist()
        df = df.iloc[1:].reset_index(drop=True)
        df.columns = [
            str(h).strip() if h is not None else f"col_{i}"
            for i, h in enumerate(new_header)
        ]

    df.columns = [str(c).strip() for c in df.columns]

    # Deduplicate columns (e.g. Excel 中有两个 '订单金额')
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

    return df.head(nrows)


def _read_raw_rows(path: Path, sheet_name: str, nrows: int = 5) -> list[list[str]]:
    """读取 Excel 工作表的前 N 行原始数据（不做任何表头检测处理）。

    返回 list[list[str]]，每行是一个字符串列表，NaN 转为空字符串。
    用于传给 LLM，让它看到工作表的真实结构（汇总行、空行、实际表头行等）。
    """
    try:
        xl = pd.ExcelFile(str(path), engine="openpyxl")
        df = xl.parse(sheet_name, header=None, dtype=str, nrows=nrows)
        xl.close()
    except Exception:
        return []

    if df.empty:
        return []

    return df.fillna("").values.tolist()


def _is_numeric_str(s: str) -> bool:
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _notify_retry(reason: str, attempt: int, max_retries: int, error: str) -> None:
    """发送重试 SSE 事件到前端。延迟导入以避免循环依赖。"""
    try:
        from desktop.common import notify_frontend
        notify_frontend("retry", {
            "reason": reason,
            "attempt": attempt,
            "max_retries": max_retries,
            "error": error[:200],
        })
    except (ImportError, Exception):
        pass  # 在 CLI 等非桌面环境运行时忽略


def _generate_with_retry(
    config: dict[str, Any],
    system_prompt: str,
    user_prompt: dict[str, Any],
    script_path: Path,
    max_retries: int = _MAX_RETRIES,
) -> tuple[Path, dict]:
    """调用 LLM 生成清洗脚本，支持最多 max_retries 次重试。

    每次重试会校验：
    1. ast.parse() 语法正确
    2. 包含 ``def clean(`` 函数定义

    校验失败时将错误信息反馈给 LLM 重新生成。

    Returns:
        (script_path, usage) — usage is a dict with total_tokens, prompt_tokens, completion_tokens.
    """
    current_prompt = user_prompt
    total_usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}

    for attempt in range(1, max_retries + 1):
        logger.info(
            "LLM generating cleaner %s (attempt %d/%d)",
            script_path.name, attempt, max_retries,
        )

        result_obj, raw_result = run_osm_cleaner_agent(config, current_prompt, system_prompt)
        # Extract token usage from the raw pydantic-ai RunResult
        usage = _extract_token_usage(raw_result)
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)

        code = result_obj.code if hasattr(result_obj, "code") else str(result_obj)
        code = strip_code_fence(code)

        # Validation 1: syntax check
        try:
            ast.parse(code)
        except SyntaxError as e:
            logger.warning("Syntax error in generated code (attempt %d): %s", attempt, e)
            if attempt < max_retries:
                _notify_retry("syntax", attempt, max_retries, str(e))
                current_prompt = _build_retry_prompt(
                    user_prompt, code, "语法错误", str(e)
                )
                continue
            raise RuntimeError(
                f"LLM 生成的代码存在语法错误，已重试 {max_retries} 次: {e}"
            ) from e

        # Validation 2: must contain clean() function
        if "def clean(" not in code:
            error_msg = "生成的代码中没有定义 def clean(...) 函数"
            logger.warning("%s (attempt %d)", error_msg, attempt)
            if attempt < max_retries:
                _notify_retry("missing_clean", attempt, max_retries, error_msg)
                current_prompt = _build_retry_prompt(
                    user_prompt, code, "缺少函数定义", error_msg
                )
                continue
            raise RuntimeError(
                f"LLM 生成的代码没有 clean() 函数，已重试 {max_retries} 次"
            )

        # Success — save to disk
        script_path.write_text(code, encoding="utf-8")
        logger.info("Generated cleaner saved: %s", script_path)
        return script_path, total_usage

    # Should not reach here, but just in case
    raise RuntimeError(f"LLM 清洗脚本生成失败，已重试 {max_retries} 次")


def _build_retry_prompt(
    original_prompt: dict[str, Any],
    generated_code: str,
    error_type: str,
    error_detail: str,
) -> dict[str, Any]:
    """构建包含错误反馈的重试 prompt。"""
    code_block = generated_code[:4000]
    if len(generated_code) > 4000:
        code_block += "\n... (truncated)"

    return {
        **original_prompt,
        "retry_info": {
            "error_type": error_type,
            "error_detail": error_detail,
            "previous_code": code_block,
            "instruction": (
                f"你上一次生成的 Python 代码存在{error_type}，请修复后重新输出完整代码。"
                f"直接输出纯 Python 代码，不要用 ``` 代码块包裹，不要任何额外解释。"
            ),
        },
    }
