"""S3-2: `EffectiveRiskPolicy` 의 hybrid 축이 필수 필드라는 행동 계약.

hybrid 는 오늘 게시자 자기신고(`facts.hybrid_enabled`)뿐이라 대조 상대가 없다.
정책 쪽 축은 기본값을 가지면 '설정이 hybrid on 인데 신고가 없다'와 '정말 off'가
구분되지 않으므로 기본값 없이 실어 bool 만 받는다. 여기 GREEN 은 대조 준비이며
실제 대조(S3-3)·송신 허가·운영 승격이 아니다. 시각은 전부 주입한다(벽시계 의존 0).
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from src.execution.safety import risk_policy as p
from src.execution.safety.policy_snapshot import PolicyContext, build_owned_snapshot

from test_execution_policy_snapshot import baseline as owned_baseline
from test_execution_regime_recheck import fixture as regime_fixture
from test_execution_risk_policy import NOW, snapshot as policy_snapshot


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def context(**changes):
    value = PolicyContext.from_snapshot(policy_snapshot(p))
    return replace(value, policy=replace(value.policy, **changes)) if changes else value


def test_the_hybrid_axis_survives_the_policy_context_round_trip():
    """[부재 RED] 오늘은 축이 없어 hybrid on 인 policy 를 만들 수조차 없다(TypeError)."""
    on, off = context(hybrid_enabled=True), context(hybrid_enabled=False)
    assert on.to_dict()['policy']['hybrid_enabled'] is True
    assert on.to_dict() != off.to_dict()
    for value in (on, off):
        assert PolicyContext.from_dict(deepcopy(value.to_dict())) == value


def test_the_hybrid_axis_has_no_default_and_no_canonical_key_is_optional():
    """[행동] 기본값·관대한 키 집합은 '신고 없음'을 조용히 off 로 만든다."""
    fields = asdict(policy_snapshot(p).policy)
    fields.pop('hybrid_enabled', None)
    with pytest.raises(TypeError):
        p.EffectiveRiskPolicy(**fields)
    row = context(hybrid_enabled=True).to_dict()
    row['policy'].pop('hybrid_enabled', None)
    with pytest.raises(ValueError, match='invalid_policy_context_fields'):
        PolicyContext.from_dict(row)


@pytest.mark.parametrize('bad', [1, 0, 'true', None])
def test_the_hybrid_axis_refuses_a_non_bool_value(bad):
    """[행동] truthy 통과 금지 — 거부 사유도 키 집합이 아니라 값 형식이어야 한다."""
    row = context().to_dict()
    row['policy']['hybrid_enabled'] = bad
    with pytest.raises(ValueError) as rejected:
        PolicyContext.from_dict(row)
    assert 'fields' not in str(rejected.value)
    with pytest.raises(ValueError, match='bool'):
        replace(policy_snapshot(p).policy, hybrid_enabled=bad)


def test_the_owned_snapshot_keeps_the_hybrid_axis_without_a_regime_owner():
    """[행동] regime owner 가 없는 owner(비-regime 분기)의 snapshot 도 축을 그대로 싣는다.

    S3-3 의 hybrid 대조 상대가 바로 이 snapshot 이다 — 여기서 축이 조용히 off 로 바뀌면
    설정 hybrid on 이 대조를 통과해 버린다.
    """
    state = owned_baseline()
    assert 'regime_policy' not in state
    value = context(hybrid_enabled=True)
    owned = build_owned_snapshot(state, context=value, version=9, now=NOW, prices={})
    assert owned.policy == value.policy and owned.policy.hybrid_enabled is True


def test_the_owned_snapshot_keeps_the_hybrid_axis_through_the_regime_replace(tmp_path, monkeypatch):
    """[행동] regime 최소현금 replace 를 거쳐도 hybrid 축이 그대로 실려 나온다."""
    async def scenario():
        f = await regime_fixture(tmp_path, monkeypatch)
        try:
            runtime, now = f['runtime'], f['clock'][0]
            value = context(hybrid_enabled=True, regime_min_cash_reserve_pct=99.0)
            owned = build_owned_snapshot(runtime.owner.state, context=value,
                                         version=runtime.owner.version, now=now, prices={})
            # regime 분기를 실제로 탔다는 증거(최소현금이 레짐 표로 교체됨).
            assert owned.policy.regime_min_cash_reserve_pct != 99.0
            assert owned.policy.hybrid_enabled is True
            assert owned.policy == replace(value.policy, regime_min_cash_reserve_pct=
                                           owned.policy.regime_min_cash_reserve_pct)
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())
