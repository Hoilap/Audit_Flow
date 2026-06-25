"""
文件检测与识别模块 —— 扫描 inputs/ 目录，用 LLM 自动识别：
  - 哪些文件是银行流水 / 哪些是序时账
  - 归属哪个银行
  - 覆盖的时间段
  - 推荐使用哪个解析器

生成 task.yml 草稿供前端确认。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, List

import yaml


# ============================================================
# 不依赖 LLM 的纯本地检测
# ============================================================

_SUPPORTED_EXTS = {".xlsx", ".xls", ".xlsm", ".csv"}

# 文件名关键词 → 类型提示
_KEYWORD_HINTS = {
    "ledger": [
        "序时账", "明细账", "ledger", "新纪元", "账务", "科目",
        "凭证", "记账", "金蝶", "用友", "xinjiyuan",
    ],
    "bank": [
        "银行流水", "银行对账单", "bank", "流水", "交易明细",
        "historydetail", "银行账"
    ],
    "working_paper":["底稿"]
}

_BANK_NAME_HINTS: dict[str, tuple[str, str]] = {
    "icbc": ("中国工商银行", "icbc_historydetail"),
    "工行": ("中国工商银行", "icbc_historydetail"),
    "boc": ("中国银行", "icbc_historydetail"),
    "中行": ("中国银行", "icbc_historydetail"),
    "abc": ("中国农业银行", "icbc_historydetail"),
    "农行": ("中国农业银行", "icbc_historydetail"),
    "ccb": ("中国建设银行", "icbc_historydetail"),
    "建行": ("中国建设银行", "icbc_historydetail"),
    "cmb": ("招商银行", "icbc_historydetail"),
    "招行": ("招商银行", "icbc_historydetail"),
}

_YEAR_RANGE = [
    (2020, 2035),
    (2020, 2035),
]  # inclusive


def _guess_bank_short(bank_name: str) -> str:
    """从银行全称中提取英文缩写。"""
    name_lower = bank_name.lower()
    for key in ("icbc", "abc", "boc", "ccb", "cmb", "cib", "spdb", "ceb", "citic"):
        if key in name_lower:
            return key
    # 中文匹配
    _cn_map = {"工行": "icbc", "农行": "abc", "中行": "boc", "建行": "ccb", "招行": "cmb", "兴业": "cib", "浦发": "spdb", "光大": "ceb", "中信": "citic"}
    for cn, abbr in _cn_map.items():
        if cn in bank_name:
            return abbr
    return "bank"


def scan_input_files(
    base_dir: str | Path,
    customer_name: str = "",
    task_name: str = "",
) -> list[dict[str, Any]]:
    """扫描 inputs/{customer}/{task}/ 下所有 Excel/CSV 文件。

    Returns:
        [{path, name, ext, size_bytes, preview_excel_sheets, ...}, ...]
    """
    root = Path(base_dir)
    if customer_name and task_name:
        scan_dir = root / customer_name / task_name
    elif customer_name:
        scan_dir = root / customer_name
    else:
        scan_dir = root

    if not scan_dir.exists():
        return []

    files = []
    for fpath in scan_dir.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in _SUPPORTED_EXTS:
            continue
        if fpath.name.startswith("~$"):
            continue

        info = {
            "path": str(fpath),
            "name": fpath.name,
            "stem": fpath.stem,
            "ext": fpath.suffix.lower(),
            "size_bytes": fpath.stat().st_size,
            "relative_path": str(fpath.relative_to(root)),
        }

        # 对 Excel 文件预览 sheet 名和前几行
        if fpath.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
            try:
                info["excel_preview"] = _preview_excel(fpath)
            except Exception:
                info["excel_preview"] = {"error": "无法读取文件"}

        files.append(info)

    return files


def _preview_excel(path: Path) -> dict[str, Any]:
    """预览 Excel 文件的 sheet 名和每个 sheet 的前 3 行。"""
    import pandas as pd

    suffix = path.suffix.lower()
    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    xl = pd.ExcelFile(path, engine=engine)
    sheets = {}
    for sheet_name in xl.sheet_names[:8]:  # 最多看 8 个 sheet
        try:
            df = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=5, engine=engine)
            sheets[sheet_name] = {
                "shape": list(df.shape),
                "preview_rows": [
                    [str(c) if not pd.isna(c) else "" for c in row]
                    for row in df.values.tolist()[:3]
                ],
            }
        except Exception:
            sheets[sheet_name] = {"error": "无法读取"}
    return {"sheets": sheets}


# ============================================================
# LLM 驱动的智能识别
# ============================================================

_FILE_IDENTIFY_SYSTEM_PROMPT = """你是一个审计数据分析助手，专门识别银行流水文件/序时账文件/底稿文件。

你需要分析给定的文件信息（文件名、sheet 名、数据预览），判断：
1. **文件类型**: "bank_statement"（银行流水）或 "ledger"（序时账/明细账）或"working_paper"（底稿）
2. **银行名称**: 如"中国工商银行贵港桂平新区支行"，尽量完整
3. **银行账号**: 从数据中提取
4. **时间范围**: 数据覆盖的起止日期（YYYY-MM-DD 格式），从数据预览中提取实际日期
5. **置信度**: 0-1 之间
6. **备注**: 任何需要注意的问题

**文件 ID 命名规则（非常重要）**:
- 格式: {bank_short}_{year}_{type}，如 icbc_bank_2022
- 同一银行有多个文件时，加上月份区分: icbc_bank_2022_01_06、icbc_bank_2022_07_12
- 序时账: xinjiyuan_ledger_2022
- bank_short 用银行英文缩写: icbc/abc/boc/ccb/cmb 等

重要规则：
- 如果文件名含"流水"、"bank"、"交易明细"等一般是银行流水
- 如果文件名含"序时账"、"账"、"新纪元"等一般是序时账
- 序时账通常有多个月份/凭证号列
- 银行流水通常有借/贷方金额、余额列
- 从数据预览中尽可能提取真实的日期范围，而非猜测
"""


def _build_llm_identify_prompt(files: list[dict[str, Any]], project_info: dict[str, Any]) -> str:
    """构建发给 LLM 的文件识别 prompt。"""
    file_desc = []
    for i, f in enumerate(files):
        desc = {
            "序号": i,
            "文件名": f["name"],
            "扩展名": f["ext"],
            "大小(KB)": round(f["size_bytes"] / 1024, 1),
        }
        if "excel_preview" in f:
            preview = f["excel_preview"]
            if "sheets" in preview:
                desc["sheets"] = {}
                for sn, sd in preview["sheets"].items():
                    desc["sheets"][sn] = {
                        "行×列": sd.get("shape", []),
                        "前3行预览": sd.get("preview_rows", []),
                    }
        file_desc.append(desc)

    prompt_data = {
        "项目信息": project_info,
        "待识别文件": file_desc,
        "要求": "请逐个分析以上文件，返回结构化的识别结果。",
    }
    return json.dumps(prompt_data, ensure_ascii=False, indent=2)


def identify_files_with_llm(
    files: list[dict[str, Any]],
    llm_config: dict[str, Any],
    project_info: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """调用 LLM 对扫描到的文件做智能识别。

    Returns:
        [{id, type, path, bank_name, account_no, date_from, date_to, confidence, notes}, ...]
    """
    from audit_workflow.llm_agent import llm_generate_structured
    from pydantic import BaseModel, Field

    class FileIdentification(BaseModel):
        file_index: int = Field(description="文件序号（对应输入中的序号）")
        id: str = Field(description="唯一标识符，如 icbc_bank_2022")
        type: str = Field(description="bank_statement 或 ledger")
        bank_name: str = Field(default="", description="银行全称")
        account_no: str = Field(default="", description="银行账号")
        date_from: str = Field(default="", description="数据起始日期 YYYY-MM-DD")
        date_to: str = Field(default="", description="数据结束日期 YYYY-MM-DD")
        sheet: str = Field(default="", description="推荐的目标 sheet 名")
        confidence: float = Field(default=0.0, description="置信度 0-1")
        notes: str = Field(default="", description="备注")

    class FileIdentificationBatch(BaseModel):
        results: list[FileIdentification] = Field(description="所有文件的识别结果列表")

    if not llm_config.get("enabled", False):
        # 退化为关键词匹配
        return _identify_files_local(files, project_info or {})

    prompt = _build_llm_identify_prompt(files, project_info or {})
    result, usage = llm_generate_structured(
        llm_config,
        prompt,
        FileIdentificationBatch,
        _FILE_IDENTIFY_SYSTEM_PROMPT,
    )

    # 补充 path 信息
    results = []
    for item in result.results:
        file_info = files[item.file_index] if item.file_index < len(files) else {}
        results.append({
            "id": item.id,
            "type": item.type,
            "path": file_info.get("path", ""),
            "name": file_info.get("name", ""),
            "bank_name": item.bank_name,
            "account_no": item.account_no,
            "date_from": item.date_from,
            "date_to": item.date_to,
            "sheet": item.sheet,
            "confidence": item.confidence,
            "notes": item.notes,
        })
    return results


async def identify_files_with_llm_async(
    files: list[dict[str, Any]],
    llm_config: dict[str, Any],
    project_info: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict]:
    """异步版本：调用 LLM 对扫描到的文件做智能识别。
    返回: (identifications, usage) 元组
    """
    from audit_workflow.llm_agent import llm_generate_structured_async
    from pydantic import BaseModel, Field

    class FileIdentification(BaseModel):
        file_index: int = Field(description="文件序号（对应输入中的序号）")
        id: str = Field(description="唯一标识符，如 icbc_bank_2022")
        type: str = Field(description="bank_statement 或 ledger")
        bank_name: str = Field(default="", description="银行全称")
        account_no: str = Field(default="", description="银行账号")
        date_from: str = Field(default="", description="数据起始日期 YYYY-MM-DD")
        date_to: str = Field(default="", description="数据结束日期 YYYY-MM-DD")
        sheet: str = Field(default="", description="推荐的目标 sheet 名")
        confidence: float = Field(default=0.0, description="置信度 0-1")
        notes: str = Field(default="", description="备注")

    class FileIdentificationBatch(BaseModel):
        results: list[FileIdentification] = Field(description="所有文件的识别结果列表")

    if not llm_config.get("enabled", False):
        return _identify_files_local(files, project_info or {}), {}

    prompt = _build_llm_identify_prompt(files, project_info or {})
    result, usage = await llm_generate_structured_async(
        llm_config,
        prompt,
        FileIdentificationBatch,
        _FILE_IDENTIFY_SYSTEM_PROMPT,
    )

    results = []
    for item in result.results:
        file_info = files[item.file_index] if item.file_index < len(files) else {}
        results.append({
            "id": item.id,
            "type": item.type,
            "path": file_info.get("path", ""),
            "name": file_info.get("name", ""),
            "bank_name": item.bank_name,
            "account_no": item.account_no,
            "date_from": item.date_from,
            "date_to": item.date_to,
            "sheet": item.sheet,
            "confidence": item.confidence,
            "notes": item.notes,
        })
    return results, usage


def _identify_files_local(
    files: list[dict[str, Any]],
    project_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """纯本地的关键词+规则文件识别（不依赖LLM）。"""
    results = []
    for i, f in enumerate(files):
        name_lower = f["name"].lower()
        bank_hits = sum(1 for kw in _KEYWORD_HINTS["bank"] if kw.lower() in name_lower)
        ledger_hits = sum(1 for kw in _KEYWORD_HINTS["ledger"] if kw.lower() in name_lower)

        if bank_hits > ledger_hits:
            ftype = "bank_statement"
        elif ledger_hits > bank_hits:
            ftype = "ledger"
        else:
            # 看扩展名和大小猜
            if f["ext"] == ".csv":
                ftype = "bank_statement"  # CSV 更像银行流水
            else:
                ftype = "bank_statement"

        # 猜测银行
        bank_name = ""
        for key, (bn, _) in _BANK_NAME_HINTS.items():
            if key.lower() in name_lower:
                bank_name = bn
                break

        # 推测年份
        year = project_info.get("audit_year", datetime.now().year)
        import re
        year_matches = re.findall(r"(20\d{2})", f["name"])
        if year_matches:
            year = int(year_matches[0])

        # 生成更有意义的 id
        if bank_name:
            bank_short = _guess_bank_short(bank_name)
            short_id = f"{bank_short}_{ftype[:4]}_{year}"
        else:
            short_id = f"{ftype[:4]}_{year}_{i}"

        results.append({
            "id": short_id,
            "type": ftype,
            "path": f["path"],
            "name": f["name"],
            "bank_name": bank_name,
            "account_no": "",
            "date_from": f"{year}-01-01",
            "date_to": f"{year}-12-31",
            "sheet": "",
            "confidence": 0.5,
            "notes": "本地关键词识别，建议用 LLM 做更准确的识别",
        })

    return results


# ============================================================
# 根据识别结果生成 task.yml
# ============================================================

def generate_task_config(
    identifications: list[dict[str, Any]],
    project_info: dict[str, Any],
    matching_defaults: dict[str, Any] | None = None,
    working_paper_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据 LLM 识别结果生成 task.yml 配置字典。

    Returns:
        可直接 yaml.dump 的配置字典
    """
    bank_items = []
    ledger_items = []

    for item in identifications:
        entry = {
            "id": item["id"],
            "enabled": True,
            "path": item["path"],
            "sheet": item.get("sheet", ""),
            "bank_name": item.get("bank_name", ""),
            "account_no": item.get("account_no", ""),
        }

        if item["type"] == "bank_statement":
            bank_items.append(entry)
        else:
            entry["year"] = int(item.get("date_from", "2022")[:4]) if item.get("date_from") else project_info.get("audit_year", 2022)
            ledger_items.append(entry)

    # 匹配参数默认值
    match_cfg = {
        "date_tolerance_days": 30,
        "group_date_tolerance_days": 30,
        "amount_tolerance": 0.01,
        "high_confidence_score": 0.82,
        "medium_confidence_score": 0.65,
        "one_to_one_min_score": 0.58,
        "group_max_size": 6,
        "group_candidate_pool_limit": 24,
        "strict_account_match": True,
    }
    if matching_defaults:
        match_cfg.update(matching_defaults)

    # 底稿参数默认值
    # 注：wp03/wp02 的详细配置维持默认，只有 output_file 根据项目信息生成
    output_dir_relative = f"outputs/{project_info.get('name', '')}/{project_info.get('task', 'bank_ledger_match')}"
    wp_cfg = {
        "enabled": True,
        "fill_only_when_fully_matched": False,
        "output_file": f"{output_dir_relative}/working_paper/资金流水专项核查工作底稿-自动填报.xlsm",
        "wp03": {
            "enabled": True,
            "sheet": "WP-03",
            "start_row": 6,
            "min_amount": 70000,
            "columns": {
                "entity": "B",
                "bank_date": "C",
                "bank_debit": "D",
                "bank_credit": "E",
                "counterparty": "F",
                "ledger_date": "G",
                "voucher_no": "H",
                "ledger_summary": "I",
                "ledger_counterparty": "J",
                "ledger_debit": "K",
                "ledger_credit": "L",
            },
        },
        "wp02": {
            "enabled": True,
            "sheet": "WP-02",
        },
    }
    if working_paper_defaults:
        # 允许外部覆盖 wp_cfg，但保留 wp03/wp02 的默认结构
        for k, v in working_paper_defaults.items():
            if k in ("wp03", "wp02"):
                wp_cfg[k].update(v)
            else:
                wp_cfg[k] = v

    output_dir = output_dir_relative

    config = {
        "project": {
            "client_name": project_info.get("client_name", ""),
            "client_short_name": project_info.get("name", ""),
            "audit_year": project_info.get("audit_year", datetime.now().year),
            "template_path": project_info.get("template_path", ""),
            "output_dir": output_dir,
        },
        "inputs": {
            "bank_statements": bank_items,
            "ledgers": ledger_items,
        },
        "matching": match_cfg,
        "working_paper": wp_cfg,
        # 元信息
        "_meta": {
            "generated_at": datetime.now().isoformat(),
            "generated_by": "file_detector",
            "llm_identified": any(item.get("confidence", 0) > 0.6 for item in identifications),
            "files_scanned": len(identifications),
        },
    }

    return config


def save_task_config(config: dict[str, Any], path: str | Path) -> Path:
    """将 task.yml 写入文件。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 移除 _meta 再写入（_meta 是内部用）
    clean = {k: v for k, v in config.items() if k != "_meta"}
    with open(out, "w", encoding="utf-8") as f:
        f.write("# ============================================================\n")
        f.write("# 任务配置文件 —— 由文件检测器自动生成\n")
        f.write(f"# 生成时间: {config.get('_meta', {}).get('generated_at', '')}\n")
        f.write("# 请检查并修改后确认\n")
        f.write("# ============================================================\n\n")
        yaml.dump(clean, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=1000000)
    return out


def merge_llm_into_full_config(
    task_config: dict[str, Any],
    llm_config: dict[str, Any],
) -> dict[str, Any]:
    """将 LLM 配置合并到 task 配置中，生成完整配置供 pipeline 使用。"""
    merged = dict(task_config)

    # 注入 LLM 配置
    merged["llm"] = llm_config.get("llm", {})

    # 注入 LLM matching 配置
    llm_matching = llm_config.get("llm", {}).get("matching", {})
    if llm_matching:
        merged.setdefault("matching", {})
        merged["matching"]["llm"] = llm_matching

    return merged