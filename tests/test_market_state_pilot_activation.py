"""미래의 한 번 설치 완료와 실제 수신 완료를 혼동하지 않는다."""
from datetime import datetime, timezone

import pytest

from test_entry_capture_activate import module, setup

PROFILE='20261014-market-state-pilot-v1'


def test_date_pinned_engine_only_pilot_profile():
    p=module().activation_profile(PROFILE)
    assert p.engine_only and p.allow_predeployed
    assert p.evaluation_epoch=='2026-10-14-market-state-pilot-v1'
    assert p.not_before==datetime(2026,10,13,23,55,tzinfo=timezone.utc)
    assert p.latest_start_at==datetime(2026,10,13,23,57,tzinfo=timezone.utc)


def prepared(s):
    s.anchors.update(scan_scope='pilot',pilot=dict(installed=True,stream_ready=False,connected=False,
        generation=0,operational_demand=2,missing_from_operational=3,registration_cap=41,
        external_reserved=0,operational_headroom=10))


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
def test_preopen_static_installation_ends_once_without_toss_or_second_restart(setup):
    s=setup; prepared(s)
    result=s.run()
    assert result['status']=='complete' and not result['toss_attempted']
    assert sum('restart' in event for event in s.events)==1
    assert not any('start' in event for event in s.events)
    count=len(s.events)
    assert s.run()['status']=='rejected' and len(s.events)==count


@pytest.mark.parametrize('setup',[PROFILE],indirect=True)
@pytest.mark.parametrize('problem',['scope','missing','capacity','closed','disconnected_generation','connected_unready'])
def test_incomplete_pilot_installation_never_passes_readiness_or_restarts_again(setup,problem):
    s=setup; prepared(s); p=s.anchors['pilot']
    if problem=='scope': s.anchors['scan_scope']='window'
    elif problem=='missing': p['installed']=False
    elif problem=='capacity': p['operational_demand']=30
    elif problem=='closed': s.anchors['capture_closed']=True
    elif problem=='disconnected_generation': p['generation']=1
    elif problem=='connected_unready': p['connected']=True
    result=s.run()
    assert result['status']=='failed' and result['phase']=='readiness'
    assert sum('restart' in event for event in s.events)==1
    assert not result['toss_attempted']
