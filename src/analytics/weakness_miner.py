"""약점 마이너 — 검증자 기반 실패 군집화 (2026-08-10, 하네스 설계 Phase 1)

포지션 원장(진실 원천)의 확정 레코드를 2층 구조로 분석한다:

  1층 결정론적 cohort: 전략 × 진입체제 × 실패양상 버킷 (재실행 시 동일 결과)
  2층 verifier 규칙 판정: cohort가 임계(표본수·손실합)를 넘으면 failure 패턴 확정

LLM은 여기 관여하지 않는다 — 패턴 설명·원인 후보는 후속 복기(LLM)가 쓰되,
causal_status='confirmed' 부여와 진화 발동은 결정론 규칙만 가능 (설계 문서 §2 갭A).

verifier 패턴 (v1 — 관측하며 확장):
  exit_profit_giveback — MFE ≥ +3%였는데 최종 손실 (청산 정책이 이익 반납)
  fast_stop_cluster    — 보유 ≤2일 손절 (진입 신호 자체 의심)
  slow_bleed           — 보유 ≥10일 & MFE < +1% & 손실 (추세 없는 진입 방치)
  data_quality         — unreliable 레코드 (숨기지 않고 별도 집계)

빈도만 보면 다수 패턴만 강화되므로 severity(손실합)·recency(최근 14일)를
별도 축으로 유지한다 (Codex 보완 반영).

출력: ~/.cache/ai_trader/evolution/failures.jsonl (주간 스냅샷 append)
      — failure_id는 pattern×cohort_key 해시로 안정적 (주간 재실행 시 갱신 병합)
소비처: StrategyEvolver._find_weakness_trigger (failure_id → bounded 제안),
        토요일 텔레그램 요약
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_CACHE = Path.home() / ".cache" / "ai_trader"
_FAILURES_PATH = _CACHE / "evolution" / "failures.jsonl"

# verifier 임계 — 표본 미달 패턴은 확정하지 않음 (소표본 과신 방지)
MIN_SAMPLES = 3
RECENT_DAYS = 14


def _fid(pattern: str, cohort_key: str) -> str:
    return hashlib.sha1(f"{pattern}|{cohort_key}".encode()).hexdigest()[:12]


def _classify(rec: Dict[str, Any]) -> Optional[str]:
    """레코드 1건의 실패양상 분류 (결정론) — 실패 아니면 None"""
    if rec.get("unreliable"):
        return "data_quality"
    try:
        pnl_pct = float(rec.get("net_pnl_pct", 0))
        mfe = rec.get("mfe_pct")
        mfe = float(mfe) if mfe is not None else None
        days = rec.get("holding_days")
        days = int(days) if days is not None else None
    except (TypeError, ValueError):
        return None
    if pnl_pct >= 0:
        return None  # 승리 포지션은 실패 마이닝 대상 아님
    if mfe is not None and mfe >= 3.0:
        return "exit_profit_giveback"
    if days is not None and days <= 2:
        _reasons = " ".join(str(e.get("reason", "")) for e in rec.get("exits", []))
        if "손절" in _reasons or "stop" in _reasons.lower():
            return "fast_stop_cluster"
    if days is not None and days >= 10 and (mfe is None or mfe < 1.0):
        return "slow_bleed"
    return None


_PATTERN_MECHANISM = {
    "exit_profit_giveback": "exit",
    "fast_stop_cluster": "entry",
    "slow_bleed": "entry",
    "data_quality": "data",
}


def mine(days: int = 30, market: str = "KR", persist: bool = True) -> List[Dict[str, Any]]:
    """최근 N일 확정 포지션에서 failure 패턴 추출 (결정론 — LLM 무관)

    persist=False면 failures.jsonl에 남기지 않는다 (일일 evolve 트리거 조회용 —
    영속 스냅샷은 토요일 주간 실행만).
    """
    suffix = "" if market.upper() == "KR" else f"_{market.lower()}"
    ledger_path = _CACHE / f"position_ledger{suffix}.jsonl"
    if not ledger_path.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    cohorts: Dict[str, Dict[str, Any]] = {}
    try:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                closed = datetime.fromisoformat(rec.get("closed_at", ""))
                if closed < cutoff:
                    continue
                pattern = _classify(rec)
                if pattern is None:
                    continue
                strategy = rec.get("strategy") or "unknown"
                regime = rec.get("entry_regime") or "unknown"
                cohort_key = f"{strategy}|{regime}|{pattern}"
                c = cohorts.setdefault(cohort_key, {
                    "pattern": pattern, "strategy": strategy, "regime": regime,
                    "n": 0, "loss_sum": 0.0, "pnl_pcts": [],
                    "evidence": [], "first": closed, "last": closed,
                })
                c["n"] += 1
                c["loss_sum"] += float(rec.get("net_pnl", 0))
                c["pnl_pcts"].append(float(rec.get("net_pnl_pct", 0)))
                c["evidence"].append(
                    f"{rec.get('symbol')}@{rec.get('closed_at', '')[:10]}"
                )
                c["first"] = min(c["first"], closed)
                c["last"] = max(c["last"], closed)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    except Exception as e:
        logger.warning(f"[약점마이너] 원장 읽기 실패: {e}")
        return []

    now = datetime.now()
    failures: List[Dict[str, Any]] = []
    for key, c in cohorts.items():
        if c["n"] < MIN_SAMPLES:
            continue  # 소표본 — 확정 보류 (다음 주 재평가)
        failures.append({
            "failure_id": _fid(c["pattern"], key),
            "verifier_outcome": "loss_cluster",
            "pattern": c["pattern"],
            "mechanism": _PATTERN_MECHANISM.get(c["pattern"], "unknown"),
            "causal_status": "supported",   # 규칙 판정 — confirmed는 사람/장기 검증만
            "cohort_key": key,
            "strategy": c["strategy"],
            "regime": c["regime"],
            "sample_size": c["n"],
            "effect_size": round(sum(c["pnl_pcts"]) / len(c["pnl_pcts"]), 2),
            "loss_sum": round(c["loss_sum"], 0),
            "recent": (now - c["last"]).days <= RECENT_DAYS,
            "evidence_refs": c["evidence"][:20],
            "first_seen": c["first"].isoformat(),
            "last_seen": c["last"].isoformat(),
            "confidence": min(0.9, 0.4 + 0.1 * c["n"]),
            "mined_at": now.isoformat(),
        })

    # severity(손실합) 우선 정렬 — 빈도 편향 방지 (희귀·고손실 우선)
    failures.sort(key=lambda f: f["loss_sum"])

    try:
        if failures and persist:
            _FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _FAILURES_PATH.open("a", encoding="utf-8") as f:
                for rec in failures:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[약점마이너] failures 기록 실패: {e}")

    return failures


def top_failure(days: int = 30, market: str = "KR") -> Optional[Dict[str, Any]]:
    """진화 트리거용 최상위 실패 패턴 — recent & 표본 5+ & 손실 최대. 없으면 None"""
    for f in mine(days=days, market=market, persist=False):
        if f["recent"] and f["sample_size"] >= 5 and f["pattern"] != "data_quality":
            return f
    return None


def format_summary(failures: List[Dict[str, Any]]) -> str:
    """토요일 텔레그램 요약 문자열 ('' = 실패 패턴 없음)"""
    if not failures:
        return ""
    lines = []
    for f in failures[:5]:
        lines.append(
            f"· {f['pattern']} [{f['strategy']}/{f['regime']}] "
            f"n={f['sample_size']}, 손실합 {f['loss_sum']:+,.0f}원, "
            f"평균 {f['effect_size']:+.1f}%"
            + (" 🔥최근" if f["recent"] else "")
        )
    return "\n".join(lines)
