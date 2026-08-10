"""포지션 단위 성과 원장 (2026-08-08 — Codex 전략 리뷰 과제①)

분할 익절이 매도 이벤트마다 별도 '승리'로 집계돼 승률·손익비가 부풀려지던 문제
(위키·진화 시스템 입력 오염)를 해소한다. **원 포지션 = 원 성과 레코드**:

  BUY fill → open 누적 (추가 매수는 수량가중) → 부분 매도마다 exit 누적
  → 전량 청산 시 확정 레코드를 append-only jsonl로 영속화

파일:
  ~/.cache/ai_trader/position_ledger[_us].jsonl     — 확정 레코드 (append-only)
  ~/.cache/ai_trader/position_ledger_open[_us].json — 미청산 누적 (재시작 생존)

레코드 필드: symbol, name, strategy, sector, market, entry_time,
  avg_entry_price, initial_qty, buy_fees, exits[{time, qty, price, fee, reason}],
  net_pnl, net_pnl_pct, fees_total, holding_days, mfe_pct, closed_at

주의: engine fill 경로에서 동기 호출된다 — 파일 쓰기는 작고(수 KB) 빈도가 낮아
(체결 시에만) 허용. 실패는 절대 매매를 막지 않는다 (전부 삼킴+경고).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

_CACHE_DIR = Path.home() / ".cache" / "ai_trader"


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


class PositionLedger:
    """원 포지션 단위 성과 원장"""

    def __init__(self, market: str = "KR"):
        self.market = market.upper()
        suffix = "" if self.market == "KR" else f"_{self.market.lower()}"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._ledger_path = _CACHE_DIR / f"position_ledger{suffix}.jsonl"
        self._open_path = _CACHE_DIR / f"position_ledger_open{suffix}.json"
        self._open: Dict[str, Dict[str, Any]] = self._load_open()

    # ── 영속화 ─────────────────────────────────────────────
    def _load_open(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self._open_path.exists():
                return json.loads(self._open_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[원장] open 상태 로드 실패 (빈 상태로 시작): {e}")
        return {}

    def _save_open(self) -> None:
        # fill 콜백(이벤트 루프)에서 호출됨 — fsync 포함 atomic_write는 디스크
        # 컨텐션 시 수십 ms 블로킹 가능 (리뷰 P1). open 상태는 유실돼도
        # 'unreliable 스킵'으로 자가 회복되는 분석 데이터라 plain write 사용.
        try:
            self._open_path.write_text(
                json.dumps(self._open, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[원장] open 상태 저장 실패: {e}")

    # ── 기록 ───────────────────────────────────────────────
    def on_buy(self, symbol: str, qty: int, price: Decimal, fee: Decimal = Decimal("0"),
               strategy: str = "", name: str = "", sector: str = "",
               regime: str = "") -> None:
        """매수 체결 — open 누적 (추가 매수는 수량가중 평단)

        regime: 진입 시점 시장 체제 (2026-08-10, weakness_miner cohort 축)
        """
        try:
            if qty <= 0:
                return
            rec = self._open.get(symbol)
            if rec is None:
                rec = {
                    "symbol": symbol,
                    "name": name,
                    "strategy": strategy,
                    "sector": sector,
                    "market": self.market,
                    "entry_regime": regime,
                    "entry_time": datetime.now().isoformat(),
                    "avg_entry_price": str(price),
                    "initial_qty": int(qty),
                    "remaining_qty": int(qty),
                    "buy_fees": str(fee),
                    "highest_price": str(price),
                    "exits": [],
                }
            else:
                prev_qty = int(rec.get("remaining_qty", 0))
                prev_avg = _dec(rec.get("avg_entry_price", "0"))
                new_qty = prev_qty + qty
                rec["avg_entry_price"] = str(
                    (prev_avg * prev_qty + price * qty) / new_qty if new_qty else price
                )
                rec["initial_qty"] = int(rec.get("initial_qty", 0)) + int(qty)
                rec["remaining_qty"] = new_qty
                rec["buy_fees"] = str(_dec(rec.get("buy_fees", "0")) + fee)
                if strategy and not rec.get("strategy"):
                    rec["strategy"] = strategy
            self._open[symbol] = rec
            self._save_open()
        except Exception as e:
            logger.warning(f"[원장] on_buy 실패 (무시): {e}")

    def on_sell(self, symbol: str, qty: int, price: Decimal, fee: Decimal = Decimal("0"),
                reason: str = "", highest_price: Optional[Decimal] = None) -> None:
        """매도 체결 — exit 누적, 전량 청산 시 확정 레코드 기록"""
        try:
            rec = self._open.get(symbol)
            if rec is None:
                # 원장 도입 전 진입한 포지션 등 — 추적 불가, 조용히 스킵
                logger.debug(f"[원장] {symbol} open 레코드 없음 — 매도 기록 스킵")
                return
            # 원장 잔량 초과분은 기록하지 않는다 (리뷰 P1: 도입 전 보유+이후 추가매수
            # 케이스에서 초과 수량이 proceeds에 들어가 손익이 왜곡됐음).
            # 불일치 발생 시 레코드에 unreliable 표시 → 통계에서 배제.
            _rem = int(rec.get("remaining_qty", 0))
            _qty_eff = min(int(qty), _rem)
            if _qty_eff < int(qty):
                logger.warning(
                    f"[원장] {symbol} 수량 불일치: 매도 {qty} > 원장 잔량 {_rem} "
                    f"— 초과분 미기록, 레코드 unreliable 처리"
                )
                rec["unreliable"] = True
            if _qty_eff > 0:
                rec["exits"].append({
                    "time": datetime.now().isoformat(),
                    "qty": _qty_eff,
                    "price": str(price),
                    "fee": str(fee),
                    "reason": reason[:80],
                })
            rec["remaining_qty"] = max(0, _rem - int(qty))
            if highest_price is not None:
                prev_high = _dec(rec.get("highest_price", "0"))
                if _dec(highest_price) > prev_high:
                    rec["highest_price"] = str(highest_price)
            if rec["remaining_qty"] <= 0:
                self._finalize(symbol, rec)
            else:
                self._save_open()
        except Exception as e:
            logger.warning(f"[원장] on_sell 실패 (무시): {e}")

    def _finalize(self, symbol: str, rec: Dict[str, Any]) -> None:
        """전량 청산 — 수량가중 순손익 확정 후 jsonl append"""
        avg_entry = _dec(rec.get("avg_entry_price", "0"))
        initial_qty = int(rec.get("initial_qty", 0))
        buy_fees = _dec(rec.get("buy_fees", "0"))
        sell_fees = sum(
            (_dec(e.get("fee", "0")) for e in rec.get("exits", [])), Decimal("0")
        )
        proceeds = sum(
            (_dec(e.get("price", "0")) * int(e.get("qty", 0))
             for e in rec.get("exits", [])), Decimal("0")
        )
        cost = avg_entry * initial_qty
        net_pnl = proceeds - cost - buy_fees - sell_fees
        net_pnl_pct = float(net_pnl / cost * 100) if cost > 0 else 0.0
        high = _dec(rec.get("highest_price", "0"))
        mfe_pct = (
            float((high - avg_entry) / avg_entry * 100)
            if avg_entry > 0 and high > 0 else None
        )
        try:
            entry_dt = datetime.fromisoformat(rec.get("entry_time", ""))
            holding_days = (datetime.now() - entry_dt).days
        except (ValueError, TypeError):
            holding_days = None

        record = {
            **{k: rec.get(k) for k in (
                "symbol", "name", "strategy", "sector", "market",
                "entry_regime", "entry_time", "avg_entry_price", "initial_qty", "exits",
            )},
            "unreliable": bool(rec.get("unreliable", False)),
            "buy_fees": str(buy_fees),
            "sell_fees": str(sell_fees),
            "fees_total": str(buy_fees + sell_fees),
            "net_pnl": str(net_pnl),
            "net_pnl_pct": round(net_pnl_pct, 3),
            "mfe_pct": round(mfe_pct, 3) if mfe_pct is not None else None,
            "holding_days": holding_days,
            "closed_at": datetime.now().isoformat(),
        }
        try:
            import os as _os
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                _os.fsync(f.fileno())  # 확정 레코드는 내구성 보장 (리뷰 P2)
            logger.info(
                f"[원장] {symbol} 포지션 확정: {net_pnl:+,.0f}원 ({net_pnl_pct:+.2f}%), "
                f"부분청산 {len(rec.get('exits', []))}회, 보유 {holding_days}일"
            )
        except Exception as e:
            logger.warning(f"[원장] 확정 레코드 기록 실패: {e}")

        # ACE Generator (2026-08-10 Phase 2): 확정 포지션을 결정론 분류해
        # 교훈 후보 등록 — LLM 무관, 실패 시 무시
        try:
            from src.analytics.weakness_miner import (
                PATTERN_MECHANISM, classify_position_record,
            )
            from src.core.evolution.lesson_store import get_lesson_store
            _pattern = classify_position_record(record)
            if _pattern is not None:
                get_lesson_store().upsert(
                    pattern=_pattern,
                    mechanism=PATTERN_MECHANISM.get(_pattern, "unknown"),
                    strategy=str(record.get("strategy") or ""),
                    regime=str(record.get("entry_regime") or ""),
                    evidence_ref=f"{symbol}@{record.get('closed_at', '')[:10]}",
                )
        except Exception as _ls_e:
            logger.debug(f"[원장] 교훈 후보 등록 실패 (무시): {_ls_e}")

        self._open.pop(symbol, None)
        self._save_open()

    # ── 조회 (전략 평가용) ─────────────────────────────────
    def stats_by_strategy(self, days: int = 90) -> Dict[str, Dict[str, Any]]:
        """포지션 단위 전략별 통계 — 승률/총손익/손익비 (분할 중복 없음)"""
        out: Dict[str, Dict[str, Any]] = {}
        try:
            if not self._ledger_path.exists():
                return out
            cutoff = datetime.now().timestamp() - days * 86400
            for line in self._ledger_path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if r.get("unreliable"):
                        continue  # 수량 불일치 레코드는 통계 제외 (리뷰 P1)
                    closed = datetime.fromisoformat(r.get("closed_at", "")).timestamp()
                    if closed < cutoff:
                        continue
                    s = r.get("strategy") or "unknown"
                    g = out.setdefault(s, {"n": 0, "wins": 0, "pnl_sum": 0.0,
                                           "win_pnl": 0.0, "loss_pnl": 0.0})
                    pnl = float(r.get("net_pnl", 0))
                    g["n"] += 1
                    g["pnl_sum"] += pnl
                    if pnl > 0:
                        g["wins"] += 1
                        g["win_pnl"] += pnl
                    else:
                        g["loss_pnl"] += abs(pnl)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
            for s, g in out.items():
                g["win_rate"] = round(g["wins"] / g["n"] * 100, 1) if g["n"] else 0.0
                g["profit_factor"] = (
                    round(g["win_pnl"] / g["loss_pnl"], 2) if g["loss_pnl"] > 0 else None
                )
        except Exception as e:
            logger.warning(f"[원장] 통계 집계 실패: {e}")
        return out


_ledgers: Dict[str, PositionLedger] = {}


def get_position_ledger(market: str = "KR") -> PositionLedger:
    m = market.upper()
    if m not in _ledgers:
        _ledgers[m] = PositionLedger(m)
    return _ledgers[m]
