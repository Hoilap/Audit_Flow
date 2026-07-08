from __future__ import annotations

import re
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from .config import output_dir
from .llm_agent import run_match_decision_agent
from .utils import join_unique, parse_amount, text, text_similarity


def _record_match_llm_usage(usage: dict) -> None:
    """记录 LLM 匹配的 token 用量并推送到前端。"""
    if not usage:
        return
    total = usage.get("total_tokens", 0)
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    print(f"LLM Token 用量: prompt={prompt}, completion={completion}, total={total}")
    try:
        from desktop.common import token_tracker, notify_frontend
        token_tracker.record(usage)
        notify_frontend("token_updated", token_tracker.snapshot())
    except ImportError:
        # 在非 desktop 环境下运行（如 CLI）时忽略
        pass


SYSTEM_PROMPT = """你是审计资金流水匹配复核助手。
你的任务是判断候选银行流水组合与候选序时账记录是否可以匹配。
必须遵守：
1. 金额合计必须一致或仅有配置允许的尾差。
2. 资金方向必须一致：in 是银行存款增加，out 是银行存款减少。
3. 同一账号优先；如账号不一致，不要通过。
4. 银行多笔小额手续费、POS/二维码收款、银行代扣等，允许与账面按期间或月份汇总的一笔记录匹配。
5. 日期可以落在摘要描述的期间内，也可以是月度汇总、月末入账、次月初入账。
6. 摘要语义矛盾时必须拒绝；不确定时拒绝。
只输出 JSON，不要输出解释性正文。"""


@dataclass
class Candidate:
    candidate_id: str
    match_type: str
    bank_group: list[Any]
    ledger_group: list[Any]
    period: tuple[date, date]
    heuristic_score: float
    review_only: bool = False
    llm_review: bool = False
    review_reason: str = ""


def apply_llm_supplemental_matches(
    config: dict[str, Any],
    bank_records: list[Any],
    ledger_records: list[Any],
    used_bank: set[str],
    used_ledger: set[str],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM 辅助匹配，返回匹配统计信息。"""
    stats: dict[str, Any] = {
        "bank_total": len(bank_records),
        "ledger_total": len(ledger_records),
        "candidates_built": 0,
        "candidates_llm_review": 0,
        "candidates_sent_to_llm": 0,
        "candidates_filtered_dedup": 0,
        "cached_decisions": 0,
        "llm_api_calls": 0,
        "llm_candidates_evaluated": 0,
        "llm_approved": 0,
        "llm_rejected": 0,
        "llm_low_confidence": 0,
        "llm_no_decision": 0,
        "manual_approved": 0,
        "pending_review": 0,
        "accepted_matches": 0,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    groups_before = len(groups)

    cfg = config.get("matching", {}).get("llm", {})
    if not cfg.get("enabled", False):
        return stats

    out = output_dir(config) / "matches"
    out.mkdir(parents=True, exist_ok=True)

    # 检查是否为完全重新匹配模式
    full_rematch = cfg.get("full_rematch", False)

    # 统计记录
    if full_rematch:
        print(f"LLM 完全重新匹配: 银行流水 {len(bank_records)} 条, 序时账 {len(ledger_records)} 条")
        # full_rematch 模式下清空已有匹配结果
        groups.clear()
        used_bank.clear()
        used_ledger.clear()
    else:
        remaining_bank = [r for r in bank_records if r.id not in used_bank]
        remaining_ledger = [r for r in ledger_records if r.id not in used_ledger]
        print(f"LLM 辅助匹配: 未匹配银行流水 {len(remaining_bank)} 条, 未匹配序时账 {len(remaining_ledger)} 条")

    candidates = _build_candidates(config, bank_records, ledger_records, used_bank, used_ledger)
    candidates_path = out / "llm_candidates.csv"
    decisions_path = out / "llm_decisions.csv"
    stats["candidates_built"] = len(candidates)
    print(f"LLM 辅助匹配: 构建 {len(candidates)} 个候选匹配")
    if not candidates:
        pd.DataFrame().to_csv(decisions_path, index=False, encoding="utf-8-sig")
        _write_candidates(candidates_path, candidates)
        print("LLM 辅助匹配: 无候选匹配，跳过")
        return stats

    llm_candidates = [candidate for candidate in candidates if not candidate.review_only or candidate.llm_review]
    review_only_count = sum(1 for c in candidates if c.review_only and not c.llm_review)
    stats["candidates_llm_review"] = len(llm_candidates)
    print(f"LLM 辅助匹配: llm_candidates={len(llm_candidates)} (review_only={review_only_count})")
    
    # 根据 deduplicate_candidates 配置决定是否过滤重复候选
    deduplicate = cfg.get("deduplicate_candidates", True)
    if deduplicate:
        bank_id_counts = Counter()
        ledger_id_counts = Counter()
        for candidate in llm_candidates:
            for record in candidate.bank_group:
                bank_id_counts[record.id] += 1
            for record in candidate.ledger_group:
                ledger_id_counts[record.id] += 1
        
        filtered_candidates = []
        for candidate in llm_candidates:
            bank_ids = [record.id for record in candidate.bank_group]
            ledger_ids = [record.id for record in candidate.ledger_group]
            has_dup_bank = any(bank_id_counts[bid] > 1 for bid in bank_ids)
            has_dup_ledger = any(ledger_id_counts[lid] > 1 for lid in ledger_ids)
            if not has_dup_bank and not has_dup_ledger:
                filtered_candidates.append(candidate)
        
        filtered_count = len(llm_candidates) - len(filtered_candidates)
        stats["candidates_filtered_dedup"] = filtered_count
        if filtered_count > 0:
            print(f"LLM 辅助匹配: 过滤掉 {filtered_count} 个含有重复记录的候选 (仅保留 duplicate_info='无重复')")
        llm_candidates = filtered_candidates
    else:
        print(f"LLM 辅助匹配: 不去重，所有 {len(llm_candidates)} 个候选送 LLM (deduplicate_candidates=False)")
    
    stats["candidates_sent_to_llm"] = len(llm_candidates)

    # full_rematch 模式下不使用缓存的决策
    use_cache = cfg.get("reuse_decisions", True) and not full_rematch
    decisions = _load_cached_decisions(decisions_path, llm_candidates) if use_cache else []
    stats["cached_decisions"] = len(decisions)
    print(f"LLM 辅助匹配: 从缓存加载 {len(decisions)} 个决策 (use_cache={use_cache})")
    
    decided_signatures = {text(item.get("candidate_signature")) for item in decisions}
    missing_candidates = [
        candidate for candidate in llm_candidates if _candidate_signature(candidate) not in decided_signatures
    ]
    stats["llm_candidates_evaluated"] = len(missing_candidates)
    print(f"LLM 辅助匹配: 需要调用 LLM 的候选={len(missing_candidates)}")
    
    if missing_candidates:
        new_decisions, batch_usage = _ask_llm(config, missing_candidates)
        stats["token_usage"] = dict(batch_usage)
        stats["llm_api_calls"] = max(1, (len(missing_candidates) + int(cfg.get("batch_size", 8)) - 1) // int(cfg.get("batch_size", 8)))
        _attach_candidate_signatures(new_decisions, missing_candidates)
        decisions.extend(new_decisions)
        _write_decisions(decisions_path, decisions)
        print(f"LLM 辅助匹配: 调用 LLM 完成，新增 {len(new_decisions)} 个决策")
    else:
        print("LLM 辅助匹配: 所有决策均来自缓存，无需调用 LLM")
    
    decisions_by_id = {text(item.get("candidate_id")): item for item in decisions}
    print(f"LLM 辅助匹配: decisions_by_id 包含 {len(decisions_by_id)} 个决策")

    # 将 LLM 决定写入 candidates CSV 的 approve 列，供人工复核编辑器初始化
    _write_candidates(candidates_path, candidates, decisions_by_id)
    review_path = out / text(cfg.get("manual_review_file") or "manual_review_candidates.csv")
    manual_approvals = _load_manual_approvals(review_path)
    amount_tol = int(round(parse_amount(config.get("matching", {}).get("amount_tolerance", 0.01)) * 100))
    date_tol = int(config.get("matching", {}).get("date_tolerance_days", 3))
    threshold = float(cfg.get("acceptance_confidence", 0.72))
    review_rows: list[dict[str, Any]] = []

    for candidate in candidates:
        signature = _candidate_signature(candidate)
        manual = manual_approvals.get(signature)
        if not manual or not _as_bool(manual.get("approve")):
            continue
        decision = decisions_by_id.get(candidate.candidate_id, {})
        accepted = _try_accept_candidate(
            config,
            candidate,
            decision,
            used_bank,
            used_ledger,
            groups,
            amount_tol,
            date_tol,
            source="manual",
        )
        if accepted:
            stats["manual_approved"] += 1
        else:
            review_rows.append(_review_row(candidate, decision, "blocked_conflict_or_amount_diff", manual))

    pending_review: list[tuple[Candidate, dict[str, Any], str, dict[str, Any] | None]] = []
    hold_bank_ids, hold_ledger_ids = _manual_review_hold_ids(config, candidates)

    for candidate in candidates:
        decision = decisions_by_id.get(candidate.candidate_id)
        signature = _candidate_signature(candidate)
        if signature in manual_approvals and _as_bool(manual_approvals[signature].get("approve")):
            continue
        if candidate.review_only:
            decision = decisions_by_id.get(candidate.candidate_id, {})
            status = "needs_manual_amount_approval"
            if decision and not _as_bool(decision.get("approve")):
                status = "needs_review_llm_rejected"
            elif decision and _confidence(decision) < threshold:
                status = "needs_review_low_confidence"
            pending_review.append((candidate, decision, status, manual_approvals.get(signature)))
            continue
        if _candidate_overlaps_ids(candidate, hold_bank_ids, hold_ledger_ids):
            continue
        if not decision:
            pending_review.append((candidate, {}, "needs_review_no_llm_decision", manual_approvals.get(signature)))
            stats["llm_no_decision"] += 1
            continue
        confidence = _confidence(decision)
        if not _as_bool(decision.get("approve")):
            pending_review.append((candidate, decision, "needs_review_llm_rejected", manual_approvals.get(signature)))
            stats["llm_rejected"] += 1
            continue
        if confidence < threshold:
            pending_review.append((candidate, decision, "needs_review_low_confidence", manual_approvals.get(signature)))
            stats["llm_low_confidence"] += 1
            continue
        accepted = _try_accept_candidate(
            config,
            candidate,
            decision,
            used_bank,
            used_ledger,
            groups,
            amount_tol,
            date_tol,
            source="llm",
        )
        if accepted:
            stats["llm_approved"] += 1
        else:
            pending_review.append((candidate, decision, "blocked_conflict_or_amount_diff", manual_approvals.get(signature)))

    for candidate, decision, status, previous in pending_review:
        if any(record.id in used_bank for record in candidate.bank_group) or any(
            record.id in used_ledger for record in candidate.ledger_group
        ):
            continue
        review_rows.append(_review_row(candidate, decision, status, previous))

    stats["pending_review"] = len(review_rows)
    stats["accepted_matches"] = len(groups) - groups_before

    _write_manual_review(review_path, review_rows)
    return stats


def _manual_review_hold_ids(config: dict[str, Any], candidates: list[Candidate]) -> tuple[set[str], set[str]]:
    cfg = config.get("matching", {}).get("llm", {})
    manual_mtm_cfg = cfg.get("manual_window_many_to_many", {}) or {}
    if not bool(manual_mtm_cfg.get("block_auto_accept", True)):
        return set(), set()
    limit = int(manual_mtm_cfg.get("block_top_candidates", manual_mtm_cfg.get("max_total_candidates", 50)))
    if limit <= 0:
        return set(), set()

    match_types = manual_mtm_cfg.get("block_match_types") or [
        "manual_time_window_many_to_many",
        "manual_monthly_balance_many_to_many",
    ]
    if isinstance(match_types, str):
        match_types = [item.strip() for item in match_types.split(",") if item.strip()]
    match_types = set(match_types)
    blockers = [
        candidate
        for candidate in candidates
        if candidate.review_only and candidate.match_type in match_types
    ]
    blockers = _sort_manual_many_to_many_candidates(blockers)[:limit]
    bank_ids = {record.id for candidate in blockers for record in candidate.bank_group}
    ledger_ids = {record.id for candidate in blockers for record in candidate.ledger_group}
    return bank_ids, ledger_ids


def _candidate_overlaps_ids(candidate: Candidate, bank_ids: set[str], ledger_ids: set[str]) -> bool:
    return any(record.id in bank_ids for record in candidate.bank_group) or any(
        record.id in ledger_ids for record in candidate.ledger_group
    )


def _build_candidates(
    config: dict[str, Any],
    bank_records: list[Any],
    ledger_records: list[Any],
    used_bank: set[str],
    used_ledger: set[str],
) -> list[Candidate]:
    cfg = config.get("matching", {}).get("llm", {})
    amount_tol = int(round(parse_amount(config.get("matching", {}).get("amount_tolerance", 0.01)) * 100))
    window_days = int(cfg.get("candidate_window_days", 45))
    max_candidates = int(cfg.get("max_candidates", 60))
    group_max_size = int(config.get("matching", {}).get("llm_group_max_size", 120))
    relaxed_cfg = cfg.get("relaxed_amount_groups", {}) or {}
    relaxed_enabled = bool(relaxed_cfg.get("enabled", True))
    relaxed_window_days = int(relaxed_cfg.get("window_days", max(window_days, 90)))
    relaxed_pool_limit = int(relaxed_cfg.get("pool_limit", 32))
    relaxed_max_size = int(relaxed_cfg.get("max_size", 40))
    relaxed_min_amount = int(round(parse_amount(relaxed_cfg.get("min_amount", 0)) * 100))
    relaxed_max_nodes = int(relaxed_cfg.get("max_search_nodes", 200000))
    manual_enum_cfg = cfg.get("manual_amount_enumeration", {}) or {}
    manual_enum_enabled = bool(manual_enum_cfg.get("enabled", True))
    manual_mtm_cfg = cfg.get("manual_window_many_to_many", {}) or {}
    manual_mtm_enabled = bool(manual_mtm_cfg.get("enabled", True))
    monthly_balance_cfg = cfg.get("monthly_balance_enumeration", {}) or {}
    monthly_balance_enabled = bool(monthly_balance_cfg.get("enabled", True))

    # full_rematch 模式下使用所有记录，否则只使用未匹配记录
    full_rematch = cfg.get("full_rematch", False)
    if full_rematch:
        remaining_bank = list(bank_records)
        remaining_ledger = list(ledger_records)
        print(f"LLM full_rematch: 使用全部 {len(remaining_bank)} 条银行流水和 {len(remaining_ledger)} 条序时账")
    else:
        remaining_bank = [record for record in bank_records if record.id not in used_bank]
        remaining_ledger = [record for record in ledger_records if record.id not in used_ledger]
        print(f"LLM 辅助匹配: 使用 {len(remaining_bank)} 条未匹配银行流水和 {len(remaining_ledger)} 条未匹配序时账")
    candidates: list[Candidate] = []
    seen: dict[tuple[tuple[str, ...], tuple[str, ...]], int] = {}

    for ledger in remaining_ledger:
        period = _infer_period(ledger)
        wider = (
            min(period[0], ledger.trans_date - timedelta(days=window_days)),
            max(period[1], ledger.trans_date + timedelta(days=window_days)),
        )
        pool = [
            bank
            for bank in remaining_bank
            if _same_flow_and_account(bank, ledger, config)
            and wider[0] <= bank.trans_date <= wider[1]
        ]

        exact_one = [bank for bank in pool if abs(bank.amount_cents - ledger.amount_cents) <= amount_tol]
        for bank in exact_one[:8]:
            candidate = Candidate(
                candidate_id=f"C{len(candidates) + 1:05d}",
                match_type="one_to_one",
                bank_group=[bank],
                ledger_group=[ledger],
                period=(min(bank.trans_date, ledger.trans_date), max(bank.trans_date, ledger.trans_date)),
                heuristic_score=_heuristic_score([bank], [ledger]),
            )
            if _add_candidate(config, candidates, seen, candidate):
                pass

        period_pool = [
            bank
            for bank in pool
            if period[0] <= bank.trans_date <= period[1]
        ]
        grouped_entry = _looks_grouped_entry(ledger)
        if grouped_entry and 1 < len(period_pool) <= group_max_size:
            period_sum = _sum_cents(period_pool)
            if abs(period_sum - ledger.amount_cents) <= amount_tol:
                candidate = Candidate(
                    candidate_id=f"C{len(candidates) + 1:05d}",
                    match_type="many_to_one",
                    bank_group=period_pool,
                    ledger_group=[ledger],
                    period=period,
                    heuristic_score=_heuristic_score(period_pool, [ledger]),
                )
                _add_candidate(config, candidates, seen, candidate)

        if grouped_entry:
            subset_pool = sorted(
                period_pool or pool,
                key=lambda bank: (
                    text_similarity(_record_text(bank), _record_text(ledger)),
                    -abs(bank.amount_cents - ledger.amount_cents),
                ),
                reverse=True,
            )[:22]
            subset = _find_subset(subset_pool, ledger.amount_cents, amount_tol, max_size=40)
            if len(subset) > 1:
                candidate = Candidate(
                    candidate_id=f"C{len(candidates) + 1:05d}",
                    match_type="many_to_one",
                    bank_group=subset,
                    ledger_group=[ledger],
                    period=(min(bank.trans_date for bank in subset), max(bank.trans_date for bank in subset)),
                    heuristic_score=_heuristic_score(subset, [ledger]),
                )
                _add_candidate(config, candidates, seen, candidate)

        if relaxed_enabled and ledger.amount_cents >= relaxed_min_amount:
            relaxed_start = ledger.trans_date - timedelta(days=relaxed_window_days)
            relaxed_end = ledger.trans_date + timedelta(days=relaxed_window_days)
            relaxed_pool = [
                bank
                for bank in remaining_bank
                if _same_flow_and_account(bank, ledger, config)
                and relaxed_start <= bank.trans_date <= relaxed_end
                and bank.amount_cents <= ledger.amount_cents + amount_tol
            ]
            relaxed_pool = _rank_amount_group_pool(ledger, relaxed_pool, relaxed_pool_limit)
            subset = _find_subset(
                relaxed_pool,
                ledger.amount_cents,
                amount_tol,
                max_size=relaxed_max_size,
                max_nodes=relaxed_max_nodes,
            )
            if len(subset) > 1:
                candidate = Candidate(
                    candidate_id=f"C{len(candidates) + 1:05d}",
                    match_type="amount_group_many_to_one",
                    bank_group=subset,
                    ledger_group=[ledger],
                    period=(min(bank.trans_date for bank in subset), max(bank.trans_date for bank in subset)),
                    heuristic_score=_heuristic_score(subset, [ledger]),
                )
                _add_candidate(config, candidates, seen, candidate)

        if manual_enum_enabled:
            for candidate in _manual_amount_candidates_for_ledger(
                config,
                ledger,
                remaining_bank,
                amount_tol,
                manual_enum_cfg,
                start_index=len(candidates) + 1,
            ):
                _add_candidate(config, candidates, seen, candidate)

    if manual_mtm_enabled:
        for candidate in _manual_many_to_many_time_window_candidates(
            config,
            remaining_bank,
            remaining_ledger,
            amount_tol,
            manual_mtm_cfg,
            start_index=len(candidates) + 1,
        ):
            _add_candidate(config, candidates, seen, candidate)

    if monthly_balance_enabled:
        for candidate in _monthly_balance_candidates(
            config,
            remaining_bank,
            remaining_ledger,
            amount_tol,
            monthly_balance_cfg,
            start_index=len(candidates) + 1,
        ):
            _add_candidate(config, candidates, seen, candidate)

    candidates.sort(
        key=lambda c: (
            len(c.bank_group) > 1 or len(c.ledger_group) > 1,
            c.heuristic_score,
            -abs(_sum_cents(c.bank_group) - _sum_cents(c.ledger_group)),
        ),
        reverse=True,
    )
    for idx, candidate in enumerate(candidates[:max_candidates], start=1):
        candidate.candidate_id = f"C{idx:05d}"
    return candidates[:max_candidates]


def _add_candidate(
    config: dict[str, Any],
    candidates: list[Candidate],
    seen: dict[tuple[tuple[str, ...], tuple[str, ...]], int],
    candidate: Candidate,
) -> bool:
    if not _candidate_same_month_flow_account(candidate, config):
        return False
    _apply_group_review_policy(candidate)
    key = _candidate_group_key(candidate)
    existing_idx = seen.get(key)
    if existing_idx is not None:
        existing = candidates[existing_idx]
        if _candidate_preference(candidate) > _candidate_preference(existing):
            candidates[existing_idx] = candidate
            return True
        return False
    seen[key] = len(candidates)
    candidates.append(candidate)
    return True


def _candidate_group_key(candidate: Candidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(record.id for record in candidate.bank_group)),
        tuple(sorted(record.id for record in candidate.ledger_group)),
    )


def _candidate_preference(candidate: Candidate) -> tuple[int, int, float, int]:
    return (
        _match_type_priority(candidate.match_type),
        1 if candidate.review_only else 0,
        candidate.heuristic_score,
        -(len(candidate.bank_group) + len(candidate.ledger_group)),
    )


def _match_type_priority(match_type: str) -> int:
    priorities = {
        "manual_monthly_balance_many_to_many": 50,
        "manual_time_window_many_to_many": 45,
        "manual_time_window_many_to_one": 40,
        "amount_group_many_to_one": 30,
        "many_to_one": 25,
        "one_to_many": 25,
        "one_to_one": 10,
    }
    return priorities.get(match_type, 0)


def _apply_group_review_policy(candidate: Candidate) -> None:
    if candidate.match_type == "one_to_one":
        return
    if len(candidate.bank_group) <= 1 and len(candidate.ledger_group) <= 1:
        return
    candidate.review_only = True
    candidate.llm_review = True
    if not candidate.review_reason:
        candidate.review_reason = (
            "Python 发现多笔/汇总金额候选，金额、月份、方向和账号满足前置规则；"
            "因涉及拆分、汇总或补登入账，默认进入人工复核，不自动写入匹配表。"
        )


def _candidate_same_month_flow_account(candidate: Candidate, config: dict[str, Any]) -> bool:
    from .matcher import _groups_same_month_flow_account

    return _groups_same_month_flow_account(candidate.bank_group, candidate.ledger_group, config)


def _same_flow_and_account(bank: Any, ledger: Any, config: dict[str, Any]) -> bool:
    if not _same_month(bank, ledger):
        return False
    if text(bank.row.get("flow")) != text(ledger.row.get("flow")):
        return False
    if not config.get("matching", {}).get("strict_account_match", True):
        return True
    b_acc = _digits(bank.row.get("account_no"))
    l_acc = _digits(ledger.row.get("account_no"))
    return not b_acc or not l_acc or b_acc == l_acc


def _same_month(left: Any, right: Any) -> bool:
    return left.trans_date.year == right.trans_date.year and left.trans_date.month == right.trans_date.month


def _rank_amount_group_pool(ledger: Any, pool: list[Any], limit: int) -> list[Any]:
    ledger_text = _record_text(ledger)
    return sorted(
        pool,
        key=lambda bank: (
            text_similarity(_record_text(bank), ledger_text),
            -abs((bank.trans_date - ledger.trans_date).days),
            bank.amount_cents,
        ),
        reverse=True,
    )[:limit]


def _looks_grouped_entry(record: Any) -> bool:
    content = _record_text(record)
    grouped_keywords = [
        "手续费",
        "银行代扣",
        "银行已扣费",
        "银行手续费",
        "食堂",
        "二维码",
        "POS",
        "商户",
        "社保",
        "医保",
        "税务",
        "税收",
        "汇缴",
    ]
    has_period = bool(
        re.search(r"\d{1,2}\s*[.月/]\s*\d{1,2}\s*(?:-|~|至|到|—)\s*\d{1,2}\s*[.月/]\s*\d{1,2}", content)
        or re.search(r"\d{4}年\d{1,2}\s*月|\d{1,2}\s*月份", content)
    )
    return has_period or any(keyword.lower() in content.lower() for keyword in grouped_keywords)


def _digits(value: object) -> str:
    return "".join(ch for ch in text(value) if ch.isdigit())


def _infer_period(ledger: Any) -> tuple[date, date]:
    summary = text(ledger.row.get("summary"))
    year = ledger.trans_date.year

    range_match = re.search(
        r"(?<!\d)(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*(?:-|~|至|到|—)\s*(\d{1,2})\s*[.月/]\s*(\d{1,2})(?!\d)",
        summary,
    )
    if range_match:
        sm, sd, em, ed = [int(part) for part in range_match.groups()]
        start = date(year, sm, sd)
        end_year = year + 1 if em < sm else year
        end = date(end_year, em, ed)
        return start, end

    month_match = re.search(r"(?:(\d{4})年)?(\d{1,2})\s*月(?:份)?", summary)
    if month_match:
        y = int(month_match.group(1) or year)
        m = int(month_match.group(2))
        return date(y, m, 1), date(y, m, monthrange(y, m)[1])

    start = date(year, ledger.trans_date.month, 1)
    end = date(year, ledger.trans_date.month, monthrange(year, ledger.trans_date.month)[1])
    return start, end


def _find_subset(
    records: list[Any],
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_nodes: int = 100000,
) -> list[Any]:
    records = [record for record in records if record.amount_cents > 0]
    records.sort(key=lambda record: record.amount_cents, reverse=True)
    suffix = [0] * (len(records) + 1)
    for idx in range(len(records) - 1, -1, -1):
        suffix[idx] = suffix[idx + 1] + records[idx].amount_cents
    best: list[Any] = []
    nodes = 0

    def dfs(start: int, chosen: list[Any], total: int) -> None:
        nonlocal best, nodes
        nodes += 1
        if nodes > max_nodes:
            return
        if len(chosen) >= 2 and abs(total - target_cents) <= tol_cents:
            best = chosen.copy()
            return
        if len(chosen) >= max_size or total > target_cents + tol_cents:
            return
        if total + suffix[start] < target_cents - tol_cents:
            return
        for idx in range(start, len(records)):
            dfs(idx + 1, chosen + [records[idx]], total + records[idx].amount_cents)
            if best:
                return

    dfs(0, [], 0)
    return best


def _manual_amount_candidates_for_ledger(
    config: dict[str, Any],
    ledger: Any,
    remaining_bank: list[Any],
    amount_tol: int,
    cfg: dict[str, Any],
    start_index: int,
) -> list[Candidate]:
    window_days = int(cfg.get("window_days", 365))
    same_flow = bool(cfg.get("same_flow", True))
    pool_limit = int(cfg.get("pool_limit", 120))
    max_size = int(cfg.get("max_size", 40))
    start = ledger.trans_date - timedelta(days=window_days)
    end = ledger.trans_date + timedelta(days=window_days)

    pool = []
    for bank in remaining_bank:
        if not _same_month(bank, ledger):
            continue
        if not _same_account(bank, ledger, config):
            continue
        if same_flow and text(bank.row.get("flow")) != text(ledger.row.get("flow")):
            continue
        if not start <= bank.trans_date <= end:
            continue
        if bank.amount_cents <= 0 or bank.amount_cents > ledger.amount_cents + amount_tol:
            continue
        pool.append(bank)

    pool = _rank_manual_time_pool(ledger, pool, pool_limit)
    subset = _find_subset(
        pool,
        ledger.amount_cents,
        amount_tol,
        max_size=max_size,
        max_nodes=int(cfg.get("max_search_nodes", 200000)),
    )
    candidates: list[Candidate] = []
    if subset and len(subset) >= 2:
        candidates.append(
            Candidate(
                candidate_id=f"C{start_index:05d}",
                match_type="manual_time_window_many_to_one",
                bank_group=subset,
                ledger_group=[ledger],
                period=(min(bank.trans_date for bank in subset), max(bank.trans_date for bank in subset)),
                heuristic_score=_manual_window_score(subset, ledger),
                review_only=True,
                review_reason="Python 发现多笔银行流水金额合计与序时账金额一致，需人工判断是否为补登/汇总入账。",
            )
        )
    return candidates


def _manual_many_to_many_time_window_candidates(
    config: dict[str, Any],
    remaining_bank: list[Any],
    remaining_ledger: list[Any],
    amount_tol: int,
    cfg: dict[str, Any],
    start_index: int,
) -> list[Candidate]:
    compare = text(cfg.get("compare") or "signed_or_abs")
    bank_max_size = int(cfg.get("bank_window_max_size", 80))
    ledger_max_size = int(cfg.get("ledger_window_max_size", 12))
    min_bank_count = int(cfg.get("min_bank_count", 2))
    min_ledger_count = int(cfg.get("min_ledger_count", 2))
    max_total = int(cfg.get("max_total_candidates", 50))

    bank_records = sorted(
        remaining_bank,
        key=lambda record: (
            _digits(record.row.get("account_no")),
            record.trans_date,
            int(float(record.row.get("row_no") or 0)),
            record.id,
        ),
    )
    ledger_records = sorted(
        remaining_ledger,
        key=lambda record: (
            _digits(record.row.get("account_no")),
            record.trans_date,
            int(float(record.row.get("row_no") or 0)),
            record.id,
        ),
    )

    ledger_windows_by_key: dict[tuple[tuple[str, str, str], str, int], list[list[Any]]] = {}
    for ledger_window in _contiguous_windows(ledger_records, ledger_max_size, min_ledger_count):
        bucket = _window_group_bucket_key(ledger_window)
        for mode, total in _window_totals(ledger_window, compare).items():
            key = (bucket, mode, total)
            ledger_windows_by_key.setdefault(key, []).append(ledger_window)

    candidates: list[Candidate] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for bank_window in _contiguous_windows(bank_records, bank_max_size, min_bank_count):
        bucket = _window_group_bucket_key(bank_window)
        for mode, total in _window_totals(bank_window, compare).items():
            for delta in range(-amount_tol, amount_tol + 1):
                key = (bucket, mode, total + delta)
                for ledger_window in ledger_windows_by_key.get(key, []):
                    signature = (
                        tuple(record.id for record in bank_window),
                        tuple(record.id for record in ledger_window),
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    candidates.append(
                        Candidate(
                            candidate_id=f"C{start_index + len(candidates):05d}",
                            match_type="manual_time_window_many_to_many",
                            bank_group=bank_window,
                            ledger_group=ledger_window,
                            period=(
                                min(min(record.trans_date for record in bank_window), min(record.trans_date for record in ledger_window)),
                                max(max(record.trans_date for record in bank_window), max(record.trans_date for record in ledger_window)),
                            ),
                            heuristic_score=_manual_many_to_many_score(bank_window, ledger_window),
                            review_only=True,
                            review_reason=f"Python 按时间排序发现银行连续窗口与序时账连续窗口总额一致（{mode}），需人工判断是否为补登/汇总入账。",
                        )
                    )
                    if len(candidates) >= max_total:
                        return _sort_manual_many_to_many_candidates(candidates)[:max_total]

    return _sort_manual_many_to_many_candidates(candidates)[:max_total]


def _contiguous_windows(records: list[Any], max_size: int, min_size: int) -> list[list[Any]]:
    windows: list[list[Any]] = []
    for start_idx in range(len(records)):
        bucket = _window_bucket_key(records[start_idx])
        window: list[Any] = []
        for end_idx in range(start_idx, min(len(records), start_idx + max_size)):
            record = records[end_idx]
            if _window_bucket_key(record) != bucket:
                break
            window.append(record)
            if len(window) >= min_size:
                windows.append(window.copy())
    return windows


def _window_bucket_key(record: Any) -> tuple[str, str, str]:
    month = f"{record.trans_date.year:04d}-{record.trans_date.month:02d}"
    flow = text(record.row.get("flow")).lower()
    return (_digits(record.row.get("account_no")), month, flow)


def _window_account_key(records: list[Any]) -> str:
    for record in records:
        account = _digits(record.row.get("account_no"))
        if account:
            return account
    return ""


def _window_group_bucket_key(records: list[Any]) -> tuple[str, str, str]:
    if not records:
        return ("", "", "")
    return _window_bucket_key(records[0])


def _window_totals(records: list[Any], compare: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    if compare in {"signed", "signed_or_abs"}:
        totals["signed"] = sum(_signed_amount_cents(record) for record in records)
    if compare in {"abs", "absolute", "signed_or_abs"}:
        totals["abs"] = sum(record.amount_cents for record in records)
    return totals


def _signed_amount_cents(record: Any) -> int:
    flow = text(record.row.get("flow"))
    sign = -1 if flow == "out" else 1
    return sign * record.amount_cents


def _sort_manual_many_to_many_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda candidate: _manual_many_to_many_priority(candidate.bank_group, candidate.ledger_group),
        reverse=True,
    )


def _manual_many_to_many_priority(bank_group: list[Any], ledger_group: list[Any]) -> tuple[float, float, float, float, float]:
    all_records = bank_group + ledger_group
    dates = [record.trans_date for record in all_records]
    bank_dates = [record.trans_date for record in bank_group]
    ledger_dates = [record.trans_date for record in ledger_group]
    bank_counterparty = _counterparty_consistency(bank_group)
    ledger_counterparty = _counterparty_consistency(ledger_group)
    center_gap = abs(
        (sum(date.toordinal() for date in bank_dates) / len(bank_dates))
        - (sum(date.toordinal() for date in ledger_dates) / len(ledger_dates))
    )
    return (
        min(bank_counterparty, ledger_counterparty),
        -(max(dates) - min(dates)).days,
        -center_gap,
        -(len(bank_group) + len(ledger_group)),
        _manual_many_to_many_score(bank_group, ledger_group),
    )


def _manual_many_to_many_score(bank_group: list[Any], ledger_group: list[Any]) -> float:
    all_records = bank_group + ledger_group
    dates = [record.trans_date for record in all_records]
    span = (max(dates) - min(dates)).days if dates else 0
    consistency = min(_counterparty_consistency(bank_group), _counterparty_consistency(ledger_group))
    span_score = 1 / (1 + span)
    count_score = 1 / (1 + len(all_records))
    return round(0.65 * consistency + 0.25 * span_score + 0.10 * count_score, 4)


def _monthly_balance_candidates(
    config: dict[str, Any],
    remaining_bank: list[Any],
    remaining_ledger: list[Any],
    amount_tol: int,
    cfg: dict[str, Any],
    start_index: int,
) -> list[Candidate]:
    max_total = int(cfg.get("max_total_candidates", 80))
    max_results_per_bucket = int(cfg.get("max_results_per_bucket", 8))
    bank_pool_limit = int(cfg.get("bank_pool_limit", 90))
    ledger_pool_limit = int(cfg.get("ledger_pool_limit", 30))
    max_bank_subset_size = int(cfg.get("max_bank_subset_size", 40))
    max_ledger_subset_size = int(cfg.get("max_ledger_subset_size", 8))
    max_ledger_subsets = int(cfg.get("max_ledger_subsets", 180))
    full_bucket_max_records = int(cfg.get("full_bucket_max_records", 120))
    llm_review_max_records = int(cfg.get("llm_review_max_records", 80))
    max_nodes = int(cfg.get("max_search_nodes", 200000))

    bank_by_key = _records_by_month_key(remaining_bank, bank_pool_limit)
    ledger_by_key = _records_by_month_key(remaining_ledger, ledger_pool_limit)
    candidates: list[Candidate] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

    for key in sorted(set(bank_by_key) & set(ledger_by_key)):
        bank_records = bank_by_key[key]
        ledger_records = ledger_by_key[key]
        bank_total = _sum_cents(bank_records)
        ledger_total = _sum_cents(ledger_records)
        if abs(bank_total - ledger_total) > amount_tol:
            continue

        bucket_candidates: list[Candidate] = []
        for ledger_subset in _monthly_ledger_subsets(ledger_records, max_ledger_subset_size, max_ledger_subsets):
            target = _sum_cents(ledger_subset)
            bank_subsets = _monthly_amount_subsets(
                bank_records,
                ledger_subset,
                target,
                amount_tol,
                max_bank_subset_size,
                max_results=max(1, max_results_per_bucket - len(bucket_candidates)),
                max_nodes=max_nodes,
            )
            for bank_subset in bank_subsets:
                if len(bank_subset) == 1 and len(ledger_subset) == 1:
                    continue
                candidate = _make_monthly_balance_candidate(
                    bank_subset,
                    ledger_subset,
                    start_index + len(candidates) + len(bucket_candidates),
                    key,
                    llm_review_max_records,
                    "子集枚举",
                )
                signature = _group_signature(candidate.bank_group, candidate.ledger_group)
                if signature in seen:
                    continue
                seen.add(signature)
                bucket_candidates.append(candidate)
                if len(bucket_candidates) >= max_results_per_bucket:
                    break
            if len(bucket_candidates) >= max_results_per_bucket:
                break

        if (
            len(bank_records) + len(ledger_records) <= full_bucket_max_records
            and (len(bank_records) > 1 or len(ledger_records) > 1)
        ):
            full_candidate = _make_monthly_balance_candidate(
                bank_records,
                ledger_records,
                start_index + len(candidates) + len(bucket_candidates),
                key,
                llm_review_max_records,
                "整月残余",
            )
            signature = _group_signature(full_candidate.bank_group, full_candidate.ledger_group)
            if signature not in seen:
                seen.add(signature)
                bucket_candidates.append(full_candidate)

        bucket_candidates.sort(key=lambda candidate: _monthly_balance_priority(candidate), reverse=True)
        candidates.extend(bucket_candidates[:max_results_per_bucket])
        if len(candidates) >= max_total:
            break

    candidates = candidates[:max_total]
    for offset, candidate in enumerate(candidates):
        candidate.candidate_id = f"C{start_index + offset:05d}"
    return candidates


def _records_by_month_key(records: list[Any], limit: int) -> dict[tuple[str, str, str], list[Any]]:
    buckets: dict[tuple[str, str, str], list[Any]] = {}
    for record in records:
        account = _digits(record.row.get("account_no"))
        month = f"{record.trans_date.year:04d}-{record.trans_date.month:02d}"
        flow = text(record.row.get("flow")).lower()
        if flow not in {"in", "out"}:
            continue
        buckets.setdefault((account, month, flow), []).append(record)
    for key, values in list(buckets.items()):
        buckets[key] = sorted(
            values,
            key=lambda record: (record.trans_date, int(float(record.row.get("row_no") or 0)), record.id),
        )[:limit]
    return buckets


def _monthly_ledger_subsets(records: list[Any], max_size: int, max_results: int) -> list[list[Any]]:
    records = sorted(records, key=lambda record: (record.trans_date, int(float(record.row.get("row_no") or 0)), record.id))
    results: list[list[Any]] = []
    seen: set[tuple[str, ...]] = set()

    for start_idx in range(len(records)):
        window: list[Any] = []
        for end_idx in range(start_idx, min(len(records), start_idx + max_size)):
            window.append(records[end_idx])
            signature = tuple(record.id for record in window)
            if signature in seen:
                continue
            seen.add(signature)
            results.append(window.copy())
            if len(results) >= max_results:
                return _sort_monthly_ledger_subsets(results)

    for record in records:
        signature = (record.id,)
        if signature not in seen:
            seen.add(signature)
            results.append([record])
            if len(results) >= max_results:
                break
    return _sort_monthly_ledger_subsets(results)


def _sort_monthly_ledger_subsets(subsets: list[list[Any]]) -> list[list[Any]]:
    return sorted(
        subsets,
        key=lambda subset: (
            _counterparty_consistency(subset),
            -len(subset),
            -(max(record.trans_date for record in subset) - min(record.trans_date for record in subset)).days,
            _sum_cents(subset),
        ),
        reverse=True,
    )


def _monthly_amount_subsets(
    bank_records: list[Any],
    ledger_subset: list[Any],
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_results: int,
    max_nodes: int,
) -> list[list[Any]]:
    records = [record for record in bank_records if record.amount_cents > 0 and record.amount_cents <= target_cents + tol_cents]
    records = _rank_monthly_bank_pool(records, ledger_subset)
    lower = target_cents - tol_cents
    upper = target_cents + tol_cents
    suffix = [0] * (len(records) + 1)
    for idx in range(len(records) - 1, -1, -1):
        suffix[idx] = suffix[idx + 1] + records[idx].amount_cents

    results: list[list[Any]] = []
    seen: set[tuple[str, ...]] = set()
    nodes = 0

    def dfs(start: int, chosen: list[int], total: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or len(results) >= max_results:
            return
        if len(chosen) >= 1 and lower <= total <= upper:
            subset = [records[item_idx] for item_idx in chosen]
            signature = tuple(sorted(record.id for record in subset))
            if signature not in seen:
                seen.add(signature)
                results.append(subset)
            return
        if len(chosen) >= max_size or total > upper:
            return
        if start >= len(records) or total + suffix[start] < lower:
            return
        for idx in range(start, len(records)):
            dfs(idx + 1, chosen + [idx], total + records[idx].amount_cents)
            if nodes > max_nodes or len(results) >= max_results:
                return

    dfs(0, [], 0)
    results.sort(key=lambda subset: _monthly_subset_priority(subset, ledger_subset), reverse=True)
    return results[:max_results]


def _rank_monthly_bank_pool(records: list[Any], ledger_subset: list[Any]) -> list[Any]:
    ledger_text = join_unique([_record_text(record) for record in ledger_subset])
    ledger_dates = [record.trans_date for record in ledger_subset]
    ledger_center = sum(item.toordinal() for item in ledger_dates) / len(ledger_dates)
    return sorted(
        records,
        key=lambda record: (
            text_similarity(_record_text(record), ledger_text),
            -abs(record.trans_date.toordinal() - ledger_center),
            _counterparty_consistency([record] + ledger_subset),
            -record.amount_cents,
        ),
        reverse=True,
    )


def _make_monthly_balance_candidate(
    bank_group: list[Any],
    ledger_group: list[Any],
    index: int,
    key: tuple[str, str, str],
    llm_review_max_records: int,
    source: str,
) -> Candidate:
    account, month, flow = key
    period = (
        min(min(record.trans_date for record in bank_group), min(record.trans_date for record in ledger_group)),
        max(max(record.trans_date for record in bank_group), max(record.trans_date for record in ledger_group)),
    )
    total_records = len(bank_group) + len(ledger_group)
    review_reason = (
        f"Python 月度平衡枚举：账号 {account or '(空)'} {month} {flow} 的未匹配残余总额相等，"
        f"通过{source}凑出 bank={round(_sum_cents(bank_group) / 100, 2)} 与 "
        f"ledger={round(_sum_cents(ledger_group) / 100, 2)}。"
        "前置规则未能唯一匹配，需结合 LLM 原因和人工判断是否为汇总/补登/拆分入账。"
    )
    if total_records > llm_review_max_records:
        review_reason += f" 记录数 {total_records} 超过 LLM 说明上限，仅写入人工复核。"
    return Candidate(
        candidate_id=f"C{index:05d}",
        match_type="manual_monthly_balance_many_to_many",
        bank_group=bank_group,
        ledger_group=ledger_group,
        period=period,
        heuristic_score=_monthly_balance_score(bank_group, ledger_group),
        review_only=True,
        llm_review=total_records <= llm_review_max_records,
        review_reason=review_reason,
    )


def _monthly_balance_priority(candidate: Candidate) -> tuple[float, float, float, float, float]:
    return (
        _monthly_balance_score(candidate.bank_group, candidate.ledger_group),
        -abs(len(candidate.bank_group) - len(candidate.ledger_group)),
        -(len(candidate.bank_group) + len(candidate.ledger_group)),
        -((candidate.period[1] - candidate.period[0]).days),
        _sum_cents(candidate.bank_group),
    )


def _monthly_subset_priority(bank_group: list[Any], ledger_group: list[Any]) -> tuple[float, float, float, float]:
    dates = [record.trans_date for record in bank_group + ledger_group]
    return (
        text_similarity(
            join_unique([_record_text(record) for record in bank_group]),
            join_unique([_record_text(record) for record in ledger_group]),
        ),
        min(_counterparty_consistency(bank_group), _counterparty_consistency(ledger_group)),
        -(max(dates) - min(dates)).days,
        -(len(bank_group) + len(ledger_group)),
    )


def _monthly_balance_score(bank_group: list[Any], ledger_group: list[Any]) -> float:
    amount_score = 1.0 if _sum_cents(bank_group) == _sum_cents(ledger_group) else 0.98
    similarity = text_similarity(
        join_unique([_record_text(record) for record in bank_group]),
        join_unique([_record_text(record) for record in ledger_group]),
    )
    consistency = min(_counterparty_consistency(bank_group), _counterparty_consistency(ledger_group))
    dates = [record.trans_date for record in bank_group + ledger_group]
    span_score = 1 / (1 + ((max(dates) - min(dates)).days if dates else 0))
    return round(0.45 * amount_score + 0.25 * similarity + 0.20 * consistency + 0.10 * span_score, 4)


def _group_signature(bank_group: list[Any], ledger_group: list[Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(record.id for record in bank_group)),
        tuple(sorted(record.id for record in ledger_group)),
    )


def _same_account(bank: Any, ledger: Any, config: dict[str, Any]) -> bool:
    if not config.get("matching", {}).get("strict_account_match", True):
        return True
    b_acc = _digits(bank.row.get("account_no"))
    l_acc = _digits(ledger.row.get("account_no"))
    return not b_acc or not l_acc or b_acc == l_acc


def _rank_manual_time_pool(ledger: Any, pool: list[Any], limit: int) -> list[Any]:
    ledger_text = _record_text(ledger)
    return sorted(
        pool,
        key=lambda bank: (
            bank.trans_date,
            text(bank.row.get("row_no")),
            bank.id,
        ),
    )[:limit]


def _find_time_window_amount_subsets(
    records: list[Any],
    ledger: Any,
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_results: int,
) -> list[list[Any]]:
    records = sorted(records, key=lambda record: (record.trans_date, int(float(record.row.get("row_no") or 0)), record.id))
    lower = target_cents - tol_cents
    upper = target_cents + tol_cents
    results: list[list[Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    for start_idx in range(len(records)):
        total = 0
        window: list[Any] = []
        for end_idx in range(start_idx, min(len(records), start_idx + max_size)):
            record = records[end_idx]
            total += record.amount_cents
            window.append(record)
            if total > upper:
                break
            if len(window) >= 2 and lower <= total <= upper:
                signature = tuple(sorted(item.id for item in window))
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    results.append(window.copy())

    results.sort(key=lambda subset: _manual_time_window_priority(subset, ledger), reverse=True)
    return results[:max_results]


def _manual_time_window_priority(records: list[Any], ledger: Any) -> tuple[float, float, float, float, float]:
    dates = [record.trans_date for record in records]
    distances = [abs((record.trans_date - ledger.trans_date).days) for record in records]
    return (
        _counterparty_consistency(records),
        -max(distances),
        -sum(distances),
        -(max(dates) - min(dates)).days,
        -len(records),
    )


def _manual_window_score(records: list[Any], ledger: Any) -> float:
    consistency = _counterparty_consistency(records)
    distances = [abs((record.trans_date - ledger.trans_date).days) for record in records]
    proximity = 1 / (1 + (sum(distances) / max(len(distances), 1)))
    span = (max(record.trans_date for record in records) - min(record.trans_date for record in records)).days
    span_score = 1 / (1 + span)
    return round(0.55 * consistency + 0.30 * proximity + 0.15 * span_score, 4)


def _counterparty_consistency(records: list[Any]) -> float:
    if not records:
        return 0.0
    counts: dict[str, int] = {}
    for record in records:
        key = _counterparty_key(record)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(records)


def _counterparty_key(record: Any) -> str:
    raw = text(record.row.get("counterparty_name")) or text(record.row.get("summary"))
    return re.sub(r"\W+", "", raw.lower())


def _rank_manual_amount_pool(ledger: Any, pool: list[Any], limit: int) -> list[Any]:
    ledger_text = _record_text(ledger)
    return sorted(
        pool,
        key=lambda bank: (
            -abs((bank.trans_date - ledger.trans_date).days),
            text_similarity(_record_text(bank), ledger_text),
            -abs(bank.amount_cents - ledger.amount_cents),
        ),
        reverse=True,
    )[:limit]


def _find_amount_subsets(
    records: list[Any],
    ledger: Any,
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_results: int,
    max_dp_cents: int,
    max_nodes: int,
) -> list[list[Any]]:
    return _find_amount_subsets_dfs(records, ledger, target_cents, tol_cents, max_size, max_results, max_nodes)


def _find_amount_subsets_dfs(
    records: list[Any],
    ledger: Any,
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_results: int,
    max_nodes: int,
) -> list[list[Any]]:
    records = [record for record in records if record.amount_cents > 0]
    lower = target_cents - tol_cents
    upper = target_cents + tol_cents
    suffix = [0] * (len(records) + 1)
    for idx in range(len(records) - 1, -1, -1):
        suffix[idx] = suffix[idx + 1] + records[idx].amount_cents

    results: list[list[Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()
    nodes = 0

    def dfs(start: int, chosen: list[int], total: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or len(results) >= max_results:
            return
        if len(chosen) >= 2 and lower <= total <= upper:
            subset = [records[item_idx] for item_idx in chosen]
            signature = tuple(sorted(record.id for record in subset))
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                results.append(subset)
            return
        if len(chosen) >= max_size or total > upper:
            return
        if start >= len(records) or total + suffix[start] < lower:
            return
        for idx in range(start, len(records)):
            dfs(idx + 1, chosen + [idx], total + records[idx].amount_cents)
            if nodes > max_nodes or len(results) >= max_results:
                return

    dfs(0, [], 0)
    return _top_amount_subsets(results, ledger, max_results)


def _find_amount_subsets_dp(
    records: list[Any],
    ledger: Any,
    target_cents: int,
    tol_cents: int,
    max_size: int,
    max_results: int,
) -> list[list[Any]]:
    upper = target_cents + tol_cents
    dp: dict[int, list[int]] = {0: []}
    results: list[list[Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()
    collect_limit = max(max_results * 20, max_results)

    for idx, record in enumerate(records):
        amount = record.amount_cents
        additions: dict[int, list[int]] = {}
        for total, chosen in list(dp.items()):
            if len(chosen) >= max_size:
                continue
            new_total = total + amount
            if new_total > upper:
                continue
            new_chosen = chosen + [idx]
            if target_cents - tol_cents <= new_total <= upper and len(new_chosen) >= 2:
                subset = [records[item_idx] for item_idx in new_chosen]
                signature = tuple(sorted(record.id for record in subset))
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    results.append(subset)
                    if len(results) >= collect_limit:
                        return _top_amount_subsets(results, ledger, max_results)
            existing = additions.get(new_total) or dp.get(new_total)
            if existing is None or _subset_priority(new_chosen, records, ledger) > _subset_priority(existing, records, ledger):
                additions[new_total] = new_chosen
        dp.update(additions)
    return _top_amount_subsets(results, ledger, max_results)


def _top_amount_subsets(results: list[list[Any]], ledger: Any, max_results: int) -> list[list[Any]]:
    results.sort(key=lambda subset: _subset_priority_by_records(subset, ledger), reverse=True)
    return results[:max_results]


def _subset_priority(indices: list[int], records: list[Any], ledger: Any) -> tuple[float, float, float, float, float]:
    subset = [records[idx] for idx in indices]
    return _subset_priority_by_records(subset, ledger)


def _subset_priority_by_records(records: list[Any], ledger: Any) -> tuple[float, float, float, float, float]:
    distances = [abs((record.trans_date - ledger.trans_date).days) for record in records]
    dates = [record.trans_date for record in records]
    text_score = sum(text_similarity(_record_text(record), _record_text(ledger)) for record in records) / len(records)
    return (
        -max(distances),
        -sum(distances),
        -(max(dates) - min(dates)).days,
        -len(records),
        text_score,
    )


def _sum_cents(records: list[Any]) -> int:
    return sum(record.amount_cents for record in records)


def _heuristic_score(bank_group: list[Any], ledger_group: list[Any]) -> float:
    amount_score = 1.0 if abs(_sum_cents(bank_group) - _sum_cents(ledger_group)) <= 1 else 0.0
    bank_text = join_unique([_record_text(record) for record in bank_group])
    ledger_text = join_unique([_record_text(record) for record in ledger_group])
    sim = text_similarity(bank_text, ledger_text)
    return round(0.7 * amount_score + 0.3 * sim, 4)


def _record_text(record: Any) -> str:
    return join_unique(
        [
            record.row.get("counterparty_name"),
            record.row.get("summary"),
            record.row.get("description"),
            record.row.get("subject"),
            record.row.get("voucher_no"),
            record.row.get("raw_text"),
        ]
    )


def _record_review_line(record: Any) -> str:
    row = record.row
    return join_unique(
        [
            record.id,
            row.get("transaction_date"),
            row.get("flow"),
            row.get("amount"),
            row.get("counterparty_name"),
            row.get("summary"),
            row.get("description"),
            row.get("voucher_no"),
        ],
        sep=" | ",
    )


def _review_lines(records: list[Any]) -> str:
    return "\n---ROW---\n".join(_record_review_line(record) for record in records)


def _try_accept_candidate(
    config: dict[str, Any],
    candidate: Candidate,
    decision: dict[str, Any],
    used_bank: set[str],
    used_ledger: set[str],
    groups: list[dict[str, Any]],
    amount_tol: int,
    date_tol: int,
    source: str,
) -> bool:
    if any(record.id in used_bank for record in candidate.bank_group):
        return False
    if any(record.id in used_ledger for record in candidate.ledger_group):
        return False
    if abs(_sum_cents(candidate.bank_group) - _sum_cents(candidate.ledger_group)) > amount_tol:
        return False
    if not _candidate_same_month_flow_account(candidate, config):
        return False

    from .matcher import _group_score, _make_group

    confidence = _confidence(decision)
    score = max(
        confidence,
        _group_score(candidate.bank_group, candidate.ledger_group, amount_tol, max(date_tol, 30)),
    )
    match_type = candidate.match_type if source == "manual" and candidate.match_type.startswith("manual_") else f"{source}_{candidate.match_type}"
    group = _make_group(
        config,
        match_type,
        candidate.bank_group,
        candidate.ledger_group,
        round(score, 4),
        len(groups) + 1,
    )
    group["llm_candidate_id"] = candidate.candidate_id
    group["llm_reason"] = text(decision.get("reason"))
    group["manual_review_signature"] = _candidate_signature(candidate)
    groups.append(group)
    used_bank.update(record.id for record in candidate.bank_group)
    used_ledger.update(record.id for record in candidate.ledger_group)
    return True


def _confidence(decision: dict[str, Any]) -> float:
    try:
        return float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ask_llm(config: dict[str, Any], candidates: list[Candidate]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cfg = config.get("matching", {}).get("llm", {})
    batch_size = int(cfg.get("batch_size", 8))
    decisions: list[dict[str, Any]] = []
    total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        payload = {
            "task": (
                "判断每个候选是否可以作为银行流水与序时账匹配结果。"
                "如果 candidate.review_only 为 true，候选只进入人工复核，请重点说明前置规则未能自动匹配的原因、"
                "可能的会计处理方式，以及人工复核时应关注的风险点。"
            ),
            "output_schema": {
                "decisions": [
                    {
                        "candidate_id": "C00001",
                        "approve": True,
                        "confidence": 0.0,
                        "reason": "简短中文理由",
                    }
                ]
            },
            "candidates": [_candidate_payload(candidate) for candidate in batch],
        }
        result, usage = run_match_decision_agent(config, payload, SYSTEM_PROMPT)
        batch_decisions = [item.model_dump() for item in result.decisions]
        decisions.extend(batch_decisions)
        _record_match_llm_usage(usage)
        if usage:
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)
    return decisions, total_usage


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raw = text(value).lower()
    return raw in {"true", "1", "yes", "y", "是", "通过"}


def _load_manual_approvals(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        existing = pd.read_csv(path).fillna("")
    except pd.errors.EmptyDataError:
        return {}
    if "candidate_signature" not in existing:
        return {}
    return {
        text(row["candidate_signature"]): row.to_dict()
        for _, row in existing.iterrows()
        if text(row.get("candidate_signature"))
    }


def _review_row(
    candidate: Candidate,
    decision: dict[str, Any],
    status: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    # 将 LLM 的 approve 决策转换为 1/0 格式，用于初始化 approve 列
    llm_approve_raw = decision.get("approve", "")
    llm_approve_val = ""
    if isinstance(llm_approve_raw, bool):
        llm_approve_val = "1" if llm_approve_raw else "0"
    elif isinstance(llm_approve_raw, str):
        llm_approve_val = "1" if llm_approve_raw.lower() in ("true", "1", "yes") else "0" if llm_approve_raw.lower() in ("false", "0", "no") else ""
    # 如果 previous 中已有 approve 值（来自人工编辑），则保留；否则使用 LLM 的决定
    approve_val = previous.get("approve", "") or llm_approve_val
    return {
        "approve": approve_val,
        "manual_note": previous.get("manual_note", ""),
        "status": status,
        "candidate_id": candidate.candidate_id,
        "candidate_signature": _candidate_signature(candidate),
        "match_type": candidate.match_type,
        "review_reason": candidate.review_reason,
        "llm_approve": llm_approve_raw,
        "llm_confidence": decision.get("confidence", ""),
        "llm_reason": decision.get("reason", ""),
        "period_start": candidate.period[0].isoformat(),
        "period_end": candidate.period[1].isoformat(),
        "date_span_days": (candidate.period[1] - candidate.period[0]).days,
        "counterparty_consistency": round(_counterparty_consistency(candidate.bank_group), 4),
        "bank_count": len(candidate.bank_group),
        "ledger_count": len(candidate.ledger_group),
        "bank_total": round(_sum_cents(candidate.bank_group) / 100, 2),
        "ledger_total": round(_sum_cents(candidate.ledger_group) / 100, 2),
        "bank_ids": "|".join(record.id for record in candidate.bank_group),
        "ledger_ids": "|".join(record.id for record in candidate.ledger_group),
        "bank_summary": _review_lines(candidate.bank_group),
        "ledger_summary": _review_lines(candidate.ledger_group),
    }


def _write_manual_review(path: Path, rows: list[dict[str, Any]]) -> None:
    # 备份已有文件，防止每次 match 运行覆盖人工标注结果
    if path.exists() and path.stat().st_size > 0:
        backup = path.with_name(f"{path.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        path.rename(backup)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _load_cached_decisions(path: Path, candidates: list[Candidate]) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        cached = pd.read_csv(path).fillna("")
    except pd.errors.EmptyDataError:
        return []
    if "candidate_id" not in cached or "candidate_signature" not in cached:
        return []
    cached_by_signature = {
        text(row["candidate_signature"]): row.to_dict()
        for _, row in cached.iterrows()
    }
    decisions: list[dict[str, Any]] = []
    for candidate in candidates:
        signature = _candidate_signature(candidate)
        row = cached_by_signature.get(signature)
        if not row:
            continue
        row = dict(row)
        row["candidate_id"] = candidate.candidate_id
        row["candidate_signature"] = signature
        decisions.append(row)
    return decisions


def _attach_candidate_signatures(decisions: list[dict[str, Any]], candidates: list[Candidate]) -> None:
    signatures = {candidate.candidate_id: _candidate_signature(candidate) for candidate in candidates}
    for decision in decisions:
        candidate_id = text(decision.get("candidate_id"))
        decision["candidate_signature"] = signatures.get(candidate_id, "")


def _candidate_signature(candidate: Candidate) -> str:
    bank_ids = "|".join(record.id for record in candidate.bank_group)
    ledger_ids = "|".join(record.id for record in candidate.ledger_group)
    return f"{candidate.match_type}:{bank_ids}:{ledger_ids}"


def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "match_type": candidate.match_type,
        "review_only": candidate.review_only,
        "review_reason": candidate.review_reason,
        "period": [candidate.period[0].isoformat(), candidate.period[1].isoformat()],
        "bank_total": round(_sum_cents(candidate.bank_group) / 100, 2),
        "ledger_total": round(_sum_cents(candidate.ledger_group) / 100, 2),
        "bank_records": [_record_payload(record) for record in candidate.bank_group],
        "ledger_records": [_record_payload(record) for record in candidate.ledger_group],
    }


def _record_payload(record: Any) -> dict[str, Any]:
    row = record.row
    return {
        "id": record.id,
        "date": row.get("transaction_date"),
        "flow": row.get("flow"),
        "amount": row.get("amount"),
        "account_no": row.get("account_no"),
        "counterparty": _short(row.get("counterparty_name"), 80),
        "summary": _short(row.get("summary"), 80),
        "description": _short(row.get("description"), 120),
        "voucher_no": row.get("voucher_no"),
        "subject": _short(row.get("subject"), 60),
        "raw_text": _short(row.get("raw_text"), 180),
    }


def _short(value: object, limit: int) -> str:
    raw = text(value)
    return raw if len(raw) <= limit else raw[:limit] + "..."


def _write_candidates(path: Path, candidates: list[Candidate], decisions_by_id: dict[str, dict[str, Any]] | None = None) -> None:
    """将候选匹配写入 CSV 文件。
    
    Args:
        path: CSV 文件路径
        candidates: 候选匹配列表
        decisions_by_id: LLM 决策字典（可选），如果提供则写入 approve 和 llm_reason 列
    """
    # 预计算：每个 bank/ledger id 出现在多少个候选中
    from collections import Counter
    bank_id_counts = Counter()
    ledger_id_counts = Counter()
    for candidate in candidates:
        for record in candidate.bank_group:
            bank_id_counts[record.id] += 1
        for record in candidate.ledger_group:
            ledger_id_counts[record.id] += 1

    rows = []
    for idx, candidate in enumerate(candidates):
        # 检测是否有数据在其他行也被匹配
        bank_ids = [record.id for record in candidate.bank_group]
        ledger_ids = [record.id for record in candidate.ledger_group]
        has_duplicate_bank = any(bank_id_counts[bid] > 1 for bid in bank_ids)
        has_duplicate_ledger = any(ledger_id_counts[lid] > 1 for lid in ledger_ids)
        duplicate_info = ""
        if has_duplicate_bank and has_duplicate_ledger:
            duplicate_info = "银行+序时账均有重复"
        elif has_duplicate_bank:
            duplicate_info = "银行流水有重复"
        elif has_duplicate_ledger:
            duplicate_info = "序时账有重复"
        else:
            duplicate_info = "无重复"

        # 获取 LLM 决策（如果提供）
        approve_val = ""
        llm_reason_val = ""
        if decisions_by_id:
            decision = decisions_by_id.get(candidate.candidate_id, {})
            if decision:
                approve_val = "1" if _as_bool(decision.get("approve")) else "0"
                llm_reason_val = text(decision.get("reason"))

        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "match_type": candidate.match_type,
                "review_only": candidate.review_only,
                "llm_review": candidate.llm_review,
                "review_reason": candidate.review_reason,
                "heuristic_score": candidate.heuristic_score,
                "period_start": candidate.period[0].isoformat(),
                "period_end": candidate.period[1].isoformat(),
                "bank_count": len(candidate.bank_group),
                "ledger_count": len(candidate.ledger_group),
                "bank_total": round(_sum_cents(candidate.bank_group) / 100, 2),
                "ledger_total": round(_sum_cents(candidate.ledger_group) / 100, 2),
                "bank_ids": "|".join(bank_ids),
                "ledger_ids": "|".join(ledger_ids),
                "candidate_signature": _candidate_signature(candidate),
                "bank_summary": _review_lines(candidate.bank_group),
                "ledger_summary": _review_lines(candidate.ledger_group),
                "duplicate_info": duplicate_info,
                "approve": approve_val,
                "llm_reason": llm_reason_val,
            }
        )
    
    # 打印写入统计
    approve_count = sum(1 for r in rows if r["approve"] == "1")
    reject_count = sum(1 for r in rows if r["approve"] == "0")
    empty_count = sum(1 for r in rows if r["approve"] == "")
    print(f"_write_candidates: 写入 {len(rows)} 行, approve=1: {approve_count}, approve=0: {reject_count}, approve=空: {empty_count}")
    
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _write_decisions(path: Path, decisions: list[dict[str, Any]]) -> None:
    pd.DataFrame(decisions).to_csv(path, index=False, encoding="utf-8-sig")



