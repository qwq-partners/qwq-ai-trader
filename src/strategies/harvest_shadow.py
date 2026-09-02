"""비대칭 수확 전략 — 독립 Shadow 실행기 (G3, 2026-08-13)

설계: docs/strategies/asymmetric-harvest-strategy.md §6 G3~G4.
Codex 협의 반영 — 실전 경로 완전 분리:
  · 엔진·exit_manager·risk_manager·position_ledger 무접촉 (주문 구조적 불가)
  · 전용 상태: ~/.cache/ai_trader/harvest_shadow/{pending,positions}.json + ledger.jsonl
  · 채널·진입 정의는 백테스트 모듈(scripts/backtest_t1_gate.py)의 상수·prep을
    직접 임포트 재사용 — 백테스트-실행 정의 불일치 원천 차단 (Codex 승인 조건 4)
  · D1~D3 상태기계 영속화 (조건 3), 피라미딩 없음 (백테스트와 일치)
  · is_core/피라미딩 게이트 논쟁은 G5 승격 전 결정 사항 (본 모듈 무관)

동작 (매 거래일 08:40, FDR 전일 확정 일봉 기준 — 지연 판정):
  1. 체제 게이트: KOSPI(KS11) 종가 > 20일선 (백테스트 _regime_ok_dates와 동일)
  2. 오픈 포지션 청산 판정: -4% 고정 손절 → 10일→(+30% 후) 20일 채널
     (backtest simulate_exit와 동일 규칙의 증분 버전)
  3. pending 체결 판정: 백테스트 Arm B 로직 그대로 (트리거+대금 게이트+과갭 skip)
  4. 신규 D0 감지: 전일 봉 조건 충족 → pending 등록 (3세션 만료)
  5. 이벤트 발생 시 텔레그램 요약 반환 (주간 집계는 토요일 블록)

한계 (의도): T+1 지연 판정 — 장중 실시간 아님. G4 execution shadow에서 실시간
경로로 승격 예정. 실패는 전부 삼킴 (봇 비차단).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

_DIR = Path.home() / ".cache" / "ai_trader" / "harvest_shadow"
_PENDING = _DIR / "pending.json"
_POSITIONS = _DIR / "positions.json"
_LEDGER = _DIR / "ledger.jsonl"
_CURSOR = _DIR / "cursor.json"   # {"last_bar": 마지막 판정 봉, "last_d0": {code: D0일}}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BT_PATH = _PROJECT_ROOT / "scripts" / "backtest_t1_gate.py"

_bt = None


def _load_bt():
    """백테스트 모듈 로드 (정의 단일 원천 — prep/상수 재사용)"""
    global _bt
    if _bt is None:
        spec = importlib.util.spec_from_file_location("_bt_t1_for_shadow", _BT_PATH)
        mod = importlib.util.module_from_spec(spec)
        root = str(_PROJECT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        sys.modules["_bt_t1_for_shadow"] = mod
        spec.loader.exec_module(mod)
        _bt = mod
    return _bt


def _load(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[수확shadow] {path.name} 로드 실패: {e}")
    return {}


def _save(path: Path, data: Dict[str, Any]) -> None:
    import os
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def exit_step(pos: Dict[str, Any], bar) -> tuple:
    """1봉 청산 판정 — 백테스트 `simulate_exit` 루프 1회분과 동치.

    `pos`를 in-place 갱신한다 (runner 승격·stop 승급). 반환: (청산가 or None, 사유).
    백테스트와의 동치는 `tests/test_harvest_shadow.py` parity test가 보장한다
    (Codex 이월 조건 #1) — 이 함수와 backtest.simulate_exit는 함께 수정할 것.
    """
    entry = float(pos["entry"])
    stop = float(pos["stop"])
    if float(bar["Open"]) <= stop:
        return float(bar["Open"]), "손절(갭관통)"
    if float(bar["Low"]) <= stop:
        return stop, "손절"
    if float(bar["High"]) >= entry * 1.30:
        pos["runner"] = True
    ch = float(bar["low20"] if pos.get("runner") else bar["low10"])
    if ch == ch:  # not NaN
        ch = max(ch, stop)
        if float(bar["Close"]) < ch:
            return float(bar["Close"]), "채널이탈"
        pos["stop"] = max(stop, ch * 0.999)  # 채널 승급 (백테스트 동일)
    return None, ""


def weekly_progress(now: Optional[datetime] = None) -> str:
    """토요일 리포트용 G4 진행 요약 (2026-08-16).

    청산 0건이어도 항상 문자열을 반환한다 — 무소식이면 "표본 미도달"인지
    "스캐너 정지"인지 구분할 수 없다. 그래서 누적 체결 수(30 게이트)와
    최근 스캔일 stale 가드를 함께 싣는다.
    """
    now = now or datetime.now()
    cut = (now - timedelta(days=7)).isoformat()
    all_r, week_r = [], []
    if _LEDGER.exists():
        for line in _LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                r = float(rec.get("r", 0))
            except Exception:
                continue
            all_r.append(r)
            if str(rec.get("time", "")) >= cut:
                week_r.append(r)

    def _count(path: Path) -> int:
        obj = _load(path)
        return len(obj) if isinstance(obj, dict) else 0

    lines = [
        f"진행 {len(all_r)}/30체결 · 보유 {_count(_POSITIONS)} · 대기 {_count(_PENDING)}"
    ]
    if week_r:
        lines.append(
            f"금주 청산 {len(week_r)}건 · 승 {sum(1 for v in week_r if v > 0)} · "
            f"합계 {sum(week_r):+.2f}R"
        )
    else:
        lines.append("금주 청산 없음")
    if all_r:
        lines.append(f"누적 {sum(all_r):+.2f}R · 평균 {sum(all_r) / len(all_r):+.2f}R")

    last = str(_load(_DIR / "last_run.json").get("date", ""))
    try:
        stale = (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days > 3
    except ValueError:
        stale = True  # 기록 없음·형식 파손 — 정상으로 볼 수 없다
    lines.append(f"{'⚠️ ' if stale else ''}최근 스캔 {last or '기록 없음'}")

    return (
        "🌾 수확 shadow 주간 (가상 — 주문 없음)\n"
        "━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
        + "\n※ G4 승격 기준: 누적 30체결 + 기대값 >0R"
    )


async def run_daily_shadow_scan() -> tuple:
    """일일 shadow 사이클. 반환: (성공 여부, 텔레그램 요약 or None)

    성공 여부 분리 (Codex 리뷰): 실패한 날을 성공으로 dedup 기록하면
    당일 재시도가 차단됨 — 스케줄러는 success=True일 때만 날짜를 기록한다.
    """
    try:
        return True, await _run()
    except Exception as e:
        logger.warning(f"[수확shadow] 일일 사이클 실패 (재시도 가능): {e}")
        return False, None


def _drop_incomplete_bar(df, today):
    """당일(진행 중) 봉 제거 — 08:40 실패 후 장중 재시도 시 네이버 실시간 행이 완성봉으로 섞이던 문제 (2026-09-03)"""
    if df is None or len(df) == 0:
        return df
    return df[df.index.normalize() < pd.Timestamp(today)]


def _bars_after(d, since: str):
    return d[d.index > pd.Timestamp(since)] if since else d


def _process(bt, data: Dict[str, Any], ok_dates, universe, pending: Dict[str, Any],
             positions: Dict[str, Any], cursor: Dict[str, Any]) -> tuple:
    """상태 전이 1회분 — 순수 함수(네트워크 없음, 원장 append만). 반환: (events, new_d0, cursor)

    봉 커서(cursor["last_bar"]) 이후의 모든 봉을 순서대로 판정한다. 구 드라이버는 최신 봉
    1개만 봐서 실행이 하루 빠지면(FDR 장애·봇 다운·유니버스 이탈) 그 봉의 손절·체결·D0가
    영구 누락돼 백테스트 simulate_exit(전 봉 순회)와 정의가 어긋났다 (2026-09-03 P1).
    커서가 없는 첫 실행은 구 동작(최신 봉만)으로 시작해 승급된 stop을 과거 봉에 재적용하지 않는다.
    """
    events = []
    last_bar = str(cursor.get("last_bar") or "")
    last_d0: Dict[str, str] = dict(cursor.get("last_d0") or {})

    # 1) pending 체결 판정 (백테스트 Arm B — detected_date 이후 미시도 봉 전부)
    for code in list(pending.keys()):
        p = pending[code]
        if p.get("status") != "waiting":
            del pending[code]
            continue
        d = data.get(code)
        if d is None:
            continue
        attempts = p.setdefault("attempts", [])
        for ts, bar in _bars_after(d, str(p["detected_date"])).iterrows():
            bar_date = str(ts)[:10]
            if bar_date in attempts:
                continue
            attempts.append(bar_date)
            gap = (float(bar["Open"]) - p["d0_close"]) / p["d0_close"] * 100
            if gap <= bt.GAP_MAX and (
                float(bar["High"]) >= p["trigger"]
                and float(bar["value"]) >= bt.VALUE_GATE_MULT * p["val_med20"]
            ):
                entry = max(float(bar["Open"]), p["trigger"])
                positions[code] = {
                    "name": p.get("name", ""), "entry": entry,
                    "stop": entry * (1 - bt.RISK_PCT / 100),
                    "entry_date": bar_date, "last_bar": bar_date, "runner": False,
                    "d0_date": p["detected_date"],
                }
                p["status"] = "filled"
                events.append(f"진입 {p.get('name') or code} @{entry:,.0f} (D0 {p['detected_date']})")
                del pending[code]
                break
            if len(attempts) >= bt.PENDING_DAYS:
                events.append(f"만료 {p.get('name') or code} ({bt.PENDING_DAYS}세션 미체결)")
                del pending[code]
                break

    # 2) 오픈 포지션 청산 판정 (simulate_exit 증분 — 마지막 판정 봉 이후 전부, 갭 채움 포함)
    for code in list(positions.keys()):
        d = data.get(code)
        pos = positions[code]
        if d is None or len(d) < 2:
            continue
        since = str(pos.get("last_bar") or last_bar or str(d.index[-2])[:10])
        since = max(since, str(pos.get("entry_date", "")))
        entry = float(pos["entry"])
        for ts, bar in _bars_after(d, since).iterrows():
            bar_date = str(ts)[:10]
            pos["last_bar"] = bar_date
            exit_px, reason = exit_step(pos, bar)
            if exit_px is None:
                continue
            pnl_pct = (exit_px - entry) / entry * 100 - bt.FEE_RT
            rec = {
                "time": datetime.now().isoformat(), "code": code,
                "name": pos.get("name", ""), "entry": entry, "exit": exit_px,
                "entry_date": pos.get("entry_date"), "exit_date": bar_date, "reason": reason,
                "pnl_pct": round(pnl_pct, 2), "r": round(pnl_pct / bt.RISK_PCT, 3),
                "execution_mode": "shadow",
            }
            _DIR.mkdir(parents=True, exist_ok=True)
            with _LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            events.append(
                f"청산 {pos.get('name') or code} {reason} {pnl_pct:+.1f}% ({rec['r']:+.2f}R)"
            )
            del positions[code]
            break

    # 3) 신규 D0 감지 (커서 이후 봉 + 체제 게이트 + 10봉 재신호 억제 — 백테스트 find_d0_signals 동일)
    new_d0 = 0
    for code, d in data.items():
        if code not in universe or code in pending or code in positions or len(d) < 2:
            continue
        since = last_bar or str(d.index[-2])[:10]
        for ts, last in _bars_after(d, since).iterrows():
            d0_date = str(ts)[:10]
            if d0_date not in ok_dates:
                continue  # 체제 게이트: 지수 20일선 아래
            prev = last_d0.get(code)
            if prev and len(d.loc[pd.Timestamp(prev):pd.Timestamp(d0_date)]) - 1 < 10:
                continue
            try:
                cond = (
                    float(last["Close"]) > float(last["high120"])
                    and float(last["value"]) >= 3 * float(last["val_avg20"])
                    and float(last["val_avg20"]) >= 3e9
                    and float(last["Close"]) >= 3000
                    and float(last["ret20"]) <= 0.60
                    and float(last["Close"]) > float(last["ma20"]) > float(last["ma60"])
                )
            except (ValueError, TypeError):
                continue
            if not cond:
                continue
            pending[code] = {
                "detected_date": d0_date,
                "d0_close": float(last["Close"]),
                "trigger": max(float(last["High"]), float(last["high20"])) * bt.TRIGGER_BUF,
                "val_med20": float(last["val_med20"]),
                "status": "waiting", "attempts": [],
            }
            last_d0[code] = d0_date
            new_d0 += 1
            break

    new_last = max([last_bar] + [str(d.index[-1])[:10] for d in data.values() if len(d)])
    if new_last:
        keep_from = (datetime.strptime(new_last, "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
        last_d0 = {c: dt for c, dt in last_d0.items() if dt >= keep_from}
    return events, new_d0, {"last_bar": new_last, "last_d0": last_d0}


async def _run() -> Optional[str]:
    import asyncio
    bt = _load_bt()

    pending = _load(_PENDING)
    positions = _load(_POSITIONS)
    cursor = _load(_CURSOR)
    held = list(positions.keys()) + list(pending.keys())

    # 데이터 로드는 스레드로 (FDR 동기 I/O — 이벤트 루프 비차단)
    def _fetch_all():
        import FinanceDataReader as fdr
        universe = list(bt.load_universe(400))
        ok_dates = bt._regime_ok_dates("2024-01-01")
        today = datetime.now().date()
        data = {}
        # 보유·대기 종목은 유니버스(시총 상위 400)에서 빠져도 계속 판정 — 승자(+30%↑)가 시총
        # 상한을 넘어 이탈하면 포지션이 영원히 동결되던 문제 (2026-09-03)
        for code in dict.fromkeys(universe + held):
            try:
                df = _drop_incomplete_bar(fdr.DataReader(code, "2024-06-01"), today)  # 채널·120일 지표에 충분
                if df is not None and len(df) >= 150:
                    data[code] = bt.prep(df)
            except Exception:
                continue
        return set(universe), ok_dates, data

    universe, ok_dates, data = await asyncio.to_thread(_fetch_all)
    logger.info(f"[수확shadow] 데이터 로드: {len(data)}/{len(universe) + len(held)}종목 (커서 {cursor.get('last_bar') or '없음'})")

    events, new_d0, cursor = _process(bt, data, ok_dates, universe, pending, positions, cursor)

    _save(_PENDING, pending)
    _save(_POSITIONS, positions)
    _save(_CURSOR, cursor)

    summary = None
    if events or new_d0:
        summary = (
            "🌾 수확 shadow (가상 — 주문 없음)\n"
            + "\n".join(f"· {e}" for e in events[:8])
            + (f"\n· 신규 D0 감지 {new_d0}건" if new_d0 else "")
            + f"\n· 보유 {len(positions)} / 대기 {len(pending)}"
        )
        logger.info(f"[수확shadow] 이벤트: 청산/진입 {len(events)}건, 신규 D0 {new_d0}건")
    return summary
