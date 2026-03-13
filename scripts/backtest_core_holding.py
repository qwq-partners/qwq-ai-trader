#!/usr/bin/env python3
"""
코어홀딩 전략 백테스트 (3개월)

pykrx OHLCV로 KOSPI 대형주 과거 데이터를 가져와서
스코어링 → 월초 리밸런싱 → ExitManager 시뮬레이션.

사용법:
    source venv/bin/activate
    python scripts/backtest_core_holding.py [--months 3] [--initial-capital 23000000]
"""

import argparse
import sys
import os
from datetime import date, timedelta, datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from pykrx import stock as pykrx_stock


# ============================================================
# KOSPI 대형주 유니버스 (시총 상위 40, 백테스트용)
# ============================================================
UNIVERSE = {
    "005930": "삼성전자",     "000660": "SK하이닉스",  "373220": "LG에너지솔루션",
    "005380": "현대차",       "068270": "셀트리온",     "207940": "삼성바이오로직스",
    "005490": "POSCO홀딩스",  "035420": "NAVER",       "000270": "기아",
    "006400": "삼성SDI",      "051910": "LG화학",      "003670": "포스코퓨처엠",
    "105560": "KB금융",       "055550": "신한지주",     "034730": "SK",
    "012330": "현대모비스",    "028260": "삼성물산",     "086790": "하나금융지주",
    "066570": "LG전자",       "032830": "삼성생명",     "096770": "SK이노베이션",
    "035720": "카카오",       "010130": "고려아연",     "018260": "삼성에스디에스",
    "003550": "LG",          "017670": "SK텔레콤",     "033780": "KT&G",
    "316140": "우리금융지주",  "011200": "HMM",         "030200": "KT",
    "034020": "두산에너빌리티", "015760": "한국전력",    "009540": "HD한국조선해양",
    "010950": "S-Oil",       "036570": "엔씨소프트",    "024110": "HSD엔진",
    "047050": "포스코인터내셔널","009830": "한화솔루션",  "004020": "현대제철",
    "352820": "하이브",
}


@dataclass
class BacktestConfig:
    initial_capital: float = 23_000_000
    core_allocation_pct: float = 30.0
    max_positions: int = 3
    position_pct: float = 10.0
    min_score: int = 70
    stop_loss_pct: float = 15.0
    trailing_stop_pct: float = 8.0
    trailing_activate_pct: float = 10.0
    min_price: int = 5000
    fee_buy_pct: float = 0.014
    fee_sell_pct: float = 0.213


@dataclass
class Position:
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    quantity: int
    cost_basis: float
    highest_price: float = 0.0
    trailing_activated: bool = False
    score_at_entry: float = 0.0


@dataclass
class Trade:
    symbol: str
    name: str
    side: str
    date: date
    price: float
    quantity: int
    amount: float
    fee: float
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0


# ============================================================
# 스코어링 (CoreScreener 로직, 펀더멘탈/수급 제외한 기술적 스코어)
# ============================================================
def score_stock(df: pd.DataFrame) -> Tuple[float, List[str]]:
    """
    기술적 지표 기반 스코어링 (최대 65점 + 수급/펀더멘탈 중립 35점)
    df: OHLCV DataFrame (200행 이상 필요)
    """
    if df is None or len(df) < 200:
        return 0.0, ["데이터 부족"]

    closes = df["close"].values.astype(float)
    highs = df["high"].values.astype(float)
    score = 0.0
    reasons = []

    close = closes[-1]
    ma5 = closes[-5:].mean()
    ma20 = closes[-20:].mean()
    ma50 = closes[-50:].mean()
    ma200 = closes[-200:].mean()

    # ── 추세 안정성 (30점) ──
    if ma5 > ma20 > ma50 > ma200:
        score += 10
        reasons.append("MA정배열")

    if close > ma200:
        score += 5
        reasons.append("MA200↑")

    high_52w = highs[-250:].max() if len(highs) >= 250 else highs.max()
    from_high = (close - high_52w) / high_52w * 100
    if from_high >= -15:
        score += 5
        if from_high >= -5:
            reasons.append(f"52주근접({from_high:+.1f}%)")

    if len(closes) >= 126:
        chg_6m = (closes[-1] - closes[-126]) / closes[-126] * 100
        if chg_6m > 0:
            score += 5
            reasons.append(f"6M+{chg_6m:.0f}%")

    if len(closes) >= 20:
        std20 = closes[-20:].std() / closes[-20:].mean() * 100
        if std20 < 3.0:
            score += 5
            reasons.append("저변동")
        elif std20 < 5.0:
            score += 3

    # ── 펀더멘탈 중립 (30점 중 20점 기본 부여 — 실데이터 없으므로) ──
    score += 20
    reasons.append("펀더멘탈중립")

    # ── 수급 중립 (20점 중 12점 기본 부여) ──
    score += 12

    # ── 모멘텀 품질 (20점) ──
    score += 3  # MRS 없음 중립

    if len(closes) >= 21:
        chg_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
        if chg_20d > 0:
            score += 5
        elif chg_20d > -3:
            score += 2

    if len(closes) >= 61:
        chg_60d = (closes[-1] - closes[-61]) / closes[-61] * 100
        if chg_60d > 0:
            score += 5
        elif chg_60d > -5:
            score += 2

    if ma5 > ma20:
        score += 5

    return min(score, 100.0), reasons


# ============================================================
# 백테스트 엔진
# ============================================================
class CoreHoldingBacktest:

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.cash: float = config.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.daily_equity: List[Tuple[date, float]] = []
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

    def run(self, start_date: date, end_date: date):
        print(f"\n{'='*60}")
        print(f"코어홀딩 백테스트")
        print(f"{'='*60}")
        print(f"기간: {start_date} ~ {end_date}")
        print(f"초기 자본: {self.config.initial_capital:,.0f}원")
        print(f"코어 배분: {self.config.core_allocation_pct}%")
        print(f"최대 {self.config.max_positions}종목, 종목당 {self.config.position_pct}%")
        print(f"손절: -{self.config.stop_loss_pct}%, "
              f"트레일링: -{self.config.trailing_stop_pct}% (+{self.config.trailing_activate_pct}% 활성화)")
        print(f"{'='*60}\n")

        # 데이터 선로드 (MA200 필요 → 시작일 -400일)
        preload_start = (start_date - timedelta(days=400)).strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")

        # 거래일 목록
        print("[1/3] 거래일 & 데이터 로드...")
        trading_days = self._get_trading_days(start_date, end_date)
        print(f"  거래일: {len(trading_days)}일")

        if not trading_days:
            print("  거래일 없음. 종료.")
            return

        # 일봉 데이터 로드
        price_data: Dict[str, pd.DataFrame] = {}
        for sym, name in UNIVERSE.items():
            try:
                df = pykrx_stock.get_market_ohlcv_by_date(preload_start, end_str, sym)
                if df is not None and not df.empty:
                    col_map = {"시가": "open", "고가": "high", "저가": "low",
                               "종가": "close", "거래량": "volume"}
                    df = df.rename(columns=col_map)
                    if "close" in df.columns and len(df) >= 200:
                        price_data[sym] = df
            except Exception:
                pass

        print(f"  데이터 로드: {len(price_data)}/{len(UNIVERSE)}종목 (200일+)")

        # 리밸런싱 일자
        rebalance_dates = self._get_rebalance_dates(trading_days)
        print(f"  리밸런싱: {[d.strftime('%m/%d') for d in rebalance_dates]}\n")

        # 일별 시뮬레이션
        print(f"[2/3] 시뮬레이션...\n")

        for day in trading_days:
            if day in rebalance_dates:
                self._do_rebalance(day, price_data)

            self._update_positions(day, price_data)

            equity = self._calc_equity(day, price_data)
            self.daily_equity.append((day, equity))

        # 종료 청산
        self._close_all(trading_days[-1], price_data)

        # 결과
        print(f"\n[3/3] 결과\n")
        self._print_results(start_date, end_date)

    def _get_trading_days(self, start: date, end: date) -> List[date]:
        try:
            df = pykrx_stock.get_market_ohlcv_by_date(
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "005930"
            )
            if df is not None and not df.empty:
                return [d.date() if hasattr(d, 'date') else d for d in df.index]
        except Exception:
            pass
        days = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        return days

    def _get_rebalance_dates(self, trading_days: List[date]) -> List[date]:
        rebalance = []
        seen = set()
        for day in trading_days:
            mk = day.strftime("%Y-%m")
            if mk not in seen and day.day <= 7:
                rebalance.append(day)
                seen.add(mk)
        return rebalance

    def _do_rebalance(self, day: date, price_data: Dict[str, pd.DataFrame]):
        print(f"{'─'*55}")
        print(f"[리밸런싱] {day.strftime('%Y-%m-%d')}  현금={self.cash:,.0f}원  보유={len(self.positions)}종목")

        per_position = self.config.initial_capital * (self.config.position_pct / 100)

        # 스코어링
        candidates = []
        for sym, df in price_data.items():
            df_until = df.loc[:day]
            if len(df_until) < 200:
                continue

            close = float(df_until["close"].iloc[-1])
            if close < self.config.min_price:
                continue

            ma200 = df_until["close"].iloc[-200:].mean()
            if close <= ma200:
                continue

            score, reasons = score_stock(df_until)
            if score >= self.config.min_score:
                candidates.append({
                    "symbol": sym,
                    "name": UNIVERSE.get(sym, sym),
                    "score": score,
                    "price": close,
                    "reasons": reasons,
                })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        print(f"  후보 {len(candidates)}종목:")
        for i, c in enumerate(candidates[:8]):
            held = " ★" if c["symbol"] in self.positions else ""
            print(f"    #{i+1} {c['name']:10s} {c['score']:5.1f}점  "
                  f"@{c['price']:>10,.0f}원  ({', '.join(c['reasons'][:2])}){held}")

        # 교체 판단
        sell_targets = []
        candidate_map = {c["symbol"]: c for c in candidates}
        for sym, pos in list(self.positions.items()):
            cand = candidate_map.get(sym)
            if cand is None:
                print(f"  {pos.name}: 스캔 미포함 → 유지")
                continue
            if cand["score"] < 55:
                sell_targets.append((sym, f"재스코어 {cand['score']:.0f}<55"))
                continue
            df_until = price_data[sym].loc[:day]
            if not df_until.empty:
                cp = float(df_until["close"].iloc[-1])
                pnl = (cp - pos.entry_price) / pos.entry_price * 100
                if pnl <= -10:
                    sell_targets.append((sym, f"리밸런싱 손절 {pnl:.1f}%"))

        for sym, reason in sell_targets:
            if sym in self.positions and sym in price_data:
                df_until = price_data[sym].loc[:day]
                if not df_until.empty:
                    self._sell(sym, day, float(df_until["close"].iloc[-1]), reason)

        # 빈 슬롯만 매수
        empty_slots = self.config.max_positions - len(self.positions)
        buy_count = 0
        for c in candidates:
            if buy_count >= empty_slots:
                break
            if c["symbol"] in self.positions:
                continue

            amount = min(per_position, self.cash * 0.95)
            if amount < 100_000:
                break

            qty = int(amount / c["price"])
            if qty <= 0:
                continue

            self._buy(c["symbol"], c["name"], day, c["price"], qty, c["score"])
            buy_count += 1

        print(f"  → 매도={len(sell_targets)} 매수={buy_count} 보유={len(self.positions)}")

    def _update_positions(self, day: date, price_data: Dict[str, pd.DataFrame]):
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym not in price_data:
                continue

            df_until = price_data[sym].loc[:day]
            if df_until.empty:
                continue

            cp = float(df_until["close"].iloc[-1])
            hp = float(df_until["high"].iloc[-1])
            pos.highest_price = max(pos.highest_price, hp)

            net_pnl = (cp - pos.entry_price) / pos.entry_price * 100 - \
                       self.config.fee_buy_pct - self.config.fee_sell_pct

            # 손절
            if net_pnl <= -self.config.stop_loss_pct:
                self._sell(sym, day, cp, f"손절 {net_pnl:.1f}%")
                continue

            # 트레일링 활성화
            if net_pnl >= self.config.trailing_activate_pct:
                pos.trailing_activated = True

            # 트레일링 스탑
            if pos.trailing_activated and pos.highest_price > 0:
                trail = (cp - pos.highest_price) / pos.highest_price * 100
                if trail <= -self.config.trailing_stop_pct:
                    self._sell(sym, day, cp,
                              f"트레일링 고점{pos.highest_price:,.0f}→{cp:,.0f} ({trail:.1f}%)")

    def _buy(self, symbol: str, name: str, day: date, price: float, qty: int, score: float):
        amount = price * qty
        fee = amount * (self.config.fee_buy_pct / 100)
        total = amount + fee

        if total > self.cash:
            qty = int((self.cash * 0.99) / price)
            if qty <= 0:
                return
            amount = price * qty
            fee = amount * (self.config.fee_buy_pct / 100)
            total = amount + fee

        self.cash -= total
        self.positions[symbol] = Position(
            symbol=symbol, name=name, entry_date=day,
            entry_price=price, quantity=qty, cost_basis=total,
            highest_price=price, score_at_entry=score,
        )
        self.trades.append(Trade(
            symbol=symbol, name=name, side="buy", date=day,
            price=price, quantity=qty, amount=amount, fee=fee,
            reason=f"진입 (점수={score:.0f})",
        ))
        self.total_trades += 1
        print(f"    BUY  {name:10s} {qty:>4}주 @ {price:>10,.0f}원 = {amount:>12,.0f}원 (점수={score:.0f})")

    def _sell(self, symbol: str, day: date, price: float, reason: str):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        amount = price * pos.quantity
        fee = amount * (self.config.fee_sell_pct / 100)
        net = amount - fee
        pnl = net - pos.cost_basis
        pnl_pct = pnl / pos.cost_basis * 100
        hd = (day - pos.entry_date).days

        self.cash += net
        del self.positions[symbol]
        self.trades.append(Trade(
            symbol=symbol, name=pos.name, side="sell", date=day,
            price=price, quantity=pos.quantity, amount=amount, fee=fee,
            reason=reason, pnl=pnl, pnl_pct=pnl_pct, holding_days=hd,
        ))
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        m = "WIN " if pnl > 0 else "LOSS"
        print(f"    SELL {pos.name:10s} {pos.quantity:>4}주 @ {price:>10,.0f}원 "
              f"| {m} {pnl:>+10,.0f}원 ({pnl_pct:>+6.1f}%) {hd}일 | {reason}")

    def _close_all(self, day: date, price_data: Dict[str, pd.DataFrame]):
        if not self.positions:
            return
        print(f"\n{'─'*55}")
        print(f"[종료 청산] {day.strftime('%Y-%m-%d')}")
        for sym in list(self.positions.keys()):
            if sym in price_data:
                df_until = price_data[sym].loc[:day]
                if not df_until.empty:
                    self._sell(sym, day, float(df_until["close"].iloc[-1]), "백테스트 종료")

    def _calc_equity(self, day: date, price_data: Dict[str, pd.DataFrame]) -> float:
        eq = self.cash
        for sym, pos in self.positions.items():
            if sym in price_data:
                df_until = price_data[sym].loc[:day]
                if not df_until.empty:
                    eq += float(df_until["close"].iloc[-1]) * pos.quantity
        return eq

    def _print_results(self, start_date: date, end_date: date):
        final = self.daily_equity[-1][1] if self.daily_equity else self.config.initial_capital
        total_ret = (final - self.config.initial_capital) / self.config.initial_capital * 100
        sell_trades = [t for t in self.trades if t.side == "sell"]
        total_pnl = sum(t.pnl for t in sell_trades)

        # MDD
        peak = self.config.initial_capital
        mdd = 0
        for _, eq in self.daily_equity:
            peak = max(peak, eq)
            dd = (eq - peak) / peak * 100
            mdd = min(mdd, dd)

        # 월별
        monthly_last: Dict[str, float] = {}
        for day, eq in self.daily_equity:
            monthly_last[day.strftime("%Y-%m")] = eq
        monthly_ret = {}
        prev = self.config.initial_capital
        for mk, eq in monthly_last.items():
            monthly_ret[mk] = (eq - prev) / prev * 100
            prev = eq

        print(f"{'='*60}")
        print(f"  결과 요약")
        print(f"{'='*60}")
        print(f"  기간         : {start_date} ~ {end_date} ({(end_date - start_date).days}일)")
        print(f"  초기 자본     : {self.config.initial_capital:>15,.0f}원")
        print(f"  최종 자산     : {final:>15,.0f}원")
        print(f"  총 수익       : {total_pnl:>+15,.0f}원 ({total_ret:+.2f}%)")
        print(f"  MDD           : {mdd:>+15.2f}%")
        print(f"{'─'*60}")

        buy_count = sum(1 for t in self.trades if t.side == "buy")
        print(f"  거래          : 매수 {buy_count}회, 매도 {len(sell_trades)}회")
        if sell_trades:
            wr = self.winning_trades / len(sell_trades) * 100
            print(f"  승/패         : {self.winning_trades}승 {self.losing_trades}패 (승률 {wr:.0f}%)")
            avg_pnl = sum(t.pnl_pct for t in sell_trades) / len(sell_trades)
            avg_hold = sum(t.holding_days for t in sell_trades) / len(sell_trades)
            wins = [t for t in sell_trades if t.pnl > 0]
            losses = [t for t in sell_trades if t.pnl <= 0]
            avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
            pf = abs(sum(t.pnl for t in wins)) / abs(sum(t.pnl for t in losses)) \
                 if losses and sum(t.pnl for t in losses) != 0 else float('inf')
            print(f"  평균 수익률   : {avg_pnl:+.2f}%")
            print(f"  평균 보유기간 : {avg_hold:.0f}일")
            print(f"  평균 승       : {avg_win:+.2f}%")
            print(f"  평균 패       : {avg_loss:+.2f}%")
            print(f"  손익비(PF)    : {pf:.2f}")

        print(f"\n{'─'*60}")
        print(f"  월별 수익률:")
        for mk, ret in monthly_ret.items():
            bar_len = max(1, int(abs(ret) * 3))
            bar = ("+" if ret >= 0 else "-") * bar_len
            print(f"    {mk}  {ret:>+7.2f}%  {bar}")

        if sell_trades:
            print(f"\n{'─'*60}")
            print(f"  거래 내역:")
            for t in sell_trades:
                m = "W" if t.pnl > 0 else "L"
                print(f"    {t.date} {t.name:10s} {t.pnl:>+10,.0f}원 "
                      f"({t.pnl_pct:>+6.1f}%) [{t.holding_days:>3}일] {t.reason}")

        # CSV 저장
        csv_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs", f"backtest_core_{start_date}_{end_date}.csv"
        )
        try:
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            with open(csv_path, "w") as f:
                f.write("date,equity\n")
                for day, eq in self.daily_equity:
                    f.write(f"{day},{eq:.0f}\n")
            print(f"\n  일별 자산 CSV: {csv_path}")
        except Exception:
            pass

        print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="코어홀딩 백테스트")
    parser.add_argument("--months", type=int, default=3, help="기간 (개월)")
    parser.add_argument("--initial-capital", type=float, default=23_000_000, help="초기 자본")
    parser.add_argument("--stop-loss", type=float, default=15.0, help="손절 %%")
    parser.add_argument("--trailing-stop", type=float, default=8.0, help="트레일링 %%")
    parser.add_argument("--trailing-activate", type=float, default=10.0, help="트레일링 활성화 %%")
    args = parser.parse_args()

    config = BacktestConfig(
        initial_capital=args.initial_capital,
        stop_loss_pct=args.stop_loss,
        trailing_stop_pct=args.trailing_stop,
        trailing_activate_pct=args.trailing_activate,
    )

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=args.months * 30)

    bt = CoreHoldingBacktest(config)
    bt.run(start_date, end_date)


if __name__ == "__main__":
    main()
