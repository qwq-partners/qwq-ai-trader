"""게이트 사후 반사실 재생 (2026-08-10 — 하네스 설계 Phase 1)

후보 원장에서 재생 만기(future_eval_due_at 경과)가 된 기각/적용 후보를
결정 이후 구간의 데이터로 A/B 재실행해, **게이트 판단이 방향적으로 옳았는지**
(calibration)를 축적한다.

용어 주의 (Codex 정정): 기각 후보는 적용된 적이 없으므로 "실현 성과"가 아니라
**사후 반사실(counterfactual) 재생**이다.

한계 (Phase 3 PIT 전까지): 백테스트 유니버스가 현재 시점 기준이라 생존편향이
있다 — replay 레코드에 pit=false로 명시하고, PIT 도입 후 개선한다.

주간 실행 (토요일), 회당 최대 2건 (백테스트 비용 제한).
결과: candidates.jsonl에 event="replay" append + 요약 문자열 반환.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from .backtest_gate import BT_TIMEOUT_SEC, BacktestGate
from .candidate_ledger import load_candidates, record_candidate

_MAX_REPLAYS_PER_RUN = 2


async def run_due_replays(gate: Optional[BacktestGate] = None) -> str:
    """만기 후보 재생. 반환: 텔레그램 요약 ('' = 재생 대상 없음)"""
    today = datetime.now().strftime("%Y-%m-%d")
    candidates = load_candidates(days=120)
    if not candidates:
        return ""

    # 후보별 최신 상태 집계 (replay 완료 여부 포함)
    latest: Dict[str, Dict[str, Any]] = {}
    replayed: set = set()
    for c in candidates:
        cid = c.get("candidate_id", "")
        if c.get("event") == "replay":
            replayed.add(cid)
            continue
        if c.get("event") in ("rejected_by_backtest", "applied"):
            latest[cid] = c  # 시간순이므로 마지막 승자

    due = [
        c for cid, c in latest.items()
        if cid not in replayed
        and c.get("future_eval_due_at")
        and c["future_eval_due_at"] <= today
    ][:_MAX_REPLAYS_PER_RUN]
    if not due:
        return ""

    gate = gate or BacktestGate()
    lines: List[str] = []
    for c in due:
        try:
            result = await _replay_one(gate, c)
            if result:
                lines.append(result)
        except Exception as e:
            logger.warning(f"[게이트재생] {c.get('candidate_id')} 실패 (무시): {e}")
    return "\n".join(lines)


async def _replay_one(gate: BacktestGate, cand: Dict[str, Any]) -> str:
    param_key = str(cand.get("parameter", ""))
    strategy, _, parameter = param_key.partition(".")
    fields = gate._resolve_fields(strategy, parameter)
    if not fields:
        # 백테스트 미지원 파라미터 — 재생 불가로 마킹 (재시도 방지)
        record_candidate(
            event="replay", parameter=param_key,
            old_value=cand.get("old_value"), new_value=cand.get("new_value"),
            source="replay", reason="백테스트 미지원 — 재생 불가",
            candidate_id=cand.get("candidate_id", ""),
        )
        return ""

    decided = datetime.fromisoformat(cand["time"])
    months = max(1, round((datetime.now() - decided).days / 30))

    module = await asyncio.to_thread(gate._load_module)
    base = await asyncio.wait_for(
        asyncio.to_thread(
            gate._run_once, module,
            {**{f: cand.get("old_value") for f in fields}, "months": months},
        ),
        timeout=BT_TIMEOUT_SEC,
    )
    candi = await asyncio.wait_for(
        asyncio.to_thread(
            gate._run_once, module,
            {**{f: cand.get("new_value") for f in fields}, "months": months},
        ),
        timeout=BT_TIMEOUT_SEC,
    )
    base_ret = float((base or {}).get("total_return_pct", 0) or 0)
    cand_ret = float((candi or {}).get("total_return_pct", 0) or 0)
    helped = cand_ret > base_ret
    was_applied = cand.get("event") == "applied"
    gate_correct = (was_applied and helped) or (not was_applied and not helped)

    record_candidate(
        event="replay", parameter=param_key,
        old_value=cand.get("old_value"), new_value=cand.get("new_value"),
        source="replay",
        reason=f"사후 반사실 재생 ({months}개월, pit=false)",
        gate={
            "replay_months": months, "pit": False,
            "base_return_pct": base_ret, "cand_return_pct": cand_ret,
            "original_event": cand.get("event"),
            "gate_correct": gate_correct,
        },
        candidate_id=cand.get("candidate_id", ""),
    )
    verdict = "✅ 게이트 판단 적중" if gate_correct else "❌ 게이트 판단 빗나감"
    return (
        f"· {param_key} {cand.get('old_value')}→{cand.get('new_value')} "
        f"[{cand.get('event')}] 재생 {months}개월: "
        f"기준 {base_ret:+.1f}% vs 변경 {cand_ret:+.1f}% → {verdict}"
    )
