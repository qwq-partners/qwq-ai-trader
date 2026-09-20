"""진입 판단 사실 builder (순수 함수).

I/O·await 가 없고 현재 시각을 스스로 읽지 않는다 — now 는 전부 인자다. CV/LLM 임계값을
다시 계산하지 않고 CV 가 보고한 기록값만 옮긴다(계획서 S2-4 금지 사항). 게시·소비·
stale 대조는 `commands.py` 가 하고 여기서는 게시 대상만 계산한다.

입력은 두 개다. `cv_decision` 은 S2-1 이 고정한 `CrossStrategyValidator.last_decision`
12키 dict, `sizing_inputs` 는 S2-2 가 고정한 `RiskManager._last_sizing_inputs` 14키
dict 다. 키 집합이 다르면 조용히 채우지 않고 거부한다.

반환은 `(facts, pending)` 이다. `facts.sources` 에는 게시 신원이 이미 확정된 출처
('regime' — 어댑터가 먼저 게시하고 `regime_row` 로 넘겨준 게시본)만 들어가고,
`pending` 은 어댑터가 `publish_qualification_source` 로 **먼저** 게시한 뒤
`ConsumedSource` 로 만들어 `dataclasses.replace(facts, sources=...)` 로 붙여야 하는
출처들이다(계약 6 게시 순서). digest 식이 여기 있어야 하므로 builder 가 먼저 돌고,
version 은 게시해야 정해지므로 facts 에 바로 담을 수 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .decisions import ConsumedSource, EntryDecisionFacts, QualificationFacts
from .policy_generations import canonical
from .protection_recovery import digest as _sha256

_KST = ZoneInfo('Asia/Seoul')

# S2-1·S2-2 가 고정한 입력 키 집합 (누락·추가는 전부 거부)
_CV_KEYS = frozenset({'token', 'symbol', 'side', 'strategy', 'regime', 'penalties',
                      'cap_applied', 'original', 'adjusted', 'memory_adj', 'panel', 'now_hm'})
_SIZING_KEYS = frozenset({'base_pct', 'strategy_allocation_pct', 'min_position_value',
                          'strength_multiplier', 'position_multiplier', 'calendar_multiplier',
                          'volatility_multiplier', 'conviction_multiplier', 'atr_pct',
                          'stop_pct', 'stop_source', 'stop_crash_capped', 'hybrid_enabled',
                          'overlay_status'})

# 계약 9 의 LLM 어휘. 반환 bool 로는 fail-open 통과와 실제 승인이 구분되지 않는다.
LLM_VERDICTS = ('approved', 'rejected_soft', 'not_required', 'skipped_no_manager',
                'skipped_bull', 'skipped_low_score', 'fail_open_quota', 'fail_open_error')

# penalties 한국어 문자열 → 안정 id (계약 11). CV 의 문자열·누적 cap 판정은 불변이므로
# 여기서 유도만 한다. 앞에서부터 처음 맞는 항목을 쓴다 — '추격매수' 두 규칙은 서로
# 겹치지 않는 부분 문자열로 구분한다.
RULE_IDS: tuple[tuple[str, str], ...] = (
    ('장초반 변동성', 'early_session_penalty'),
    ('점심 sweet spot', 'lunch_sweet_spot_bonus'),
    ('SEPA 90+ 추격매수', 'sepa_chase_penalty'),
    ('추격매수(등락/ATR', 'surge_chase_penalty'),
    ('지표결손(', 'missing_indicator_penalty'),
    ('RSI과매수(', 'rsi_overbought_penalty'),
    ('[규칙2] 기관+외국인 동시 순매도', 'supply_dual_sell_penalty'),
    ('손절종목 존재', 'sector_stoploss_penalty'),
    ('외국인 매수 상위 섹터(', 'sector_foreign_bonus'),
    ('MA200하방(', 'ma200_below_penalty'),
    ('적자+고PBR(', 'deficit_high_pbr_penalty'),
    ('극단PER(', 'extreme_per_penalty'),
    ('메모리보정(', 'trade_memory_adjust'),
    ('전문가패널 추천(', 'panel_outlook_bonus'),
    ('누적감점캡(', 'total_penalty_cap'),
)

# CV 시간 규칙이 바뀌는 경계(KST). 12:30~13:00 보너스는 13:00 을 포함하므로 13:01 이 경계다.
_CV_BOUNDARIES = ((9, 30), (10, 30), (12, 30), (13, 1))
_SEPA_DEADLINE = (14, 30)


class QualificationRefused(ValueError):
    """사유 코드를 가진 facts 미생성. 보정으로 넘기지 않고 자동 매수 게시를 0건으로 끝낸다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PendingSource:
    """아직 게시되지 않은 소비 출처. version 은 게시해야 정해지므로 담지 않는다."""
    name: str
    as_of: datetime
    digest: str


def _refuse(reason):
    raise QualificationRefused(reason)


def _float(value, reason):
    # bool 은 int 가 아니다(type 검사). 0·0.0 을 falsy 로 거르지 않는다.
    if type(value) not in (int, float):
        _refuse(reason)
    return float(value)


def _positive(value, reason):
    result = _float(value, reason)
    if not result > 0:
        _refuse(reason)
    return result


def _money(value, reason):
    if type(value) is not Decimal:
        _refuse(reason)
    return value


def _kst(value, reason):
    """naive 는 KST 지역시각으로 본다 — CV 의 `_panel_loaded_at` 이 naive 벽시계다."""
    if type(value) is not datetime:
        _refuse(reason)
    return value.replace(tzinfo=_KST) if value.utcoffset() is None else value.astimezone(_KST)


def regime_digest(regime) -> str:
    """계약 2 — CV 가 실제로 받은 레짐 문자열 하나가 digest 의 전부다."""
    if type(regime) is not str or not regime:
        _refuse('invalid_regime')
    return _sha256(canonical({'effective_regime': regime}))


def config_version(*, validator, llm, sizing, stops, experts) -> str:
    """호출자가 넘긴 설정 dict 들의 canonical digest. 이 모듈은 config 를 직접 읽지 않는다.

    S3 publisher 가 빠뜨리면 그 축의 변화가 검출되지 않는다. 넣어야 하는 것:

    - `validator`: `config/default.yml` 의 **중첩** `kr.validator` 블록 중 CV 판정에 쓰이는
      값(min_pass_score, missing_indicator_penalty_step/cap, rule_penalties).
      경로는 `scripts/run_trader.py:839` `kr_cfg.get("validator")` → `RiskManager(...,
      validator_config=...)` → `src/core/engine.py:1339` `_vcfg` → `CrossStrategyValidator(...)`.
    - `llm`: 같은 블록의 llm_daily_max·llm_check_score_min·llm_bypass_score·
      llm_reject_size_mult(engine.py:1394-1396 의 G4 트리거).
    - `sizing`: `src/core/engine.py:2616` `strategy_position_pct` 전략별 base_pct 표(설정
      객체가 아니라 함수 안 리터럴이라 `_entry_risk_config_hash` 로는 잡히지 않는다) +
      base_position_pct·max_position_pct·sizing_mode·risk_per_trade_pct·
      risk_max_position_pct·min_position_value·strategy_allocation·hybrid.enabled.
    - `stops`: `scripts/run_trader.py:498` 의 전략별 청산 파라미터(위험 사이징 분모인 고정
      SL. 제품에 `STRATEGY_EXIT_PARAMS` 라는 상수는 없고 이 dict 가 그 자리다) + 적용 중인
      ExitConfig 기본 손절·급락 cap.
    - `experts`: `config/default.yml:634` `experts.shadow_mode` 등 — 규칙11/12 가 판정에
      기여하기 시작하는 변화는 여기서만 드러난다.

    Decimal·datetime 은 canonical 이 거부하므로 호출자가 문자열로 정규화해 넘긴다.
    """
    try:
        return _sha256(canonical({'validator': validator, 'llm': llm, 'sizing': sizing,
                                  'stops': stops, 'experts': experts}))
    except ValueError:
        _refuse('invalid_config_version_input')


def entry_expires_at(decided_at, *, strategy, panel_loaded_at=None) -> datetime:
    """CV 다음 시간 경계·SEPA 14:30·패널 6시간·당일 말 중 **최소**(KST 자정 불초과).

    경계 직전 판단이 곧 만료되는 것은 fail-closed 방향이다(계획서 §7).
    """
    local = _kst(decided_at, 'invalid_decision_time')
    if decided_at.utcoffset() is None:
        _refuse('invalid_decision_time')
    limits = [local.replace(hour=23, minute=59, second=59, microsecond=999999)]
    for hour, minute in _CV_BOUNDARIES:
        edge = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if edge > local:
            limits.append(edge)
            break
    if strategy == 'sepa_trend':
        edge = local.replace(hour=_SEPA_DEADLINE[0], minute=_SEPA_DEADLINE[1],
                             second=0, microsecond=0)
        if edge > local:
            limits.append(edge)
    if panel_loaded_at is not None:
        limits.append(_kst(panel_loaded_at, 'invalid_panel_time') + timedelta(hours=6))
    return min(limits)


def _bucket(hm, strategy):
    """CV 시간 규칙이 달라지는 구간. 09:00~09:29 는 core_holding 만 면제된다."""
    if 900 <= hm < 930:
        return 'open_core' if strategy == 'core_holding' else 'blocked'
    if 930 <= hm < 1030:
        return 'early'
    if 1230 <= hm <= 1300:
        return 'lunch'
    return 'other'


def _check_clock(cv_decision, decided_at, strategy, rule_ids):
    """계약 12 — decided_at(aware KST)으로 재계산한 구간과 CV 보고가 다르면 거부한다.

    CV 의 now_hm 은 naive 지역 시각이고 decided_at 은 aware 다. UTC CI 에서 상시
    발화하지 않도록 비교는 "decided_at 을 KST 로 본 HHMM" 과 now_hm 의 **구간**으로 한다.
    """
    now_hm = cv_decision['now_hm']
    if type(now_hm) is not int or not 0 <= now_hm <= 2359 or now_hm % 100 > 59:
        # KR 매수 가드가 돌지 않았다(US 인스턴스·조기 return) — 자동 매수 facts 를 만들지 않는다
        _refuse('decision_clock_disagreement')
    local = decided_at.astimezone(_KST)
    local_hm = local.hour * 100 + local.minute
    reported, recomputed = _bucket(now_hm, strategy), _bucket(local_hm, strategy)
    if reported != recomputed or reported == 'blocked':
        _refuse('decision_clock_disagreement')
    # 계약 13·결정 ⑥ — SEPA 14:30 상한은 'other' 버킷 안쪽 경계라 위 대조로는 드러나지 않는다.
    # decided_at 은 자기 llm_second_check(네트워크) 뒤에 찍히므로 "14:29 판정 → 14:31 게시"
    # 교차가 실제로 생기고, 그러면 상한이 통째로 사라진다. 창을 늘리지 않고 판단을 버린다.
    if strategy == 'sepa_trend' and (now_hm < 1430) != (local_hm < 1430):
        _refuse('decision_clock_disagreement')
    # 양쪽 시계가 모두 상한을 넘긴 SEPA 판단은 entry_expires_at 의 14:30 항이 소멸해 당일 말까지
    # 유효해진다. 상류(sepa_trend 의 14:30 신호 차단)는 naive 벽시계라 믿지 않는다 — 버린다.
    if strategy == 'sepa_trend' and local_hm >= 1430:
        _refuse('sepa_entry_deadline_passed')
    # 장초반 감점은 배치·core·swing 면제가 있어 한 방향만 검사한다.
    if 'early_session_penalty' in rule_ids and reported != 'early':
        _refuse('decision_clock_disagreement')
    # 점심 보너스는 면제가 없다 — 양방향으로 일치해야 한다.
    if ('lunch_sweet_spot_bonus' in rule_ids) != (reported == 'lunch'):
        _refuse('decision_clock_disagreement')


def _rule_ids(penalties, overlay_status):
    if type(penalties) is not tuple:
        _refuse('unexpected_cv_decision')
    ids = []
    for penalty in penalties:
        if type(penalty) is not str:
            _refuse('unknown_penalty_rule')
        for needle, rule_id in RULE_IDS:
            if needle in penalty:
                ids.append(rule_id)
                break
        else:
            # 새 감점 규칙이 생겼는데 표가 따라오지 않은 것이다. 조용히 버리지 않는다.
            _refuse('unknown_penalty_rule')
    # 계약 10 — overlay 예외는 배율 1.0 과 구분되지 않으므로 rule id 로만 드러난다.
    if type(overlay_status) is not dict or set(overlay_status) != {'calendar', 'volatility', 'conviction'}:
        _refuse('unexpected_sizing_inputs')
    for kind in ('calendar', 'volatility', 'conviction'):
        status = overlay_status[kind]
        if status not in ('applied', 'unavailable'):
            _refuse('unexpected_sizing_inputs')
        if status == 'unavailable':
            ids.append(f'overlay_{kind}_unavailable')
    if len(set(ids)) != len(ids):
        _refuse('duplicate_rule_id')
    return tuple(ids)


def _regime_source(regime_used, regime_row, decided_at):
    """계약 4 — 어댑터가 먼저 게시한 'regime' 게시본과 같은 digest 여야 한다."""
    expected = regime_digest(regime_used)
    if type(regime_row) is not dict or regime_row.keys() != {'version', 'as_of', 'digest'}:
        _refuse('regime_source_unpublished')
    if regime_row['digest'] != expected:
        _refuse('regime_source_digest_mismatch')
    try:
        as_of = datetime.fromisoformat(regime_row['as_of'])
    except (TypeError, ValueError):
        _refuse('regime_source_unpublished')
    if as_of.utcoffset() is None:
        _refuse('regime_source_unpublished')
    if as_of > decided_at:
        # `_consumed_sources` 가 소비 시각 역전을 거부한다 — 여기서 먼저 끊는다.
        _refuse('regime_source_future')
    return ConsumedSource(name='regime', version=regime_row['version'], as_of=as_of,
                          digest=expected)


def _pending_sources(cv_decision, sector, decided_at):
    """판정을 실제로 바꾼 출처만 게시 대상으로 남긴다(계약 1).

    as_of 는 관측(판독) 시각이라 판단 시각을 쓰고, 원본 생성시각은 digest 안에 넣는다
    (계약 3). digest 에는 계산된 결과값을 담아야 파일이 그대로여도 날짜로 값이 달라지는
    경우를 잡는다(계획서 §7).
    """
    pending = []
    panel = cv_decision['panel']
    if panel is not None:
        if type(panel) is not dict or panel.keys() != {'created_at', 'conviction', 'bonus', 'loaded_at'}:
            _refuse('unexpected_cv_decision')
        body = {'created_at': panel['created_at'], 'conviction': panel['conviction'],
                'bonus': panel['bonus']}
        try:
            row_digest = _sha256(canonical(body))
        except ValueError:
            _refuse('unexpected_cv_decision')
        pending.append(PendingSource('panel_outlook', decided_at, row_digest))
    memory_adj = cv_decision['memory_adj']
    if type(memory_adj) is not int:
        _refuse('unexpected_cv_decision')
    if memory_adj != 0:
        # last_decision 에는 적용 규칙·score_delta 가 없다. 보정을 결정하는 입력(전략·섹터)과
        # 적용된 결과값으로 digest 를 만든다.
        body = {'strategy': cv_decision['strategy'], 'sector': sector, 'memory_adj': memory_adj}
        pending.append(PendingSource('trade_memory', decided_at, _sha256(canonical(body))))
    return tuple(pending)


def build_decision_facts(*, intent_id, symbol, side, strategy, origin, sector, cv_decision,
                         llm_reason, sizing_inputs, config_version, regime_used, regime_row,
                         decided_at) -> tuple[EntryDecisionFacts, tuple[PendingSource, ...]]:
    """CV 증거 + 사이징 입력 + 레짐 게시본 → 불변 facts 와 게시 대상 출처.

    보정하지 않는다: 0 이하 배율·hybrid 는 `0 or 1.0` 류로 넘기지 않고 사유 코드와 함께
    facts 미생성으로 끝낸다(계약 15). 반환 facts 는 기록일 뿐 송신 허가가 아니다.
    """
    if type(cv_decision) is not dict or cv_decision.keys() != _CV_KEYS:
        _refuse('unexpected_cv_decision')
    if type(sizing_inputs) is not dict or sizing_inputs.keys() != _SIZING_KEYS:
        _refuse('unexpected_sizing_inputs')
    if type(decided_at) is not datetime or decided_at.utcoffset() is None:
        _refuse('invalid_decision_time')
    if (cv_decision['symbol'], cv_decision['side'], cv_decision['strategy']) != (symbol, side, strategy):
        _refuse('decision_identity_mismatch')
    if cv_decision['regime'] != regime_used:
        _refuse('regime_used_mismatch')
    if llm_reason not in LLM_VERDICTS:
        _refuse('unknown_llm_verdict')

    if sizing_inputs['hybrid_enabled'] is not False:
        # legacy wrapper 가 호라이즌별 base/max/pool 로 갈아타 재유도가 성립하지 않는다.
        _refuse('unsupported_hybrid_sizing')
    multipliers = {key: _positive(sizing_inputs[key], 'non_positive_multiplier')
                   for key in ('strength_multiplier', 'position_multiplier', 'calendar_multiplier',
                               'volatility_multiplier', 'conviction_multiplier')}

    rule_ids = _rule_ids(cv_decision['penalties'], sizing_inputs['overlay_status'])
    _check_clock(cv_decision, decided_at, strategy, rule_ids)

    source = _regime_source(regime_used, regime_row, decided_at)
    pending = _pending_sources(cv_decision, sector, decided_at)

    panel = cv_decision['panel']
    expires_at = entry_expires_at(decided_at, strategy=strategy,
                                  panel_loaded_at=None if panel is None else panel['loaded_at'])
    if expires_at <= decided_at:
        # 패널 6시간 창이 이미 지났다 — 창을 늘리지 않고 판단을 버린다.
        _refuse('decision_already_expired')

    original = _float(cv_decision['original'], 'invalid_decision_score')
    atr_pct = sizing_inputs['atr_pct']
    stop_pct = sizing_inputs['stop_pct']
    facts = EntryDecisionFacts(
        intent_id=intent_id, symbol=symbol, side=side, strategy=strategy, origin=origin,
        sector=sector, config_version=config_version, hybrid_enabled=False,
        decided_at=decided_at, expires_at=expires_at,
        base_pct=_positive(sizing_inputs['base_pct'], 'invalid_decision_base_pct'),
        strategy_allocation_pct=(None if sizing_inputs['strategy_allocation_pct'] is None
                                 else _positive(sizing_inputs['strategy_allocation_pct'],
                                                'invalid_decision_allocation')),
        min_position_value=_money(sizing_inputs['min_position_value'],
                                  'invalid_decision_min_position_value'),
        strength_multiplier=multipliers['strength_multiplier'],
        position_multiplier=multipliers['position_multiplier'],
        calendar_multiplier=multipliers['calendar_multiplier'],
        volatility_multiplier=multipliers['volatility_multiplier'],
        conviction_multiplier=multipliers['conviction_multiplier'],
        atr_pct=None if atr_pct is None else _float(atr_pct, 'invalid_decision_atr'),
        stop_pct=None if stop_pct is None else _money(stop_pct, 'invalid_decision_stop'),
        stop_source=sizing_inputs['stop_source'],
        stop_crash_capped=sizing_inputs['stop_crash_capped'],
        qualification=QualificationFacts(
            signal_score=original, original_score=original,
            adjusted_score=_float(cv_decision['adjusted'], 'invalid_decision_score'),
            applied_rule_ids=rule_ids, llm_verdict=llm_reason),
        sources=(source,))
    return facts, pending
