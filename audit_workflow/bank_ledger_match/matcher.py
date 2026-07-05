from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import output_dir
from .utils import amount_to_cents, join_unique, parse_amount, parse_date, text, text_similarity


@dataclass
class Record:
    id: str
    kind: str
    row: dict[str, Any]
    amount_cents: int
    trans_date: date


def write_monthly_flow_check(config: dict[str, Any]) -> Path:
    """从当前 clean 数据重新生成 monthly_flow_check.csv（供 check 端点调用）。"""
    clean_dir = output_dir(config) / "clean"
    bank_path = clean_dir / "bank_transactions.csv"
    ledger_path = clean_dir / "ledger_entries.csv"
    bank_df = _load_csv(bank_path)
    ledger_df = _load_csv(ledger_path)
    out = output_dir(config) / "matches"
    out.mkdir(parents=True, exist_ok=True)
    return _write_monthly_flow_check(bank_df, ledger_df, config, out)


def match_to_csv(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    clean_dir = output_dir(config) / "clean"
    bank_path = clean_dir / "bank_transactions.csv"
    ledger_path = clean_dir / "ledger_entries.csv"
    bank_df = _load_csv(bank_path)
    ledger_df = _load_csv(ledger_path)
    out = output_dir(config) / "matches"
    out.mkdir(parents=True, exist_ok=True)
    _write_monthly_flow_check(bank_df, ledger_df, config, out)
    matches_df, unmatched_bank_df, unmatched_ledger_df = match_transactions(bank_df, ledger_df, config)

    matches_path = out / "matches.csv"
    unmatched_bank_path = out / "unmatched_bank.csv"
    unmatched_ledger_path = out / "unmatched_ledger.csv"
    matches_path = _safe_to_csv(matches_df, matches_path)
    unmatched_bank_path = _safe_to_csv(unmatched_bank_df, unmatched_bank_path)
    unmatched_ledger_path = _safe_to_csv(unmatched_ledger_df, unmatched_ledger_path)
    return matches_path, unmatched_bank_path, unmatched_ledger_path


def match_transactions(
    bank_df: pd.DataFrame, ledger_df: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cfg = config.get("matching", {})
    amount_tol = amount_to_cents(cfg.get("amount_tolerance", 0.01))
    one_to_one_date_tol = int(cfg.get("date_tolerance_days", 3))
    group_date_tol = int(cfg.get("group_date_tolerance_days", one_to_one_date_tol))
    one_to_one_min = float(cfg.get("one_to_one_min_score", 0.58))
    group_max_size = int(cfg.get("group_max_size", 6))
    pool_limit = int(cfg.get("group_candidate_pool_limit", 24))

    bank_records = _records(bank_df, "bank", "txn_id")
    ledger_records = _records(ledger_df, "ledger", "entry_id")
    bank_by_id = {r.id: r for r in bank_records}
    ledger_by_id = {r.id: r for r in ledger_records}

    used_bank: set[str] = set()
    used_ledger: set[str] = set()
    groups: list[dict[str, Any]] = []

    # 检查是否为完全重新匹配模式（llm_regenerate）
    llm_cfg = config.get("matching", {}).get("llm", {})
    full_rematch = llm_cfg.get("enabled", False) and llm_cfg.get("full_rematch", False)
    if full_rematch:
        print("完全重新匹配模式 (llm_regenerate)：跳过规则匹配，仅使用 LLM 进行匹配")

    if not full_rematch:
        # Pass 0: 手续费聚合预匹配
        _fee_aggregation_match(bank_records, ledger_records, used_bank, used_ledger, amount_tol, config, groups)

        # Pass 0.5: 跨账号调拨匹配
        _cross_account_transfer_match(bank_records, ledger_records, used_bank, used_ledger, amount_tol, config, groups)

        pair_candidates: list[tuple[float, Record, Record]] = []
        for bank in bank_records:
            for ledger in ledger_records:
                if not _candidate_ok(bank, ledger, amount_tol, one_to_one_date_tol, config):
                    continue
                score = _pair_score(bank, ledger, amount_tol, one_to_one_date_tol)
                if score >= one_to_one_min:
                    pair_candidates.append((score, bank, ledger))
        pair_candidates.sort(key=lambda x: x[0], reverse=True)

        for score, bank, ledger in pair_candidates:
            if bank.id in used_bank or ledger.id in used_ledger:
                continue
            groups.append(_make_group(config, "one_to_one", [bank], [ledger], score, len(groups) + 1))
            used_bank.add(bank.id)
            used_ledger.add(ledger.id)

        # Pass 2: 1:N matching (one bank → many ledger)
        for bank in bank_records:
            if bank.id in used_bank:
                continue
            candidates = [
                ledger
                for ledger in ledger_records
                if ledger.id not in used_ledger
                and _candidate_ok(
                    bank,
                    ledger,
                    amount_tol=max(amount_tol, bank.amount_cents),
                    date_tol=group_date_tol,
                    config=config,
                    ignore_amount=True,
                )
            ]
            candidates = _rank_group_candidates(bank, candidates, amount_tol, group_date_tol)[:pool_limit]
            subset = _find_subset(candidates, bank.amount_cents, amount_tol, group_max_size)
            if subset:
                score = _group_score([bank], subset, amount_tol, group_date_tol)
                groups.append(_make_group(config, "one_to_many", [bank], subset, score, len(groups) + 1))
                used_bank.add(bank.id)
                used_ledger.update(item.id for item in subset)

        # Pass 3: N:1 matching (many bank → one ledger)
        for ledger in ledger_records:
            if ledger.id in used_ledger:
                continue
            candidates = [
                bank
                for bank in bank_records
                if bank.id not in used_bank
                and _candidate_ok(
                    bank,
                    ledger,
                    amount_tol=max(amount_tol, ledger.amount_cents),
                    date_tol=group_date_tol,
                    config=config,
                    ignore_amount=True,
                )
            ]
            candidates = _rank_group_candidates(ledger, candidates, amount_tol, group_date_tol)[:pool_limit]
            subset = _find_subset(candidates, ledger.amount_cents, amount_tol, group_max_size)
            if subset:
                score = _group_score(subset, [ledger], amount_tol, group_date_tol)
                groups.append(_make_group(config, "many_to_one", subset, [ledger], score, len(groups) + 1))
                used_bank.update(item.id for item in subset)
                used_ledger.add(ledger.id)

        for group in _daily_many_to_many(bank_records, ledger_records, used_bank, used_ledger, amount_tol, config):
            bank_group, ledger_group = group
            score = _group_score(bank_group, ledger_group, amount_tol, group_date_tol)
            groups.append(_make_group(config, "many_to_many", bank_group, ledger_group, score, len(groups) + 1))
            used_bank.update(item.id for item in bank_group)
            used_ledger.update(item.id for item in ledger_group)

        # Pass 4: 清理残余 — 对少量未匹配记录使用宽松规则匹配
        _cleanup_small_unmatched(bank_records, ledger_records, used_bank, used_ledger, amount_tol, config, groups)

        print(f"规则匹配完成: {len(groups)} 条匹配, 未匹配银行流水 {len(bank_records) - len(used_bank)} 条, 未匹配序时账 {len(ledger_records) - len(used_ledger)} 条")

    # LLM 辅助匹配（由前端 parser 参数控制）
    llm_cfg = config.get("matching", {}).get("llm", {})
    if llm_cfg.get("enabled", False):
        from .llm_matcher import apply_llm_supplemental_matches

        if full_rematch:
            print("LLM 完全重新匹配模式: 对所有记录进行 LLM 匹配...")
        else:
            print(f"LLM 辅助匹配已启用，对未匹配记录进行匹配...")
        apply_llm_supplemental_matches(config, bank_records, ledger_records, used_bank, used_ledger, groups)
        print(f"LLM 辅助匹配完成，当前共 {len(groups)} 条匹配")

    matches_df = pd.DataFrame(groups)
    unmatched_bank = bank_df[~bank_df["txn_id"].isin(used_bank)].copy() if "txn_id" in bank_df else bank_df
    unmatched_ledger = ledger_df[~ledger_df["entry_id"].isin(used_ledger)].copy() if "entry_id" in ledger_df else ledger_df
    return matches_df, unmatched_bank, unmatched_ledger


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def _safe_to_csv(df: pd.DataFrame, path: Path) -> Path:
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        return fallback


def _write_monthly_flow_check(
    bank_df: pd.DataFrame, ledger_df: pd.DataFrame, config: dict[str, Any], out: Path
) -> Path:
    cfg = config.get("matching", {})
    amount_tol = amount_to_cents(cfg.get("monthly_flow_check_tolerance", cfg.get("amount_tolerance", 0.01)))
    bank_summary = _monthly_flow_summary(bank_df, "bank")
    ledger_summary = _monthly_flow_summary(ledger_df, "ledger")
    keys = sorted(
        set(zip(bank_summary["account_no"], bank_summary["month"], bank_summary["flow"]))
        | set(zip(ledger_summary["account_no"], ledger_summary["month"], ledger_summary["flow"]))
    )

    bank_lookup = _monthly_summary_lookup(bank_summary, "bank")
    ledger_lookup = _monthly_summary_lookup(ledger_summary, "ledger")
    rows = []
    for account_no, month, flow in keys:
        key = (account_no, month, flow)
        bank_count, bank_total = bank_lookup.get(key, (0, 0))
        ledger_count, ledger_total = ledger_lookup.get(key, (0, 0))
        diff = bank_total - ledger_total
        rows.append(
            {
                "account_no": account_no,
                "month": month,
                "flow": flow,
                "bank_count": bank_count,
                "bank_total": round(bank_total / 100, 2),
                "ledger_count": ledger_count,
                "ledger_total": round(ledger_total / 100, 2),
                "amount_diff": round(diff / 100, 2),
                "status": "ok" if abs(diff) <= amount_tol else "mismatch",
            }
        )

    report = pd.DataFrame(
        rows,
        columns=[
            "account_no",
            "month",
            "flow",
            "bank_count",
            "bank_total",
            "ledger_count",
            "ledger_total",
            "amount_diff",
            "status",
        ],
    )
    path = _safe_to_csv(report, out / "monthly_flow_check.csv")
    _print_monthly_flow_warnings(report, path)
    return path


def _monthly_flow_summary(df: pd.DataFrame, side: str) -> pd.DataFrame:
    columns = ["account_no", "month", "flow", f"{side}_count", f"{side}_total_cents"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    totals: dict[tuple[str, str, str], dict[str, int]] = {}
    for _, row in df.iterrows():
        trans_date = parse_date(row.get("transaction_date"))
        flow = text(row.get("flow")).lower()
        if not trans_date or flow not in {"in", "out"}:
            continue
        account_no = _digits(row.get("account_no"))
        key = (account_no, f"{trans_date.year:04d}-{trans_date.month:02d}", flow)
        item = totals.setdefault(key, {"count": 0, "total_cents": 0})
        item["count"] += 1
        item["total_cents"] += amount_to_cents(row.get("amount", 0))

    rows = [
        {
            "account_no": account_no,
            "month": month,
            "flow": flow,
            f"{side}_count": values["count"],
            f"{side}_total_cents": values["total_cents"],
        }
        for (account_no, month, flow), values in sorted(totals.items())
    ]
    return pd.DataFrame(rows, columns=columns)


def _monthly_summary_lookup(summary: pd.DataFrame, side: str) -> dict[tuple[str, str, str], tuple[int, int]]:
    lookup: dict[tuple[str, str, str], tuple[int, int]] = {}
    if summary.empty:
        return lookup
    for _, row in summary.iterrows():
        key = (text(row.get("account_no")), text(row.get("month")), text(row.get("flow")))
        lookup[key] = (
            int(row.get(f"{side}_count") or 0),
            int(row.get(f"{side}_total_cents") or 0),
        )
    return lookup


def _print_monthly_flow_warnings(report: pd.DataFrame, path: Path) -> None:
    if report.empty or "status" not in report:
        return
    mismatches = report[report["status"] != "ok"]
    if mismatches.empty:
        return
    print(f"警告：匹配前月度 in/out 汇总不一致，详见 {path}")
    for _, row in mismatches.head(12).iterrows():
        print(
            "  - "
            f"{row['month']} {row['flow']} "
            f"账号 {row['account_no'] or '(空)'}: "
            f"bank={row['bank_total']}, ledger={row['ledger_total']}, diff={row['amount_diff']}"
        )
    if len(mismatches) > 12:
        print(f"  - 另有 {len(mismatches) - 12} 条差异未在控制台展示。")


def _records(df: pd.DataFrame, kind: str, id_col: str) -> list[Record]:
    if df.empty or id_col not in df:
        return []
    records: list[Record] = []
    for _, row in df.iterrows():
        trans_date = parse_date(row.get("transaction_date"))
        amount_cents = amount_to_cents(row.get("amount", 0))

        # fallback: amount=0 时从 debit/credit 字段恢复金额和方向
        if amount_cents <= 0:
            if kind == "ledger":
                debit = amount_to_cents(row.get("ledger_debit", 0))
                credit = amount_to_cents(row.get("ledger_credit", 0))
            else:
                debit = amount_to_cents(row.get("bank_debit", 0))
                credit = amount_to_cents(row.get("bank_credit", 0))
            recovered = False
            if debit > 0:
                amount_cents = debit
                recovered = True
            elif credit > 0:
                amount_cents = credit
                recovered = True
            if recovered:
                row = row.to_dict()
                row["amount"] = str(amount_cents / 100)
                if not text(row.get("flow")):
                    # ledger: debit=借方(in); bank: debit=借方(out)
                    if kind == "ledger":
                        row["flow"] = "in" if debit > 0 else "out"
                    else:
                        row["flow"] = "out" if debit > 0 else "in"
                if not trans_date or amount_cents <= 0:
                    continue
                records.append(Record(text(row[id_col]), kind, row, amount_cents, trans_date))
                continue

        if not trans_date or amount_cents <= 0:
            continue
        records.append(Record(text(row[id_col]), kind, row.to_dict(), amount_cents, trans_date))
    return records


def _fee_aggregation_match(
    bank_records: list[Record],
    ledger_records: list[Record],
    used_bank: set[str],
    used_ledger: set[str],
    amount_tol: int,
    config: dict[str, Any],
    groups: list[dict[str, Any]],
) -> int:
    """Pass 0: 手续费聚合匹配 — 银行逐笔扣费 → 序时账月度汇总。

    仅同月匹配，跨月手续费留给人工/LLM处理。
    三级匹配策略避免DFS组合爆炸:
      1. 全池匹配 (所有bank手续费总和 ≈ ledger金额)
      2. 贪心累加 (按日期排序，逐步加入直到金额匹配)
      3. 小子集DFS (max_size=8 兜底)
    返回新增匹配数。
    """
    bank_fees = [r for r in bank_records if r.id not in used_bank and _is_fee_record(r)]
    ledger_fees = [r for r in ledger_records if r.id not in used_ledger and _is_fee_record(r)]
    if not bank_fees or not ledger_fees:
        return 0

    count = 0
    for ledger in ledger_fees:
        if ledger.id in used_ledger:
            continue
        ledger_acc = _digits(ledger.row.get("account_no"))
        ledger_flow = text(ledger.row.get("flow"))

        pool = [
            b for b in bank_fees
            if b.id not in used_bank
            and b.trans_date.year == ledger.trans_date.year
            and b.trans_date.month == ledger.trans_date.month
            and _digits(b.row.get("account_no")) == ledger_acc
            and text(b.row.get("flow")) == ledger_flow
        ]
        if len(pool) < 2:
            continue

        pool.sort(key=lambda r: abs((r.trans_date - ledger.trans_date).days))
        pool = pool[:50]

        target = ledger.amount_cents
        subset = None

        # Tier 1: 全池匹配 — 所有bank手续费总和 ≈ ledger金额
        total_all = sum(r.amount_cents for r in pool)
        if abs(total_all - target) <= amount_tol:
            subset = list(pool)

        # Tier 2: 贪心累加 — 按日期近→远逐步加入
        if subset is None:
            running = 0
            chosen = []
            for r in pool:
                if running + r.amount_cents <= target + amount_tol:
                    chosen.append(r)
                    running += r.amount_cents
                    if abs(running - target) <= amount_tol and len(chosen) >= 2:
                        subset = list(chosen)
                        break

        # Tier 3: 小子集DFS (max_size=8，避免组合爆炸)
        if subset is None:
            dfs_result = _find_subset(pool, target, amount_tol, max_size=8)
            if dfs_result and len(dfs_result) >= 2:
                subset = dfs_result

        if subset and len(subset) >= 2:
            score = _group_score(subset, [ledger], amount_tol, 30)
            serial = len(groups) + 1
            groups.append(_make_group(config, "fee_aggregation", subset, [ledger], score, serial))
            used_bank.update(r.id for r in subset)
            used_ledger.add(ledger.id)
            count += 1

    if count:
        print(f"Pass 0 (手续费聚合): 新增 {count} 条匹配")

    # ── 第二轮: 扩展池 — 对未匹配的 ledger fee，放宽 bank pool 到所有小额记录 ──
    count2 = 0
    for ledger in ledger_fees:
        if ledger.id in used_ledger:
            continue
        ledger_acc = _digits(ledger.row.get("account_no"))
        ledger_flow = text(ledger.row.get("flow"))

        expanded_pool = [
            b for b in bank_records
            if b.id not in used_bank
            and b.trans_date.year == ledger.trans_date.year
            and b.trans_date.month == ledger.trans_date.month
            and _digits(b.row.get("account_no")) == ledger_acc
            and text(b.row.get("flow")) == ledger_flow
            and b.amount_cents <= _FEE_MAX_CENTS
            and not _is_obss_giro(b)
        ]
        if len(expanded_pool) < 2:
            continue

        expanded_pool.sort(key=lambda r: abs((r.trans_date - ledger.trans_date).days))
        expanded_pool = expanded_pool[:50]

        target = ledger.amount_cents
        subset = None

        total_all = sum(r.amount_cents for r in expanded_pool)
        if abs(total_all - target) <= amount_tol:
            subset = list(expanded_pool)

        if subset is None:
            running = 0
            chosen = []
            for r in expanded_pool:
                if running + r.amount_cents <= target + amount_tol:
                    chosen.append(r)
                    running += r.amount_cents
                    if abs(running - target) <= amount_tol and len(chosen) >= 2:
                        subset = list(chosen)
                        break

        if subset is None:
            dfs_result = _find_subset(expanded_pool, target, amount_tol, max_size=8)
            if dfs_result and len(dfs_result) >= 2:
                subset = dfs_result

        if subset and len(subset) >= 2:
            score = _group_score(subset, [ledger], amount_tol, 30)
            serial = len(groups) + 1
            groups.append(_make_group(config, "fee_aggregation", subset, [ledger], score, serial))
            used_bank.update(r.id for r in subset)
            used_ledger.add(ledger.id)
            count2 += 1

    if count2:
        print(f"Pass 0 (手续费扩展池): 新增 {count2} 条匹配")

    return count + count2


def _cross_account_transfer_match(
    bank_records: list[Record],
    ledger_records: list[Record],
    used_bank: set[str],
    used_ledger: set[str],
    amount_tol: int,
    config: dict[str, Any],
    groups: list[dict[str, Any]],
) -> int:
    """Pass 0.5: 跨账号调拨匹配 — ledger标记'调拨'，跨账号找bank counterpart。

    关键词触发，跨账号优先于同账号（避免false match）。
    返回新增匹配数。
    """
    transfer_ledgers = [
        r for r in ledger_records
        if r.id not in used_ledger and _is_transfer_record(r)
    ]
    if not transfer_ledgers:
        return 0

    date_tol = 3  # 调拨通常同日或±3天
    count = 0

    for ledger in transfer_ledgers:
        ledger_acc = _digits(ledger.row.get("account_no"))
        ledger_flow = text(ledger.row.get("flow"))
        if ledger_flow not in ("in", "out"):
            continue

        # 反向flow: ledger in → bank out (钱从另一账号流入); ledger out → bank in
        expected_bank_flow = "out" if ledger_flow == "in" else "in"

        candidates = []
        for bank in bank_records:
            if bank.id in used_bank:
                continue
            # 严格同月匹配，不跨月
            if bank.trans_date.year != ledger.trans_date.year or bank.trans_date.month != ledger.trans_date.month:
                continue
            if abs((bank.trans_date - ledger.trans_date).days) > date_tol:
                continue
            if abs(bank.amount_cents - ledger.amount_cents) > amount_tol:
                continue
            if text(bank.row.get("flow")) != expected_bank_flow:
                continue
            candidates.append(bank)

        if not candidates:
            continue

        # 优先跨账号候选（不同account_no），避免false match
        cross = [c for c in candidates if _digits(c.row.get("account_no")) != ledger_acc]
        same = [c for c in candidates if _digits(c.row.get("account_no")) == ledger_acc]

        match_rec = None
        if len(cross) == 1:
            match_rec = cross[0]
        elif cross:
            cross.sort(key=lambda c: (
                abs((c.trans_date - ledger.trans_date).days),
                -_pair_score(c, ledger, amount_tol, date_tol),
            ))
            match_rec = cross[0]
        elif len(same) == 1:
            match_rec = same[0]

        if match_rec:
            # bank是counterpart: bank_group/ledger_group按bank视角排列
            if expected_bank_flow == "out":
                bank_group, ledger_group = [match_rec], [ledger]
            else:
                bank_group, ledger_group = [match_rec], [ledger]
            score = _group_score(bank_group, ledger_group, amount_tol, date_tol)
            serial = len(groups) + 1
            groups.append(_make_group(
                config, "cross_account_transfer", bank_group, ledger_group, score, serial,
            ))
            used_bank.add(match_rec.id)
            used_ledger.add(ledger.id)
            count += 1

    if count:
        print(f"Pass 0.5 (跨账号调拨): 新增 {count} 条匹配")
    return count


def _candidate_ok(
    bank: Record,
    ledger: Record,
    amount_tol: int,
    date_tol: int,
    config: dict[str, Any],
    ignore_amount: bool = False,
) -> bool:
    if not _same_month_flow_account(bank, ledger, config):
        return False
    if abs((bank.trans_date - ledger.trans_date).days) > date_tol:
        return False
    if not ignore_amount and abs(bank.amount_cents - ledger.amount_cents) > amount_tol:
        return False
    return True


def _same_month_flow_account(bank: Record, ledger: Record, config: dict[str, Any]) -> bool:
    if bank.trans_date.year != ledger.trans_date.year or bank.trans_date.month != ledger.trans_date.month:
        return False
    if text(bank.row.get("flow")) != text(ledger.row.get("flow")):
        return False
    if config.get("matching", {}).get("strict_account_match", True):
        b_acc = _digits(bank.row.get("account_no"))
        l_acc = _digits(ledger.row.get("account_no"))
        if b_acc and l_acc and b_acc != l_acc:
            return False
    return True


def _groups_same_month_flow_account(
    bank_group: list[Record],
    ledger_group: list[Record],
    config: dict[str, Any],
) -> bool:
    if not bank_group or not ledger_group:
        return False
    for bank in bank_group:
        for ledger in ledger_group:
            if not _same_month_flow_account(bank, ledger, config):
                return False
    return True


def _digits(value: object) -> str:
    return "".join(ch for ch in text(value) if ch.isdigit())


_FEE_KEYWORDS = ("手续费", "SMSP", "Service Charge")
_FEE_MAX_CENTS = 100000  # ≤1000元视为手续费，排除OBSS/GIRO大额支付


def _is_fee_record(rec: Record) -> bool:
    """判断一条记录是否为银行手续费。"""
    row_text = " ".join(
        str(rec.row.get(k, "")) for k in ("summary", "description", "counterparty_name")
    )
    has_keyword = any(kw in row_text for kw in _FEE_KEYWORDS)
    return has_keyword and rec.amount_cents <= _FEE_MAX_CENTS


_OBSS_PREFIXES = ("OBSS", "GIRO")


def _is_obss_giro(rec: Record) -> bool:
    """判断一条记录是否为 OBSS/GIRO 正常付款（非手续费）。"""
    for field in ("summary", "description"):
        val = str(rec.row.get(field, "")).strip()
        if val and val.upper().startswith(_OBSS_PREFIXES):
            return True
    return False


_TRANSFER_KEYWORDS = ("调拨", "资金调拨")


def _is_transfer_record(rec: Record) -> bool:
    """判断一条ledger记录是否为跨账号资金调拨。

    需同时满足: 摘要含'调拨' + 会计科目为'银行存款'（避免误配工程款等正常付款）。
    """
    summary = str(rec.row.get("summary", ""))
    if not any(kw in summary for kw in _TRANSFER_KEYWORDS):
        return False
    subject = str(rec.row.get("subject", ""))
    return "银行存款" in subject


def _pair_score(bank: Record, ledger: Record, amount_tol: int, date_tol: int) -> float:
    amount_diff = abs(bank.amount_cents - ledger.amount_cents)
    amount_score = max(0.0, 1 - amount_diff / max(amount_tol + 1, 1))
    date_diff = abs((bank.trans_date - ledger.trans_date).days)
    date_score = max(0.0, 1 - date_diff / (date_tol + 1))
    bank_text = join_unique([bank.row.get("counterparty_name"), bank.row.get("summary"), bank.row.get("description")])
    ledger_text = join_unique([ledger.row.get("counterparty_name"), ledger.row.get("summary"), ledger.row.get("subject")])
    sim = text_similarity(bank_text, ledger_text)
    return round(0.52 * amount_score + 0.25 * date_score + 0.23 * sim, 4)


def _rank_group_candidates(anchor: Record, candidates: list[Record], amount_tol: int, date_tol: int) -> list[Record]:
    return sorted(
        candidates,
        key=lambda rec: (
            _pair_score(anchor if anchor.kind == "bank" else rec, rec if anchor.kind == "bank" else anchor, amount_tol, date_tol),
            -abs(anchor.amount_cents - rec.amount_cents),
        ),
        reverse=True,
    )


def _find_subset(records: list[Record], target_cents: int, tol_cents: int, max_size: int, node_limit: int = 2_000_000) -> list[Record]:
    records = [r for r in records if r.amount_cents > 0]
    records.sort(key=lambda r: r.amount_cents, reverse=True)
    best: list[Record] = []
    best_diff = target_cents + 1
    nodes = [0]

    def dfs(start: int, chosen: list[Record], total: int) -> None:
        nonlocal best, best_diff
        nodes[0] += 1
        if nodes[0] > node_limit:
            return
        diff = abs(total - target_cents)
        if len(chosen) >= 2 and diff <= tol_cents:
            best = chosen.copy()
            best_diff = diff
            return
        if len(chosen) >= max_size or total > target_cents + tol_cents or diff >= best_diff and best:
            return
        for idx in range(start, len(records)):
            dfs(idx + 1, chosen + [records[idx]], total + records[idx].amount_cents)
            if best and best_diff <= tol_cents:
                return
            if nodes[0] > node_limit:
                return

    dfs(0, [], 0)
    return best


def _group_score(bank_group: list[Record], ledger_group: list[Record], amount_tol: int, date_tol: int) -> float:
    bank_total = sum(item.amount_cents for item in bank_group)
    ledger_total = sum(item.amount_cents for item in ledger_group)
    amount_score = max(0.0, 1 - abs(bank_total - ledger_total) / max(amount_tol + 1, 1))
    date_scores = []
    for bank in bank_group:
        nearest = min(abs((bank.trans_date - ledger.trans_date).days) for ledger in ledger_group)
        date_scores.append(max(0.0, 1 - nearest / (date_tol + 1)))
    date_score = sum(date_scores) / len(date_scores) if date_scores else 0
    bank_text = join_unique([r.row.get("counterparty_name") or r.row.get("summary") for r in bank_group])
    ledger_text = join_unique([r.row.get("counterparty_name") or r.row.get("summary") for r in ledger_group])
    sim = text_similarity(bank_text, ledger_text)
    return round(0.55 * amount_score + 0.25 * date_score + 0.20 * sim, 4)


def _daily_many_to_many(
    bank_records: list[Record],
    ledger_records: list[Record],
    used_bank: set[str],
    used_ledger: set[str],
    amount_tol: int,
    config: dict[str, Any],
) -> list[tuple[list[Record], list[Record]]]:
    groups: list[tuple[list[Record], list[Record]]] = []
    keys = set()
    for record in bank_records:
        if record.id not in used_bank:
            keys.add((record.trans_date, text(record.row.get("flow")), _digits(record.row.get("account_no"))))
    for trans_date, flow, account in sorted(keys):
        banks = [
            r
            for r in bank_records
            if r.id not in used_bank
            and r.trans_date == trans_date
            and text(r.row.get("flow")) == flow
            and (not account or _digits(r.row.get("account_no")) == account)
        ]
        ledgers = [
            r
            for r in ledger_records
            if r.id not in used_ledger
            and r.trans_date == trans_date
            and text(r.row.get("flow")) == flow
            and (not config.get("matching", {}).get("strict_account_match", True) or not account or _digits(r.row.get("account_no")) == account)
        ]
        if len(banks) <= 1 or len(ledgers) <= 1:
            continue
        if abs(sum(r.amount_cents for r in banks) - sum(r.amount_cents for r in ledgers)) <= amount_tol:
            groups.append((banks, ledgers))
            used_bank.update(r.id for r in banks)
            used_ledger.update(r.id for r in ledgers)
    return groups


def _cleanup_small_unmatched(
    bank_records: list[Record],
    ledger_records: list[Record],
    used_bank: set[str],
    used_ledger: set[str],
    amount_tol: int,
    config: dict[str, Any],
    groups: list[dict[str, Any]],
) -> int:
    """Pass 4: 清理残余 — 对少量未匹配记录使用宽松规则匹配。

    策略：
    1. 按 (账号, 月份, 方向) 分组未匹配记录
    2. 对每个小组（≤5条 bank + ≤5条 ledger），使用宽松规则：
       - 日期容差扩大到 7 天
       - 金额容差扩大到 0.02（2分钱）
       - 降低匹配分数阈值到 0.50
    3. 仅同月匹配，不跨月
    4. 优先匹配金额完全一致的记录
    返回新增匹配数。
    """
    # 收集未匹配记录
    remaining_bank = [r for r in bank_records if r.id not in used_bank]
    remaining_ledger = [r for r in ledger_records if r.id not in used_ledger]

    if not remaining_bank or not remaining_ledger:
        return 0

    # 按 (账号, 月份, 方向) 分组
    def _group_key(rec: Record) -> tuple[str, str, str]:
        return (
            _digits(rec.row.get("account_no")),
            f"{rec.trans_date.year:04d}-{rec.trans_date.month:02d}",
            text(rec.row.get("flow")).lower(),
        )

    bank_by_key: dict[tuple[str, str, str], list[Record]] = {}
    for rec in remaining_bank:
        bank_by_key.setdefault(_group_key(rec), []).append(rec)

    ledger_by_key: dict[tuple[str, str, str], list[Record]] = {}
    for rec in remaining_ledger:
        ledger_by_key.setdefault(_group_key(rec), []).append(rec)

    count = 0
    # 宽松参数
    relaxed_date_tol = 7
    relaxed_amount_tol = max(amount_tol, amount_to_cents(0.02))
    relaxed_min_score = 0.50

    for key in sorted(set(bank_by_key) & set(ledger_by_key)):
        banks = sorted(bank_by_key[key], key=lambda r: r.amount_cents, reverse=True)
        ledgers = sorted(ledger_by_key[key], key=lambda r: r.amount_cents, reverse=True)

        # 仅处理小组（避免误匹配）
        if len(banks) > 5 or len(ledgers) > 5:
            continue

        # 尝试一对一匹配
        for bank in banks:
            if bank.id in used_bank:
                continue
            best_ledger = None
            best_score = -1.0
            for ledger in ledgers:
                if ledger.id in used_ledger:
                    continue
                if abs((bank.trans_date - ledger.trans_date).days) > relaxed_date_tol:
                    continue
                if abs(bank.amount_cents - ledger.amount_cents) > relaxed_amount_tol:
                    continue
                score = _pair_score(bank, ledger, relaxed_amount_tol, relaxed_date_tol)
                if score >= relaxed_min_score and score > best_score:
                    best_score = score
                    best_ledger = ledger
            if best_ledger:
                groups.append(_make_group(config, "one_to_one", [bank], [best_ledger], best_score, len(groups) + 1))
                used_bank.add(bank.id)
                used_ledger.add(best_ledger.id)
                count += 1

        # 尝试多对多匹配（剩余记录）
        remaining_banks = [r for r in banks if r.id not in used_bank]
        remaining_ledgers = [r for r in ledgers if r.id not in used_ledger]
        if len(remaining_banks) >= 2 and len(remaining_ledgers) >= 1:
            bank_total = sum(r.amount_cents for r in remaining_banks)
            for ledger in remaining_ledgers:
                if ledger.id in used_ledger:
                    continue
                if abs(bank_total - ledger.amount_cents) <= relaxed_amount_tol:
                    # 检查日期范围
                    all_dates = [r.trans_date for r in remaining_banks] + [ledger.trans_date]
                    if (max(all_dates) - min(all_dates)).days <= relaxed_date_tol:
                        score = _group_score(remaining_banks, [ledger], relaxed_amount_tol, relaxed_date_tol)
                        groups.append(_make_group(config, "many_to_one", remaining_banks, [ledger], score, len(groups) + 1))
                        used_bank.update(r.id for r in remaining_banks)
                        used_ledger.add(ledger.id)
                        count += 1
                        break

    if count:
        print(f"Pass 4 (清理残余): 新增 {count} 条匹配")
    return count


def _make_group(
    config: dict[str, Any],
    match_type: str,
    bank_group: list[Record],
    ledger_group: list[Record],
    score: float,
    serial: int,
) -> dict[str, Any]:
    high = float(config.get("matching", {}).get("high_confidence_score", 0.82))
    medium = float(config.get("matching", {}).get("medium_confidence_score", 0.65))
    confidence = "high" if score >= high else "medium" if score >= medium else "low"
    bank_amount = sum(parse_amount(item.row.get("amount")) for item in bank_group)
    ledger_amount = sum(parse_amount(item.row.get("amount")) for item in ledger_group)
    return {
        "match_id": f"M{serial:05d}",
        "match_type": match_type,
        "confidence": confidence,
        "score": score,
        "flow": text(bank_group[0].row.get("flow")) if bank_group else "",
        "amount": round(max(bank_amount, ledger_amount), 2),
        "amount_diff": round(bank_amount - ledger_amount, 2),
        "bank_txn_ids": "|".join(item.id for item in bank_group),
        "ledger_entry_ids": "|".join(item.id for item in ledger_group),
        "bank_date": join_unique([item.row.get("transaction_date") for item in bank_group]),
        "ledger_date": join_unique([item.row.get("transaction_date") for item in ledger_group]),
        "bank_name": join_unique([item.row.get("bank_name") for item in bank_group + ledger_group]),
        "account_no": join_unique([item.row.get("account_no") for item in bank_group + ledger_group]),
        "counterparty_name": join_unique([item.row.get("counterparty_name") for item in bank_group + ledger_group]),
        "bank_summary": join_unique([item.row.get("summary") or item.row.get("description") for item in bank_group]),
        "ledger_summary": join_unique([item.row.get("summary") for item in ledger_group]),
        "voucher_no": join_unique([item.row.get("voucher_no") for item in ledger_group]),
        "bank_debit": round(sum(parse_amount(item.row.get("bank_debit")) for item in bank_group), 2),
        "bank_credit": round(sum(parse_amount(item.row.get("bank_credit")) for item in bank_group), 2),
        "ledger_debit": round(sum(parse_amount(item.row.get("ledger_debit")) for item in ledger_group), 2),
        "ledger_credit": round(sum(parse_amount(item.row.get("ledger_credit")) for item in ledger_group), 2),
    }
