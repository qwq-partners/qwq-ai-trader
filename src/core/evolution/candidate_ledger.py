"""진화 후보 불변 원장 (2026-08-10 — 하네스 설계 Phase 0)

evolve()의 모든 후보 결정(적용/기각/보류/롤백/확정)을 append-only jsonl로
영속화한다. 기존에는 `total_rejected_by_backtest` 카운터만 남아 기각 상세가
소실됐고, 같은 아이디어의 재시도·게이트 예측력 검증이 불가능했다.

파일: ~/.cache/ai_trader/evolution/candidates.jsonl

이벤트 종류:
  applied              — 게이트 통과, 적용됨
  rejected_by_backtest — 게이트 성능 기각 (decision_type=performance_reject)
  gate_error           — 게이트 장애 보류 (decision_type=infra_hold)
  rollback             — 적용 후 평가에서 롤백 (손익비<1.0 등)
  keep                 — 적용 후 평가에서 유지 확정

metric_version: "v2_position" (2026-08-10~, 포지션 원장 기준).
2026-08-08 이전 결정은 v1_event(분할익절 부풀림) — metric_contamination_audit 참조.

future_eval_due_at: 기각 후보의 사후 반사실 재생 예정일 (+28일 ≈ 20영업일).
Phase 1의 predicted-vs-counterfactual 잡이 이 필드로 만기 후보를 찾는다.

실패는 절대 진화를 막지 않는다 (전부 삼킴+경고).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_LEDGER_PATH = Path.home() / ".cache" / "ai_trader" / "evolution" / "candidates.jsonl"

# 사후 반사실 재생까지의 대기일 (달력일 — 20영업일 근사)
_EVAL_DUE_DAYS = 28


def make_candidate_id(parameter: str, old_value: Any, new_value: Any,
                      date_str: str = "") -> str:
    """후보 식별자 — 동일 파라미터·값 변경의 재시도를 추적 가능하게"""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    raw = f"{parameter}|{old_value}|{new_value}|{date_str}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def record_candidate(event: str, parameter: str, old_value: Any, new_value: Any,
                     source: str = "", reason: str = "",
                     gate: Optional[Dict[str, Any]] = None,
                     trigger_failure_ids: Optional[List[str]] = None,
                     candidate_id: str = "") -> str:
    """후보 결정 1건 기록. 반환: candidate_id (호출측이 rollback/keep 연결에 사용)"""
    try:
        cid = candidate_id or make_candidate_id(parameter, old_value, new_value)
        decision_type = {
            "rejected_by_backtest": "performance_reject",
            "gate_error": "infra_hold",
        }.get(event)
        rec: Dict[str, Any] = {
            "time": datetime.now().isoformat(),
            "candidate_id": cid,
            "event": event,
            "parameter": parameter,
            "old_value": old_value,
            "new_value": new_value,
            "source": source,
            "reason": str(reason)[:300],
            "metric_version": "v2_position",
        }
        if decision_type:
            rec["decision_type"] = decision_type
        if gate:
            # 게이트 수치는 예측력 검증(Phase 1)의 predicted 축
            rec["gate"] = gate
        if trigger_failure_ids:
            rec["trigger_failure_ids"] = list(trigger_failure_ids)
        if event in ("rejected_by_backtest", "applied"):
            rec["future_eval_due_at"] = (
                datetime.now() + timedelta(days=_EVAL_DUE_DAYS)
            ).strftime("%Y-%m-%d")
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return cid
    except Exception as e:
        logger.warning(f"[후보원장] 기록 실패 (무시): {e}")
        return candidate_id or ""


def load_candidates(days: int = 90) -> List[Dict[str, Any]]:
    """최근 N일 후보 레코드 (Phase 1 반사실 재생·중복 재시도 차단용)"""
    out: List[Dict[str, Any]] = []
    try:
        if not _LEDGER_PATH.exists():
            return out
        cutoff = datetime.now() - timedelta(days=days)
        for line in _LEDGER_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["time"]) >= cutoff:
                    out.append(r)
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    except Exception as e:
        logger.warning(f"[후보원장] 로드 실패: {e}")
    return out
