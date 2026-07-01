"""
文件检测与识别模块 —— 扫描 inputs/ 目录，根据子目录结构自动分类：
  - bank/          → 银行流水 (bank_statement)
  - ledger/        → 序时账 (ledger)
  - working paper/ → 底稿 (working_paper)

银行名称 / 银行账号 / 时间范围等元数据仍从文件名和数据预览中提取。

生成 task.yml 草稿供前端确认。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

# ============================================================
# 常量与配置
# ============================================================

_SUPPORTED_EXTS = {".xlsx", ".xls", ".xlsm", ".csv"}

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
    "民生": ("中国民生银行", "icbc_historydetail"),
    "华润": ("华润银行", "icbc_historydetail"),
}

# 账号提取表头关键词
_ACCOUNT_HEADER_KEYWORDS = ("账号", "卡号", "account", "账户号", "acc_no", "accno")


# ============================================================
# 外部配置加载 (config/config.blm.detect.yaml)
# ============================================================

_DETECT_CONFIG_CACHE: dict | None = None


def _load_detect_config() -> dict:
    """加载 config/config.blm.detect.yaml，带缓存。"""
    global _DETECT_CONFIG_CACHE
    if _DETECT_CONFIG_CACHE is not None:
        return _DETECT_CONFIG_CACHE
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.blm.detect.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            _DETECT_CONFIG_CACHE = yaml.safe_load(f) or {}
    else:
        _log.warning("Detect 配置文件不存在: %s，使用内置默认值", config_path)
        _DETECT_CONFIG_CACHE = {}
    return _DETECT_CONFIG_CACHE


# ============================================================
# 目录分类
# ============================================================

def _classify_by_directory(
    base_dir: str | Path,
    files: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str, str]]:
    """根据文件所在的子目录分类文件类型。

    Args:
        base_dir: inputs/{client}/{task}/ 的绝对路径
        files: scan_input_files 返回的文件列表

    Returns:
        [(file_info, file_type, sub_dir_name), ...]
        file_type: bank_statement / ledger / working_paper / unknown
    """
    detect_cfg = _load_detect_config()
    # 构建不区分大小写的映射
    raw_map = detect_cfg.get("directory_types", {
        "bank": "bank_statement",
        "ledger": "ledger",
        "working paper": "working_paper",
    })
    dir_type_map = {k.lower(): v for k, v in raw_map.items()}

    base = Path(base_dir)
    results = []
    for f in files:
        fpath = Path(f["path"])
        try:
            rel = fpath.relative_to(base)
        except ValueError:
            results.append((f, "unknown", ""))
            continue

        parts = rel.parts
        if len(parts) <= 1:
            # 根级文件——不在任何子目录中
            results.append((f, "unknown", ""))
        else:
            sub_dir = parts[0]
            ftype = dir_type_map.get(sub_dir.lower(), "unknown")
            results.append((f, ftype, sub_dir))

    return results


# ============================================================
# 银行名称识别
# ============================================================

def _guess_bank_short(bank_name: str) -> str:
    """从银行全称中提取英文缩写。"""
    name_lower = bank_name.lower()
    for key in ("icbc", "abc", "boc", "ccb", "cmb", "cib", "spdb", "ceb", "citic"):
        if key in name_lower:
            return key
    _cn_map = {"工商银行": "icbc", "工行": "icbc", "建设银行": "ccb", "建行": "ccb",
               "农业银行": "abc", "农行": "abc", "中国银行": "boc", "中行": "boc",
               "招商银行": "cmb", "招行": "cmb", "兴业银行": "cib", "兴业": "cib",
               "浦发银行": "spdb", "浦发": "spdb", "光大银行": "ceb", "光大": "ceb",
               "中信银行": "citic", "中信": "citic", "民生银行": "ceb", "民生": "ceb"}
    for cn, abbr in _cn_map.items():
        if cn in bank_name:
            return abbr
    return "unknown"


# ============================================================
# 文件扫描
# ============================================================

def scan_input_files(
    base_dir: str | Path,
    customer_name: str = "",
    task_name: str = "",
) -> list[dict[str, Any]]:
    """扫描 inputs/{customer}/{task}/ 下所有 Excel/CSV 文件。

    Returns:
        [{path, name, ext, size_bytes, sub_directory, excel_preview, ...}, ...]
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

        rel = fpath.relative_to(scan_dir)
        # 顶层子目录名（如 "bank", "ledger"），根级文件为空
        sub_directory = rel.parts[0] if len(rel.parts) > 1 else ""

        info = {
            "path": str(fpath),
            "name": fpath.name,
            "stem": fpath.stem,
            "ext": fpath.suffix.lower(),
            "size_bytes": fpath.stat().st_size,
            "relative_path": str(rel),
            "sub_directory": sub_directory,
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
# 目录驱动的文件识别
# ============================================================

def _identify_files_local(
    files: list[dict[str, Any]],
    project_info: dict[str, Any],
    base_dir: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """基于目录结构的文件分类。

    根据文件所在子目录（bank/ledger/working paper）确定类型，
    银行名称、账号、年份仍从文件名和数据预览提取。

    Returns:
        (identifications, warnings) 元组
    """
    if not base_dir and files:
        # 从第一个文件的 path 推断 base_dir
        first_path = Path(files[0]["path"])
        # 向上找到 task 目录（倒数第二级）
        base_dir = str(first_path.parent if len(first_path.parts) > 1 else first_path.parent.parent)

    classified = _classify_by_directory(base_dir, files)
    results: list[dict[str, Any]] = []
    warnings: list[str] = []

    for f, ftype, sub_dir in classified:
        if ftype == "unknown":
            warnings.append(f"文件 {f['name']} 不在标准子目录(bank/ledger/working paper)中，已跳过")
            continue

        # ── 提取元数据 ──
        name_lower = f["name"].lower()

        # 银行名称
        bank_name = ""
        for key, (bn, _) in _BANK_NAME_HINTS.items():
            if key.lower() in name_lower:
                bank_name = bn
                break

        # 年份
        year = project_info.get("audit_year", datetime.now().year)
        year_matches = re.findall(r"(20\d{2})", f["name"])
        if year_matches:
            year = int(year_matches[0])

        # 银行账号（从数据预览中提取）
        account_no = _extract_account_no_from_preview(f.get("excel_preview"))

        # ── 生成 ID ──
        type_short = "bank" if ftype == "bank_statement" else ftype
        if bank_name:
            bank_short = _guess_bank_short(bank_name)
            short_id = f"{bank_short}_{type_short}_{year}"
        else:
            short_id = f"unknown_{type_short}_{year}_{len(results)}"

        entry = {
            "id": short_id,
            "type": ftype,
            "path": f["path"],
            "name": f["name"],
            "bank_name": bank_name,
            "account_no": account_no,
            "date_from": f"{year}-01-01",
            "date_to": f"{year}-12-31",
            "sheet": "",
            "confidence": 0.9,
            "notes": f"目录分类: {sub_dir}/",
        }
        results.append(entry)

    # ── 按 (目录 + 银行) 做账号回填 ──
    # 同一子目录中、同一银行的文件通常属于同一账号，
    # 如果某个文件提取到了账号，回填给同目录+同银行的其他文件
    bank_entries = [e for e in results if e["type"] == "bank_statement"]
    # 按 (父目录, bank_name) 分组
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in bank_entries:
        key = (Path(e["path"]).parent.name, e["bank_name"])
        groups.setdefault(key, []).append(e)
    for (_dir, _bank), entries in groups.items():
        found_account = next((e["account_no"] for e in entries if e["account_no"]), "")
        if found_account:
            for e in entries:
                if not e["account_no"]:
                    e["account_no"] = found_account
                    e["notes"] += f"（账号从同组文件回填: {found_account}）"

    # ── 检查 bank 文件是否有空账号 ──
    for entry in results:
        if entry["type"] == "bank_statement" and not entry["account_no"]:
            msg = f"银行流水 {entry['name']} 未能识别银行账号，请检查 task.yml 并手动补充"
            warnings.append(msg)
            _log.warning(msg)

    return results, warnings


# ── 账号程序化提取 ─────────────────────────────────────────────

def _extract_account_no_from_preview(excel_preview: dict[str, Any]) -> str:
    """从 _preview_excel 返回的预览数据中程序化提取银行账号。

    策略：在每个 sheet 的 preview_rows 中找到表头行（含"账号/卡号"等关键词），
    然后读取下一行同列的值作为账号。
    """
    if not excel_preview:
        return ""
    sheets = excel_preview.get("sheets", {})
    for _sheet_name, sheet_data in sheets.items():
        rows = sheet_data.get("preview_rows", [])
        for r_idx, row in enumerate(rows):
            acct_col = None
            for c_idx, cell in enumerate(row):
                cell_lower = str(cell).lower().strip()
                if any(kw in cell_lower for kw in _ACCOUNT_HEADER_KEYWORDS):
                    acct_col = c_idx
                    break
            if acct_col is None:
                continue
            for next_row in rows[r_idx + 1:]:
                if acct_col < len(next_row):
                    val = str(next_row[acct_col]).strip()
                    if val and val.isdigit() and len(val) >= 8:
                        return val
    return ""


# ============================================================
# LLM 增强识别：目录分类 + LLM 元数据提取
# ============================================================

_METADATA_EXTRACT_SYSTEM_PROMPT = """\
你是一个审计数据分析助手。下面给出了若干银行流水或序时账文件的 **各工作表** 名称和数据样本。

请逐个分析每个工作表，判断其是否与审计核查相关，并从相关的工作表中提取元数据：

1. **relevant** (bool): 该工作表是否相关。只有包含实际交易流水或账目明细的工作表才相关。
   不相关的例子：汇总页、目录页、说明页、空白表、封面、图表等。
2. **bank_name** (str): 银行全称，如"中国工商银行贵港桂平新区支行"，尽量完整。
3. **account_no** (str): 银行账号/卡号，通常是纯数字，至少 8 位。
4. **date_from** (str): 该工作表数据的最早日期，YYYY-MM-DD 格式。
5. **date_to** (str): 该工作表数据的最晚日期，YYYY-MM-DD 格式。
6. **notes** (str): 备注，如不相关的原因说明。

重要规则：
- 只提取数据中确实存在的信息，不要猜测
- 如果样本中找不到某个字段，留空字符串
- 日期从实际数据行中提取，不要从文件名推断
- 不相关的工作表也必须返回，relevant 设为 false
"""


async def identify_files_with_llm_async(
    files: list[dict[str, Any]],
    llm_config: dict[str, Any],
    project_info: dict[str, Any] | None = None,
    base_dir: str = "",
) -> tuple[list[dict[str, Any]], list[str], dict]:
    """目录分类 + LLM 按工作表元数据提取。

    1. 文件类型由子目录决定（同 script 模式）
    2. 为每个工作表构建数据样本，LLM 判断是否相关并提取元数据
    3. 仅保留 LLM 判定为相关的工作表，sheet 字段填充实际工作表名
    4. LLM 提取失败时异常直接向上抛出（不兜底）

    Returns:
        (identifications, warnings, usage) 元组
    """
    from audit_workflow.llm_agent import llm_generate_structured_async
    from audit_workflow.util import build_smart_sample, read_excel_for_sample
    from pydantic import BaseModel, Field

    class WorksheetMetadata(BaseModel):
        file_index: int = Field(description="文件序号")
        sheet_name: str = Field(default="", description="工作表名称")
        relevant: bool = Field(default=False, description="该工作表是否与审计核查相关")
        bank_name: str = Field(default="", description="银行全称")
        account_no: str = Field(default="", description="银行账号")
        date_from: str = Field(default="", description="起始日期 YYYY-MM-DD")
        date_to: str = Field(default="", description="结束日期 YYYY-MM-DD")
        notes: str = Field(default="", description="备注")

    class WorksheetMetadataBatch(BaseModel):
        results: list[WorksheetMetadata] = Field(description="所有工作表的元数据列表")

    # ── Step 1: 目录分类（同 script 模式）──
    results, warnings = _identify_files_local(files, project_info or {}, base_dir)

    if not llm_config.get("enabled", True):
        return results, warnings, {}

    # ── Step 2: 为 bank/ledger 文件的每个工作表构建样本 ──
    # target_entries 记录需要 LLM 分析的 (result_index, entry)
    target_entries = [
        (i, r) for i, r in enumerate(results)
        if r["type"] in ("bank_statement", "ledger")
    ]

    if not target_entries:
        return results, warnings, {}

    # 收集每个文件的 sheet 名称（从 excel_preview 中获取）
    file_sheet_names: dict[str, list[str]] = {}
    for _idx, (_ri, entry) in enumerate(target_entries):
        fpath = entry["path"]
        if fpath not in file_sheet_names:
            file_info = next((f for f in files if f["path"] == fpath), None)
            preview = file_info.get("excel_preview", {}) if file_info else {}
            sheet_data = preview.get("sheets", {})
            file_sheet_names[fpath] = list(sheet_data.keys())

    # 为每个工作表构建描述（一个 desc 对应一个工作表）
    sheet_desc_list: list[dict[str, Any]] = []
    for idx, (result_idx, entry) in enumerate(target_entries):
        fpath = entry["path"]
        sheet_names = file_sheet_names.get(fpath, [])

        for sheet_name in sheet_names:
            desc: dict[str, Any] = {
                "序号": idx,
                "文件名": entry["name"],
                "文件类型": entry["type"],
                "工作表": sheet_name,
            }

            # 尝试从 preview 取该 sheet 的前几行
            file_info = next((f for f in files if f["path"] == fpath), None)
            preview = file_info.get("excel_preview", {}) if file_info else {}
            sheet_info = preview.get("sheets", {}).get(sheet_name, {})
            preview_rows = sheet_info.get("preview_rows", [])

            if preview_rows:
                desc["数据样本"] = preview_rows[:3]
            else:
                # preview 不可用，回退到读文件
                try:
                    df = read_excel_for_sample(fpath, sheet=sheet_name, nrows=20)
                    sample = build_smart_sample(df, data_rows=6)
                    desc["数据样本"] = sample[:8]
                except Exception as exc:
                    desc["error"] = f"无法读取样本: {exc}"

            sheet_desc_list.append(desc)

    prompt_data = {
        "项目信息": {"审计年度": project_info.get("audit_year", 2022)} if project_info else {},
        "工作表列表": sheet_desc_list,
        "要求": "请逐个分析以上工作表，判断是否与审计核查相关（relevant），并从相关工作表中提取银行名称、账号、日期范围等元数据。",
    }
    prompt = json.dumps(prompt_data, ensure_ascii=False, indent=2)

    # ── Step 3: 调用 LLM ──
    llm_result, usage = await llm_generate_structured_async(
        llm_config,
        prompt,
        WorksheetMetadataBatch,
        _METADATA_EXTRACT_SYSTEM_PROMPT,
    )

    # ── Step 4: 按工作表合并 LLM 结果，仅保留 relevant=True ──
    # 构建 (file_index, sheet_name) → result entry 的查找表
    entry_lookup: dict[tuple[int, str], dict[str, Any]] = {}
    for idx, (result_idx, entry) in enumerate(target_entries):
        fpath = entry["path"]
        for sn in file_sheet_names.get(fpath, []):
            entry_lookup[(idx, sn)] = dict(entry)  # 浅拷贝，每个 sheet 独立

    relevant_results: list[dict[str, Any]] = []
    skipped_sheets: list[str] = []

    for meta in llm_result.results:
        lookup_key = (meta.file_index, meta.sheet_name)
        base_entry = entry_lookup.get(lookup_key)

        if not meta.relevant:
            if base_entry:
                skipped_sheets.append(f"{base_entry['name']}[{meta.sheet_name}]")
            continue

        if base_entry is None:
            # file_index 越界或 sheet 找不到对应 entry
            if meta.file_index < len(target_entries):
                _ri, orig = target_entries[meta.file_index]
                base_entry = dict(orig)
            else:
                continue

        # 合并 LLM 元数据
        if meta.bank_name:
            base_entry["bank_name"] = meta.bank_name
        if meta.account_no:
            base_entry["account_no"] = meta.account_no
        if meta.date_from:
            base_entry["date_from"] = meta.date_from
        if meta.date_to:
            base_entry["date_to"] = meta.date_to
        # sheet 来自 LLM 判断的实际工作表名
        base_entry["sheet"] = meta.sheet_name
        if meta.notes:
            base_entry["notes"] += f" LLM: {meta.notes}"

        # 用 LLM 提取的 bank_name 重新生成 ID
        if meta.bank_name:
            bank_short = _guess_bank_short(meta.bank_name)
            year = base_entry.get("date_from", "")[:4] or str(project_info.get("audit_year", 2022))
            type_short = "bank" if base_entry["type"] == "bank_statement" else base_entry["type"]
            base_entry["id"] = f"{bank_short}_{type_short}_{year}_{meta.sheet_name}"

        relevant_results.append(base_entry)

    # 记录被 LLM 跳过的工作表
    if skipped_sheets:
        skip_msg = f"以下工作表被 LLM 判定为不相关，已排除: {', '.join(skipped_sheets)}"
        warnings.append(skip_msg)
        _log.info(skip_msg)

    # ── 重组最终结果：非目标文件（working_paper）保持原样 + LLM 筛选后的相关条目 ──
    target_paths = {r["path"] for _, r in target_entries}
    final_results = [
        r for r in results if r["path"] not in target_paths
    ] + relevant_results

    # ── 清除 _identify_files_local 产生的旧账号警告（LLM 可能已补全） ──
    warnings = [w for w in warnings if "未能识别银行账号" not in w]

    # ── 账号回填（按目录+银行分组）──
    bank_entries = [e for e in final_results if e["type"] == "bank_statement"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in bank_entries:
        key = (Path(e["path"]).parent.name, e["bank_name"])
        groups.setdefault(key, []).append(e)
    for (_dir, _bank), entries in groups.items():
        found_account = next((e["account_no"] for e in entries if e["account_no"]), "")
        if found_account:
            for e in entries:
                if not e["account_no"]:
                    e["account_no"] = found_account
                    e["notes"] += f"（账号从同组文件回填: {found_account}）"

    # ── 检查 bank 文件是否有空账号 ──
    for entry in final_results:
        if entry["type"] == "bank_statement" and not entry["account_no"]:
            msg = f"银行流水 {entry['name']}[{entry.get('sheet', '')}] 未能识别银行账号（LLM 也未提取到），请检查 task.yml 并手动补充"
            warnings.append(msg)
            _log.warning(msg)

    return final_results, warnings, usage


# ── 废弃的同步 LLM 入口（保留向后兼容） ─────────────────────────

def identify_files_with_llm(
    files: list[dict[str, Any]],
    llm_config: dict[str, Any],
    project_info: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """[已废弃] 使用 identify_files_with_llm_async 替代。保留仅供向后兼容。"""
    results, _warnings = _identify_files_local(files, project_info or {})
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
    """根据文件识别结果生成 task.yml 配置字典。

    默认值从 config/config.blm.detect.yaml 读取，不再硬编码。

    Returns:
        可直接 yaml.dump 的配置字典
    """
    detect_cfg = _load_detect_config()

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
        elif item["type"] == "ledger":
            entry["year"] = (
                int(item.get("date_from", "2022")[:4])
                if item.get("date_from")
                else project_info.get("audit_year", 2022)
            )
            ledger_items.append(entry)

    # 匹配参数：从外部配置文件读取
    match_cfg_defaults = detect_cfg.get("matching_defaults", {})
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
    if match_cfg_defaults:
        match_cfg.update(match_cfg_defaults)
    if matching_defaults:
        match_cfg.update(matching_defaults)

    # 底稿参数：从外部配置文件读取
    wp_defaults = detect_cfg.get("working_paper_defaults", {})
    if working_paper_defaults:
        # 调用者传入的覆盖配置文件中的默认值
        wp_defaults = {**wp_defaults, **working_paper_defaults}

    output_dir_relative = f"outputs/{project_info.get('name', '')}/{project_info.get('task', 'bank_ledger_match')}"
    output_filename = wp_defaults.get("output_filename", "资金流水专项核查工作底稿-自动填报.xlsm")

    wp_cfg = {
        "enabled": True,
        "fill_only_when_fully_matched": False,
        "output_file": f"{output_dir_relative}/working_paper/{output_filename}",
        "wp03": wp_defaults.get("wp03", {
            "enabled": True,
            "sheet": "WP-03",
            "start_row": 6,
            "min_amount": 70000,
            "columns": {
                "entity": "B", "bank_date": "C", "bank_debit": "D",
                "bank_credit": "E", "counterparty": "F", "ledger_date": "G",
                "voucher_no": "H", "ledger_summary": "I",
                "ledger_counterparty": "J", "ledger_debit": "K", "ledger_credit": "L",
            },
        }),
        "wp02": wp_defaults.get("wp02", {
            "enabled": True,
            "sheet": "WP-02",
        }),
    }

    # working paper 模板路径：从目录检测到的底稿文件取第一个
    wp_items = [i for i in identifications if i["type"] == "working_paper"]
    template_path = wp_items[0]["path"] if wp_items else project_info.get("template_path", "")

    # ── 校验：收集缺失字段信息，供前端结构化展示 ──
    incomplete_entries = []
    for item in identifications:
        missing = []
        if item["type"] in ("bank_statement", "ledger"):
            if not item.get("bank_name"):
                missing.append("bank_name")
            if not item.get("account_no"):
                missing.append("account_no")
        if missing:
            incomplete_entries.append({
                "id": item["id"],
                "name": item.get("name", ""),
                "sheet": item.get("sheet", ""),
                "type": item["type"],
                "missing_fields": missing,
            })

    config = {
        "project": {
            "client_name": project_info.get("client_name", ""),
            "client_short_name": project_info.get("name", ""),
            "audit_year": project_info.get("audit_year", datetime.now().year),
            "template_path": template_path,
            "output_dir": output_dir_relative,
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
            "directory_based": True,
            "files_scanned": len(identifications),
        },
        # 校验信息：缺失关键字段的条目
        "_validation": {
            "incomplete_entries": incomplete_entries,
            "has_issues": len(incomplete_entries) > 0,
        },
    }

    return config


def save_task_config(config: dict[str, Any], path: str | Path) -> Path:
    """将 task.yml 写入文件。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in config.items() if k not in ("_meta", "_validation")}
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
