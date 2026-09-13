"""ExitManager 특성화 테스트 (2026-09-13 리뷰 — 미검증 자금 경로 고정)

대상: src/strategies/exit_manager.py
  분할 익절 단계 전이 / 손절(ATR 클램프·우선순위) / 본전 보호 / 복합 트레일링 /
  stale·보유기간 청산 / on_fill 부분체결 / stage 영속화·복원 / exit_exempt / 레짐 파라미터

실행: venv/bin/python -m pytest tests/test_exit_manager_characterization.py -q
프로덕션 캐시·네트워크 무접촉 — Path.home()을 tmp_path로 패치, 합성 가격만 사용.

⚠️ 현재 동작을 "그대로" 고정한다 (리팩터 회귀 감지용). 여기서 깨지면 의도된 변경인지
   먼저 확인하고 docs/risk/risk-and-exit.md 와 함께 갱신할 것.
   기준 시나리오: KR, 100주 @10,000원. KR은 수수료 포함 순손익률(net)로 판정하므로
   gross +10% (11,000원) ≈ net +9.75% → TP1 미발동, 11,100원(net +10.75%) 발동.
"""

import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.types import Position  # noqa: E402
from src.strategies.exit_manager import (  # noqa: E402
    REGIME_EXIT_PARAMS,
    ExitConfig,
    ExitManager,
    ExitStage,
)

SYM = "000001"
D = Decimal


# ── 공통 픽스처/헬퍼 ────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    """ExitManager가 ~/.cache/ai_trader 대신 tmp_path를 쓰도록 Path.home() 패치."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _pos(qty=100, price="10000", cur=None, **kw):
    return Position(
        symbol=SYM, quantity=qty, avg_price=D(price),
        current_price=D(cur if cur is not None else price),
        entry_time=kw.pop("entry_time", datetime.now()), **kw,
    )


def _freeze_bizdays(monkeypatch, n):
    """보유 영업일·신고가 경과일 계산을 상수로 고정 (달력/공휴일 의존 제거)."""
    monkeypatch.setattr(ExitManager, "_count_business_days", lambda self, s, e: n)


def _bars(n=20, close=10000, rng=300):
    """calculate_atr용 합성 OHLC (최신→과거). TR=rng*2 → ATR%=rng*2/close*100."""
    return {
        "high": [D(close + rng)] * n,
        "low": [D(close - rng)] * n,
        "close": [D(close)] * n,
    }


def _walk(em, price, fill=True):
    """update_price 후 (fill=True면) 반환 수량을 그대로 체결 처리."""
    sig = em.update_price(SYM, D(price))
    if sig and fill:
        em.on_fill(SYM, sig[1], D(price))
    return sig


def _to_first(em):
    em.register_position(_pos())
    assert _walk(em, 11100) == ("sell_partial", 10, "1차 익절 (10%): 10.75% (목표=10.0%)")
    assert em.get_state(SYM).current_stage is ExitStage.FIRST
    return em.get_state(SYM)


# ── 1) 단계 전이 NONE → FIRST → SECOND → THIRD → TRAILING ────


def test_stage_walk_none_to_trailing(home):
    em = ExitManager()
    em.register_position(_pos())
    st = em.get_state(SYM)
    assert (st.current_stage, st.remaining_quantity, st.initial_quantity) == (ExitStage.NONE, 100, 100)

    # FIRST: +10% → 10% 매도 (int(100*0.10)=10). stage는 on_fill 후에만 승격
    sig = em.update_price(SYM, D(11100))
    assert sig == ("sell_partial", 10, "1차 익절 (10%): 10.75% (목표=10.0%)")
    assert st.current_stage is ExitStage.NONE and st.pending_stage is ExitStage.FIRST
    assert st.pending_target_qty == 10 and st.remaining_quantity == 100
    # pending 중 재호출은 중복 신호를 내지 않는다
    assert em.update_price(SYM, D(11100)) is None
    em.on_fill(SYM, 10, D(11100))
    assert (st.current_stage, st.remaining_quantity, st.pending_stage) == (ExitStage.FIRST, 90, None)

    # SECOND: +15% → 잔여의 50% (int(90*0.5)=45)
    assert _walk(em, 11600) == ("sell_partial", 45, "2차 익절 (50%): 15.74% (목표=15.0%)")
    assert (st.current_stage, st.remaining_quantity) == (ExitStage.SECOND, 45)

    # THIRD: +25% → 잔여의 50% (int(45*0.5)=22)
    assert _walk(em, 12600) == ("sell_partial", 22, "3차 익절 (50%): 25.71% (목표=25.0%)")
    assert (st.current_stage, st.remaining_quantity) == (ExitStage.THIRD, 23)

    # TRAILING 전환: THIRD & net ≥ third_pct+1 (26%) — 신호 없이 stage만 승격,
    # 같은 호출에서 본전보호(1R=SL 5% 도달)도 활성화
    assert _walk(em, 12700) is None
    assert st.current_stage is ExitStage.TRAILING and st.breakeven_activated is True
    assert st.highest_price == D(12700)

    # 고점 대비 -3% (ATR 미전달 → max(2%×1.5, 3%)=3%) → 잔량 전량
    assert _walk(em, 12300, fill=False) == (
        "sell_all", 23, "ATR트레일링: 고점 대비 -3.15% (한도=-3.0%)")
    assert len(st.exit_history) == 4


def test_kr_net_pnl_includes_fees_but_us_does_not(home):
    # KR: gross +10% = net +9.75% → TP1 미발동 (수수료·거래세 0.227% 차감)
    kr = ExitManager()
    kr.register_position(_pos())
    assert kr.update_price(SYM, D(11000)) is None
    # US: zero-commission → include_fees 강제 False, gross +10%에서 즉시 발동
    us = ExitManager(config=ExitConfig(), market="US")
    assert us.config.include_fees is False
    us.register_position(_pos())
    assert us.update_price(SYM, D(11000)) == ("sell_partial", 10, "1차 익절 (10%): 10.00% (목표=10.0%)")


def test_trailing_before_first_requires_activate_pct_at_current_price(home):
    """NONE 단계 트레일링은 '현재가 기준 net ≥ trailing_activate_pct(5%)'일 때만 판정.
    고점 대비 -5%라도 현재 수익이 5% 미만이면 침묵한다 (설계: 활성화 조건)."""
    em = ExitManager()
    em.register_position(_pos())
    assert em.update_price(SYM, D(11000)) is None          # net 9.75% — 고점 갱신
    assert em.update_price(SYM, D(10450)) is None          # 고점 -5%, net 4.26% < 5 → 무반응
    assert em.get_state(SYM).current_stage is ExitStage.NONE
    assert em.update_price(SYM, D(10650)) == (            # net 6.26%, 고점 -3.18%
        "sell_all", 100, "트레일링: 고점 대비 -3.18% (한도=-3.0%)")


# ── 2) 손절 / 본전보호 / 복합 트레일링 / stale / 보유기간 ────


@pytest.mark.parametrize("min_stop,rng,atr,expected_sl,eff_ts", [
    # ATR 6% × 2.0 = 12 → max_stop 8.0 캡 / ATR-linked TS = min(max(3, 7.2), 6) = 6
    (4.0, 300, 6.0, 8.0, 6.0),
    # ATR 1% × 2.0 = 2 → min_stop 4.0(ExitConfig 기본) 하한 / TS = max(3, 1.2) = 3
    (4.0, 50, 1.0, 4.0, 3.0),
    # 문서(docs/risk) 표기 3.5% 하한은 config 주입 시에만 실효 — 코드 기본은 4.0
    (3.5, 50, 1.0, 3.5, 3.0),
])
def test_stop_loss_atr_clamp(home, min_stop, rng, atr, expected_sl, eff_ts):
    em = ExitManager(config=ExitConfig(min_stop_pct=min_stop))
    em.register_position(_pos(), price_history=_bars(rng=rng))
    st = em.get_state(SYM)
    assert st.atr_pct == pytest.approx(atr)
    assert st.dynamic_stop_pct == pytest.approx(expected_sl)
    assert st.effective_trailing_stop_pct == pytest.approx(eff_ts)

    # 경계: SL 미만 하락은 무반응, SL 도달 시 전량 손절 (사유에 SL·ATR 명시)
    just_above = 10000 * (1 - (expected_sl - 0.3) / 100)
    assert em.update_price(SYM, D(int(just_above))) is None
    sig = em.update_price(SYM, D(int(10000 * (1 - (expected_sl + 0.5) / 100))))
    assert sig[0] == "sell_all" and sig[1] == 100
    assert sig[2].startswith("손절: ") and f"(SL={expected_sl:.2f}%, ATR={atr:.2f}%)" in sig[2]


@pytest.mark.parametrize("reg_kw,expected_sl", [
    ({}, 5.0),                    # ATR·전략 SL 없음 → ExitConfig.stop_loss_pct
    ({"stop_loss_pct": 3.0}, 3.0),  # 전략 SL은 min_stop 클램프 대상이 아니다 (2026-08-04 P1)
])
def test_stop_loss_precedence_without_atr(home, reg_kw, expected_sl):
    em = ExitManager()
    em.register_position(_pos(), **reg_kw)
    assert em.get_state(SYM).dynamic_stop_pct is None
    sig = em.update_price(SYM, D(int(10000 * (1 - (expected_sl + 0.7) / 100))))
    assert sig[0] == "sell_all" and sig[2].endswith(f"(SL={expected_sl:.2f}%)")


def test_breakeven_protection_after_first(home):
    em = ExitManager()
    st = _to_first(em)
    # FIRST 이후 첫 무신호 호출에서 1R(=SL 5%) 도달 → 본전보호 활성화
    assert em.update_price(SYM, D(11100)) is None
    assert st.breakeven_activated is True
    # 고점 대비 -3% 이탈이지만 FIRST(분할 잔여) → 전량 매도 대신 고점 리셋
    assert em.update_price(SYM, D(9980)) is None          # net -0.43% > -0.5 버퍼
    assert st.highest_price == D(9980)
    # net ≤ -0.5% → 본전 이탈 전량
    assert em.update_price(SYM, D(9970)) == (
        "sell_all", 90, "본전 이탈: -0.53% (stage=first, 버퍼=-0.5%)")


@pytest.mark.parametrize("strategy,price,fires", [
    ("sepa_trend", 10950, False),    # 버퍼 0.5% → 10,945 미만이어야 발동
    ("gap_and_go", 10950, True),     # 버퍼 0.4% → 10,956
    ("rsi2_reversal", 10960, True),  # 버퍼 0.3% → 10,967
])
def test_composite_ma5_after_first(home, strategy, price, fires):
    em = ExitManager()
    em.register_position(_pos(), strategy_name=strategy)
    _walk(em, 11100)
    sig = em.update_price(SYM, D(price), market_data={"ma5": 11000})
    if fires:
        buf = {"gap_and_go": 0.4, "rsi2_reversal": 0.3}[strategy]
        assert sig == ("sell_all", 90, f"복합트레일링: MA5(11,000) - {buf}% 이탈")
    else:
        assert sig is None


def test_composite_prev_low_after_first(home):
    em = ExitManager()
    _to_first(em)
    # 당일저가가 전일저가를 깨지 않았으면 무반응
    assert em.update_price(SYM, D(10960), market_data={"prev_low": 11000, "low": 11000}) is None
    assert em.update_price(SYM, D(10960), market_data={"prev_low": 11000, "low": 10950}) == (
        "sell_all", 90, "복합트레일링: 전일저가(11,000) 이탈")


def test_composite_inactive_before_first(home):
    em = ExitManager()
    em.register_position(_pos())
    md = {"ma5": 11000, "prev_low": 11000, "low": 10900}
    assert em.update_price(SYM, D(10900), market_data=md) is None
    assert em.get_state(SYM).current_stage is ExitStage.NONE


def test_stale_exit_sideways(home, monkeypatch):
    em = ExitManager()
    em.register_position(_pos())
    _freeze_bizdays(monkeypatch, 4)
    assert em.update_price(SYM, D(10150)) is None
    _freeze_bizdays(monkeypatch, 5)
    assert em.update_price(SYM, D(10150)) == (
        "sell_all", 100, "횡보 청산: 5영업일 보유, 수익률 +1.27% (±2.0% 이내)")


def test_stale_exit_skips_core_and_outside_band(home, monkeypatch):
    _freeze_bizdays(monkeypatch, 5)
    em = ExitManager()
    em.register_position(_pos())
    assert em.update_price(SYM, D(10300)) is None          # net +2.75% — 밴드 밖
    core = ExitManager()
    core.register_position(_pos(), is_core=True, max_holding_days=0)
    assert core.update_price(SYM, D(10150)) is None        # 코어는 stale 면제


def test_stale_high_failure(home, monkeypatch):
    _freeze_bizdays(monkeypatch, 3)
    em = ExitManager()
    em.register_position(_pos())                            # 글로벌 stale_high_days=0 → 비활성
    assert em.update_price(SYM, D(10100)) is None
    em2 = ExitManager()
    em2.register_position(_pos(), stale_high_days=3)
    assert em2.update_price(SYM, D(10100)) == (
        "sell_all", 100, "추세 무효화: 3영업일 신고가 실패, 수익률 +0.77% (< 3.0%)")


def test_max_holding_days(home, monkeypatch):
    em = ExitManager()
    em.register_position(_pos())
    _freeze_bizdays(monkeypatch, 9)
    assert em.update_price(SYM, D(10500)) is None
    _freeze_bizdays(monkeypatch, 10)                        # >= (2026-08-08 오프바이원 수정)
    assert em.update_price(SYM, D(10500)) == (
        "sell_all", 100, "보유기간 초과: 10영업일 (최대 10영업일)")
    # 포지션별 0 = 무제한
    em2 = ExitManager()
    em2.register_position(_pos(), max_holding_days=0)
    assert em2.update_price(SYM, D(10500)) is None


def test_count_business_days_kr_holidays_vs_us_weekends(home):
    kr, us = ExitManager(), ExitManager(market="US")
    assert kr._count_business_days(date(2026, 9, 7), date(2026, 9, 14)) == 5
    # 추석(9/24·25) 포함 구간: KR은 공휴일 제외, US는 주말만 제외
    assert kr._count_business_days(date(2026, 9, 23), date(2026, 9, 29)) == 2
    assert us._count_business_days(date(2026, 9, 23), date(2026, 9, 29)) == 4
    assert kr._count_business_days(date(2026, 9, 14), date(2026, 9, 14)) == 0


# ── 3) on_fill ───────────────────────────────────────────────


def test_on_fill_partial_accumulates_then_promotes(home):
    em = ExitManager()
    em.register_position(_pos())
    assert em.update_price(SYM, D(11100))[1] == 10
    st = em.get_state(SYM)
    em.on_fill(SYM, 4, D(11100))
    assert (st.remaining_quantity, st.current_stage, st.pending_filled_qty) == (96, ExitStage.NONE, 4)
    assert st.pending_stage is ExitStage.FIRST
    em.on_fill(SYM, 6, D(11100))
    assert (st.remaining_quantity, st.current_stage, st.pending_stage) == (90, ExitStage.FIRST, None)
    assert st.total_realized_pnl > 0


def test_on_fill_full_removes_state_and_file_entry(home):
    em = ExitManager()
    em.register_position(_pos())
    assert SYM in json.loads(em._stage_file.read_text())
    em.on_fill(SYM, 100, D(9000))
    assert em.get_state(SYM) is None and SYM not in em._entry_times and SYM not in em._persisted
    assert json.loads(em._stage_file.read_text()) == {}
    assert em.update_price(SYM, D(20000)) is None


def test_on_fill_clamps_oversell(home):
    em = ExitManager()
    em.register_position(_pos(qty=10))
    em.on_fill(SYM, 15, D(10000))
    assert em.get_state(SYM) is None


# ── 4) 영속화 / 복원 ─────────────────────────────────────────


def _to_second(em):
    em.register_position(_pos())
    _walk(em, 11100)
    _walk(em, 11600)
    assert em.get_state(SYM).current_stage is ExitStage.SECOND
    return em


def test_persistence_roundtrip_second_stage(home):
    em1 = _to_second(ExitManager())
    f = home / ".cache" / "ai_trader" / f"exit_stages_{date.today().isoformat()}.json"
    assert em1._stage_file == f
    entry = json.loads(f.read_text())[SYM]
    assert entry["stage"] == "second" and entry["highest_price"] == "11600"
    assert entry["initial_qty"] == 100 and "pending_stage" not in entry

    em2 = ExitManager()
    assert em2._persisted[SYM]["stage"] == "second"
    assert em2.get_state(SYM) is None                       # 파일만 로드 — 등록 전엔 상태 없음
    em2.register_position(_pos(qty=45, cur="11500"))        # 고점 괴리 <5% → 고점 유지
    st = em2.get_state(SYM)
    assert (st.current_stage, st.remaining_quantity, st.initial_quantity) == (ExitStage.SECOND, 45, 100)
    assert st.highest_price == D(11600) and st.breakeven_activated is False
    # 복원 직후 +25% → 3차 익절이 이어진다 (1·2차 재발행 없음)
    assert em2.update_price(SYM, D(12600))[:2] == ("sell_partial", 22)


def test_persistence_high_reset_when_gap_over_5pct(home):
    _to_second(ExitManager())
    em2 = ExitManager()
    em2.register_position(_pos(qty=45, cur="10000"))        # 11,600 vs 10,000 = 16% 괴리
    assert em2.get_state(SYM).highest_price == D(10000)


@pytest.mark.parametrize("kis_qty,expected", [
    (100, ExitStage.NONE),    # 1차도 안 팔림 → NONE (재발행 예정)
    (90, ExitStage.FIRST),    # 2차 미체결 → FIRST 강등
    (47, ExitStage.SECOND),   # 45+5% 허용치(5주) 이내 → 유지
    (45, ExitStage.SECOND),
])
def test_persistence_integrity_demotes_to_deepest_consistent_stage(home, kis_qty, expected):
    _to_second(ExitManager())
    em2 = ExitManager()
    em2.register_position(_pos(qty=kis_qty, cur="11500"))
    st = em2.get_state(SYM)
    assert st.current_stage is expected
    assert (SYM in em2._integrity_reset_symbols) is (expected is not ExitStage.SECOND)
    if expected is ExitStage.SECOND:
        return
    # 강등된 심볼은 hp_cache(restore_stages) 업그레이드로 되돌릴 수 없다
    em2.restore_stages({SYM: 2})
    assert st.current_stage is expected


def test_restore_stages_requires_register_first_and_never_downgrades(home):
    em = ExitManager()
    em.restore_stages({SYM: 2})                             # 등록 전 → 무음 no-op (MEMORY 함정)
    em.register_position(_pos())
    st = em.get_state(SYM)
    assert st.current_stage is ExitStage.NONE
    em.restore_stages({SYM: 2})
    assert st.current_stage is ExitStage.SECOND
    em.restore_stages({SYM: 1})                             # 다운그레이드 금지
    assert st.current_stage is ExitStage.SECOND
    em.restore_stages({SYM: 9})                             # 범위 밖 무시
    assert st.current_stage is ExitStage.SECOND
    assert em.get_stages() == {SYM: 2}


def test_remaining_zero_makes_update_price_silent(home):
    em = ExitManager()
    em.register_position(_pos())
    em.get_state(SYM).remaining_quantity = 0
    assert em.update_price(SYM, D(15000)) is None
    assert em.update_price(SYM, D(5000)) is None


def test_stage_file_naming_and_fallback_to_latest_existing(home):
    today = date.today().isoformat()
    assert ExitManager()._stage_file.name == f"exit_stages_{today}.json"
    assert ExitManager(market="US")._stage_file.name == f"exit_stages_us_{today}.json"
    # 당일 파일이 없으면 최근 7일 내 가장 최신 파일 1개만 사용 (빈 dict도 유효 — 2026-08-04 P1)
    d = home / ".cache" / "ai_trader"
    (d / f"exit_stages_{today}.json").unlink(missing_ok=True)
    (d / f"exit_stages_{(date.today() - timedelta(days=1)).isoformat()}.json").write_text("{}")
    (d / f"exit_stages_{(date.today() - timedelta(days=2)).isoformat()}.json").write_text(
        json.dumps({SYM: {"stage": "second", "highest_price": "11600"}}))
    assert ExitManager()._persisted == {}


def test_readd_buy_over_10pct_resets_stage(home):
    em = ExitManager()
    st = _to_first(em)                                      # remaining 90
    em.register_position(_pos(qty=95))                      # +5.6% (sync 오차) → stage 유지
    assert (st.current_stage, st.remaining_quantity) == (ExitStage.FIRST, 95)
    em.register_position(_pos(qty=110))                     # +15.8% 추가매수 → NONE 리셋
    assert (st.current_stage, st.remaining_quantity, st.initial_quantity) == (ExitStage.NONE, 110, 110)
    # _persisted는 기동 시 로드 스냅샷(세션 중 미갱신) — 파일이 진실
    assert json.loads(em._stage_file.read_text())[SYM]["initial_qty"] == 110


# ── 5) exit_exempt ───────────────────────────────────────────


def test_exit_exempt_never_sells(home, monkeypatch):
    em = ExitManager()
    em.register_position(_pos())
    em.add_exit_exempt(SYM, "수동 풀매수")
    _freeze_bizdays(monkeypatch, 30)                        # 보유기간 초과 조건까지 성립
    for p in (5000, 20000, 9000):
        assert em.update_price(SYM, D(p)) is None
    st = em.get_state(SYM)
    assert st.highest_price == D(10000) and st.current_stage is ExitStage.NONE  # 진입부 스킵 — 고점도 안 건드림
    assert em.is_exit_exempt(SYM)
    em.remove_exit_exempt(SYM)
    assert em.update_price(SYM, D(5000))[0] == "sell_all"


# ── 6) 레짐 파라미터 ─────────────────────────────────────────


def test_apply_regime_trending_bear_tightens(home):
    bear = REGIME_EXIT_PARAMS["trending_bear"]
    assert bear == {"first_exit_pct": 5.0, "second_exit_pct": 8.0, "third_exit_pct": 14.0,
                    "trailing_stop_pct": 2.0, "stop_loss_pct": 3.5, "stale_high_days": 3}
    em = ExitManager()
    em.register_position(_pos(), atr_pct_hint=1.0)          # NONE, effective_ts = max(3, 1.2) = 3
    st = em.get_state(SYM)
    assert st.effective_trailing_stop_pct == pytest.approx(3.0)

    assert em.apply_regime_params("nope") == 0
    assert em.apply_regime_params("trending_bear") == 1
    assert (st.stop_loss_pct, st.trailing_stop_pct, st.stale_high_days) == (3.5, 2.0, 3)
    assert (st.first_exit_pct, st.second_exit_pct, st.third_exit_pct) == (5.0, 8.0, 14.0)
    assert st.effective_trailing_stop_pct == pytest.approx(2.0)   # min(max(2.0, 1.2), 6)
    cfg = em.config
    assert (cfg.stop_loss_pct, cfg.trailing_stop_pct, cfg.first_exit_pct,
            cfg.second_exit_pct, cfg.third_exit_pct, cfg.stale_high_days) == (3.5, 2.0, 5.0, 8.0, 14.0, 3)
    assert em.apply_regime_params("trending_bear") == 0     # 동일 레짐 재적용 스킵
    assert em.apply_regime_params("trending_bear", force=True) == 0  # 강제여도 변경값 없으면 0
    assert json.loads(em._stage_file.read_text())[SYM]["stage"] == "none"

    # 조여진 SL 3.5%가 즉시 판정에 반영 — 종전 5%면 살아남을 -4.0%에서 손절
    assert em.update_price(SYM, D(9620)) == ("sell_all", 100, "손절: -4.02% (SL=3.50%, ATR=1.00%)")


def test_apply_regime_stage_gating_and_core_exempt(home):
    em = ExitManager()
    _to_first(em)                                           # FIRST: TP1은 이미 완료
    core_sym = "000002"
    em.register_position(Position(symbol=core_sym, quantity=10, avg_price=D(10000),
                                  current_price=D(10000), entry_time=datetime.now()),
                         is_core=True, stop_loss_pct=10.0, trailing_stop_pct=12.0)
    assert em.apply_regime_params("trending_bear") == 1     # 코어 제외
    st, core = em.get_state(SYM), em.get_state(core_sym)
    assert st.first_exit_pct is None                        # FIRST 이상은 TP1 미갱신
    assert (st.second_exit_pct, st.third_exit_pct, st.stop_loss_pct) == (8.0, 14.0, 3.5)
    assert (core.stop_loss_pct, core.trailing_stop_pct, core.first_exit_pct) == (10.0, 12.0, None)
