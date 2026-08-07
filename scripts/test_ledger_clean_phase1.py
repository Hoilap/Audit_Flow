"""Phase 1 序时账清洗结果测试模块。

功能:
    1. 本地直接调用 clean_ledger_to_csv 执行序时账清洗（不依赖后端服务）
       - 测试产物重命名为 ledger_entries_phase1test_<时间戳>.csv，
         与正常流程产物（ledger_entries.csv）在命名上区分；
         若运行前已存在 ledger_entries.csv，会先备份、测试结束后原样恢复。
       - llm_regenerate / llm_init 会设置 _force_regenerate=True，
         跳过解析脚本缓存、强制重新调用 LLM（与后端 routes_blm 行为一致）。
    2. 解析器诊断（排查 account_no/bank_name 为空问题）:
       - 输出每个数据源实际使用的解析脚本路径、是缓存复用还是本次新生成
       - 脚本修改时间早于提示词代码（llm_cleaner.py）时给出警告
       - 列出脚本内部 bank_name/account_no 的赋值逻辑代码行
    3. --show-sample: 打印 LLM 生成解析器时实际收到的样本数据，
       用于判断"提示词是否看得到银行信息"（样本里看不到 → 提示词无法解决）。
    4. 对清洗产物做全面验证:
       - 结构性检查（FAIL）: schema、flow 合法性、金额非负、日期 ISO 格式、
         借贷方向与 flow 一致性、NaN 残留、必填字段缺失、
         account_no/bank_name 全空（违反"宁多勿缺"填充规则）
       - 前瞻性检查（WARNING）: account_no/bank_name 是否为科目路径等不规范格式
         （这些值后续由 Phase 2 规范化处理）、Phase 1 是否漏填关键信息
       - 统计信息: 每个数据源的行数、账号数、日期范围
    5. 退出码: 0 = 无 FAIL，1 = 存在 FAIL，2 = 执行出错

用法:
    python scripts/test_ledger_clean_phase1.py
    python scripts/test_ledger_clean_phase1.py --customer 桂平金山 --task bank_ledger_match
    python scripts/test_ledger_clean_phase1.py --parser llm_regenerate   # 跳过脚本缓存，强制重新调 LLM
    python scripts/test_ledger_clean_phase1.py --show-sample             # 预览 LLM 收到的样本数据
    python scripts/test_ledger_clean_phase1.py --csv outputs/桂平金山/bank_ledger_match/clean/ledger_entries.csv
    python scripts/test_ledger_clean_phase1.py --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml as yaml_lib

# Windows GBK 控制台下强制 UTF-8 输出，避免 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 项目根目录（脚本位于 scripts/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from audit_workflow.bank_ledger_match.cleaners import (  # noqa: E402
    LEDGER_COLUMNS,
    clean_ledger_to_csv,
    enabled_items,
)
from audit_workflow.bank_ledger_match.file_detector import merge_llm_into_full_config  # noqa: E402

DEFAULT_CUSTOMER = "桂平金山"
DEFAULT_TASK = "bank_ledger_match"
DEFAULT_PARSER = "llm"  # 与前端默认一致，会映射为 llm_ledger

# 测试产物文件名前缀（与正常流程产物 ledger_entries.csv 区分）
TEST_FILE_PREFIX = "ledger_entries_phase1test_"

# 可被 Phase 2 规范化的解析器值 → 实际 cleaner 名称
_LLM_PARSER_NAMES = {"llm", "llm_regenerate", "llm_init", "llm_step", "llm_step_once", "llm_step_all"}

# 日期 ISO 格式 (YYYY-MM-DD)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 科目路径特征（Phase 2 会规范化的 bank_name 特征）
_SUBJECT_PATH_MARKERS = ("｜", "|", "银行存款")


# ------------------------------------------------------------
# 配置加载（与 desktop.routes_blm._build_full_config 等价，不依赖后端）
# ------------------------------------------------------------

def _query_db_task_paths(customer: str, task: str) -> dict | None:
    """从 projects.db 查询 task_configs 表，返回 {task_yml_path, llm_yml_path}。"""
    db_path = PROJECT_ROOT / "projects.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT task_yml_path, llm_yml_path FROM task_configs "
            "WHERE customer_name=? AND task_name=?",
            (customer, task),
        ).fetchone()
        conn.close()
        if row and row["task_yml_path"]:
            return {
                "task_yml_path": row["task_yml_path"],
                "llm_yml_path": row["llm_yml_path"] or str(PROJECT_ROOT / "config" / "config.llm.yml"),
            }
    except sqlite3.Error as e:
        print(f"⚠ 查询 projects.db 失败（{e}），回退默认路径")
    return None


def _load_full_config(customer: str, task: str) -> dict:
    """加载 llm.yml + matching.yml + task.yml，合并为完整配置。"""
    default_llm_path = PROJECT_ROOT / "config" / "config.llm.yml"
    default_matching_path = PROJECT_ROOT / "config" / "config.matching.yml"

    paths = _query_db_task_paths(customer, task)
    if paths is None:
        paths = {
            "task_yml_path": str(PROJECT_ROOT / "outputs" / customer / task / "task.yml"),
            "llm_yml_path": str(default_llm_path),
        }

    # 加载 llm.yml
    llm_cfg: dict = {}
    if os.path.exists(paths["llm_yml_path"]):
        with open(paths["llm_yml_path"], "r", encoding="utf-8") as f:
            llm_cfg = yaml_lib.safe_load(f) or {}

    # 合并 matching.yml 到 llm_cfg["llm"]["matching"]
    if default_matching_path.exists():
        with open(default_matching_path, "r", encoding="utf-8") as f:
            matching_cfg = yaml_lib.safe_load(f) or {}
        llm_cfg.setdefault("llm", {})
        llm_cfg["llm"]["matching"] = matching_cfg.get("matching", {})

    # 加载 task.yml
    task_cfg: dict = {}
    if os.path.exists(paths["task_yml_path"]):
        with open(paths["task_yml_path"], "r", encoding="utf-8") as f:
            task_cfg = yaml_lib.safe_load(f) or {}
    else:
        print(f"❌ task.yml 不存在: {paths['task_yml_path']}")
        sys.exit(2)

    cfg = merge_llm_into_full_config(task_cfg, llm_cfg)
    print(f"  配置: task.yml = {paths['task_yml_path']}")
    return cfg


def _normalize_parser_for_ledger(parser: str) -> str:
    if parser in _LLM_PARSER_NAMES:
        return "llm_ledger"
    return parser


# ------------------------------------------------------------
# 验证器
# ------------------------------------------------------------

class Verifier:
    """对 Phase 1 清洗后的 CSV 做结构化验证，收集 FAIL / WARNING。"""

    def __init__(self, df: pd.DataFrame, cfg: dict):
        self.df = df
        self.cfg = cfg
        self.fails: list[str] = []
        self.warnings: list[str] = []
        self.phase2_outlook: dict[str, list[str]] = {}  # 列名 -> 将被 Phase 2 处理的唯一值

    # ── 数值解析 ──
    @staticmethod
    def _to_float(val: str) -> float | None:
        try:
            return float(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    # ── 检查项 ──
    def check_schema(self) -> None:
        missing = [c for c in LEDGER_COLUMNS if c not in self.df.columns]
        extra = [c for c in self.df.columns if c not in LEDGER_COLUMNS]
        if missing:
            self.fails.append(f"缺少列: {missing}")
        if extra:
            self.warnings.append(f"多余列: {extra}")
        self._schema_missing = missing

    def check_nan_literals(self) -> None:
        """检查是否有 'nan'/'None' 字符串残留（pandas NaN 污染）。"""
        mask = self.df.isin(["nan", "None", "NaN", "N/A", "NoneType"]).any(axis=1)
        cnt = int(mask.sum())
        if cnt > 0:
            sample = self.df.loc[mask].head(3).to_dict("records")
            self.fails.append(f"发现 {cnt} 行含 'nan'/'None' 字面量（NaN 污染），示例: {sample}")

    def check_required_fields(self) -> None:
        """必填字段: entry_id, source_id, transaction_date, flow, amount。"""
        for col in ("entry_id", "source_id"):
            empty = int((self.df[col].astype(str).str.strip() == "").sum())
            if empty > 0:
                self.fails.append(f"字段 {col} 有 {empty} 行为空")
        for col in ("transaction_date", "flow", "amount"):
            empty = int((self.df[col].astype(str).str.strip() == "").sum())
            if empty > 0:
                self.fails.append(f"必填字段 {col} 有 {empty} 行为空")

    def check_flow(self) -> None:
        bad = self.df[~self.df["flow"].isin(["in", "out"])]
        if len(bad) > 0:
            vals = bad["flow"].value_counts().to_dict()
            self.fails.append(f"flow 非法值（应为 in/out）: {vals}")

    def check_amounts(self) -> None:
        """amount/ledger_debit/ledger_credit 必须非负且可解析为数字。"""
        for col in ("amount", "ledger_debit", "ledger_credit"):
            neg_cnt = 0
            bad_cnt = 0
            for val in self.df[col].astype(str):
                if val.strip() == "":
                    continue
                num = self._to_float(val)
                if num is None:
                    bad_cnt += 1
                elif num < 0:
                    neg_cnt += 1
            if bad_cnt > 0:
                self.fails.append(f"字段 {col} 有 {bad_cnt} 个无法解析的数字")
            if neg_cnt > 0:
                self.fails.append(f"字段 {col} 有 {neg_cnt} 个负数值（应输出 0）")

    def check_dates(self) -> None:
        bad = self.df[~self.df["transaction_date"].astype(str).str.match(_DATE_RE.pattern)]
        if len(bad) > 0:
            sample = bad["transaction_date"].head(5).tolist()
            self.fails.append(
                f"transaction_date 有 {len(bad)} 个非 ISO(YYYY-MM-DD) 值，示例: {sample}"
            )

    def check_flow_direction(self) -> None:
        """flow 与借贷方向一致性: in → 仅借方>0；out → 仅贷方>0。"""
        df = self.df
        debit = pd.to_numeric(df["ledger_debit"], errors="coerce").fillna(0.0)
        credit = pd.to_numeric(df["ledger_credit"], errors="coerce").fillna(0.0)

        in_bad = (df["flow"] == "in") & ~((debit > 0) & (credit == 0))
        out_bad = (df["flow"] == "out") & ~((credit > 0) & (debit == 0))
        bad = int(in_bad.sum()) + int(out_bad.sum())
        if bad > 0:
            self.fails.append(
                f"{bad} 行借贷方向与 flow 不一致（in 应仅借方>0，out 应仅贷方>0）"
            )

        # amount 应等于非零侧金额
        expect = debit.where(df["flow"] == "in", credit)
        amount = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        mismatch = int(((amount - expect).abs() > 0.01).sum())
        if mismatch > 0:
            self.fails.append(f"{mismatch} 行 amount 与非零侧金额不一致（应取借方或贷方非零值）")

    def check_account_fill(self) -> None:
        """Phase 1 填充规则检查: account_no/bank_name 尽量不为空。"""
        df = self.df
        total = len(df)
        acct_empty = int((df["account_no"].astype(str).str.strip() == "").sum())
        bank_empty = int((df["bank_name"].astype(str).str.strip() == "").sum())

        # 两列 100% 为空 = 违反 Phase 1 "宁多勿缺"填充规则（FAIL）
        if total > 0 and acct_empty == total and bank_empty == total:
            self.fails.append(
                "account_no 与 bank_name 全部为空，违反 Phase 1『宁多勿缺』填充规则。排查步骤: "
                "(1) 看上方【解析器诊断】，若脚本为缓存复用且早于提示词代码 → 用 --parser llm_regenerate 重新生成; "
                "(2) 若脚本为新生成仍为空 → 用 --show-sample 查看银行信息在哪一列、LLM 样本能否看到; "
                "(3) 若数据中确实无任何银行信息 → 需在 task.yml 的 ledger inputs 补充 bank_name/account_no"
            )
        else:
            if acct_empty > 0:
                self.warnings.append(f"account_no 有 {acct_empty} 行为空（Phase 1 应尽量从科目列填入原始信息）")
            if bank_empty > 0:
                self.warnings.append(f"bank_name 有 {bank_empty} 行为空（Phase 1 应尽量从科目列填入原始信息）")

        # 三列全空 = 关键信息缺失
        key_cols = ["account_no", "bank_name", "subject"]
        all_empty = df[key_cols].apply(
            lambda r: all(str(v).strip() == "" for v in r), axis=1
        )
        cnt = int(all_empty.sum())
        if cnt > 0:
            srcs = df.loc[all_empty, "source_id"].unique().tolist()
            self.fails.append(f"有 {cnt} 行 account_no/bank_name/subject 全为空（关键信息缺失），来源: {srcs[:5]}")

    def check_phase2_outlook(self) -> None:
        """统计会被 Phase 2 规范化的值（WARNING + 统计）。"""
        df = self.df

        # account_no 非纯数字 → Phase 2 处理
        bad_acct = df["account_no"][
            (df["account_no"].astype(str).str.strip() != "")
            & ~(df["account_no"].astype(str).str.replace(" ", "", regex=False).str.isdigit())
        ]
        unique_acct = sorted(bad_acct.unique().tolist())
        if unique_acct:
            self.phase2_outlook["account_no"] = unique_acct
            self.warnings.append(
                f"account_no 有 {len(unique_acct)} 个唯一非数字值（将被 Phase 2 规范化）: "
                f"{json.dumps(unique_acct[:5], ensure_ascii=False)}..."
            )

        # bank_name 含科目路径特征 → Phase 2 处理
        bad_bank = df["bank_name"][
            df["bank_name"].astype(str).str.contains("|".join(
                re.escape(m) for m in _SUBJECT_PATH_MARKERS
            ), regex=True)
        ]
        unique_bank = sorted(bad_bank.unique().tolist())
        if unique_bank:
            self.phase2_outlook["bank_name"] = unique_bank
            self.warnings.append(
                f"bank_name 有 {len(unique_bank)} 个唯一科目路径值（将被 Phase 2 规范化）: "
                f"{json.dumps(unique_bank[:5], ensure_ascii=False)}..."
            )

        # Phase 2 需要 task.yaml 的 bank_statements 作为参考
        bs_count = len(self.cfg.get("inputs", {}).get("bank_statements", []))
        if self.phase2_outlook and bs_count == 0:
            self.warnings.append("存在待规范化值，但 task.yaml 中没有 bank_statements，Phase 2 将无法参考账号列表")

    def report_stats(self) -> str:
        """统计信息汇总。"""
        df = self.df
        lines = []
        lines.append(f"  总行数: {len(df)}")
        lines.append(f"  schema: {len(self.df.columns)} 列，"
                     f"{'完整' if not getattr(self, '_schema_missing', []) else '缺少列: ' + str(self._schema_missing)}")
        lines.append(f"  数据源数: {df['source_id'].nunique()}")
        lines.append(f"  flow 分布: {df['flow'].value_counts().to_dict()}")

        # 按数据源统计
        for src, grp in df.groupby("source_id"):
            accts = grp["account_no"].astype(str).str.strip()
            accts = accts[accts != ""]
            dates = grp["transaction_date"].astype(str)
            dates = dates[dates.str.match(_DATE_RE.pattern)]
            dmin, dmax = ("-", "-")
            if len(dates) > 0:
                dmin, dmax = dates.min(), dates.max()
            lines.append(
                f"    ▸ {src}: {len(grp)} 行 | 账号 {accts.nunique()} 个 | 日期 {dmin} ~ {dmax}"
            )
        return "\n".join(lines)


# ------------------------------------------------------------
# 解析器诊断（排查 account_no/bank_name 为空问题）
# ------------------------------------------------------------

# 每个数据源实际使用的解析脚本（由 _install_parser_spy 收集）
_PARSER_USAGE: list = []


def _install_parser_spy() -> None:
    """监听 ensure_llm_ledger_parser，记录每个数据源实际使用的解析脚本。

    parse_llm_ledger 内部是函数级 import，因此补丁打在模块属性上即可生效。
    """
    from audit_workflow.bank_ledger_match import llm_cleaner as llm_mod

    orig = llm_mod.ensure_llm_ledger_parser

    def spy(config, item):
        path = orig(config, item)
        _PARSER_USAGE.append({
            "item_id": item.get("id", "?"),
            "parser_path": Path(str(path)),
            "force": bool(config.get("_force_regenerate", False)),
            "regenerated": False,
        })
        return path

    llm_mod.ensure_llm_ledger_parser = spy


def _extract_bank_fill_lines(parser_path: Path, limit: int = 12) -> list:
    """提取解析脚本内部 bank_name/account_no 的赋值行（用于人工判断填充逻辑）。"""
    try:
        lines = parser_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    hits = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("#"):
            continue
        if re.search(r"\b(bank_name|account_no)\s*=(?!=)", s):
            hits.append((i, s[:110]))
        if len(hits) >= limit:
            break
    return hits


def report_parser_diagnostics(run_start_ts: float) -> None:
    """输出解析脚本诊断: 缓存/新生成、脚本新旧、bank_name/account_no 赋值逻辑。"""
    print("\n【解析器诊断】")
    if not _PARSER_USAGE:
        print("  （未检测到 LLM 解析器调用：非 LLM 解析器或 --csv 模式）")
        return

    prompt_file = PROJECT_ROOT / "audit_workflow" / "bank_ledger_match" / "llm_cleaner.py"
    prompt_mtime = prompt_file.stat().st_mtime if prompt_file.exists() else None

    for u in _PARSER_USAGE:
        p = u["parser_path"]
        try:
            mt = p.stat().st_mtime
        except OSError:
            print(f"  ▸ [{u['item_id']}] {p}（文件不存在）")
            continue
        u["regenerated"] = mt >= run_start_ts - 1
        mt_s = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
        status = "本次新生成" if u["regenerated"] else "缓存复用"
        print(f"  ▸ [{u['item_id']}]")
        print(f"      脚本: {p}")
        print(f"      状态: {status} | 脚本修改时间: {mt_s}")
        if not u["regenerated"] and prompt_mtime and mt < prompt_mtime:
            pt_s = datetime.fromtimestamp(prompt_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"      ⚠ 脚本早于提示词代码（llm_cleaner.py 更新于 {pt_s}），")
            print("        可能不含最新的 bank_name/account_no 填充规则（提示词改动对缓存脚本无效）")
            print("        → 若结果为空，用 --parser llm_regenerate 强制重新生成")
        fill_lines = _extract_bank_fill_lines(p)
        if fill_lines:
            print("      脚本内 bank_name/account_no 赋值逻辑:")
            for i, s in fill_lines:
                print(f"        L{i}: {s}")
        else:
            print("      ⚠ 脚本中未找到 bank_name/account_no 赋值语句（这是输出为空的直接原因）")


def show_llm_sample(cfg: dict) -> None:
    """打印 LLM 生成解析器时实际收到的样本数据。

    排查"是不是提示词的问题"的关键一步: 如果银行信息（科目路径、账号）
    在样本里根本看不到，那么无论提示词怎么写都无法提取。
    """
    from audit_workflow.bank_ledger_match.config import resolve_path
    from audit_workflow.bank_ledger_match.utils import read_excel_headerless
    from audit_workflow.util import build_smart_sample

    items = enabled_items(cfg.get("inputs", {}).get("ledgers", []))
    print("\n【LLM 样本预览】（LLM 生成解析脚本时看到的数据，决定它能否'看到'银行信息）")
    for item in items:
        src = resolve_path(cfg, item.get("path", ""))
        if src is None or not Path(src).exists():
            print(f"  ▸ [{item.get('id')}] 文件不存在: {item.get('path')}")
            continue
        sheet, df = read_excel_headerless(Path(src), item.get("sheet"))
        sample = build_smart_sample(df)
        print(
            f"\n  ▸ [{item.get('id')}] sheet={sheet} | "
            f"config: bank_name={item.get('bank_name', '')!r} account_no={item.get('account_no', '')!r}"
        )
        text = json.dumps(sample, ensure_ascii=False, indent=1)
        if len(text) > 3000:
            text = text[:3000] + "\n...(截断)"
        print("   " + text.replace("\n", "\n   "))


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def run_clean(cfg: dict, parser: str) -> tuple:
    """执行 Phase 1 清洗，返回 (测试产物 CSV 路径, 运行开始时间戳)。

    测试产物会从 ledger_entries.csv 重命名为
    ledger_entries_phase1test_<时间戳>.csv，与正常流程产物区分。
    """
    # 与后端 routes_blm 保持一致: llm_regenerate/llm_init 跳过解析脚本缓存
    if parser in ("llm_regenerate", "llm_init"):
        cfg["_force_regenerate"] = True
        print(f"  ⚠ parser={parser} → 已设置 _force_regenerate=True（跳过脚本缓存，重新调 LLM）")

    effective = _normalize_parser_for_ledger(parser)
    print(f"\n▶ 执行 Phase 1 序时账清洗（parser={effective}）...")

    _install_parser_spy()
    run_start_ts = datetime.now().timestamp()
    ledger_path = Path(str(clean_ledger_to_csv(cfg, parser=effective)))

    # 打印 pipeline 自带的数据质量警告
    warnings = cfg.get("_cleaning", {}).get("warnings", [])
    if warnings:
        print(f"\n⚠ pipeline 自带数据质量警告（{len(warnings)} 条）:")
        for w in warnings[:20]:
            print(f"  - {w.get('source_file', '?')}: {w.get('message', w)}")

    # 重命名为测试专属文件名，与正常流程产物 ledger_entries.csv 区分
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_path = ledger_path.with_name(f"{TEST_FILE_PREFIX}{ts}.csv")
    shutil.move(str(ledger_path), str(test_path))
    print(f"\n  ℹ 测试产物: {test_path.name}")
    print("    （测试文件已用专属命名，与正常流程产物 ledger_entries.csv 区分；原有正常产物已恢复）")
    return test_path, run_start_ts


def validate_csv(csv_path: Path, cfg: dict, verbose: bool = False) -> int:
    """验证清洗产物，返回退出码（0=无FAIL, 1=有FAIL）。"""
    print(f"\n▶ 验证 CSV: {csv_path}")

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        print("❌ FAIL: CSV 不存在或为空")
        return 1

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    verifier = Verifier(df, cfg)

    # 执行所有检查
    verifier.check_schema()
    verifier.check_nan_literals()
    verifier.check_required_fields()
    verifier.check_flow()
    verifier.check_amounts()
    verifier.check_dates()
    verifier.check_flow_direction()
    verifier.check_account_fill()
    verifier.check_phase2_outlook()

    # ── 输出报告 ──
    print("\n" + "=" * 60)
    print("Phase 1 清洗结果报告")
    print("=" * 60)

    print(f"\n【统计信息】")
    print(verifier.report_stats())

    print(f"\n【FAIL — 结构性问题】({len(verifier.fails)})")
    if verifier.fails:
        for f in verifier.fails:
            print(f"  ❌ {f}")
    else:
        print("  ✅ 无结构性问题")

    print(f"\n【WARNING — 可修复/轻微问题】({len(verifier.warnings)})")
    if verifier.warnings:
        for w in verifier.warnings:
            print(f"  ⚠ {w}")
    else:
        print("  ✅ 无警告")

    # Phase 2 前瞻统计
    if verifier.phase2_outlook:
        total_bad_rows = 0
        for col, vals in verifier.phase2_outlook.items():
            bad_rows = int(
                df[col].isin(vals).sum()
            )
            total_bad_rows += bad_rows
            print(f"\n  📋 Phase 2 将处理 {col}: {len(vals)} 个唯一值 / {bad_rows} 行")
        print(f"  📋 合计 {total_bad_rows} 行将在 Phase 2 被规范化")
    else:
        print("\n  📋 无待 Phase 2 规范化的值")

    # 数据质量警告（pipeline 自带的）
    warnings = cfg.get("_cleaning", {}).get("warnings", [])
    if warnings:
        print(f"\n【pipeline 数据质量警告】({len(warnings)})")
        for w in warnings[:10]:
            print(f"  ⚠ {w.get('source_file', '?')}: {w.get('message', w)}")

    # ── 结论 ──
    print("\n" + "=" * 60)
    if verifier.fails:
        print(f"❌ 结论: FAIL（{len(verifier.fails)} 个结构性问题）")
        return 1
    print(f"✅ 结论: PASS（结构性问题 0，警告 {len(verifier.warnings)} 条）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 序时账清洗结果测试")
    parser.add_argument("--customer", default=DEFAULT_CUSTOMER, help="客户名")
    parser.add_argument("--task", default=DEFAULT_TASK, help="任务名")
    parser.add_argument("--parser", default=DEFAULT_PARSER,
                        help="解析器: llm | llm_regenerate | llm_init | llm_step | "
                             "llm_step_once | llm_step_all | llm_ledger | xinjiyuan_bank_ledger")
    parser.add_argument("--csv", default="", help="指定已有 CSV 路径，跳过清洗直接验证")
    parser.add_argument("--show-sample", action="store_true",
                        help="预览 LLM 生成解析器时收到的样本数据（排查提示词/列映射问题）")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志（含 LLM 生成过程）")
    args = parser.parse_args()

    print("=" * 60)
    print("Phase 1 序时账清洗测试")
    print("=" * 60)
    print(f"  客户: {args.customer}")
    print(f"  任务: {args.task}")
    print(f"  解析器: {args.parser} → {_normalize_parser_for_ledger(args.parser)}")

    try:
        cfg = _load_full_config(args.customer, args.task)
        if args.show_sample:
            show_llm_sample(cfg)
        if not args.csv:
            # 保护正常流程产物: 若已存在 ledger_entries.csv，先备份，测试后恢复
            from audit_workflow.bank_ledger_match.config import output_dir as _output_dir
            normal_csv = _output_dir(cfg) / "clean" / "ledger_entries.csv"
            backup_csv = None
            if normal_csv.exists():
                backup_csv = normal_csv.with_name(normal_csv.name + ".phase1test_bak")
                shutil.copy2(str(normal_csv), str(backup_csv))
            try:
                csv_path, run_start_ts = run_clean(cfg, args.parser)
            finally:
                if backup_csv is not None:
                    if backup_csv.exists():
                        shutil.copy2(str(backup_csv), str(normal_csv))
                        backup_csv.unlink()
                    elif not normal_csv.exists():
                        print("  ⚠ 备份文件丢失且正常产物不存在，请检查 clean/ 目录")
            report_parser_diagnostics(run_start_ts)
        else:
            csv_path = Path(args.csv)
        return validate_csv(csv_path, cfg, verbose=args.verbose)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ 执行出错: {type(e).__name__}: {e}")
        if args.verbose:
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
