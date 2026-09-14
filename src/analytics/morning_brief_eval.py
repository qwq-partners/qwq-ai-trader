"""장전 전망의 장후 평가 (2026-09-14 리뷰 후속 T9 요청 5)

07:00 모닝브리프가 단정한 내용을 장 마감 뒤 실측과 대조해 JSONL 원장에 누적한다.
평가 축 4개: 개장 방향 · 종가 방향 · 언급 업종 상대성과 · 전문가 종합 방향.

원칙:
- 모르는 것은 채우지 않는다. 주장이 없거나 실측이 없으면 `hit=None` + 사유.
- 하루 결과로 규칙·임계값을 바꾸지 않는다. 누적 표본(`summarize`)으로만 판단한다.

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


def _expert_claim(expert: Optional[Dict]) -> Optional[str]:
    """전문가 종합점수/bias → 방향 주장"""
    if not expert:
        return None
    score = expert.get("score")
    if score is not None:
        if score >= _EXPERT_BAND:
            return "up"
        if score <= -_EXPERT_BAND:
            return "down"
        return "flat"
    return _BIAS_DIRECTION.get(expert.get("bias"))


def evaluate(brief: Dict, actual: Dict) -> Dict:
    """브리프 주장과 당일 실측을 대조한다 (순수 함수).

    Args:
        brief: 모닝브리프 캐시 레코드 (claims/scope/generated_at/expert_consensus)
        actual: {"date": "YYYY-MM-DD",
                 "kospi": {"open_change_pct": float, "close_change_pct": float},
                 "kosdaq": {...},                      # 선택
                 "sectors": {"업종명": 등락률, ...}}     # 선택
    """
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
    expert_axis = _axis(
        _expert_claim(brief.get("expert_consensus")), close_pct,
        claim_reason="전문가 종합판단 미기록",
        actual_reason="KOSPI 종가 미수집",
    )

    sector_returns = actual.get("sectors") or {}
    sectors: List[Dict] = []
    for name in claims.get("sectors") or []:
        ret = sector_returns.get(name)
        if ret is None or close_pct is None:
            sectors.append({
                "name": name, "return_pct": ret, "relative_pct": None,
                "outperformed": None,
                "reason": "업종 수익률 미수집" if ret is None else "KOSPI 종가 미수집",
            })
            continue
        relative = round(ret - close_pct, 4)
        sectors.append({
            "name": name, "return_pct": ret, "relative_pct": relative,
            "outperformed": relative > 0, "reason": "",
        })

    return {
        "date": actual.get("date") or brief.get("date"),
        "scope": brief.get("scope"),
        "brief_generated_at": brief.get("generated_at"),
        "evaluated_at": datetime.now().isoformat(),
        "evaluated": evaluated,
        "reason": "" if evaluated else "당일 KOSPI 시가·종가 실측 없음",
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
    for rec in records:
        for sector in rec.get("sectors") or []:
            if sector.get("outperformed") is None:
                continue
            sector_n += 1
            sector_hits += 1 if sector["outperformed"] else 0
    summary["sectors"] = {
        "n": sector_n, "hits": sector_hits,
        "hit_rate": (sector_hits / sector_n) if sector_n else None,
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
