"""S2-1: CrossStrategyValidator 특성화 기준선.

C1~C7 은 제품을 건드리지 않고 현재 감점 산식·시간 구간·임계값을 그대로 못 박는다
(착수 시 GREEN 이 정상 — RED 가 아니다). tests/ 전체에 실인스턴스가 0건이라
감점 산식을 고정하는 증거가 없었다.

시계는 전부 주입한다. `validate()` 안의 지역 재임포트(`from datetime import datetime as _dt`)
때문에 한쪽만 얼면 경계가 풀려서 `datetime.datetime` 과 `cross_validator.datetime` 을
함께 고정한다 — 09:29/09:45/12:45/13:00/13:01 이 실제로 얼리는 것을 실측했다.
"""
import datetime as datetime_module
from datetime import datetime as _RealDateTime
from pathlib import Path

import pytest

import src.core.cross_validator as cv_module
from src.core.cross_validator import CrossStrategyValidator


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """규칙11/12 로그와 패널 파일이 운영 캐시(~/.cache/ai_trader)를 건드리지 않게 한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


@pytest.fixture
def freeze(monkeypatch):
    """CV 시계를 고정한다 — 모듈 최상위 이름과 validate 내부 지역 재임포트 양쪽."""
    def _freeze(hour, minute, *, day=21):
        stamp = _RealDateTime(2026, 9, day, hour, minute)

        class Frozen(_RealDateTime):
            @classmethod
            def now(cls, tz=None):
                return stamp

        monkeypatch.setattr(datetime_module, 'datetime', Frozen)
        monkeypatch.setattr(cv_module, 'datetime', Frozen)
        return stamp
    return _freeze


# 감점 0 이 되는 지표 묶음 — 규칙1(RSI)·6(추격)·7(MA200)·8(밸류)·지표결손을 전부 비켜간다.
FULL_INDICATORS = {
    'atr_pct': 3.0, 'per': 15.0, 'pbr': 1.5,
    'foreign_net_buy': 100.0, 'inst_net_buy': 50.0,
}


def meta(indicators=None, **top):
    """시그널 metadata — indicators 를 넘기지 않으면 감점 0 묶음을 쓴다."""
    body = {'indicators': dict(FULL_INDICATORS) if indicators is None else dict(indicators)}
    body.update(top)
    return body


def cv(market='KR', **kwargs):
    return CrossStrategyValidator(market=market, **kwargs)


def run(validator, *, symbol='005930', side='buy', strategy='momentum_breakout',
        score=70.0, metadata=None, regime='neutral', **kwargs):
    return validator.validate(
        symbol=symbol, side=side, strategy=strategy, score=score,
        metadata=meta() if metadata is None else metadata,
        market_regime=regime, **kwargs,
    )


# ── C1: 진입 시간대 3구간 ─────────────────────────────────────────────

@pytest.mark.parametrize('hm, expected', [
    ((8, 59), 70.0),    # 장전 — 가드 미적용
    ((9, 30), 62.0),    # -8 시작 경계 포함
    ((9, 45), 62.0),
    ((10, 29), 62.0),
    ((10, 30), 70.0),   # -8 종료 경계 미포함
    ((12, 29), 70.0),
    ((12, 30), 75.0),   # +5 시작 경계 포함
    ((12, 45), 75.0),
    ((13, 0), 75.0),    # +5 종료 경계 포함
    ((13, 1), 70.0),    # 13:01 은 미포함
])
def test_c1_time_bands(freeze, hm, expected):
    freeze(*hm)
    assert run(cv()) == (True, expected, '')


@pytest.mark.parametrize('hm', [(9, 0), (9, 15), (9, 29)])
def test_c1_hard_block_window(freeze, hm):
    freeze(*hm)
    assert run(cv()) == (False, 0, '장초반 30분 진입 차단 (09:00~09:29)')


# ── C2: 09:30~10:30 감점 면제 ─────────────────────────────────────────

@pytest.mark.parametrize('strategy', ['core_holding', 'strategic_swing'])
def test_c2_batch_strategies_exempt(freeze, strategy):
    freeze(9, 45)
    assert run(cv(), strategy=strategy) == (True, 70.0, '')


def test_c2_batch_signal_marker_exempt(freeze):
    freeze(9, 45)
    assert run(cv(), metadata=meta(batch_signal=True)) == (True, 70.0, '')


def test_c2_core_holding_is_the_only_hard_block_exemption(freeze):
    freeze(9, 15)
    assert run(cv(), strategy='core_holding') == (True, 70.0, '')
    assert run(cv(), strategy='strategic_swing')[0] is False
    # batch_signal 마커는 -8 만 면제하고 하드 차단은 면제하지 않는다.
    assert run(cv(), metadata=meta(batch_signal=True))[0] is False


# ── C3: 지표 결손 개수 × STEP · cap ───────────────────────────────────

@pytest.mark.parametrize('drop, penalty', [
    ((), 0),
    (('atr_pct',), 2),
    (('atr_pct', 'per'), 4),
    (('atr_pct', 'per', 'pbr'), 6),
    (('atr_pct', 'per', 'pbr', 'foreign_net_buy', 'inst_net_buy'), 8),
])
def test_c3_missing_indicator_penalty(freeze, drop, penalty):
    freeze(11, 0)
    indicators = {k: v for k, v in FULL_INDICATORS.items() if k not in drop}
    assert run(cv(), metadata=meta(indicators)) == (True, 70.0 - penalty, '')


def test_c3_missing_penalty_cap_and_market_scope(freeze):
    """KR 은 수급까지 4종(=cap 8), US 는 수급을 세지 않아 3종(-6)이 최대다."""
    freeze(11, 0)
    assert run(cv('KR'), metadata=meta({}))[1] == 62.0
    assert run(cv('US'), metadata=meta({}))[1] == 64.0


# ── C4: 누적 감점 cap 15 와 hard-block 태그 면제 ──────────────────────

def test_c4_total_penalty_cap(freeze):
    """장초반 -8 + 지표결손 -8 = -16 → cap 으로 -15 만 반영된다."""
    freeze(9, 45)
    assert run(cv(), metadata=meta({})) == (True, 55.0, '')


@pytest.mark.parametrize('indicators, expected', [
    # '추격매수' — 등락/ATR 2.5배 -15 + 장초반 -8 = -23, cap 미적용.
    ({'atr_14': 2.0, 'change_1d': 5.0, 'per': 15.0, 'pbr': 1.5,
      'foreign_net_buy': 1.0, 'inst_net_buy': 1.0}, 57.0),
    # 'RSI과매수' -5 + 장초반 -8 + 지표결손 4종 -8 = -21, cap 미적용.
    ({'rsi_14': 75.0}, 59.0),
    # '적자+고PBR' -10 + 장초반 -8 + 지표결손(ATR,수급) -4 = -22, cap 미적용.
    ({'per': -1.0, 'pbr': 6.0}, 58.0),
])
def test_c4_hard_block_tags_disable_cap(freeze, indicators, expected):
    freeze(9, 45)
    assert run(cv(), score=80.0, metadata=meta(indicators)) == (True, expected, '')


def test_c4_same_shape_without_hard_block_is_capped(freeze):
    """hard-block 태그만 빼면 같은 모양이 cap 에 걸린다 — 면제의 대조군."""
    freeze(9, 45)
    assert run(cv(), score=80.0, metadata=meta({'rsi_14': 50.0})) == (True, 65.0, '')


# ── C5: MIN_PASS_SCORE 50 경계 ────────────────────────────────────────

@pytest.mark.parametrize('score, expected', [
    (58.0, (True, 50.0, '')),
    (57.0, (False, 49.0, '크로스 감점 후 점수 부족 (49)')),
])
def test_c5_min_pass_score_boundary(freeze, score, expected):
    """지표결손 -8 만 걸린 상태에서 50 은 통과, 49 는 차단이다."""
    freeze(11, 0)
    assert run(cv(), score=score, metadata=meta({})) == expected


# ── C6: 매도는 무검증 통과 ────────────────────────────────────────────

@pytest.mark.parametrize('hm', [(9, 15), (9, 45), (12, 45)])
def test_c6_sell_bypasses_every_rule(freeze, hm):
    freeze(*hm)
    validator = cv()
    assert run(validator, side='sell', score=12.0, metadata=meta({})) == (True, 12.0, '')
    # 규칙 3(약세장 차단)조차 타지 않는다.
    assert run(validator, side='sell', strategy='gap_and_go', regime='bear') == (True, 70.0, '')


# ── C7: US 인스턴스의 규칙 적용 범위 ──────────────────────────────────

def test_c7_us_skips_kr_only_rules(freeze):
    """시간 가드·규칙2 는 KR 전용, 규칙3 약세 차단은 US 에서 모멘텀만이다."""
    freeze(9, 15)
    assert run(cv('KR'))[0] is False
    assert run(cv('US')) == (True, 70.0, '')

    freeze(11, 0)
    dual_sell = meta({**FULL_INDICATORS, 'foreign_net_buy': -100.0, 'inst_net_buy': -50.0})
    assert run(cv('KR'), metadata=dual_sell) == (
        False, 0, '수급 불일치: 기관+외국인 동시 순매도')
    assert run(cv('US'), metadata=dual_sell) == (True, 70.0, '')

    assert run(cv('US'), strategy='gap_and_go', regime='bear') == (True, 70.0, '')
    assert run(cv('US'), strategy='momentum_breakout', regime='bear') == (
        False, 0, '체제 부적합: 약세장에서 momentum_breakout 차단')
    assert run(cv('KR'), strategy='gap_and_go', regime='bear') == (
        False, 0, '체제 부적합: 약세장에서 gap_and_go 차단')
