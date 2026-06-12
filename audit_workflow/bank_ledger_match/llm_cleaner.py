from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import resolve_path
from .llm_agent import run_bank_parser_agent
from .utils import read_excel_headerless


SYSTEM_PROMPT = """你是审计数据清洗助手。你的任务是为银行流水 Excel 生成一个 Python 解析脚本。
脚本必须定义 parse(path: str, config: dict) -> list[dict]。
返回的每条记录必须包含标准银行流水字段：
txn_id, source_id, source_file, source_sheet, row_no, bank_name, account_no,
transaction_date, flow, amount, bank_debit, bank_credit, counterparty_name,
counterparty_account, summary, description, balance, raw_text。
flow 只能是 in 或 out。bank_debit 表示银行流水借方/资金流出，bank_credit 表示银行流水贷方/资金流入。
只输出 Python 代码，不要输出解释。"""


def ensure_llm_bank_parser(config: dict[str, Any], item: dict[str, Any]) -> Path:
    llm_config = config.get("llm", {})
    parser_dir = resolve_path(config, llm_config.get("generated_parser_dir", "generated_parsers"))
    assert parser_dir is not None
    parser_dir.mkdir(parents=True, exist_ok=True)
    parser_path = parser_dir / f"{item['id']}.py"
    if parser_path.exists():
        return parser_path

    if not llm_config.get("enabled", False):
        raise RuntimeError(
            f"{item['id']} 需要 LLM 生成解析器。请设置 llm.enabled=true，或手工提供 generated_parser。"
        )

    model = llm_config.get("model") or os.getenv("OPENAI_MODEL")
    if not model:
        raise RuntimeError("请在 config 的 llm.model 或 .env 的 OPENAI_MODEL 中填写模型名。")

    source_path = resolve_path(config, item["path"])
    assert source_path is not None
    sheet, df = read_excel_headerless(source_path, item.get("sheet"))
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

    result = run_bank_parser_agent(config, user_prompt, SYSTEM_PROMPT)
    code = result.code
    code = _strip_code_fence(code)
    if "def parse(" not in code:
        raise RuntimeError("LLM 返回内容中没有 parse 函数，请检查模型输出。")
    parser_path.write_text(code, encoding="utf-8")
    return parser_path


def _strip_code_fence(value: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", value, flags=re.S)
    return match.group(1).strip() if match else value.strip()
