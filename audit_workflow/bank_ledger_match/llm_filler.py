"""LLM-based 工作底稿填表模块。

让 LLM 生成 Python 代码，用 openpyxl 将匹配结果填入工作底稿模板。
LLM 可以自适应任意模板布局，不依赖硬编码的列映射。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from audit_workflow.llm_agent import build_agent, _agent_output, _extract_token_usage

from .config import output_dir, resolve_path


# ── public entry ──────────────────────────────────────────────────────────

def fill_working_paper_llm(config: dict[str, Any]) -> tuple[Path, dict]:
    """使用 LLM 生成填表代码并执行，返回 (输出文件路径, usage)。"""
    wp_config = config.get("working_paper", {})
    if not wp_config.get("enabled", True):
        raise RuntimeError("working_paper.enabled=false，跳过底稿填报。")

    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("enabled", False):
        raise RuntimeError("LLM 未启用，无法使用 LLM 版填表。请在 llm.yml 中设置 llm.enabled=true")

    # 读取数据
    matches_path = output_dir(config) / "matches" / "matches.csv"
    clean_dir = output_dir(config) / "clean"
    bank_path = clean_dir / "bank_transactions.csv"
    ledger_path = clean_dir / "ledger_entries.csv"

    template = resolve_path(config, config.get("project", {}).get("template_path"))
    if template is None or not template.exists():
        raise FileNotFoundError(f"找不到底稿模板：{template}")

    output_file = resolve_path(config, wp_config.get("output_file"))
    assert output_file is not None
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # 构建 prompt
    prompt = _build_fill_prompt(config, template, output_file, matches_path, bank_path, ledger_path)

    # 调用 LLM 生成代码
    agent = build_agent(
        llm_cfg,
        system_prompt=_FILL_SYSTEM_PROMPT,
        output_type=_code_output_model(),
    )
    result = agent.run_sync(prompt)
    output = _agent_output(result)
    usage = _extract_token_usage(result)
    code = output.code if hasattr(output, "code") else str(output)

    # 从 LLM 响应中提取代码
    code = _extract_code_block(code)

    # 语法检查
    try:
        ast.parse(code)
    except SyntaxError as e:
        raise RuntimeError(f"LLM 生成的代码有语法错误: {e}\n\n生成的代码:\n{code}") from e

    # 写入临时文件并执行
    tmp_path = output_file.parent / "_llm_fill_script.py"
    tmp_path.write_text(code, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp_path)],
            capture_output=True, text=True, timeout=120,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"LLM 生成的填表代码执行失败 (exit={proc.returncode}):\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\n\n"
                f"生成的代码保存在: {tmp_path}"
            )
    finally:
        # 保留脚本供调试
        pass

    if not output_file.exists():
        raise RuntimeError(f"LLM 填表代码执行完毕，但输出文件未生成: {output_file}")

    return output_file, usage


# ── prompt 构建 ───────────────────────────────────────────────────────────

def _build_fill_prompt(
    config: dict[str, Any],
    template: Path,
    output_file: Path,
    matches_path: Path,
    bank_path: Path,
    ledger_path: Path,
) -> str:
    """构建给 LLM 的填表提示词。"""

    # 读入模板结构信息
    template_info = _inspect_template(template)

    # 读入数据样本
    matches_sample = ""
    if matches_path.exists():
        df = pd.read_csv(matches_path, dtype=str).fillna("")
        matches_sample = df.head(10).to_csv(index=False)

    bank_sample = ""
    if bank_path.exists():
        df = pd.read_csv(bank_path, dtype=str).fillna("")
        bank_sample = df.head(5).to_csv(index=False)

    ledger_sample = ""
    if ledger_path.exists():
        df = pd.read_csv(ledger_path, dtype=str).fillna("")
        ledger_sample = df.head(5).to_csv(index=False)

    project = config.get("project", {})
    entity = project.get("client_short_name") or project.get("client_name", "")
    min_amount = config.get("working_paper", {}).get("wp03", {}).get("min_amount", 70000)

    prompt_parts = [
        "请生成一个完整的 Python 脚本，使用 openpyxl 将银行流水匹配结果填入工作底稿模板。",
        "",
        "## 输入文件路径",
        f"- 模板文件: {template}",
        f"- 输出文件: {output_file}",
        f"- 匹配结果: {matches_path}",
        f"- 银行流水: {bank_path}",
        f"- 序时账: {ledger_path}",
        "",
        "## 项目信息",
        f"- 主体名称: {entity}",
        f"- 最低金额阈值: {min_amount} 元（仅填 >= 此金额的匹配记录）",
        "",
        "## 模板结构 (WP-03 银行存款收支完整性检查表)",
        template_info,
        "",
        "## 填表规则",
        "1. 将模板文件复制到输出路径，然后编辑",
        "2. WP-03 表：按 account_no 分组，每组填入对应账户的数据",
        "3. WP-03 列映射：B=主体名称, C=银行对账单日期(bank_date), D=银行借方(bank_debit), E=银行贷方(bank_credit), F=对方户名(counterparty_name), G=记账日期(ledger_date), H=凭证字号(voucher_no), I=摘要(ledger_summary), J=对方公司名称(counterparty_name), K=借方金额(ledger_debit), L=贷方金额(ledger_credit)",
        "4. WP-02 表：按 account_no 分组，每月汇总银行流水和序时账的借/贷方金额",
        "5. 金额列只填数字，不要填公式",
        "6. 若数据行数超过模板容量，用 openpyxl 的 insert_rows 插入新行",
        "7. 插入行时保留原有样式（边框、字体、填充色等）",
        "8. 保留模板中已有的公式列（如 G 列差异、J 列差异等），不要覆盖",
        "9. 保存时使用 keep_vba=True（模板是 .xlsm 文件）",
        "",
        "## matches.csv 列说明",
        "bank_date, ledger_date, bank_name, account_no, counterparty_name, bank_summary, ledger_summary, voucher_no, bank_debit, bank_credit, ledger_debit, ledger_credit, amount, flow",
        "",
        "## matches.csv 数据样本",
        "```csv",
        matches_sample,
        "```",
        "",
        "## bank_transactions.csv 列说明",
        "bank_name, account_no, transaction_date, flow, amount, bank_debit, bank_credit, counterparty_name, summary",
        "",
        "## bank_transactions.csv 数据样本",
        "```csv",
        bank_sample,
        "```",
        "",
        "## ledger_entries.csv 列说明",
        "bank_name, account_no, transaction_date, flow, amount, ledger_debit, ledger_credit, voucher_no, summary, counterparty_name",
        "",
        "## ledger_entries.csv 数据样本",
        "```csv",
        ledger_sample,
        "```",
        "",
        "## 要求",
        "- 生成一个完整的、可直接运行的 Python 脚本",
        "- 脚本必须包含 `if __name__ == '__main__':` 入口",
        "- 使用 openpyxl（不要用 xlwings 或 win32com）",
        "- 先复制模板再编辑",
        "- 输出文件路径即上面指定的路径",
        "- 金额数字保留2位小数",
        "- 日期格式处理要兼容 '2022-06-15' 和 '2022-06-15 00:00:00' 等格式",
        "- 如果模板中有多个账户区块，自动检测并按 account_no 填入",
    ]

    return "\n".join(prompt_parts)


def _inspect_template(template: Path) -> str:
    """检查模板结构，返回结构描述文本。"""
    from openpyxl import load_workbook

    wb = load_workbook(template, read_only=True, data_only=False, keep_vba=template.suffix.lower() == ".xlsm")
    lines = []

    for sheet_name in wb.sheetnames:
        if sheet_name not in ("WP-02", "WP-03"):
            continue
        ws = wb[sheet_name]
        lines.append(f"\n### Sheet: {sheet_name} (rows={ws.max_row}, cols={ws.max_column})")

        for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 70), max_col=16, values_only=False), start=1):
            r = r_idx
            non_empty = []
            for c in row:
                if c.value is not None:
                    col_letter = c.coordinate.replace(str(r), "")
                    v = str(c.value)[:100]
                    non_empty.append(f"{col_letter}={v}")
            if non_empty:
                lines.append(f"  Row {r}: {'; '.join(non_empty)}")

    wb.close()
    return "\n".join(lines)


def _extract_code_block(text: str) -> str:
    """从 LLM 响应中提取 Python 代码块。"""
    # 尝试提取 ```python ... ``` 块
    import re
    match = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


# ── system prompt ─────────────────────────────────────────────────────────

_FILL_SYSTEM_PROMPT = """你是一个精通 Python 和 openpyxl 的审计工作底稿自动化专家。
你的任务是根据给定的模板结构和数据，生成完整的 Python 脚本来填充工作底稿。

## 技术约束
- 使用 openpyxl 库（不要使用 xlwings 或 win32com）
- 模板是 .xlsm 文件，load_workbook 时使用 keep_vba=True
- 金额列只填数字（float），不要填公式字符串
- 保留模板中已有的公式列，不要覆盖
- 如果需要插入行，使用 ws.insert_rows()，然后复制样式
- 复制样式的方法：遍历源行每个单元格，将 _style, number_format, alignment, font, fill, border 复制到目标单元格

## 关键 openpyxl 用法
```python
from openpyxl import load_workbook
from copy import copy
import shutil

# 复制模板
shutil.copy2(template_path, output_path)

# 打开工作簿
wb = load_workbook(output_path, keep_vba=True)

# 操作 sheet
ws = wb['WP-03']

# 写入单元格
ws['B9'] = '主体名称'
ws.cell(row=9, column=2).value = '主体名称'

# 插入行
ws.insert_rows(10, 5)  # 在第10行前插入5行

# 复制样式
def copy_row_style(ws, src_row, dst_row):
    for col in range(1, ws.max_column + 1):
        src = ws.cell(src_row, col)
        dst = ws.cell(dst_row, col)
        if src.has_style:
            dst._style = copy(src._style)
        if src.number_format:
            dst.number_format = src.number_format
        if src.alignment:
            dst.alignment = copy(src.alignment)
        if src.font:
            dst.font = copy(src.font)
        if src.fill:
            dst.fill = copy(src.fill)
        if src.border:
            dst.border = copy(src.border)

# 保存
wb.save(output_path)
```

## 数据读取
使用 pandas 读取 CSV 文件：
```python
import pandas as pd
matches = pd.read_csv(matches_path, dtype=str).fillna('')
```

## 注意事项
- 金额列使用 float 类型，不要用字符串
- 日期的处理：如果数据中有 '2022-06-15' 格式，直接使用；如果有 Timestamp 格式，转为 date
- 如果模板中已有多个账户区块（通过「开户行名称：」标记），按顺序匹配
- 确保输出文件路径存在（先创建目录）
"""


def _code_output_model():
    """创建 LLM 输出模型。"""
    try:
        from pydantic import Field, create_model
    except ImportError as exc:
        raise RuntimeError("请先安装 PydanticAI：pip install pydantic-ai") from exc

    return create_model(
        "FillScript",
        code=(str, Field(description="完整的 Python 源码，用于填充工作底稿")),
    )