"""S2-4: 판단 사실 builder(순수 함수)의 행동 계약.

입력은 S2-1 이 고정한 CV 증거 dict(`last_decision`·`last_llm_reason`)와 S2-2 가 고정한
사이징 입력 dict 뿐이다. 실제 CV/엔진 인스턴스는 만들지 않으며 시각은 전부 주입한다
(벽시계 의존 0 — TZ=UTC·Asia/Seoul 에서 같은 결과). 여기서 만든 facts 는 기록일 뿐
주문 송신·운영 승격 근거가 아니다.
"""
import ast
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import importlib
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

KST = ZoneInfo('Asia/Seoul')
DAY = (2026, 9, 21)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def api():
    return importlib.import_module('src.execution.safety.qualification')


def facts_api():
    return importlib.import_module('src.execution.safety.decisions')


def _at(hm, *, second=0, tz=KST):
    """KST 기준 HHMM 을 aware datetime 으로."""
    return datetime(*DAY, hm // 100, hm % 100, second, tzinfo=tz)


def _naive(hm):
    return datetime(*DAY, hm // 100, hm % 100)


EARLY = '장초반 변동성 -8 (09:30~10:30)'
LUNCH = '점심 sweet spot +5 (12:30~13:00)'


def _cv(**changes):
    """S2-1 이 고정한 13키 증거 dict (09:45 통과 판단 표본)."""
    row = {
        'token': 'req-1', 'symbol': '005930', 'side': 'buy', 'strategy': 'sepa_trend',
        'regime': 'sideways', 'penalties': (EARLY,), 'cap_applied': False,
        'original': 72.0, 'adjusted': 64.0, 'memory_adj': 0, 'memory_sector': None,
        'panel': None, 'now_hm': 945,
    }
    row.update(changes)
    return row


def _sizing(**changes):
    """S2-2 가 고정한 14키 반출 dict (nominal 표본)."""
    row = {
        'base_pct': 0.25, 'strategy_allocation_pct': 42.0, 'min_position_value': D('200000'),
        'strength_multiplier': 1.0, 'position_multiplier': 1.0, 'calendar_multiplier': 1.0,
        'volatility_multiplier': 1.0, 'conviction_multiplier': 1.0, 'atr_pct': None,
        'stop_pct': None, 'stop_source': None, 'stop_crash_capped': None,
        'hybrid_enabled': False,
        'overlay_status': {'calendar': 'applied', 'volatility': 'applied', 'conviction': 'applied'},
    }
    row.update(changes)
    return row


def _regime_row(module, regime='sideways', *, version=1, as_of=None):
    """commands 가 저장하는 형태(as_of 는 isoformat 문자열)."""
    return {'version': version, 'as_of': (as_of or _at(900)).isoformat(),
            'digest': module.regime_digest(regime)}


def _build(module, *, decided_at=None, cv=None, sizing=None, regime_used='sideways',
           regime_row=..., llm_reason='not_required', strategy=None, observed_at=..., **changes):
    cv = cv if cv is not None else _cv()
    decided_at = decided_at if decided_at is not None else _at(945)
    # 기본 판독 시각은 판단 시각보다 1초 앞이다 — 둘을 같은 값으로 두면 as_of 계약이 안 드러난다.
    kwargs = dict(
        intent_id='intent-1', symbol='005930', side='buy',
        strategy=strategy if strategy is not None else cv['strategy'],
        origin='automatic', sector='반도체', cv_decision=cv, llm_reason=llm_reason,
        sizing_inputs=sizing if sizing is not None else _sizing(),
        config_version='synthetic-config-version', regime_used=regime_used,
        regime_row=_regime_row(module, regime_used) if regime_row is ... else regime_row,
        decided_at=decided_at,
        observed_at=(decided_at - timedelta(seconds=1) if observed_at is ... else observed_at),
    )
    kwargs.update(changes)
    return module.build_decision_facts(**kwargs)


def _refusal(module, **changes):
    with pytest.raises(module.QualificationRefused) as caught:
        _build(module, **changes)
    return caught.value.reason


# --- R17: CV 다음 시간 경계 / 당일 말 ----------------------------------------


@pytest.mark.parametrize('decided_hm, expected_hm', [
    (910, 930), (945, 1030), (1100, 1230), (1245, 1301),
])
def test_r17_expires_at_next_cross_validator_boundary(decided_hm, expected_hm):
    module = api()
    assert module.entry_expires_at(_at(decided_hm), strategy='gap_and_go') == _at(expected_hm)


def test_r17_after_last_boundary_expires_at_end_of_kst_day():
    module = api()
    expires = module.entry_expires_at(_at(1525), strategy='gap_and_go')
    assert expires == datetime(*DAY, 23, 59, 59, 999999, tzinfo=KST)
    assert expires.astimezone(KST).date() == _at(1525).astimezone(KST).date()
    assert expires < datetime(2026, 9, 22, tzinfo=KST)


# --- R18: SEPA 14:30 상한 ----------------------------------------------------


def test_r18_sepa_trend_expires_at_1430():
    module = api()
    assert module.entry_expires_at(_at(1429), strategy='sepa_trend') == _at(1430)
    # 이미 지난 상한은 후보가 아니다(만료 시각이 판단 시각보다 앞설 수 없다)
    assert module.entry_expires_at(_at(1431), strategy='sepa_trend') == datetime(
        *DAY, 23, 59, 59, 999999, tzinfo=KST)


def test_r18_sepa_decision_crossing_1430_refuses():
    """계약 13 — CV 는 14:30 전에 판정했는데 decided_at 이 14:30 을 넘으면 상한이 사라진다.

    decided_at 은 자기 llm_second_check(네트워크 await) 뒤에 찍히므로 이 교차는 실제로
    생긴다. 창을 늘리는 대신 판단을 버린다.
    """
    module = api()
    cv = _cv(now_hm=1429, penalties=())
    assert _refusal(module, cv=cv, decided_at=_at(1430, second=5)) == 'decision_clock_disagreement'
    # 표기 시간대를 바꿔도 같은 순간이면 같은 결과다(UTC CI 에서도 동일)
    assert _refusal(module, cv=cv, decided_at=_at(1430, second=5).astimezone(
        timezone.utc)) == 'decision_clock_disagreement'


def test_r18_sepa_decision_inside_the_deadline_still_builds():
    module = api()
    built, _pending = _build(module, cv=_cv(now_hm=1429, penalties=()),
                             decided_at=_at(1429, second=30))
    assert built.expires_at == _at(1430)


def test_r18_other_strategies_are_not_bound_by_the_sepa_deadline():
    module = api()
    cv = _cv(now_hm=1429, penalties=(), strategy='momentum_breakout')
    built, _pending = _build(module, cv=cv, decided_at=_at(1430, second=5))
    assert built.strategy == 'momentum_breakout'
    assert built.expires_at == datetime(*DAY, 23, 59, 59, 999999, tzinfo=KST)


def test_r18_missing_reported_clock_keeps_the_existing_refusal():
    module = api()
    assert _refusal(module, cv=_cv(now_hm=None, penalties=()),
                    decided_at=_at(1430, second=5)) == 'decision_clock_disagreement'


# --- R19: 패널 기여 시 panel_loaded_at + 6h ----------------------------------


def test_r19_panel_contribution_caps_expiry_six_hours_after_load():
    module = api()
    # 패널을 12:00 에 읽었다 → 18:00 이 당일 말보다 앞선다
    assert module.entry_expires_at(_at(1525), strategy='gap_and_go',
                                   panel_loaded_at=_naive(1200)) == _at(1800)
    # CV 의 _panel_loaded_at 은 naive 지역시각이다. aware KST 를 줘도 같은 값이어야 한다.
    assert module.entry_expires_at(_at(1525), strategy='gap_and_go',
                                   panel_loaded_at=_at(1200)) == _at(1800)


def test_r18_sepa_decision_after_the_deadline_is_refused_not_extended():
    """양쪽 시계가 모두 14:30 을 넘기면 14:30 항이 소멸해 당일 말까지 유효해진다 — 버린다."""
    module = api()
    cv = _cv(now_hm=1430, penalties=())
    assert _refusal(module, cv=cv, decided_at=_at(1430, second=5)) == 'sepa_entry_deadline_passed'
    late = _cv(now_hm=1525, penalties=())
    assert _refusal(module, cv=late, decided_at=_at(1525)) == 'sepa_entry_deadline_passed'
    # 대조: 다른 전략은 같은 시각에도 만들어지고 당일 말까지 유효하다
    other = _cv(strategy='momentum_breakout', now_hm=1525, penalties=())
    built, _ = _build(module, cv=other, decided_at=_at(1525))
    assert built.expires_at == _at(2359, second=59).replace(microsecond=999999)


def test_r19_panel_bound_applies_to_built_facts():
    module = api()
    # 패널 창만 본다 — SEPA 는 14:30 이후 판단 자체가 거부되므로 다른 전략 표본을 쓴다
    cv = _cv(strategy='momentum_breakout', now_hm=1525,
             penalties=('전문가패널 추천(+7 conv=90%/신선도80%)',),
             panel={'created_at': '2026-09-20T21:00:00', 'conviction': 0.9, 'bonus': 7,
                    'loaded_at': _naive(1200)})
    built, _pending = _build(module, cv=cv, decided_at=_at(1525))
    assert built.expires_at == _at(1800)


def test_r19_expired_panel_window_refuses_instead_of_extending():
    module = api()
    cv = _cv(strategy='momentum_breakout', now_hm=1525,
             penalties=('전문가패널 추천(+7 conv=90%/신선도80%)',),
             panel={'created_at': '2026-09-20T21:00:00', 'conviction': 0.9, 'bonus': 7,
                    'loaded_at': _naive(900)})
    assert _refusal(module, cv=cv, decided_at=_at(1525)) == 'decision_already_expired'


# --- R20: 시계 교차검증 (UTC/KST 무관) ---------------------------------------


def test_r20_reported_time_bucket_must_match_decided_at():
    module = api()
    # CV 는 09:45(장초반 -8)를 보고했는데 판단 시각이 11:00 → 불일치
    assert _refusal(module, decided_at=_at(1100)) == 'decision_clock_disagreement'
    # 같은 구간이면 만들어진다
    built, _pending = _build(module, decided_at=_at(945))
    assert built.decided_at == _at(945)


def test_r20_lunch_tag_requires_lunch_window_both_directions():
    module = api()
    lunch_cv = _cv(now_hm=1245, penalties=(LUNCH,), adjusted=77.0)
    built, _pending = _build(module, cv=lunch_cv, decided_at=_at(1245))
    assert built.qualification.applied_rule_ids == ('lunch_sweet_spot_bonus',)
    # 점심 보너스는 면제가 없다 — 태그가 없는데 점심 구간이면 CV 보고와 어긋난다
    assert _refusal(module, cv=_cv(now_hm=1245, penalties=()),
                    decided_at=_at(1245)) == 'decision_clock_disagreement'
    # 태그는 있는데 구간이 아니면 마찬가지다
    assert _refusal(module, cv=_cv(now_hm=1100, penalties=(LUNCH,)),
                    decided_at=_at(1100)) == 'decision_clock_disagreement'


def test_r20_blocked_window_never_yields_facts():
    module = api()
    # 09:00~09:29 는 CV 가 차단하는 구간이다 — 통과 증거가 올 수 없다
    assert _refusal(module, cv=_cv(now_hm=915, penalties=()),
                    decided_at=_at(915)) == 'decision_clock_disagreement'
    # core_holding 은 그 차단의 면제 대상이라 판단이 성립한다
    built, _pending = _build(module, cv=_cv(now_hm=915, penalties=(), strategy='core_holding'),
                             decided_at=_at(915))
    assert built.strategy == 'core_holding'


def test_r20_missing_reported_clock_refuses():
    module = api()
    assert _refusal(module, cv=_cv(now_hm=None, penalties=())) == 'decision_clock_disagreement'


def test_r20_same_instant_in_utc_gives_identical_facts():
    module = api()
    kst_built, _a = _build(module, decided_at=_at(945))
    utc_built, _b = _build(module, decided_at=_at(945).astimezone(timezone.utc))
    # 같은 순간이면 표기 시간대와 무관하게 같은 판단이어야 한다(digest 는 표기까지 굳으므로 제외)
    assert kst_built.decided_at == utc_built.decided_at
    assert kst_built.expires_at == utc_built.expires_at
    assert kst_built.qualification == utc_built.qualification
    assert kst_built.sources == utc_built.sources


# --- R21: 패널 digest 는 해당 종목의 3값만 -----------------------------------


def test_r21_panel_digest_covers_only_this_symbol_values():
    module = api()
    base = {'created_at': '2026-09-20T21:00:00', 'conviction': 0.9, 'bonus': 7,
            'loaded_at': _naive(1200)}
    penalties = ('전문가패널 추천(+7 conv=90%/신선도80%)',)

    def digest_of(panel):
        _built, pending = _build(module, cv=_cv(penalties=penalties, panel=panel))
        return {source.name: source.digest for source in pending}['panel_outlook:005930']

    # 로드 시각(다른 종목 판단으로도 움직인다)은 digest 에 들어가지 않는다
    assert digest_of(base) == digest_of({**base, 'loaded_at': _naive(1230)})
    # 계산된 결과값이 바뀌면 digest 가 바뀐다
    assert digest_of(base) != digest_of({**base, 'bonus': 8})
    assert digest_of(base) != digest_of({**base, 'conviction': 0.8})
    assert digest_of(base) != digest_of({**base, 'created_at': '2026-09-13T21:00:00'})


# --- R22·R23: 기여한 출처만 인용 ---------------------------------------------


def test_r22_only_regime_is_cited_when_nothing_else_contributed():
    module = api()
    built, pending = _build(module)
    assert [source.name for source in built.sources] == ['regime']
    assert built.sources[0].version == 1
    assert built.sources[0].digest == module.regime_digest('sideways')
    assert pending == ()


def test_r22_memory_and_panel_are_cited_only_when_they_moved_the_score():
    module = api()
    cv = _cv(penalties=(EARLY, '메모리보정(-3)', '전문가패널 추천(+7 conv=90%/신선도80%)'),
             memory_adj=-3, memory_sector='반도체',
             panel={'created_at': '2026-09-20T21:00:00', 'conviction': 0.9, 'bonus': 7,
                    'loaded_at': _naive(900)})
    _built, pending = _build(module, cv=cv)
    # 이름은 소비 범위까지 담는다 — 종목별·전략/섹터별 digest 를 전역 한 행에 실을 수 없다(계약 1)
    assert [source.name for source in pending] == [
        'panel_outlook:005930', 'trade_memory:sepa_trend:반도체']
    # 관측 시각은 CV 판독 시각이다(판단 시각도, 원본 생성시각도 아니다 — 생성시각은 digest 안)
    assert all(source.as_of == _at(945) - timedelta(seconds=1) for source in pending)
    # memory_adj 가 0 으로 돌아오면 인용도 사라진다
    _b2, pending2 = _build(module, cv={**cv, 'memory_adj': 0, 'memory_sector': None})
    assert [source.name for source in pending2] == ['panel_outlook:005930']
    # CV 가 섹터를 모르는 채로 보정했으면 이름의 그 자리는 '-' 다(빈 조각은 commands 의
    # 신원 검사를 통과하지 못한다). 조회 후 섹터가 있어도 귀속은 바뀌지 않는다.
    _b3, pending3 = _build(module, cv={**cv, 'memory_sector': ''}, sector=None)
    assert [source.name for source in pending3] == [
        'panel_outlook:005930', 'trade_memory:sepa_trend:-']


def test_memory_source_is_attributed_to_the_sector_the_validator_used():
    """CV 가 빈 섹터로 계산한 보정을 조회 후 섹터로 귀속하면 다른 범위를 stale 로 만든다."""
    module = api()
    cv = _cv(penalties=(EARLY, '메모리보정(-3)'), memory_adj=-3, memory_sector='')
    built, pending = _build(module, cv=cv, sector='반도체')
    # 이름·digest 는 CV 가 넘긴 섹터를, facts.sector 는 조회 후 섹터를 쓴다
    assert [source.name for source in pending] == ['trade_memory:sepa_trend:-']
    assert built.sector == '반도체'
    _looked_up, looked_up_pending = _build(
        module, cv={**cv, 'memory_sector': '반도체'}, sector='반도체')
    assert looked_up_pending[0].name == 'trade_memory:sepa_trend:반도체'
    assert looked_up_pending[0].digest != pending[0].digest


def test_memory_source_without_a_reported_sector_is_refused():
    """보정이 붙었는데 귀속 섹터가 없으면 아무 섹터로 채우지 않고 끝낸다."""
    module = api()
    cv = _cv(penalties=(EARLY, '메모리보정(-3)'), memory_adj=-3)
    assert cv['memory_sector'] is None
    assert _refusal(module, cv=cv, sector='반도체') == 'unexpected_cv_decision'
    assert _refusal(module, cv={**cv, 'memory_sector': 3}) == 'unexpected_cv_decision'


def test_r23_non_deciding_inputs_are_never_cited_as_sources():
    module = api()
    cv = _cv(penalties=(EARLY, '동일섹터(반도체) 손절종목 존재 -5',
                        '외국인 매수 상위 섹터(반도체) +5'), adjusted=64.0)
    built, pending = _build(module, cv=cv)
    names = {source.name for source in built.sources} | {source.name for source in pending}
    assert names == {'regime'}
    assert not names & {'sector_council', 'exited_today', 'portfolio_sector', 'trade_wiki',
                        'expert_orchestrator'}
    # 규칙 4·5 는 점수를 바꿨으므로 rule id 로는 남는다
    assert built.qualification.applied_rule_ids == (
        'early_session_penalty', 'sector_stoploss_penalty', 'sector_foreign_bonus')


# --- 계약 3: 출처 as_of 는 CV 판독 시각 --------------------------------------


def test_observed_at_must_be_aware_and_not_after_the_decision():
    """판독 시각이 판단 시각 뒤면 '소비 시각 역전'을 owner 가 아니라 여기서 끊는다."""
    module = api()
    assert _refusal(module, observed_at=_naive(944)) == 'invalid_observation_time'
    assert _refusal(module, observed_at=None) == 'invalid_observation_time'
    assert _refusal(module, observed_at=_at(946)) == 'invalid_observation_time'
    # 같은 KST 날짜여야 한다 — 전일 판독은 당일 게시본과 짝이 될 수 없다
    assert _refusal(module, observed_at=_at(945) - timedelta(days=1)) == 'invalid_observation_time'
    # 표기 시간대가 달라도 같은 순간이면 받는다
    built, _pending = _build(module, observed_at=_at(944).astimezone(timezone.utc))
    assert built.decided_at == _at(945)


def test_expiry_is_anchored_on_the_decision_time_not_the_observation():
    """만료 기준은 계속 decided_at 이다 — 판독 시각으로 창을 늘리거나 줄이지 않는다."""
    module = api()
    early, _a = _build(module, decided_at=_at(1029), observed_at=_at(1000))
    late, _b = _build(module, decided_at=_at(1029), observed_at=_at(1029))
    assert early.expires_at == late.expires_at == _at(1030)


def test_source_scope_segments_must_be_usable_as_a_published_name():
    """이름 조각에 공백·빈값·':' 이 섞이면 다른 범위와 겹치거나 게시 자체가 거부된다."""
    module = api()
    cv = _cv(memory_adj=-3, memory_sector='반도체', penalties=(EARLY, '메모리보정(-3)'))
    # 이름 조각이 되는 값은 CV 가 보고한 memory_sector 다('' 는 '-' 로 대체되므로 제외)
    for bad in (' 반도체', '반도체:2차전지'):
        assert _refusal(module, cv={**cv, 'memory_sector': bad}) == 'invalid_source_scope'
    built, pending = _build(module, cv=cv, sector='반도체')
    assert [source.name for source in pending] == ['trade_memory:sepa_trend:반도체']
    assert built.sector == '반도체'
    # 조회 후 섹터는 이름에 쓰이지 않지만 facts 로는 남으므로 DTO 계약이 계속 거른다
    for bad in ('', ' 반도체'):
        with pytest.raises(ValueError, match='invalid_decision_sector'):
            _build(module, cv=cv, sector=bad)


# --- R24: overlay fail-open 표식 ---------------------------------------------


def test_r24_overlay_exception_shows_up_as_rule_id_with_neutral_multiplier():
    module = api()
    sizing = _sizing(overlay_status={'calendar': 'unavailable', 'volatility': 'applied',
                                     'conviction': 'unavailable'})
    built, _pending = _build(module, sizing=sizing)
    assert built.qualification.applied_rule_ids == (
        'early_session_penalty', 'overlay_calendar_unavailable', 'overlay_conviction_unavailable')
    assert (built.calendar_multiplier, built.volatility_multiplier,
            built.conviction_multiplier) == (1.0, 1.0, 1.0)


def test_r24_overlay_status_inner_keys_are_pinned():
    """계약 10 — 오버레이 3종 집합이 어긋나면 fail-open 표식을 셀 수 없다."""
    module = api()
    extra = _sizing(overlay_status={'calendar': 'applied', 'volatility': 'applied',
                                    'conviction': 'applied', 'liquidity': 'unavailable'})
    assert _refusal(module, sizing=extra) == 'unexpected_sizing_inputs'
    missing = _sizing(overlay_status={'calendar': 'applied', 'volatility': 'unavailable'})
    assert _refusal(module, sizing=missing) == 'unexpected_sizing_inputs'


# --- R25: LLM 어휘 ------------------------------------------------------------


def test_r25_fail_open_quota_is_distinguishable_from_approval():
    module = api()
    quota, _a = _build(module, llm_reason='fail_open_quota')
    approved, _b = _build(module, llm_reason='approved')
    assert quota.qualification.llm_verdict == 'fail_open_quota'
    assert approved.qualification.llm_verdict == 'approved'
    assert quota.digest != approved.digest


@pytest.mark.parametrize('reason', ['approved', 'rejected_soft', 'not_required',
                                    'skipped_no_manager', 'skipped_bull', 'skipped_low_score',
                                    'fail_open_quota', 'fail_open_error'])
def test_r25_whole_vocabulary_is_accepted(reason):
    module = api()
    built, _pending = _build(module, llm_reason=reason)
    assert built.qualification.llm_verdict == reason


@pytest.mark.parametrize('reason', ['allow', '', None, 'APPROVED'])
def test_r25_unknown_verdict_refuses(reason):
    module = api()
    assert _refusal(module, llm_reason=reason) == 'unknown_llm_verdict'


# --- R26: config_version 입력 ------------------------------------------------


def _config_inputs():
    """S3 publisher 가 넣어야 하는 입력 — 실제 운영 경로를 그대로 본뜬 표본.

    validator/llm: `config/default.yml` 의 중첩 `kr.validator` 블록
    (run_trader.py:839 → engine.py:1339 `_vcfg` → CrossStrategyValidator/`_LLM_*`).
    sizing: engine.py:2616 의 전략별 base_pct 표 + RiskConfig 수치.
    stops: run_trader.py:498 `_strategy_exit_params`(전략 고정 SL).
    experts: `config/default.yml:634` shadow_mode.
    """
    return dict(
        validator={'min_pass_score': 50, 'missing_indicator_penalty_step': 2,
                   'missing_indicator_penalty_cap': 8,
                   'rule_penalties': {'early_session': 8, 'sepa_chase': 10}},
        llm={'llm_daily_max': 10, 'llm_check_score_min': 85, 'llm_bypass_score': 95,
             'llm_reject_size_mult': 0.5},
        sizing={'strategy_position_pct': {'sepa_trend': 25.0, 'gap_and_go': 15.0},
                'base_position_pct': 25.0, 'max_position_pct': 28.0, 'sizing_mode': 'risk',
                'hybrid_enabled': False},
        stops={'sepa_trend': {'stop_loss_pct': 5.0}, 'gap_and_go': {'stop_loss_pct': 3.5}},
        experts={'shadow_mode': True},
    )


@pytest.mark.parametrize('key, changed', [
    ('validator', {'min_pass_score': 49, 'missing_indicator_penalty_step': 2,
                   'missing_indicator_penalty_cap': 8,
                   'rule_penalties': {'early_session': 8, 'sepa_chase': 10}}),
    ('llm', {'llm_daily_max': 11, 'llm_check_score_min': 85, 'llm_bypass_score': 95,
             'llm_reject_size_mult': 0.5}),
    ('sizing', {'strategy_position_pct': {'sepa_trend': 20.0, 'gap_and_go': 15.0},
                'base_position_pct': 25.0, 'max_position_pct': 28.0, 'sizing_mode': 'risk',
                'hybrid_enabled': False}),
    ('sizing', {'strategy_position_pct': {'sepa_trend': 25.0, 'gap_and_go': 15.0},
                'base_position_pct': 25.0, 'max_position_pct': 28.0, 'sizing_mode': 'risk',
                'hybrid_enabled': True}),
    ('stops', {'sepa_trend': {'stop_loss_pct': 4.0}, 'gap_and_go': {'stop_loss_pct': 3.5}}),
    ('experts', {'shadow_mode': False}),
])
def test_r26_config_version_reacts_to_each_input(key, changed):
    module = api()
    base = _config_inputs()
    assert module.config_version(**{**base, key: changed}) != module.config_version(**base)


def test_r26_config_version_is_recomputed_not_cached():
    module = api()
    base = _config_inputs()
    first = module.config_version(**base)
    base['sizing']['strategy_position_pct']['sepa_trend'] = 20.0
    assert module.config_version(**base) != first


def test_r26_contrast_entry_risk_config_hash_only_hashes_the_risk_config():
    """대조: 기존 `_entry_risk_config_hash` 의 입력은 RiskConfig 하나뿐이다.

    표가 다른 두 사례는 만들 수 없다 — 전략별 base_pct 표는 `_calculate_position_size` 안의
    리터럴이라 설정 객체에 실리지 않는다. 그래서 여기서 주장하는 것은 두 가지로 좁힌다:
    같은 설정에서 결정적이라는 것과, 해시 입력 목록에 그 표가 없다는 소스 사실이다.
    """
    import inspect
    from src.core.engine import RiskManager
    from test_execution_sizing_characterization import _config  # noqa: F401 — 특성화 설정 재사용

    config = _config()
    assert is_dataclass(config)
    # 전략별 base_pct 표는 engine.py `_calculate_position_size` 안의 리터럴이라 설정 객체에 없다
    assert 'strategy_position_pct' not in asdict(config)
    source = inspect.getsource(RiskManager._entry_risk_config_hash)
    assert 'strategy_position_pct' not in source
    # 해시 입력은 self.config 뿐이다 — 다른 축이 추가되면 이 대조를 다시 세워야 한다
    assert [name for name in ('self.config', 'self.engine', 'strategy_position_pct')
            if name in source] == ['self.config']
    manager = object.__new__(RiskManager)
    manager.config = config
    other = object.__new__(RiskManager)
    other.config = _config()
    assert manager._entry_risk_config_hash() == other._entry_risk_config_hash()


# --- R27: rule id 유도 표 ----------------------------------------------------


def test_r27_rule_ids_are_unique_and_follow_penalty_order():
    module = api()
    cv = _cv(penalties=(
        EARLY, 'SEPA 90+ 추격매수 -10', '지표결손(ATR,PER) -4', 'RSI과매수(78>70) -5',
        '[규칙2] 기관+외국인 동시 순매도 — SEPA 감점 -10', '추격매수(등락/ATR=1.8x) -15',
        'MA200하방(-3.2%) -5', '적자+고PBR(6.1) -10', '극단PER(88) -5',
        '메모리보정(-3)', '누적감점캡(26→15)'), memory_adj=-3, memory_sector='반도체',
        cap_applied=True, adjusted=57.0)
    built, _pending = _build(module, cv=cv)
    ids = built.qualification.applied_rule_ids
    assert len(set(ids)) == len(ids)
    assert ids == ('early_session_penalty', 'sepa_chase_penalty', 'missing_indicator_penalty',
                   'rsi_overbought_penalty', 'supply_dual_sell_penalty', 'surge_chase_penalty',
                   'ma200_below_penalty', 'deficit_high_pbr_penalty', 'extreme_per_penalty',
                   'trade_memory_adjust', 'total_penalty_cap')


def test_r27_duplicate_or_unknown_penalty_refuses():
    module = api()
    assert _refusal(module, cv=_cv(penalties=(EARLY, EARLY))) == 'duplicate_rule_id'
    assert _refusal(module, cv=_cv(penalties=('신규 감점 -3',))) == 'unknown_penalty_rule'


# --- R28: regime 게시본 대조 --------------------------------------------------


def test_r28_regime_row_must_exist_and_match():
    module = api()
    assert _refusal(module, regime_row=None) == 'regime_source_unpublished'
    assert _refusal(module, regime_row=_regime_row(module, 'bull')) == 'regime_source_digest_mismatch'
    # 판단보다 미래에 게시된 출처를 소비했다고 적을 수 없다
    assert _refusal(module, regime_row=_regime_row(module, as_of=_at(1000))) == 'regime_source_future'


def test_r28_regime_used_must_be_what_cross_validator_received():
    module = api()
    assert _refusal(module, regime_used='bull', cv=_cv(regime='sideways'),
                    regime_row=_regime_row(module, 'bull')) == 'regime_used_mismatch'


def test_r28_consumed_regime_source_reuses_the_published_identity():
    module = api()
    row = _regime_row(module, version=4, as_of=_at(930))
    built, _pending = _build(module, regime_row=row)
    source = built.sources[0]
    assert (source.version, source.as_of, source.digest) == (4, _at(930), row['digest'])


# --- 계약 15: 0 배율·hybrid 는 보정하지 않고 거부 ----------------------------


@pytest.mark.parametrize('key', ['strength_multiplier', 'position_multiplier',
                                 'calendar_multiplier', 'volatility_multiplier',
                                 'conviction_multiplier'])
def test_contract15_non_positive_multiplier_refuses_instead_of_defaulting(key):
    module = api()
    assert _refusal(module, sizing=_sizing(**{key: 0.0})) == 'non_positive_multiplier'
    assert _refusal(module, sizing=_sizing(**{key: -1.0})) == 'non_positive_multiplier'


def test_contract15_hybrid_refuses():
    module = api()
    assert _refusal(module, sizing=_sizing(hybrid_enabled=True)) == 'unsupported_hybrid_sizing'


# --- 입력 계약과 필드 전달 ---------------------------------------------------


def test_input_key_sets_are_pinned():
    module = api()
    assert _refusal(module, cv=_cv(extra=1)) == 'unexpected_cv_decision'
    short = _cv()
    short.pop('panel')
    assert _refusal(module, cv=short) == 'unexpected_cv_decision'
    without_memory_sector = _cv()
    without_memory_sector.pop('memory_sector')
    assert _refusal(module, cv=without_memory_sector) == 'unexpected_cv_decision'
    assert _refusal(module, sizing=_sizing(extra=1)) == 'unexpected_sizing_inputs'


def test_cv_identity_must_match_the_request():
    module = api()
    assert _refusal(module, cv=_cv(symbol='000660')) == 'decision_identity_mismatch'
    assert _refusal(module, cv=_cv(side='sell')) == 'decision_identity_mismatch'
    assert _refusal(module, strategy='gap_and_go') == 'decision_identity_mismatch'


def test_risk_mode_inputs_are_copied_verbatim():
    module = api()
    sizing = _sizing(atr_pct=3.2, stop_pct=D('5.0'), stop_source='strategy',
                     stop_crash_capped=False, position_multiplier=0.8)
    built, _pending = _build(module, sizing=sizing)
    assert built.atr_pct == 3.2
    assert (built.stop_pct, built.stop_source, built.stop_crash_capped) == (D('5.0'), 'strategy', False)
    # ATR skip 이전 배율을 그대로 싣는다(계약 8) — 여기서 1.0 으로 덮지 않는다
    assert built.position_multiplier == 0.8
    assert built.min_position_value == D('200000')
    assert built.base_pct == 0.25 and built.strategy_allocation_pct == 42.0


def test_missing_atr_stays_missing_and_is_not_reported_as_zero():
    """계약 8 — ATR 미측정(None)과 실제 0% 는 다른 사실이다. DTO 는 0.0 을 정상값으로 받는다."""
    module = api()
    absent, _a = _build(module, sizing=_sizing(atr_pct=None))
    assert absent.atr_pct is None
    zero, _b = _build(module, sizing=_sizing(atr_pct=0.0))
    assert zero.atr_pct is not None
    assert zero.atr_pct == 0.0
    assert absent.digest != zero.digest


def test_missing_strategy_allocation_stays_missing():
    """배분표에 없는 전략은 None 으로 실린다 — 양수 검사로 거부하지 않는다."""
    module = api()
    built, _pending = _build(module, sizing=_sizing(strategy_allocation_pct=None))
    assert built.strategy_allocation_pct is None


def test_scores_come_from_the_recorded_cross_validator_values():
    module = api()
    built, _pending = _build(module, cv=_cv(original=72, adjusted=64))
    qualification = built.qualification
    assert (qualification.signal_score, qualification.original_score,
            qualification.adjusted_score) == (72.0, 72.0, 64.0)
    assert type(qualification.original_score) is float


def test_built_facts_survive_the_dto_round_trip():
    module = api()
    built, _pending = _build(module)
    assert facts_api().EntryDecisionFacts.from_dict(built.to_dict()) == built
    assert built.decided_at < built.expires_at
    assert built.config_version == 'synthetic-config-version'


def test_only_automatic_buy_is_in_scope():
    module = api()
    with pytest.raises(ValueError) as caught:
        _build(module, side='sell', cv=_cv(side='sell'))
    assert 'unsupported_decision_scope' in str(caught.value)


def test_module_is_pure():
    """비동기 대기·파일·벽시계 0 — 단위 검증이 가능한 순수 builder 라는 계약(주석 아닌 AST 로)."""
    tree = ast.parse(Path(api().__file__).read_text(encoding='utf-8'))
    banned = {'now', 'utcnow', 'today', 'open', 'sleep', 'get', 'post'}
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Await, ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith))
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            assert name not in banned, name
