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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

_DIR = Path.home() / ".cache" / "ai_trader" / "harvest_shadow"
_PENDING = _DIR / "pending.json"
_POSITIONS = _DIR / "positions.json"
_LEDGER = _DIR / "ledger.jsonl"

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


async def _run() -> Optional[str]:
    import asyncio
    bt = _load_bt()
    events = []

    # 데이터 로드는 스레드로 (FDR 동기 I/O — 이벤트 루프 비차단)
    def _fetch_all():
        import FinanceDataReader as fdr
        codes = bt.load_universe(400)
        ok_dates = bt._regime_ok_dates("2024-01-01")
        data = {}
        for code in codes:
            try:
                df = fdr.DataReader(code, "2024-06-01")  # 채널·120일 지표에 충분
                if df is not None and len(df) >= 150:
                    data[code] = bt.prep(df)
            except Exception:
                continue
        return codes, ok_dates, data

    codes, ok_dates, data = await asyncio.to_thread(_fetch_all)
    logger.info(f"[수확shadow] 데이터 로드: {len(data)}/{len(codes)}종목")

    pending = _load(_PENDING)
    positions = _load(_POSITIONS)

    # 1) 오픈 포지션 청산 판정 (백테스트 simulate_exit 증분 규칙과 동일)
    for code in list(positions.keys()):
        d = data.get(code)
        pos = positions[code]
        if d is None or len(d) < 2:
            continue
        last = d.iloc[-1]          # 전일 완성봉
        last_date = str(d.index[-1])[:10]
        if last_date <= str(pos.get("entry_date", "")):
            continue  # 진입일 이후 새 봉 없음
        entry = float(pos["entry"])
        exit_px, reason = exit_step(pos, last)
        if exit_px is not None:
            pnl_pct = (exit_px - entry) / entry * 100 - bt.FEE_RT
            rec = {
                "time": datetime.now().isoformat(), "code": code,
                "name": pos.get("name", ""), "entry": entry, "exit": exit_px,
                "entry_date": pos.get("entry_date"), "reason": reason,
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

    # 2) pending 체결 판정 (백테스트 Arm B 로직 — 전일 봉 기준)
    for code in list(pending.keys()):
        p = pending[code]
        if p.get("status") != "waiting":
            del pending[code]
            continue
        d = data.get(code)
        if d is None:
            continue
        last = d.iloc[-1]
        last_date = str(d.index[-1])[:10]
        if last_date <= p["detected_date"] or last_date in p.get("attempts", []):
            continue  # 새 봉 없음 (휴장 등)
        p.setdefault("attempts", []).append(last_date)
        gap = (float(last["Open"]) - p["d0_close"]) / p["d0_close"] * 100
        if gap <= bt.GAP_MAX and (
            float(last["High"]) >= p["trigger"]
            and float(last["value"]) >= bt.VALUE_GATE_MULT * p["val_med20"]
        ):
            entry = max(float(last["Open"]), p["trigger"])
            positions[code] = {
                "name": p.get("name", ""), "entry": entry,
                "stop": entry * (1 - bt.RISK_PCT / 100),
                "entry_date": last_date, "runner": False,
                "d0_date": p["detected_date"],
            }
            p["status"] = "filled"
            events.append(f"진입 {p.get('name') or code} @{entry:,.0f} (D0 {p['detected_date']})")
            del pending[code]
        elif len(p["attempts"]) >= 3:
            events.append(f"만료 {p.get('name') or code} (3세션 미체결)")
            del pending[code]

    # 3) 신규 D0 감지 (전일 봉이 조건 충족 + 체제 게이트)
    new_d0 = 0
    for code, d in data.items():
        if code in pending or code in positions:
            continue
        i = len(d) - 1
        last = d.iloc[i]
        d0_date = str(d.index[i])[:10]
        if d0_date not in ok_dates:
            continue  # 체제 게이트: 지수 20일선 아래
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
        new_d0 += 1

    _save(_PENDING, pending)
    _save(_POSITIONS, positions)

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
