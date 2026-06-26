from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from audit_workflow.llm_agent import resolve_provider
from .config import resolve_path, output_dir
from .llm_agent import run_bank_parser_agent, run_ledger_parser_agent
from .utils import read_excel_headerless, strip_code_fence

_log = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是审计数据清洗助手。你的任务是为银行流水 Excel 生成一个 Python 解析脚本。
脚本必须定义 parse(path: str, config: dict) -> list[dict]。
返回的每条记录必须包含标准银行流水字段：
txn_id, source_id, source_file, source_sheet, row_no, bank_name, account_no,
transaction_date, flow, amount, bank_debit, bank_credit, counterparty_name,
counterparty_account, summary, description, balance, raw_text。
flow 只能是 in 或 out。bank_debit 表示银行流水借方/资金流出，bank_credit 表示银行流水贷方/资金流入。
只输出 Python 代码，不要输出解释。"""

LEDGER_SYSTEM_PROMPT = """你是审计数据清洗助手。你的任务是为序时账（会计账簿/分录）Excel 生成一个 Python 解析脚本。
脚本必须定义 parse(path: str, config: dict) -> list[dict]。
返回的每条记录必须包含标准序时账字段：
entry_id, source_id, source_file, source_sheet, row_no, bank_name, account_no,
transaction_date, flow, amount, ledger_debit, ledger_credit, voucher_no,
voucher_type, summary, subject, counterparty_name, raw_text。
flow 只能是 in 或 out。ledger_debit 表示序时账借方金额，ledger_credit 表示序时账贷方金额。
flow 判断规则：如果只有借方金额则为 out（资金流出），如果只有贷方金额则为 in（资金流入）。
amount 取借方或贷方中非零的那个值。
只输出 Python 代码，不要输出解释。"""


def _parser_dir(config: dict[str, Any]) -> Path:
    """获取 BLM 生成脚本的存储目录（outputs/generated_parsers/blm/）。"""
    from .config import output_dir
    llm_config = config.get("llm", {})
    explicit_dir = llm_config.get("generated_parser_dir")
    if explicit_dir:
        parser_dir = resolve_path(config, explicit_dir)
    else:
        parser_dir = output_dir(config) / "generated_parsers" / "blm"
    assert parser_dir is not None
    parser_dir.mkdir(parents=True, exist_ok=True)
    return parser_dir


def _legacy_parser_dir(config: dict[str, Any]) -> Path | None:
    """返回旧缓存目录（项目根 generated_parsers/），向后兼容已有文件。"""
    legacy_dir = resolve_path(config, "generated_parsers")
    parser_dir = _parser_dir(config)
    if legacy_dir and legacy_dir != parser_dir:
        return legacy_dir
    return None


def ensure_llm_bank_parser(config: dict[str, Any], item: dict[str, Any]) -> Path:
    llm_config = config.get("llm", {})
    parser_dir = _parser_dir(config)

    # 旧缓存目录（项目根 generated_parsers/），向后兼容已有文件
    legacy_dir = _legacy_parser_dir(config)
    if legacy_dir:
        legacy_id = legacy_dir / f"{item['id']}.py"
        if legacy_id.exists():
            _log.info("BLM parser cache hit (legacy id): %s", legacy_id)
            return legacy_id

    # ── 缓存检查 1: 按 item ID（向后兼容已有文件）──
    parser_path = parser_dir / f"{item['id']}.py"
    if parser_path.exists():
        _log.info("BLM parser cache hit (by id): %s", parser_path.name)
        return parser_path

    if not llm_config.get("enabled", False):
        raise RuntimeError(
            f"{item['id']} 需要 LLM 生成解析器。请设置 llm.enabled=true，或手工提供 generated_parser。"
        )

    provider_cfg = resolve_provider(llm_config)
    if not provider_cfg.model_name:
        raise RuntimeError("请在 config 的 llm.providers 中配置 model，或在 .env 中设置对应环境变量。")

    source_path = resolve_path(config, item["path"])
    assert source_path is not None
    sheet, df = read_excel_headerless(source_path, item.get("sheet"))

    # ── 缓存检查 2: 按列签名（相同表头结构的文件复用已生成代码，避免重复调用 LLM）──
    col_sig = _compute_column_signature(df)
    sig_path = parser_dir / f"bank_{col_sig}.py"
    if sig_path.exists():
        _log.info(
            "BLM parser cache hit (by column signature %s): %s → reusing for %s",
            col_sig, sig_path.name, item["id"],
        )
        _notify("blm_clean", {
            "status": "cache_hit",
            "item_id": item["id"],
            "signature": col_sig,
            "message": f"表头结构与已生成脚本匹配（{col_sig}），跳过 LLM 调用",
        })
        return sig_path

    _notify("blm_clean", {
        "status": "generating",
        "item_id": item["id"],
        "bank_name": item.get("bank_name", ""),
        "message": f"正在为 {item['id']}（{item.get('bank_name', '')}）生成解析代码…",
    })

    sample = df.head(30).fillna("").astype(str).to_dict(orient="records")
    user_prompt = {
        "bank_input": {
            "id": item["id"],
            "path": source_path.name,
            "sheet": sheet,
            "bank_name": item.get("bank_name", ""),
            "account_no": item.get("account_no", ""),
        },
        "sample_rows": sample,
        "requirements": [
            "使用 pandas 读取 Excel，兼容 xls/xlsx/xlsm。",
            "金额字段要去掉逗号和货币符号。",
            "日期输出 ISO 格式 YYYY-MM-DD。",
            "无法识别的空值输出空字符串或 0。",
            "不要联网，不要读取 path 以外的文件。",
        ],
    }

    result, usage = run_bank_parser_agent(config, user_prompt, SYSTEM_PROMPT)
    code = result.code
    code = strip_code_fence(code)
    if "def parse(" not in code:
        raise RuntimeError("LLM 返回内容中没有 parse 函数，请检查模型输出。")

    # 保存到列签名路径（便于后续同结构文件复用）
    sig_path.write_text(code, encoding="utf-8")
    _log.info("BLM parser generated: %s (for %s)", sig_path.name, item["id"])

    _notify("blm_clean", {
        "status": "generated",
        "item_id": item["id"],
        "signature": col_sig,
        "message": f"已生成 {item['id']} 的解析代码",
    })
    _track_usage(usage)

    return sig_path


def ensure_llm_ledger_parser(config: dict[str, Any], item: dict[str, Any]) -> Path:
    """为序时账生成或缓存 LLM 解析器，返回 .py 文件路径。"""
    llm_config = config.get("llm", {})
    parser_dir = _parser_dir(config)

    # 旧缓存目录（项目根 generated_parsers/），向后兼容
    legacy_dir = _legacy_parser_dir(config)
    if legacy_dir:
        legacy_id = legacy_dir / f"{item['id']}.py"
        if legacy_id.exists():
            _log.info("Ledger parser cache hit (legacy id): %s", legacy_id)
            return legacy_id

    # ── 缓存检查 1: 按 item ID ──
    parser_path = parser_dir / f"{item['id']}.py"
    if parser_path.exists():
        _log.info("Ledger parser cache hit (by id): %s", parser_path.name)
        return parser_path

    if not llm_config.get("enabled", False):
        raise RuntimeError(
            f"{item['id']} 需要 LLM 生成序时账解析器。请设置 llm.enabled=true，或手工提供 generated_parser。"
        )

    provider_cfg = resolve_provider(llm_config)
    if not provider_cfg.model_name:
        raise RuntimeError("请在 config 的 llm.providers 中配置 model，或在 .env 中设置对应环境变量。")

    source_path = resolve_path(config, item["path"])
    assert source_path is not None
    sheet, df = read_excel_headerless(source_path, item.get("sheet"))

    # ── 缓存检查 2: 按列签名（ledger_ 前缀区分银行流水）──
    col_sig = _compute_column_signature(df)
    sig_path = parser_dir / f"ledger_{col_sig}.py"
    if sig_path.exists():
        _log.info(
            "Ledger parser cache hit (by column signature %s): %s → reusing for %s",
            col_sig, sig_path.name, item["id"],
        )
        _notify("blm_clean", {
            "type": "ledger",
            "status": "cache_hit",
            "item_id": item["id"],
            "signature": col_sig,
            "message": f"表头结构与已生成脚本匹配（{col_sig}），跳过 LLM 调用",
        })
        return sig_path

    _notify("blm_clean", {
        "type": "ledger",
        "status": "generating",
        "item_id": item["id"],
        "message": f"正在为 {item['id']}（序时账）生成解析代码…",
    })

    sample = df.head(30).fillna("").astype(str).to_dict(orient="records")
    user_prompt = {
        "ledger_input": {
            "id": item["id"],
            "path": source_path.name,
            "sheet": sheet,
            "bank_name": item.get("bank_name", ""),
            "account_no": item.get("account_no", ""),
        },
        "sample_rows": sample,
        "requirements": [
            "使用 pandas 读取 Excel，兼容 xls/xlsx/xlsm。",
            "金额字段要去掉逗号和货币符号。",
            "日期输出 ISO 格式 YYYY-MM-DD。",
            "借方金额填入 ledger_debit，贷方金额填入 ledger_credit。",
            "flow 根据借贷方向判断：仅借方为 out，仅贷方为 in。",
            "无法识别的空值输出空字符串或 0。",
            "不要联网，不要读取 path 以外的文件。",
        ],
    }

    result, usage = run_ledger_parser_agent(config, user_prompt, LEDGER_SYSTEM_PROMPT)
    code = result.code
    code = strip_code_fence(code)
    if "def parse(" not in code:
        raise RuntimeError("LLM 返回内容中没有 parse 函数，请检查模型输出。")

    sig_path.write_text(code, encoding="utf-8")
    _log.info("Ledger parser generated: %s (for %s)", sig_path.name, item["id"])

    _notify("blm_clean", {
        "type": "ledger",
        "status": "generated",
        "item_id": item["id"],
        "signature": col_sig,
        "message": f"已生成 {item['id']} 的序时账解析代码",
    })
    _track_usage(usage)

    return sig_path


def _compute_column_signature(df) -> str:
    """从 headerless DataFrame 的第一行提取列名，计算 md5 前 8 位签名。"""
    first_row = df.iloc[0] if len(df) > 0 else []
    cols = [str(c).strip() for c in first_row if str(c).strip()]
    key = "|".join(sorted(cols))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def _notify(event: str, data: dict) -> None:
    """向后端 SSE 队列发送事件（延迟导入避免循环依赖）。"""
    try:
        from desktop.common import notify_frontend
        notify_frontend(event, data)
    except (ImportError, RuntimeError):
        pass  # CLI 环境无 desktop 模块，静默忽略


def _track_usage(usage: dict | None) -> None:
    """将 token 用量记录到全局 TokenTracker。"""
    if not usage:
        return
    try:
        from desktop.common import token_tracker
        token_tracker.record(usage)
    except (ImportError, RuntimeError):
        pass
