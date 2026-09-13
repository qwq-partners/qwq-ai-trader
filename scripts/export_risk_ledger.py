#!/usr/bin/env python3
"""위험 사이징 canary 원장 exporter (계획서 T3, 2026-09).

거래 원장(TradeStorage DB / TradeJournal JSON)과 ExitManager 영속 상태에서
`scripts/review_risk_canary.py` 가 읽는 **검증용 포지션 원장 JSON** 을 만든다.
읽기 전용 — 주문·설정·상태파일을 바꾸지 않는다.

사용:
    venv/bin/python scripts/export_risk_ledger.py --source journal --output ledger.json [--days 90]
    venv/bin/python scripts/export_risk_ledger.py --source db --output ledger.json [--days 90]
    # canary 로 이어서:
    venv/bin/python scripts/review_risk_canary.py --input ledger.json \\
        --cohort risk-sepa_trend-v1 --output report.json

분류 규칙:
- `market_context.entry_risk` 가 없는 과거 거래·수동 포지션은 `entry_risk: null`,
  `cohort_id: "legacy-unmeasured"` (현재 설정으로 초기 위험을 추정하지 않는다 — canary 가 제외).
- `initial_risk_amount` 는 ExitManager 영속 상태(체결 시 확정)를 우선 쓰고, 없으면 매수 체결과
  실제 초기 SL 로 재계산한다(`entry_risk.stop_pct`). 둘 다 없으면 null.
- 같은 종목의 보유 구간이 겹치면(추가 매수로 lot 구분 불가) `lots_ambiguous: true`.

종료 코드: 0 정상 / 2 입력·쓰기 오류.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.entry_risk import confirm_initial_risk, planned_vs_filled_delta  # noqa: E402
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402

LEDGER_VERSION = 1
LEGACY_COHORT = "legacy-unmeasured"


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _ambiguous_ids(trades: Sequence[Any]) -> set:
    """같은 종목의 보유 구간이 겹치는 거래 id 집합 (추가 매수 → lot 구분 불가)."""
    far = datetime.max
    by_symbol: Dict[str, List[Any]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    ambiguous = set()
    for rows in by_symbol.values():
        spans = [(t.entry_time or datetime.min, t.exit_time or far, t.id) for t in rows]
        spans.sort(key=lambda s: s[0])
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                if spans[j][0] < spans[i][1]:
                    ambiguous.add(spans[i][2])
                    ambiguous.add(spans[j][2])
    return ambiguous


def _fills_and_exits(trade: Any, fee_calc) -> (List[Dict[str, Any]], List[Dict[str, Any]]):
    buy_amount = Decimal(str(trade.entry_price)) * int(trade.entry_quantity)
    fills = [{
        "ts": _iso(trade.entry_time),
        "side": "buy",
        "price": str(Decimal(str(trade.entry_price))),
        "quantity": int(trade.entry_quantity),
        "fee": str(fee_calc.calculate_buy_fee(buy_amount)),
    }]
    exits: List[Dict[str, Any]] = []
    if trade.exit_quantity and trade.exit_price:
        sell_amount = Decimal(str(trade.exit_price)) * int(trade.exit_quantity)
        sell = {
            "ts": _iso(trade.exit_time),
            "price": str(Decimal(str(trade.exit_price))),
            "quantity": int(trade.exit_quantity),
            "fee": str(fee_calc.calculate_sell_fee(sell_amount)),
            "reason": trade.exit_type or trade.exit_reason or "",
        }
        exits.append(sell)
        fills.append({**{k: sell[k] for k in ("ts", "price", "quantity", "fee")}, "side": "sell"})
    return fills, exits


def _net_pnl(trade: Any, fills: List[Dict[str, Any]]) -> Optional[str]:
    """수수료 포함 순손익 — 체결에서 재계산(canary 기술 검증과 같은 식)."""
    sells = [f for f in fills if f["side"] == "sell"]
    if not sells:
        return None
    buys = [f for f in fills if f["side"] == "buy"]
    total = Decimal("0")
    for f in sells:
        total += Decimal(f["price"]) * f["quantity"] - Decimal(f["fee"])
    for f in buys:
        total -= Decimal(f["price"]) * f["quantity"] + Decimal(f["fee"])
    return str(total)


def build_ledger(trades: Sequence[Any], exit_states: Dict[str, Dict],
                 applied_sha: Optional[str] = None) -> Dict[str, Any]:
    """TradeRecord 목록 + ExitManager 영속 상태 → canary 원장 dict."""
    fee_calc = get_fee_calculator("KR")
    ambiguous = _ambiguous_ids(trades)
    positions: List[Dict[str, Any]] = []
    for trade in sorted(trades, key=lambda t: (t.entry_time or datetime.min, t.id)):
        ctx = trade.market_context or {}
        snapshot = ctx.get("entry_risk") if isinstance(ctx, dict) else None
        if not isinstance(snapshot, dict):
            snapshot = None
        fills, exits = _fills_and_exits(trade, fee_calc)
        closed = bool(trade.exit_quantity) and int(trade.exit_quantity) >= int(trade.entry_quantity)
        state = exit_states.get(trade.symbol) or {}

        initial_risk: Optional[Decimal] = None
        actual_stop: Optional[Decimal] = None
        if snapshot is not None:
            actual_stop = _dec(state.get("actual_stop_pct"))
            if actual_stop is None:
                actual_stop = _dec(snapshot.get("stop_pct"))
            initial_risk = _dec(state.get("initial_risk_amount"))
            if initial_risk is None and actual_stop is not None:
                try:
                    initial_risk, _ = confirm_initial_risk(
                        [f for f in fills if f["side"] == "buy"], actual_stop)
                except ValueError:
                    initial_risk = None

        delta = (planned_vs_filled_delta(snapshot["planned_risk_amount"], initial_risk)
                 if snapshot is not None and initial_risk is not None
                 and snapshot.get("planned_risk_amount") is not None else None)

        positions.append({
            "position_id": trade.id,
            "symbol": trade.symbol,
            "strategy": trade.entry_strategy or "",
            "cohort_id": snapshot.get("cohort_id", LEGACY_COHORT) if snapshot else LEGACY_COHORT,
            "applied_sha": (snapshot.get("applied_sha") if snapshot else None) or applied_sha or "",
            "status": "closed" if closed else "open",
            "entry_risk": snapshot,
            "initial_risk_amount": str(initial_risk) if initial_risk is not None else None,
            "planned_vs_filled_risk_delta": str(delta) if delta is not None else None,
            "fills": fills,
            "exits": exits,
            "net_pnl": _net_pnl(trade, fills) if closed else None,
            "actual_stop_pct": str(actual_stop) if actual_stop is not None else None,
            "lots_ambiguous": trade.id in ambiguous,
        })

    return {
        "version": LEDGER_VERSION,
        "generated_at": datetime.now().isoformat(),
        "positions": positions,
    }


def load_exit_states(stage_file: Optional[Path] = None) -> Dict[str, Dict]:
    """ExitManager stage 파일(당일 → 최근 7일 폴백) 로드. 없으면 빈 dict."""
    if stage_file is not None:
        try:
            return json.loads(stage_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    cache_dir = Path.home() / ".cache" / "ai_trader"
    today = datetime.now().date()
    for delta in range(0, 7):
        candidate = cache_dir / f"exit_stages_{(today - timedelta(days=delta)).isoformat()}.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def load_trades(source: str, days: int) -> List[Any]:
    """거래 원장 로드 — journal(JSON) 또는 db(TradeStorage 재사용)."""
    from src.core.evolution.trade_journal import TradeJournal
    if source == "journal":
        return TradeJournal().get_recent_trades(days=days)

    from src.data.storage.trade_storage import TradeStorage
    storage = TradeStorage()
    return asyncio.run(_load_from_db(storage, days))


async def _load_from_db(storage: Any, days: int) -> List[Any]:
    """DB 의 trades 를 market_context 포함으로 읽어 저널 캐시에 병합한다.

    `TradeJournal.sync_from_db` 는 market_context 컬럼을 조회하지 않아 entry_risk 가 유실된다 —
    exporter 는 같은 연결(TradeStorage)로 직접 SELECT 한다.
    """
    from src.core.evolution.trade_journal import TradeRecord
    await storage.connect()
    try:
        if storage.pool is None:
            print("[exporter] DB 연결 없음 — JSON 저널만 사용", file=sys.stderr)
            return storage._journal.get_recent_trades(days=days)
        rows = await storage.pool.fetch(
            """SELECT id, symbol, name, entry_time, entry_price, entry_quantity,
                      entry_reason, entry_strategy, entry_signal_score, market_context,
                      exit_time, exit_price, exit_quantity, exit_reason, exit_type,
                      pnl, pnl_pct, holding_minutes
               FROM trades
               WHERE entry_time >= $1 AND market = 'KR'
               ORDER BY entry_time""",
            datetime.now() - timedelta(days=days),
        )
        trades: List[Any] = []
        for row in rows:
            ctx = row["market_context"]
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except json.JSONDecodeError:
                    ctx = {}
            trades.append(TradeRecord(
                id=row["id"], symbol=row["symbol"], name=row["name"] or "",
                entry_time=row["entry_time"], entry_price=float(row["entry_price"]),
                entry_quantity=int(row["entry_quantity"]),
                entry_reason=row["entry_reason"] or "",
                entry_strategy=row["entry_strategy"] or "",
                entry_signal_score=float(row["entry_signal_score"] or 0),
                exit_time=row["exit_time"], exit_price=float(row["exit_price"] or 0),
                exit_quantity=int(row["exit_quantity"] or 0),
                exit_reason=row["exit_reason"] or "", exit_type=row["exit_type"] or "",
                pnl=float(row["pnl"] or 0), pnl_pct=float(row["pnl_pct"] or 0),
                holding_minutes=int(row["holding_minutes"] or 0),
                market_context=ctx if isinstance(ctx, dict) else {},
            ))
        return trades
    finally:
        await storage.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="canary 검증용 위험 원장 생성 (읽기 전용)")
    parser.add_argument("--source", choices=("db", "journal"), default="journal",
                        help="거래 원장 소스 (기본 journal)")
    parser.add_argument("--output", required=True, help="원장 JSON 경로")
    parser.add_argument("--days", type=int, default=90, help="조회 기간 (기본 90일)")
    parser.add_argument("--stage-file", default=None,
                        help="ExitManager stage 파일 경로 (기본: ~/.cache/ai_trader 최근 파일)")
    args = parser.parse_args(argv)

    try:
        trades = load_trades(args.source, args.days)
    except Exception as e:
        print(f"[exporter] 거래 원장 로드 실패: {e}", file=sys.stderr)
        return 2
    states = load_exit_states(Path(args.stage_file) if args.stage_file else None)
    ledger = build_ledger(trades, states, applied_sha=os.getenv("APPLIED_SHA"))
    try:
        Path(args.output).write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8")
    except OSError as e:
        print(f"[exporter] 원장 쓰기 실패: {args.output}: {e}", file=sys.stderr)
        return 2
    measured = sum(1 for p in ledger["positions"] if p["entry_risk"] is not None)
    print(f"[exporter] positions={len(ledger['positions'])} (entry_risk 있음 {measured}) "
          f"→ {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
