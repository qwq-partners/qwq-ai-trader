from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, Mock
import ast
import asyncio
from pathlib import Path

import pytest

from src.analytics.audience_reports import MarketSnapshot, render_engine_digest, render_market_report
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule, run_audience_reports
from src.utils.telegram import TelegramNotifier

KST = ZoneInfo('Asia/Seoul')


def test_morning_uses_actual_us_quote_session_date():
    now = datetime(2026, 10, 27, 7, 42, tzinfo=KST)
    quote = {'price': 6000, 'change_pct': 1.25,
             'market_time': datetime(2026, 10, 26, 16, tzinfo=ZoneInfo('America/New_York')).timestamp()}
    text = render_market_report('morning', MarketSnapshot(us_quotes={'^GSPC': quote}, us_basis_date='2026-10-26'), now)
    assert text and 'S&amp;P500 +1.25%' in text
    assert render_market_report('morning', MarketSnapshot(us_quotes={'^GSPC': quote}, us_basis_date='2026-10-23'), now) is None


def test_record_calls_match_supported_interface_and_core_warning_stays_immediate():
    root = Path(__file__).resolve().parents[1]
    for name in ('src/schedulers/kr_scheduler.py', 'src/core/batch_analyzer.py'):
        tree = ast.parse((root/name).read_text())
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, (ast.Name, ast.Attribute)):
                fn = call.func.id if isinstance(call.func, ast.Name) else call.func.attr
                if fn == 'record_report':
                    assert not call.keywords, ast.unparse(call)
                    assert not (call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == 'core_stale_watch')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['send_photo', 'send_document'])
async def test_explicit_missing_media_target_does_not_fall_back(method):
    notifier = TelegramNotifier(bot_token='fake', alert_chat_id='private')
    notifier._session = Mock(closed=False)
    assert not await getattr(notifier, method)(b'data', chat_id=None)
    notifier._session.post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,expected', [(200, {'ok': True}, 'accepted'), (400, {'ok': False}, 'rejected'), (500, {}, 'unknown')])
async def test_routine_transport_preserves_acceptance_rejection_uncertainty(tmp_path, status, body, expected):
    notifier = TelegramNotifier(bot_token='fake', report_chat_id='channel')
    response = AsyncMock()
    response.status = status
    response.json.return_value = body
    response.__aenter__.return_value = response
    notifier._session = Mock(closed=False)
    notifier._session.post.return_value = response
    store = ReportStore(tmp_path/'r.sqlite3')
    assert await notifier.publish_report('market', 'morning', '2026-10-27', 'x', '시장', store=store) == expected
    assert await notifier.publish_report('market', 'morning', '2026-10-27', 'x', '시장', store=store) == 'already_consumed'
    assert notifier._session.post.call_count == 1


@pytest.mark.asyncio
async def test_invalid_body_is_terminal_and_delivery_issues_reach_digest(tmp_path):
    store = ReportStore(tmp_path/'r.sqlite3')
    notifier = TelegramNotifier(bot_token='fake')
    notifier._session = Mock(closed=False)
    assert await notifier.publish_report('market', 'morning', '2026-10-27', 'a', 'x'*3001, store=store) == 'failed_validation'
    assert await notifier.publish_report('market', 'morning', '2026-10-27', 'b', 'fixed', store=store) == 'already_consumed'
    store.claim('market', 'midday', '2026-10-27', 'b')  # Simulated interrupted process.
    store.claim('market', 'after', '2026-10-27', 'c')
    store.finish('market', 'after', '2026-10-27', 'unknown')
    spy = AsyncMock()
    spy.publish_report.return_value = 'accepted'
    await ReportSchedule(store, spy).tick(datetime(2026,10,27,21,0,tzinfo=KST), True, MarketSnapshot(), 'context')
    text = spy.publish_report.call_args.args[4]
    assert '본문 검증 실패' in text and '처리 중단·결과 미확인' in text and '접수 여부 미확인' in text
    notifier._session.post.assert_not_called()


def test_repeated_digest_inputs_show_count_and_latest_without_claiming_totals():
    rows = [{'kind':'expert_quality', 'text':'08시 자료'}, {'kind':'expert_quality', 'text':'15시 자료'}]
    text = render_engine_digest(rows, '현재 대응', datetime(2026,10,27,21,0,tzinfo=KST))
    assert '2건 중 최신' in text and '15시 자료' in text
    assert '유형별 최근 결과' in text


@pytest.mark.asyncio
async def test_report_loop_respects_stopped_bot():
    await asyncio.wait_for(run_audience_reports(Mock(running=False)), timeout=0.2)


def test_digest_with_all_quality_receipts_still_fits_mobile():
    issues = [{'report_date': '2026-10-27', 'kind': kind, 'status': 'attempted'} for kind in ('morning', 'midday', 'after', 'daily')]*2
    rows = [{'kind':f'topic{i}', 'text':'&'*500} for i in range(12)]
    text = render_engine_digest(rows, ('&'*500+'\n')*3, datetime(2026,10,27,21,tzinfo=KST), deliveries=issues)
    assert len(text) <= 1800 and len(text.splitlines()) <= 35


def test_legacy_report_row_survives_append_migration(tmp_path):
    import sqlite3
    path = tmp_path/'r.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE reports (report_date TEXT, kind TEXT, text TEXT, recorded_at TEXT, PRIMARY KEY(report_date,kind))')
        db.execute("INSERT INTO reports VALUES ('2026-10-27','batch','legacy','2026-10-27T08:00:00+09:00')")
    store = ReportStore(path)
    store.record('batch','new', now=datetime(2026,10,27,9,tzinfo=KST))
    assert [r['text'] for r in store.records('2026-10-27')] == ['legacy','new']


def test_fixed_snapshot_record_mode_has_dependency_closure(tmp_path):
    import shutil
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    artifact = tmp_path/'fixed'
    for relative in ('scripts/llm_migration_monitor.py', 'scripts/analyze_shadow.py',
                     'src/__init__.py', 'src/utils/__init__.py', 'src/utils/telegram.py',
                     'src/analytics/__init__.py', 'src/analytics/report_delivery.py'):
        target = artifact/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root/relative, target)
    # Isolated subprocess imports only the packaged source, not this worktree's src.
    # Explicit paths are synthetic. Never replace HOME or touch production data.
    code = '''
import sys, socket, runpy, sqlite3
from pathlib import Path
root, target = map(Path, sys.argv[1:3])
sys.path.insert(0,str(root))
from src.analytics import report_delivery
report_delivery.default_store_path = lambda: target
socket.socket.connect = lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden'))
sys.argv = [str(root/'scripts/llm_migration_monitor.py'), '--state-file',str(root/'synthetic.json'),'--log-dir',str(root/'empty')]
try:
    runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
with sqlite3.connect(target) as db:
    assert db.execute('SELECT kind FROM report_events').fetchall() == [('llm_migration',)]
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(artifact), str(tmp_path/'reports.sqlite3')], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_unknown_and_failed_delivery_precede_expected_data_skips(tmp_path):
    store = ReportStore(tmp_path/'r.sqlite3')
    for day, audience, kind, status in [('2026-10-27','market','after','unknown'), ('2026-10-27','market','midday','skipped_missing_current_market_basis'), ('2026-10-27','market','morning','skipped_missing_market_basis'), ('2026-10-26','market','after','rejected'), ('2026-10-26','engine','daily','attempted')]:
        store.claim(audience,kind,day,kind)
        if status != 'attempted':store.finish(audience,kind,day,status)
    issues=store.delivery_issues('2026-10-27')
    assert [r['status'] for r in issues[:3]] == ['unknown','rejected','attempted']
    text=render_engine_digest([], '', datetime(2026,10,27,21,tzinfo=KST), deliveries=issues)
    assert '개인 요약: 처리 중단·결과 미확인' in text


def test_legacy_skipped_evaluation_rows_are_labelled_without_rewriting_history(tmp_path):
    import json
    from src.analytics.morning_brief_eval import summarize, summary_line
    path=tmp_path/'eval.jsonl'
    path.write_text(json.dumps({'evaluated':False,'open_direction':{'hit':None}})+'\n')
    result=summarize(path)
    assert result['samples']==1 and result['legacy_rows']==1 and result['open_direction']['n']==0
    assert '구형 1건 포함' in summary_line(path)


@pytest.mark.asyncio
@pytest.mark.parametrize('hour,minute', [(12,0),(13,12)])
async def test_report_loop_live_iteration_uses_real_heartbeat_api(monkeypatch, hour, minute):
    from types import SimpleNamespace
    from src.schedulers import report_scheduler as module
    from src.utils import loop_heartbeat as hb
    bot=SimpleNamespace(running=True)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,27,hour,minute,tzinfo=KST)
    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(module,'market_snapshot',lambda *a,**k:MarketSnapshot())
    monkeypatch.setattr('src.risk.kill_switch.status', lambda _: {})
    tick=AsyncMock(return_value='skipped_missing_current_market_basis')
    monkeypatch.setattr(module.ReportSchedule,'tick',tick)
    monkeypatch.setattr(hb,'_states',{})
    monkeypatch.setattr(hb,'_beats',{})
    async def stop(_):bot.running=False
    monkeypatch.setattr(module.asyncio,'sleep',stop)
    await module.run_audience_reports(bot)
    assert 'kr_audience_reports' in hb._states
    assert tick.await_count == (1 if hour==13 else 0)
    if hour == 13:
        assert '검증 시각 미확인' in tick.call_args.args[3]
        assert '현금 상태 미확인' in tick.call_args.args[3]
