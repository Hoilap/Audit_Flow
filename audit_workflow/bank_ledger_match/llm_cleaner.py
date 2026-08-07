from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import create_model, Field

from audit_workflow.llm_agent import llm_generate, llm_generate_structured, resolve_provider
from audit_workflow.util import build_smart_sample
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

多行表头（常见陷阱）：
  很多序时账有"分组表头 + 明细表头"两行，例如第 N 行只有分组名（"记账凭证"横跨"字/号"两列、
  "2022年"横跨"月/日"两列），第 N+1 行才是明细列名（月、日、字、号…）。
  此时必须选择列名更完整的一行（通常是明细表头行，即非空单元格更多的那行）作为 header=N。
  如果错选了靠前的分组表头行，明细表头行会混入数据，被输出成一条脏记录。
  同时在脚本循环中加一道保险过滤：如果某行借方/贷方都没有数值金额（均为空或 0），
  且日期列不是有效日期，则视其为残留表头行或空行，跳过不输出。

列索引映射：
  根据 sample_rows 中表头行的内容，确定每列的含义，然后在代码中硬编码列名或索引。
  例如：df["摘要"]、df["借方金额"]、df["贷方金额"]，或 df.iloc[:, 7] 等。

bank_name 与 account_no 填充规则（重要）：
  当无法从数据中准确确定银行名称或账号时，不要置空，应尽可能填入原始数据中的信息：
  1. 优先从科目列（"科目"、"明细科目"、"会计科目"等）中提取银行信息填入对应字段。
  2. 如果科目列包含完整科目路径（如"银行存款｜工行花城支行3359｜中国工商银行股份有限公司广州花城支行"），
     将该原始字符串原样填入 account_no 和 bank_name（两列都可以填该字符串，宁多勿缺）。
  3. 科目列没有银行信息时，使用 config["bank_name"] / config["account_no"] 作为兜底值。
  4. 这些字段后续会有专门的步骤做规范化（转纯数字账号、标准银行名称），此处保留原始信息即可，
     不要因为格式"不标准"而置空或截断。

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

    sample = build_smart_sample(df)
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

    sample = build_smart_sample(df)
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
            "多行表头（分组表头+明细表头）时，选列名更完整的一行作为 header=N；并在代码中跳过无数字金额且无有效日期的残留表头行/空行，不要输出为记录。",
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
            "无法准确确定 bank_name/account_no 时不要置空：将科目列中的原始银行信息填入（如'银行存款｜工行花城支行3359｜...'原样填入两列），宁多勿缺，后续会自动规范化。",
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


# ============================================================
# Phase 2: 列格式检查 + LLM 生成替换映射
# ============================================================

_NORMALIZE_SYSTEM_PROMPT = """\
你是审计数据清洗助手。下面给出了序时账清洗后某一列的唯一原始值列表、该列的预期格式，
以及 task.yaml 中定义的银行账号参考列表（银行名称 + 账号 + 工作表线索）。
请为每个需要修正的值生成一个 Python 字典映射：{原始值字符串: 规范化后的值字符串}。

规则：
1. bank_name 列：值应为银行正式名称（如"中国工商银行股份有限公司广州花城支行"），
   不是科目路径（如"银行存款｜工行花城支行3359｜..."）
2. account_no 列：值应为纯数字银行账号（如"6222020200123456789"），
   不是科目路径。可参考银行账号参考列表中的账号（根据支行名、账号后四位等线索匹配）
3. 如果原始值本身就是规范格式，直接保留原值作为映射值
4. 如果无法规范化某个值，保留原值作为映射值（即不做替换）
5. 只输出 JSON 格式的映射字典，不要输出其他内容"""


def normalize_ledger_columns(cfg: dict[str, Any], ledger_csv) -> dict | None:
    """Phase 2: 对清洗后的序时账 CSV 进行列格式检查和规范化。

    清洗阶段（Phase 1）可能输出格式不规范的数据（如 account_no 包含科目字符串
    "银行存款｜工行花城支行3359｜中国工商银行股份有限公司广州花城支行"）。
    此函数对每一列做格式检查，发现问题的列提取唯一值送给 LLM，
    让 LLM 生成替换映射（Python dict），然后运行替换。

    Args:
        cfg: 完整的 pipeline 配置（含 llm）
        ledger_csv: 清洗后的 ledger CSV 路径

    Returns:
        规范化结果字典，或 None（CSV 不存在/无需规范化时）
    """
    csv_path = Path(str(ledger_csv))
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")

    # ── 定义列格式检查规则 ──
    def _is_bad_account_no(val: str) -> bool:
        """account_no 应为纯数字，包含非数字字符（除空格外）则不规范"""
        if not val:
            return False
        return not val.replace(" ", "").isdigit()

    def _is_bad_bank_name(val: str) -> bool:
        """bank_name 不应包含 '｜'、'|'、'银行存款' 等科目路径特征"""
        if not val:
            return False
        return ("｜" in val or "|" in val or "银行存款" in val)

    column_checkers = {
        "account_no": {
            "check": _is_bad_account_no,
            "description": "银行账号，应为纯数字",
        },
        "bank_name": {
            "check": _is_bad_bank_name,
            "description": "银行名称，应为银行正式名称，不含科目路径",
        },
    }

    # ── 检查每列，收集需要规范化的列及其唯一不规范值 ──
    columns_to_fix: dict[str, list[str]] = {}
    for col, rule in column_checkers.items():
        if col not in df.columns:
            continue
        bad_values = [
            v for v in df[col].unique()
            if v and v.strip() and rule["check"](v)
        ]
        if bad_values:
            columns_to_fix[col] = bad_values

    if not columns_to_fix:
        return {
            "normalized_columns": 0,
            "total_columns_checked": len(column_checkers),
            "replacements": 0,
            "details": [],
            "skipped": True,
            "reason": "所有列格式正确，无需规范化",
        }

    # ── 收集 task.yaml 中的银行账号信息作为 LLM 参考 ──
    bank_reference: list[dict] = []
    seen_ref: set[str] = set()
    for bs in cfg.get("inputs", {}).get("bank_statements", []):
        acct = bs.get("account_no", "")
        if acct and acct not in seen_ref:
            seen_ref.add(acct)
            bank_reference.append({
                "account_no": acct,
                "bank_name": bs.get("bank_name", ""),
                "sheet": bs.get("sheet", ""),
            })

    # ── 对每个需要修正的列，每 10 个唯一值调用一次 LLM 生成替换映射 ──
    _MappingOutput = create_model(
        "ColumnMappingOutput",
        mapping=(dict[str, str], Field(description="映射字典：{原始值: 规范化值}")),
    )

    BATCH_SIZE = 10
    all_mappings: dict[str, dict[str, str]] = {}

    for col, bad_values in columns_to_fix.items():
        rule = column_checkers[col]
        col_mapping: dict[str, str] = {}
        total_calls = 0

        for i in range(0, len(bad_values), BATCH_SIZE):
            batch = bad_values[i:i + BATCH_SIZE]
            prompt = (
                f"列名: {col}\n"
                f"预期格式: {rule['description']}\n"
                f"task.yaml 银行账号参考列表（共 {len(bank_reference)} 个）:\n"
                + json.dumps(bank_reference, ensure_ascii=False)
                + f"\n唯一原始值（批次 {i // BATCH_SIZE + 1}，共 {len(batch)} 个）:\n"
                + json.dumps(batch, ensure_ascii=False)
            )

            try:
                llm_config = cfg.get("llm", {})
                result, _usage = llm_generate_structured(
                    llm_config,
                    prompt,
                    _MappingOutput,
                    _NORMALIZE_SYSTEM_PROMPT,
                )
                total_calls += 1
                if result and result.mapping:
                    col_mapping.update(result.mapping)
            except Exception as e:
                _log.error("列 %s 批次 %d 的 LLM 规范化失败: %s",
                           col, i // BATCH_SIZE + 1, e, exc_info=True)
                continue

        if col_mapping:
            all_mappings[col] = col_mapping
            _log.info("列 %s: %d 次 LLM 调用，共 %d 条映射",
                       col, total_calls, len(col_mapping))

    if not all_mappings:
        return {
            "normalized_columns": 0,
            "total_columns_checked": len(column_checkers),
            "replacements": 0,
            "details": [
                {"column": col, "bad_count": len(vals), "mapped": 0,
                 "reason": "LLM 未返回映射"}
                for col, vals in columns_to_fix.items()
            ],
            "skipped": False,
            "reason": "LLM 未返回任何映射结果",
        }

    # ── 运行替换 ──
    total_replaced = 0
    details = []
    for col, mapping in all_mappings.items():
        replaced_count = 0
        for old_val, new_val in mapping.items():
            if old_val == new_val:
                continue
            mask = df[col] == old_val
            cnt = int(mask.sum())
            if cnt > 0:
                df.loc[mask, col] = new_val
                replaced_count += cnt
        total_replaced += replaced_count
        details.append({
            "column": col,
            "bad_count": len(columns_to_fix.get(col, [])),
            "mapped": len(mapping),
            "replaced_rows": replaced_count,
        })

    if total_replaced > 0:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        _log.info("列规范化完成: %d 行已更新, 涉及 %d 列",
                   total_replaced, len(all_mappings))

    return {
        "normalized_columns": len(all_mappings),
        "total_columns_checked": len(column_checkers),
        "replacements": total_replaced,
        "details": details,
        "skipped": False,
        "reason": f"规范化了 {len(all_mappings)} 列，替换了 {total_replaced} 行",
    }
