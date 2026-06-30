from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from audit_workflow.llm_agent import llm_generate, resolve_provider
from .config import resolve_path, output_dir
from .llm_agent import run_bank_parser_agent, run_ledger_parser_agent
from .utils import read_excel_headerless, strip_code_fence

import re as _re

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 生成代码自动修补：将 bank_input/ledger_input 嵌套访问改为扁平 config
# ──────────────────────────────────────────────────────────────────

_NESTED_KEYS = ("bank_input", "ledger_input")


def _has_nested_config(code: str) -> bool:
    """检查生成的代码是否仍使用 bank_input / ledger_input 嵌套模式。

    只检查实际代码行，忽略注释和 docstring 中的提及。
    """
    import tokenize, io
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except tokenize.TokenError:
        # 如果 tokenize 失败（不完整代码），回退到简单字符串检查
        return any(k in code for k in _NESTED_KEYS)
    # 只检查 NAME 和 STRING token（排除 COMMENT 和 docstring）
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string in _NESTED_KEYS:
            return True
    return False


def _patch_nested_config(code: str) -> str:
    """自动修补 LLM 生成的代码中 bank_input/ledger_input 嵌套访问。

    处理两种常见模式：
    1. 单行链式: config.get('bank_input', {}).get('sheet', X)  →  config.get('sheet', X)
    2. 两步式:
         bank_input = config.get('bank_input', {})
         xxx = bank_input.get('sheet', X)  →  xxx = config.get('sheet', X)
    """
    for nested_key in _NESTED_KEYS:
        # 模式 1: 链式 config.get('bank_input', {}).get('key', default)
        code = _re.sub(
            _re.escape(f"config.get('{nested_key}', " + "{}" + f").get(")
            + r"""(['"])(\w+)\1,\s*([^)]+)\)""",
            r"config.get(\1\2\1, \3)",
            code,
        )
        code = _re.sub(
            _re.escape(f'config.get("{nested_key}", ' + "{}" + f").get(")
            + r"""(['"])(\w+)\1,\s*([^)]+)\)""",
            r"config.get(\1\2\1, \3)",
            code,
        )

        # 模式 2: 两步式
        # Step A: 找到中间变量  var_name = config.get('bank_input', {})
        var_pattern = _re.compile(
            r"(\w+)\s*=\s*config\.get\(['\"]" + _re.escape(nested_key) + r"['\"],\s*\{\}\)"
        )
        match = var_pattern.search(code)
        if match:
            var_name = match.group(1)
            # Step B: var.get('key', default) → config.get('key', default)
            code = _re.sub(
                _re.escape(var_name) + r"""\.get\((['"])(\w+)\1,\s*([^)]+)\)""",
                r"config.get(\1\2\1, \3)",
                code,
            )
            code = _re.sub(
                _re.escape(var_name) + r"""\.get\((['"])(\w+)\1\)""",
                r"config.get(\1\2\1)",
                code,
            )
            # Step C: 删除中间变量赋值行
            code = var_pattern.sub("", code)

        # 清理可能产生的空行
        code = _re.sub(r"\n{3,}", "\n\n", code)

    return code


SYSTEM_PROMPT = """你是审计数据清洗助手。你的任务是为银行流水 Excel 生成一个 Python 解析脚本。
脚本必须定义 parse(path: str, config: dict) -> list[dict]。
返回的每条记录必须包含标准银行流水字段：
txn_id, source_id, source_file, source_sheet, row_no, bank_name, account_no,
transaction_date, flow, amount, bank_debit, bank_credit, counterparty_name,
counterparty_account, summary, description, balance, raw_text。
flow 只能是 in 或 out。bank_debit 表示银行流水借方/资金流出，bank_credit 表示银行流水贷方/资金流入。
amount、bank_debit、bank_credit 必须始终为非负数。资金流向由 flow 字段标记，不要用负号表示方向。
如果原始数据中借方或贷方列出现负数，不要取绝对值，而是将该字段输出为 0（表示异常值），后续数据质量检查会捕获并报告。
例如：支出 500 元应记录为 flow="out", amount=500, bank_debit=500，而不是 amount=-500。
如果某行的借方列值为 -500（负数），则 bank_debit=0, amount=0，不要取绝对值。

重要：读取 Excel 后，单元格值可能是 float(NaN)。对任何单元格值使用 in 操作符之前，必须先用 str() 转换，
例如：if '日期' in str(val) 而不是 if '日期' in val，否则会触发 TypeError: argument of type 'float' is not iterable。
同理，所有字符串方法（如 .split()、.strip()、.lower()）调用前也要先 str() 转换并用 pd.notna() 判空。

表头行定位（关键）：
  sample_rows 是从原始 Excel 按行提取的样本（行号从 0 开始）。
  请你从 sample_rows 中直接判断表头行（包含"日期""摘要""金额"等列名的行）是第几行，
  然后在 pd.read_excel() 中直接写死 header=N（N 是表头行号，0-based）。
  例如：如果 sample_rows[2] 那一行包含列名，则 header=2。
  这样 pandas 会自动用该行作为列名，df 里只包含数据行，无需在代码中写运行时查找表头的逻辑。

列索引映射：
  根据 sample_rows 中表头行的内容，确定每列的含义，然后在代码中硬编码列名或索引。
  例如：df["日期"]、df["收入"]、df["支出"]，或 df.iloc[:, 1] 等。

config 参数是扁平字典，字段直接在顶层，没有嵌套：
  config["sheet"]      — 建议的工作表名称或索引（可能是字符串、整数 0、或空字符串）
  config["bank_name"]  — 银行名称
  config["account_no"] — 银行账号
  config["id"]         — 数据源 ID
注意：config 中没有 "bank_input" 这样的嵌套键，请直接从 config 顶层读取。

工作表选择：
  sheet_hint = config.get("sheet", "")
  读取方式：pd.read_excel(path, sheet_name=sheet_hint if sheet_hint else 0, header=N, engine=...)
  其中 N 是你从 sample_rows 判断出的表头行号。

只输出 Python 代码，不要输出解释。"""

LEDGER_SYSTEM_PROMPT = """你是审计数据清洗助手。你的任务是为序时账（会计账簿/分录）Excel 生成一个 Python 解析脚本。
脚本必须定义 parse(path: str, config: dict) -> list[dict]。
返回的每条记录必须包含标准序时账字段：
entry_id, source_id, source_file, source_sheet, row_no, bank_name, account_no,
transaction_date, flow, amount, ledger_debit, ledger_credit, voucher_no,
voucher_type, summary, subject, counterparty_name, raw_text。
flow 只能是 in 或 out。ledger_debit 表示序时账借方金额，ledger_credit 表示序时账贷方金额。
flow 判断规则：如果只有借方金额则为 in（资金流入，银行存款增加），如果只有贷方金额则为 out（资金流出，银行存款减少）。
amount 取借方或贷方中非零的那个值。
amount、ledger_debit、ledger_credit 必须始终为非负数。资金流向由 flow 字段标记，不要用负号表示方向。
如果原始数据中借方或贷方列出现负数，不要取绝对值，而是将该字段输出为 0（表示异常值），后续数据质量检查会捕获并报告。
例如：某行贷方列为 -10000（负数），则 ledger_credit=0, amount=0，不要取绝对值。

重要：读取 Excel 后，单元格值可能是 float(NaN)。对任何单元格值使用 in 操作符之前，必须先用 str() 转换，
例如：if '摘要' in str(val) 而不是 if '摘要' in val，否则会触发 TypeError: argument of type 'float' is not iterable。

表头行定位（关键）：
  sample_rows 是从原始 Excel 按行提取的样本（行号从 0 开始）。
  请你从 sample_rows 中直接判断表头行（包含"凭证""摘要""借方""贷方"等列名的行）是第几行，
  然后在 pd.read_excel() 中直接写死 header=N（N 是表头行号，0-based）。
  例如：如果 sample_rows[1] 那一行包含列名，则 header=1。
  这样 pandas 会自动用该行作为列名，df 里只包含数据行，无需在代码中写运行时查找表头的逻辑。

列索引映射：
  根据 sample_rows 中表头行的内容，确定每列的含义，然后在代码中硬编码列名或索引。
  例如：df["摘要"]、df["借方金额"]、df["贷方金额"]，或 df.iloc[:, 7] 等。

config 参数是扁平字典，字段直接在顶层，没有嵌套：
  config["sheet"]      — 建议的工作表名称或索引（可能是字符串、整数 0、或空字符串）
  config["bank_name"]  — 银行名称
  config["account_no"] — 银行账号
  config["id"]         — 数据源 ID
注意：config 中没有 "ledger_input" 这样的嵌套键，请直接从 config 顶层读取。

工作表选择：
  sheet_hint = config.get("sheet", "")
  读取方式：pd.read_excel(path, sheet_name=sheet_hint if sheet_hint else 0, header=N, engine=...)
  其中 N 是你从 sample_rows 判断出的表头行号。

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
    force_regenerate = config.get("_force_regenerate", False)

    if not force_regenerate:
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
    if not force_regenerate and sig_path.exists():
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

    sample = _build_smart_sample(df)
    user_prompt = {
        "id": item["id"],
        "path": source_path.name,
        "sheet": sheet,
        "bank_name": item.get("bank_name", ""),
        "account_no": item.get("account_no", ""),
        "sample_rows": sample,
        "requirements": [
            "使用 pandas 读取 Excel。必须根据文件扩展名选择引擎：.xls 用 engine='xlrd'，.xlsx/.xlsm 用 engine='openpyxl'。不要写死 engine='openpyxl'，.xls 文件用 openpyxl 会报 'File contains no valid workbook part'。",
            "从 sample_rows 直接判断表头行号，在 pd.read_excel() 中写死 header=N，不要在代码中运行时查找表头。",
            "金额字段要去掉逗号和货币符号。不要用正负号表示方向。遇到负数金额输出 0，不要取绝对值。",
            "日期输出 ISO 格式 YYYY-MM-DD。",
            "无法识别的空值输出空字符串或 0。",
            "不要联网，不要读取 path 以外的文件。",
            "config 是扁平字典，直接从顶层读取 sheet/bank_name 等字段，不要使用 bank_input 嵌套键。",
            "使用 .join() 拼接时务必先用 str() 转换每个元素，避免 TypeError。",
            "对单元格值使用 in 操作符前必须先用 str() 转换（如 '日期' in str(val)），避免 float(NaN) 导致 TypeError。",
        ],
    }
    extra = config.get("_user_requirement", "")
    if extra:
        user_prompt["requirements"].append(f"【用户额外要求】{extra}")

    result, usage = run_bank_parser_agent(config, user_prompt, SYSTEM_PROMPT)
    code = result.code
    code = strip_code_fence(code)

    # 自动修补嵌套 config 模式 + 重试
    max_attempts = 2
    for attempt in range(max_attempts):
        if "def parse(" not in code:
            raise RuntimeError("LLM 返回内容中没有 parse 函数，请检查模型输出。")
        patched = _patch_nested_config(code)
        if not _has_nested_config(patched):
            if patched != code:
                _log.info("BLM parser auto-patched nested config for %s", item["id"])
            code = patched
            break
        if attempt < max_attempts - 1:
            _log.warning(
                "BLM parser still has nested config after patch (attempt %d/%d) for %s, retrying",
                attempt + 1, max_attempts, item["id"],
            )
            retry_prompt = {**user_prompt, "requirements": [
                *user_prompt["requirements"],
                "【重要】绝对不要使用 bank_input 变量或 config.get('bank_input') 嵌套访问。"
                "必须直接用 config.get('sheet')、config.get('bank_name') 等顶层键。",
            ]}
            result, usage = run_bank_parser_agent(config, retry_prompt, SYSTEM_PROMPT)
            code = strip_code_fence(result.code)
        else:
            _log.warning(
                "BLM parser auto-patch could not fully fix nested config for %s, "
                "proceeding with patched code",
                item["id"],
            )
            code = patched

    # ── 执行测试：在真实文件上试跑，失败则让 LLM 分析错误后重新生成 ──
    total_usage = {k: usage.get(k, 0) for k in ("total_tokens", "prompt_tokens", "completion_tokens")}
    sig_path.write_text(code, encoding="utf-8")
    try:
        _test_generated_parser(sig_path, source_path, config, item)
    except Exception as test_exc:
        _log.warning(
            "BLM parser execution test failed for %s: %s: %s",
            item["id"], type(test_exc).__name__, test_exc,
        )
        _notify("blm_clean", {
            "status": "retry",
            "item_id": item["id"],
            "message": f"解析器执行出错（{type(test_exc).__name__}），正在分析错误并重新生成…",
        })
        fix_hint = _analyze_execution_error(
            config, code, type(test_exc).__name__, str(test_exc),
        )
        _log.info("LLM fix hint for %s: %s", item["id"], fix_hint)
        _notify("blm_clean", {
            "status": "retry",
            "item_id": item["id"],
            "message": f"建议引入新prompt:{fix_hint}",
        })
        # retry_prompt = {**user_prompt, "requirements": [
        #     *user_prompt["requirements"],
        #     f"【上次执行错误修复要求】{fix_hint}",
        # ]}
        # result2, usage2 = run_bank_parser_agent(config, retry_prompt, SYSTEM_PROMPT)
        # for k in total_usage:
        #     total_usage[k] += usage2.get(k, 0)
        # code = strip_code_fence(result2.code)
        # patched = _patch_nested_config(code)
        # if not _has_nested_config(patched):
        #     code = patched
        # sig_path.write_text(code, encoding="utf-8")
        # try:
        #     _test_generated_parser(sig_path, source_path, config, item)
        # except Exception as retry_exc:
        #     raise RuntimeError(
        #         f"BLM 解析器重试后仍失败 ({item['id']})\n"
        #         f"  首次错误: {type(test_exc).__name__}: {test_exc}\n"
        #         f"  LLM 修复建议: {fix_hint}\n"
        #         f"  重试错误: {type(retry_exc).__name__}: {retry_exc}\n"
        #         f"  解析器路径: {sig_path}"
        #     ) from retry_exc
        # _log.info("BLM parser retry succeeded for %s (fix: %s)", item["id"], fix_hint)

    _log.info("BLM parser generated: %s (for %s)", sig_path.name, item["id"])

    _notify("blm_clean", {
        "status": "generated",
        "item_id": item["id"],
        "signature": col_sig,
        "message": f"已生成 {item['id']} 的解析代码",
    })
    _track_usage(total_usage)

    return sig_path


def ensure_llm_ledger_parser(config: dict[str, Any], item: dict[str, Any]) -> Path:
    """为序时账生成或缓存 LLM 解析器，返回 .py 文件路径。"""
    llm_config = config.get("llm", {})
    parser_dir = _parser_dir(config)
    force_regenerate = config.get("_force_regenerate", False)

    if not force_regenerate:
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
    if not force_regenerate and sig_path.exists():
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

    sample = _build_smart_sample(df)
    user_prompt = {
        "id": item["id"],
        "path": source_path.name,
        "sheet": sheet,
        "bank_name": item.get("bank_name", ""),
        "account_no": item.get("account_no", ""),
        "sample_rows": sample,
        "requirements": [
            "使用 pandas 读取 Excel。必须根据文件扩展名选择引擎：.xls 用 engine='xlrd'，.xlsx/.xlsm 用 engine='openpyxl'。不要写死 engine='openpyxl'，.xls 文件用 openpyxl 会报 'File contains no valid workbook part'。",
            "从 sample_rows 直接判断表头行号，在 pd.read_excel() 中写死 header=N，不要在代码中运行时查找表头。",
            "金额字段要去掉逗号和货币符号，且必须为非负数。遇到负数金额输出 0，不要取绝对值。不要用负号表示方向。",
            "日期输出 ISO 格式 YYYY-MM-DD。",
            "借方金额填入 ledger_debit，贷方金额填入 ledger_credit。",
            "flow 根据借贷方向判断：仅借方为 in（银行存款增加），仅贷方为 out（银行存款减少）。",
            "无法识别的空值输出空字符串或 0。",
            "不要联网，不要读取 path 以外的文件。",
            "config 是扁平字典，直接从顶层读取 sheet/bank_name 等字段，不要使用 ledger_input 嵌套键。",
            "使用 .join() 拼接时务必先用 str() 转换每个元素，避免 TypeError。",
            "对单元格值使用 in 操作符前必须先用 str() 转换（如 '日期' in str(val)），避免 float(NaN) 导致 TypeError。",
            "有些数据没有属性行，请根据具体数据推断",
        ],
    }
    extra = config.get("_user_requirement", "")
    if extra:
        user_prompt["requirements"].append(f"【用户额外要求】{extra}")

    result, usage = run_ledger_parser_agent(config, user_prompt, LEDGER_SYSTEM_PROMPT)
    code = result.code
    code = strip_code_fence(code)

    # 自动修补嵌套 config 模式 + 重试
    max_attempts = 2
    for attempt in range(max_attempts):
        if "def parse(" not in code:
            raise RuntimeError("LLM 返回内容中没有 parse 函数，请检查模型输出。")
        patched = _patch_nested_config(code)
        if not _has_nested_config(patched):
            if patched != code:
                _log.info("Ledger parser auto-patched nested config for %s", item["id"])
            code = patched
            break
        if attempt < max_attempts - 1:
            _log.warning(
                "Ledger parser still has nested config after patch (attempt %d/%d) for %s, retrying",
                attempt + 1, max_attempts, item["id"],
            )
            retry_prompt = {**user_prompt, "requirements": [
                *user_prompt["requirements"],
                "【重要】绝对不要使用 ledger_input 变量或 config.get('ledger_input') 嵌套访问。"
                "必须直接用 config.get('sheet')、config.get('bank_name') 等顶层键。",
            ]}
            result, usage = run_ledger_parser_agent(config, retry_prompt, LEDGER_SYSTEM_PROMPT)
            code = strip_code_fence(result.code)
        else:
            _log.warning(
                "Ledger parser auto-patch could not fully fix nested config for %s, "
                "proceeding with patched code",
                item["id"],
            )
            code = patched

    # ── 执行测试：在真实文件上试跑，失败则让 LLM 分析错误后重新生成 ──
    total_usage = {k: usage.get(k, 0) for k in ("total_tokens", "prompt_tokens", "completion_tokens")}
    sig_path.write_text(code, encoding="utf-8")
    try:
        _test_generated_parser(sig_path, source_path, config, item)
    except Exception as test_exc:
        _log.warning(
            "Ledger parser execution test failed for %s: %s: %s",
            item["id"], type(test_exc).__name__, test_exc,
        )
        _notify("blm_clean", {
            "type": "ledger",
            "status": "retry",
            "item_id": item["id"],
            "message": f"序时账解析器执行出错（{type(test_exc).__name__}），正在分析错误并重新生成…",
        })
        fix_hint = _analyze_execution_error(
            config, code, type(test_exc).__name__, str(test_exc),
        )
        _log.info("LLM fix hint for ledger %s: %s", item["id"], fix_hint)
        retry_prompt = {**user_prompt, "requirements": [
            *user_prompt["requirements"],
            f"【上次执行错误修复要求】{fix_hint}",
        ]}
        result2, usage2 = run_ledger_parser_agent(config, retry_prompt, LEDGER_SYSTEM_PROMPT)
        for k in total_usage:
            total_usage[k] += usage2.get(k, 0)
        code = strip_code_fence(result2.code)
        patched = _patch_nested_config(code)
        if not _has_nested_config(patched):
            code = patched
        sig_path.write_text(code, encoding="utf-8")
        try:
            _test_generated_parser(sig_path, source_path, config, item)
        except Exception as retry_exc:
            raise RuntimeError(
                f"序时账解析器重试后仍失败 ({item['id']})\n"
                f"  首次错误: {type(test_exc).__name__}: {test_exc}\n"
                f"  LLM 修复建议: {fix_hint}\n"
                f"  重试错误: {type(retry_exc).__name__}: {retry_exc}\n"
                f"  解析器路径: {sig_path}"
            ) from retry_exc
        _log.info("Ledger parser retry succeeded for %s (fix: %s)", item["id"], fix_hint)

    _log.info("Ledger parser generated: %s (for %s)", sig_path.name, item["id"])

    _notify("blm_clean", {
        "type": "ledger",
        "status": "generated",
        "item_id": item["id"],
        "signature": col_sig,
        "message": f"已生成 {item['id']} 的序时账解析代码",
    })
    _track_usage(total_usage)

    return sig_path


def _build_smart_sample(df, *, header_min_cells: int = 8, data_rows: int = 8) -> list[dict]:
    """构建发送给 LLM 的样本数据。

    策略：先找到表头行（第一个拥有 >=header_min_cells 个非空值的行），
    然后取从文件开头到表头行 + 其后 data_rows 行数据行作为样本。
    这样 LLM 能看到标题/元信息行 + 列名表头 + 真实数据，理解完整的文件结构。

    如果找不到表头行，回退到前 30 行。
    """
    header_idx = None
    for i in range(min(20, len(df))):
        non_empty = sum(1 for v in df.iloc[i] if pd.notna(v) and str(v).strip())
        if non_empty >= header_min_cells:
            header_idx = i
            break

    if header_idx is not None:
        end = min(header_idx + 1 + data_rows, len(df))
        sample_df = df.iloc[:end]
    else:
        sample_df = df.head(30)

    # 逐列 fillna 避免 pandas FutureWarning（object dtype downcast）
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", FutureWarning)
        sample_df = sample_df.fillna("")
    return sample_df.astype(str).to_dict(orient="records")


def _compute_column_signature(df) -> str:
    """从 headerless DataFrame 的第一个数据行提取签名（跳过标题行）。

    标题行（如 "银行存款明细账"）在不同格式的文件中可能完全相同，
    导致结构完全不同的文件共享同一个缓存签名。改为寻找第一个
    包含 >=4 个非空值的数据行来区分列结构。
    """
    data_row = None
    for idx in range(len(df)):
        row = df.iloc[idx]
        non_nan = row.dropna()
        if len(non_nan) >= 4:
            data_row = non_nan
            break
    if data_row is None:
        # fallback: 所有行都稀疏，仍用第一行
        data_row = df.iloc[0].dropna() if len(df) > 0 else []
    cols = [str(c).strip() for c in data_row if str(c).strip()]
    key = "|".join(sorted(cols))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


# ──────────────────────────────────────────────────────────────────
# 生成代码执行验证 + LLM 错误分析重试
# ──────────────────────────────────────────────────────────────────

_ERROR_ANALYZER_SYSTEM_PROMPT = (
    "你是一个 Python 代码调试助手。下面是一段 LLM 生成的 Excel 解析代码以及它在实际运行时遇到的错误。"
    "请分析错误原因，输出一条简短的中文修复要求（一句话），作为下次重新生成时的附加 requirement。"
    "只输出这一条要求文本本身，不要编号、不要加引号、不要任何解释。"
)


def _analyze_execution_error(
    config: dict[str, Any], code: str, error_type: str, error_detail: str,
) -> str:
    """将生成的代码和运行时错误发给 LLM，返回一条修复要求文本。"""
    prompt = (
        f"错误类型: {error_type}\n"
        f"错误详情: {error_detail}\n\n"
        f"生成的代码:\n```python\n{code[:4000]}\n```\n\n"
        f"请输出一条修复要求："
    )
    llm_config = config.get("llm", {})
    try:
        text, _ = llm_generate(llm_config, prompt, _ERROR_ANALYZER_SYSTEM_PROMPT)
        return text.strip().strip('"').strip("'")
    except Exception as exc:
        _log.warning("LLM error analysis failed: %s", exc)
        return f"修复 {error_type}: {error_detail}"


def _test_generated_parser(
    parser_path: Path, source_path: Path, config: dict[str, Any], item: dict[str, Any],
) -> None:
    """加载生成的解析器并在实际文件上执行 parse()。成功返回 None，失败抛异常。"""
    spec = importlib.util.spec_from_file_location("_test_parser", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载解析器进行测试：{parser_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    test_config = {**item}
    if not test_config.get("sheet"):
        test_config["sheet"] = 0
    mod.parse(str(source_path), test_config)


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
