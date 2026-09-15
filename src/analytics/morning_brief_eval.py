"""장전 전망의 장후 평가 (2026-09-14 리뷰 후속 T9 요청 5)

07:00 모닝브리프가 단정한 내용을 장 마감 뒤 실측과 대조해 JSONL 원장에 누적한다.
평가 축 4개: 개장 방향 · 종가 방향 · 언급 업종 상대성과 · 전문가 종합 방향.

원칙:
- 모르는 것은 채우지 않는다. 주장이 없거나 실측이 없으면 `hit=None` + 사유.
- 하루 결과로 규칙·임계값을 바꾸지 않는다. 누적 표본(`summarize`)으로만 판단한다.
- 브리프 기준일(`kr_date`)과 평가일이 다르면 평가하지 않는다(`evaluated=False` + 사유).

순수 함수(`evaluate`)와 원장 입출력(`append_ledger`/`summarize`)만 제공하며
시세 조회는 호출자(DailyReportGenerator.evaluate_morning_brief)가 담당한다.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# 방향 판정의 보합 밴드 — |등락률| 이 이 값 미만이면 "flat"
# (09-14 사례 -3.14% 처럼 실제 판정에 영향을 주는 값이 아니라 보합 표기용 상수)
FLAT_BAND_PCT = 0.3

# 전문가 종합점수 → 방향 (07:30 브리핑의 라벨 기준과 동일한 ±5)
_EXPERT_BAND = 5
_BIAS_DIRECTION = {"bull": "up", "bear": "down", "neutral": "flat"}

_AXES = ("open_direction", "close_direction", "expert_direction")


def _direction(pct: Optional[float]) -> Optional[str]:
    """등락률 → up/down/flat. 값이 없으면 None (0 으로 채우지 않는다)."""
    if pct is None:
        return None
    if pct >= FLAT_BAND_PCT:
        return "up"
    if pct <= -FLAT_BAND_PCT:
        return "down"
    return "flat"


def _axis(claimed: Optional[str], actual_pct: Optional[float], *,
          claim_reason: str, actual_reason: str) -> Dict:
    """한 축의 적중 판정 — 주장·실측 중 하나라도 없으면 hit=None"""
    actual_dir = _direction(actual_pct)
    if actual_dir is None:
        return {"claimed": claimed, "actual": None, "actual_pct": None,
                "hit": None, "reason": actual_reason}
    if claimed is None:
        return {"claimed": None, "actual": actual_dir, "actual_pct": actual_pct,
                "hit": None, "reason": claim_reason}
    return {"claimed": claimed, "actual": actual_dir, "actual_pct": actual_pct,
            "hit": claimed == actual_dir, "reason": ""}


def _expert_abstain_reason(expert: Optional[Dict]) -> str:
    """집계의 무보정(0)은 보합 예측이 아니다. 생산자와 같은 커버리지 기준 사용."""
    if not expert:
        return "전문가 종합판단 미기록"
    from ..experts.orchestrator import ExpertOrchestrator

    valid_n = expert.get("valid_n")
    if type(valid_n) is not int or valid_n < 0:
        return "전문가 커버리지 미기록 또는 무효 — 방향 미평가"
    minimum = ExpertOrchestrator.MIN_VALID_EXPERTS
    if valid_n < minimum:
        return f"전문가 자료 부족(유효 {valid_n}명 < {minimum}명) — 무보정은 보합 예측이 아님"
    return ""


def _expert_claim(expert: Optional[Dict]) -> Optional[str]:
    """유효 커버리지가 확인된 전문가 종합점수/bias만 방향 주장으로 해석한다."""
    if _expert_abstain_reason(expert):
        return None
    score = expert.get("score")
    if score is not None:
        if score >= _EXPERT_BAND:
            return "up"
        if score <= -_EXPERT_BAND:
            return "down"
        return "flat"
    return _BIAS_DIRECTION.get(expert.get("bias"))


def evaluate(
    brief: Dict, actual: Dict, *,
    dispatched: bool = False,
    brief_ref: Optional[Dict] = None,
    dispatch_reason: Optional[str] = None,
) -> Dict:
    """브리프 주장과 당일 실측을 대조한다 (순수 함수).

    Args:
        brief: 평가에 쓸 브리프 (claims/scope/generated_at/expert_consensus).
            DailyReportGenerator.evaluate_morning_brief 가 날짜별 아카이브의
            발송 스냅샷을 우선 골라 넘긴다 (2026-09-15 T10 F19).
        actual: {"date": "YYYY-MM-DD",
                 "kospi": {"open_change_pct": float, "close_change_pct": float},
                 "kosdaq": {...},                      # 선택
                 "sectors": {"업종명": 등락률, ...},    # 선택 — KIS 업종명 키
                 "note": str}                           # 선택 — 실측 미수집 사유
        dispatched: 07:30 발송 성공(status=sent) 스냅샷을 실제로 썼는가.
            생성본만 있고 발송 기록이 없으면 False (F19).
        brief_ref: 사용한 생성본 참조 {"version","text_sha256","generated_at","model"}
            (F22 — 원장 레코드가 어떤 버전을 평가했는지 추적).
        dispatch_reason: expert_consensus 가 없을 때 쓸 사유. 발송 스냅샷이
            아예 없으면 "07:30 발송 기록 없음", 발송 실패만 있으면
            "07:30 발송 실패 — 전문가 판단 미평가". expert_consensus 가 있으면
            (claimed 가 None 이 아니면) 이 사유는 쓰이지 않는다.
    """
    # 기준일 대조 — 07:00 생성이 실패한 날 전날 브리프를 오늘 실측과 대조하지 않는다
    brief_date = brief.get("kr_date") or (brief.get("generated_at") or "")[:10]
    actual_date = actual.get("date")
    if brief_date and actual_date and brief_date != actual_date:
        reason = (
            f"브리프 기준일({brief_date}) ≠ 평가일({actual_date}) — "
            f"전날 브리프를 오늘 실측과 대조하지 않는다"
        )
        skipped = {"claimed": None, "actual": None, "actual_pct": None,
                   "hit": None, "reason": reason}
        return {
            "date": actual_date,
            "scope": brief.get("scope"),
            "brief_generated_at": brief.get("generated_at"),
            "brief_date": brief_date,
            "dispatched": dispatched,
            "brief_ref": brief_ref,
            "evaluated_at": datetime.now().isoformat(),
            "evaluated": False,
            "reason": reason,
            "open_direction": dict(skipped),
            "close_direction": dict(skipped),
            "expert_direction": dict(skipped),
            "sectors": [],
        }

    claims = brief.get("claims") or {}
    kospi = actual.get("kospi") or {}
    open_pct = kospi.get("open_change_pct")
    close_pct = kospi.get("close_change_pct")
    evaluated = open_pct is not None or close_pct is not None

    open_axis = _axis(
        claims.get("open_direction"), open_pct,
        claim_reason="브리프에 개장 방향 주장 없음",
        actual_reason="KOSPI 시가 미수집",
    )
    close_axis = _axis(
        claims.get("close_direction"), close_pct,
        claim_reason="브리프에 종가 방향 주장 없음",
        actual_reason="KOSPI 종가 미수집",
    )
    expert = brief.get("expert_consensus")
    expert_axis = _axis(
        _expert_claim(expert), close_pct,
        claim_reason=(_expert_abstain_reason(expert) if expert else dispatch_reason)
                     or "전문가 종합판단 미기록",
        actual_reason="KOSPI 종가 미수집",
    )

    sector_returns = actual.get("sectors") or {}
    sectors: List[Dict] = []
    for sector_claim in claims.get("sectors") or []:
        if isinstance(sector_claim, str):
            # 구버전 브리프(claims.sectors 가 문자열 목록) — 평가 대상이
            # 생성 시점에 고정돼 있지 않으므로 평가하지 않는다 (F20)
            sectors.append({
                "name": sector_claim, "eval_targets": [], "return_pct": None,
                "relative_pct": None, "outperformed": None,
                "reason": "구버전 브리프 — 평가 대상 미기록",
            })
            continue
        name = sector_claim.get("theme") or sector_claim.get("name")
        targets = sector_claim.get("eval_targets") or []
        if not targets:
            sectors.append({
                "name": name, "eval_targets": [], "return_pct": None,
                "relative_pct": None, "outperformed": None,
                "reason": sector_claim.get("reason") or "평가 대상 미합의",
            })
            continue
        vals = [sector_returns[t] for t in targets if sector_returns.get(t) is not None]
        if not vals:
            sectors.append({
                "name": name, "eval_targets": targets, "return_pct": None,
                "relative_pct": None, "outperformed": None,
                "reason": "업종 수익률 미수집",
            })
            continue
        ret = round(sum(vals) / len(vals), 4)   # agg="mean" (현재 유일 규칙)
        if close_pct is None:
            sectors.append({
                "name": name, "eval_targets": targets, "return_pct": ret,
                "relative_pct": None, "outperformed": None,
                "reason": "KOSPI 종가 미수집",
            })
            continue
        relative = round(ret - close_pct, 4)
        sectors.append({
            "name": name, "eval_targets": targets, "return_pct": ret,
            "relative_pct": relative, "outperformed": relative > 0, "reason": "",
        })

    return {
        "date": actual_date or brief.get("date"),
        "scope": brief.get("scope"),
        "brief_generated_at": brief.get("generated_at"),
        "brief_date": brief_date or None,
        "dispatched": dispatched,
        "brief_ref": brief_ref,
        "evaluated_at": datetime.now().isoformat(),
        "evaluated": evaluated,
        "reason": "" if evaluated else (actual.get("note") or "당일 KOSPI 시가·종가 실측 없음"),
        "open_direction": open_axis,
        "close_direction": close_axis,
        "expert_direction": expert_axis,
        "sectors": sectors,
    }


def append_ledger(path, record: Dict) -> None:
    """평가 결과를 JSONL 원장에 1줄 추가한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_ledger_record(path, date_str: str) -> Optional[Dict]:
    """해당 날짜의 기존 평가 레코드를 찾는다.

    재실행(재시도) 시 evaluate_morning_brief 가 같은 날짜를 중복 기록하지
    않도록 막는 용도다 — 원장 레코드가 이미 있으면 재평가하지 않는다
    (2026-09-15 T10 F19 — 평가 분모 중복 증가 방지).
    """
    for rec in _read_ledger(path):
        if rec.get("date") == date_str:
            return rec
    return None


def _read_ledger(path, last_n: Optional[int] = None) -> List[Dict]:
    path = Path(path)
    if not path.exists():
        return []
    records: List[Dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning(f"[브리프평가] 원장 파싱 실패 (건너뜀): {e}")
    if last_n is not None and last_n > 0:
        records = records[-last_n:]
    return records


def summarize(path, last_n: Optional[int] = None) -> Dict:
    """누적 적중률 — 표본 수를 함께 반환한다 (하루 결과로 규칙을 바꾸지 않는다)."""
    records = _read_ledger(path, last_n=last_n)
    summary: Dict = {"samples": len(records)}

    for axis in _AXES:
        hits = 0
        n = 0
        for rec in records:
            hit = (rec.get(axis) or {}).get("hit")
            if hit is None:
                continue
            n += 1
            hits += 1 if hit else 0
        summary[axis] = {
            "n": n, "hits": hits,
            "hit_rate": (hits / n) if n else None,
        }

    sector_hits = 0
    sector_n = 0
    sector_excluded: Dict[str, int] = {}
    for rec in records:
        for sector in rec.get("sectors") or []:
            if sector.get("outperformed") is None:
                reason = sector.get("reason") or "미상"
                sector_excluded[reason] = sector_excluded.get(reason, 0) + 1
                continue
            sector_n += 1
            sector_hits += 1 if sector["outperformed"] else 0
    summary["sectors"] = {
        "n": sector_n, "hits": sector_hits,
        "hit_rate": (sector_hits / sector_n) if sector_n else None,
        # 유효 표본에서 제외된 건수를 사유별로 — F20 (평가 대상 미합의 등)
        "excluded": sector_excluded,
    }

    summary["note"] = (
        f"표본 {len(records)}건 — 하루 결과로 임계값·규칙을 바꾸지 않는다 "
        f"(누적 표본으로만 판단)"
    )
    return summary


def summary_line(path, last_n: Optional[int] = None) -> str:
    """저녁 리포트 한 줄 요약"""
    s = summarize(path, last_n=last_n)
    if s["samples"] == 0:
        return "🔎 장전 전망 평가: 표본 0건 (누적 중)"
    return (
        "🔎 <b>장전 전망 적중</b>: "
        f"개장 {s['open_direction']['hits']}/{s['open_direction']['n']} · "
        f"종가 {s['close_direction']['hits']}/{s['close_direction']['n']} · "
        f"언급업종 초과 {s['sectors']['hits']}/{s['sectors']['n']} · "
        f"전문가 {s['expert_direction']['hits']}/{s['expert_direction']['n']} "
        f"(표본 {s['samples']}건, 하루 결과로 규칙 변경 금지)"
    )
