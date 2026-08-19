"""팀 심의 conviction 사이징 부스트 — shadow 심의의 소프트 연결 (2026-08-20)

에이전트 팀(Bull/Bear 토론, src/agents)은 shadow 단계로 주문 권한이 없다.
counterfactual 실측(2026-08-19, 표본 47건)에서 팀의 HOLD 차단 후보가 5일 평균
+6.31% 상승(하락 26%뿐) — **차단(감액 포함) 용도로는 손해**가 확인됐다.
따라서 역방향만 연결한다: 팀이 BUY로 승인하고 conviction이 높은 종목의
신규 매수 사이즈만 소폭 부스트. HOLD/REJECT는 사이징에 일절 반영하지 않는다.

  - stance == "buy" AND approved AND conviction >= 0.90 → ×1.20
  - stance == "buy" AND approved AND conviction >= 0.75 → ×1.10
  - 그 외 → ×1.0 (무개입)

리서치 근거(docs/research/ai-trading-research-2026-08.md §3-1): 토론 구조에
사이징·주문 "권한"을 주지 말 것 — 이 모듈은 권한이 아니라 ±20% 이내 배율이며,
상한(max_position_pct)은 엔진에서 재적용된다. 승격/철회는 부스트 적용 거래의
성과 표본이 쌓인 뒤 판단한다.

캘린더 시즈널리티와 동일한 '사이징 배율' 패턴. TEAM_CONVICTION=0 으로 비활성화.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

VERDICT_DIR = Path.home() / ".cache" / "ai_trader" / "team_verdicts"

CONV_STRONG = 0.90       # ×1.20
CONV_BASE = 0.75         # ×1.10 (MIN_CONVICTION과 동일 선)
MULT_STRONG = 1.20
MULT_BASE = 1.10

# 핫패스 캐시 — 파일 mtime이 바뀔 때만 재파싱 (심의는 하루 2회 갱신)
_cache: dict = {"path": None, "mtime": None, "verdicts": {}}
_logged: set = set()


def _enabled() -> bool:
    return os.getenv("TEAM_CONVICTION", "1") != "0"


def _load_today_verdicts() -> dict:
    """오늘 verdicts 파일을 {symbol: (stance, approved, conviction)}로 로드 (mtime 캐시)"""
    path = VERDICT_DIR / f"verdicts_{date.today():%Y%m%d}.json"
    try:
        if not path.exists():
            if _cache["path"] == str(path):
                return _cache["verdicts"]
            _cache.update({"path": str(path), "mtime": None, "verdicts": {}})
            return {}
        mtime = path.stat().st_mtime
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return _cache["verdicts"]
        rows = json.loads(path.read_text(encoding="utf-8"))
        verdicts = {}
        for row in rows if isinstance(rows, list) else []:
            sym = row.get("symbol")
            decision = row.get("decision") or {}
            proposal = decision.get("proposal") or {}
            conv = proposal.get("conviction")
            if sym is None or conv is None:
                continue
            verdicts[str(sym)] = (
                str(decision.get("stance", "")).lower(),
                bool(decision.get("approved", False)),
                float(conv),
            )
        _cache.update({"path": str(path), "mtime": mtime, "verdicts": verdicts})
        return verdicts
    except Exception as e:
        logger.debug(f"[팀conviction] verdicts 로드 실패 (무시): {e}")
        return _cache.get("verdicts", {})


def team_conviction_multiplier(symbol: str) -> Tuple[float, str]:
    """종목별 팀 심의 conviction 부스트 배율.

    Returns:
        (배율, 사유 태그) — 무개입이면 (1.0, "")
    """
    if not _enabled() or not symbol:
        return 1.0, ""

    verdicts = _load_today_verdicts()
    entry: Optional[tuple] = verdicts.get(str(symbol).lstrip("A"))
    if entry is None:
        return 1.0, ""
    stance, approved, conviction = entry
    if stance != "buy" or not approved:
        return 1.0, ""

    if conviction >= CONV_STRONG:
        mult, tag = MULT_STRONG, f"팀BUY(conv {conviction:.2f})"
    elif conviction >= CONV_BASE:
        mult, tag = MULT_BASE, f"팀BUY(conv {conviction:.2f})"
    else:
        return 1.0, ""

    key = (date.today().isoformat(), symbol)
    if key not in _logged:
        _logged.add(key)
        if len(_logged) > 64:
            _logged.clear()
            _logged.add(key)
        logger.info(f"[팀conviction] {symbol} 사이징 ×{mult:.2f} — {tag}")
    return mult, tag
