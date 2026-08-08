"""TCA — 체결 비용(슬리피지) 계측 (2026-08-08, Codex 리뷰 후속 과제②)

주문 결정가(Order.price = 신호 시점 가격/매도 호가) 대비 실제 체결가의
비용을 bps로 기록한다. 삽입점은 브로커 check_fills() 단일 지점 —
모든 주문 경로(엔진·배치·안전자산·수동·폴백)가 _pending_orders로
수렴하므로 엔진 측 배관이 필요 없다. 체결 간주(assumed fill) 경로는
check_fills를 타지 않아 자연히 제외된다 (실체결가만 계측).

파일: ~/.cache/ai_trader/tca[_us].jsonl (append-only)
레코드: time, symbol, side, order_type, strategy, qty,
  decision_price, fill_price, cost_bps(+ = 불리), detect_delay_sec, order_id

부호 규칙: cost_bps > 0 = 결정가보다 불리하게 체결
  BUY:  (fill - decision) / decision × 10000
  SELL: (decision - fill) / decision × 10000
SELL 지정가는 decision=매수1호가라 0 근처가 정상, 음수는 가격 개선.

주의: fill 폴링 경로에서 동기 호출 — 실패는 절대 매매를 막지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from loguru import logger

_CACHE_DIR = Path.home() / ".cache" / "ai_trader"


def _tca_path(market: str) -> Path:
    suffix = "" if market.upper() == "KR" else f"_{market.lower()}"
    return _CACHE_DIR / f"tca{suffix}.jsonl"


def record_fill_tca(order: Any, fill: Any, market: str = "KR") -> None:
    """체결 1건의 TCA 레코드 기록 (부분 체결은 증분마다 1레코드)"""
    try:
        decision = order.price
        if decision is None or decision <= 0:
            return  # 벤치마크 없음 (순수 시장가) — 계측 불가
        fill_price = Decimal(str(fill.price))
        decision = Decimal(str(decision))
        raw_bps = float((fill_price - decision) / decision * 10000)
        side = getattr(fill.side, "value", str(fill.side))
        cost_bps = raw_bps if side.upper() == "BUY" else -raw_bps
        try:
            delay = (fill.timestamp - order.created_at).total_seconds()
        except (TypeError, AttributeError):
            delay = None

        rec = {
            "time": datetime.now().isoformat(),
            "symbol": fill.symbol,
            "side": side,
            "order_type": getattr(order.order_type, "value", str(order.order_type)),
            "strategy": order.strategy or "",
            "qty": int(fill.quantity),
            "decision_price": str(decision),
            "fill_price": str(fill_price),
            "cost_bps": round(cost_bps, 1),
            "detect_delay_sec": round(delay, 1) if delay is not None else None,
            "order_id": fill.order_id,
        }
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _tca_path(market).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if abs(cost_bps) >= 50:  # 0.5% 이상 이탈은 즉시 가시화
            logger.warning(
                f"[TCA] {fill.symbol} {side} 슬리피지 {cost_bps:+.1f}bps "
                f"(결정 {decision} → 체결 {fill_price})"
            )
    except Exception as e:
        logger.debug(f"[TCA] 기록 실패 (무시): {e}")


def tca_summary(days: int = 7, market: str = "KR") -> str:
    """전략×방향별 주간 요약 — 토요일 성적표용 텔레그램 문자열 ('' = 데이터 없음)"""
    try:
        path = _tca_path(market)
        if not path.exists():
            return ""
        cutoff = datetime.now().timestamp() - days * 86400
        groups: Dict[str, Dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["time"]).timestamp() < cutoff:
                    continue
                key = f"{r.get('strategy') or 'unknown'}/{r.get('side', '?')}"
                g = groups.setdefault(key, {"n": 0, "sum": 0.0, "worst": 0.0})
                bps = float(r.get("cost_bps", 0))
                g["n"] += 1
                g["sum"] += bps
                g["worst"] = max(g["worst"], bps)
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
        if not groups:
            return ""
        lines = []
        for key in sorted(groups):
            g = groups[key]
            lines.append(
                f"· {key}: n={g['n']}, 평균 {g['sum'] / g['n']:+.1f}bps, "
                f"최악 {g['worst']:+.1f}bps"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[TCA] 요약 실패: {e}")
        return ""
