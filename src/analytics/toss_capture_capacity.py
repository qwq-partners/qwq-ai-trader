"""실제 토스 수신기와 저장 경로의 합성 용량 검증. 외부 접속 없음."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
import tempfile
import time
import tracemalloc

from src.data.providers.toss.orderbook_stream import TossOrderbookCapture, receive_orderbooks
from src.observation.toss_ws_service import CaptureArtifact, read_capture_artifact, _json

START = datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc)
SYMBOLS = ('111111', '222222', '333333')
FIELDS = set('name duration_seconds frame_count max_frames distribution timing levels horizon_seconds expected_stop artifact_max_bytes'.split())
BUDGETS = dict(python_peak_bytes=256*1024*1024, wall_seconds=120, cpu_seconds=120)
RATE = 10000 / 435.343147


def _validate(case):
    if type(case) is not dict or set(case)!=FIELDS:raise ValueError('fixed synthetic case required')
    if type(case['name']) is not str or re.fullmatch('[a-z][a-z0-9_-]{0,63}',case['name']) is None:
        raise ValueError('bounded case name required')
    for key,limit in (('frame_count',100000),('max_frames',50000),('levels',64),('artifact_max_bytes',67108864)):
        if type(case[key]) is not int or not 1<=case[key]<=limit:raise ValueError('case integer bound')
    for key in ('duration_seconds','horizon_seconds'):
        if type(case[key]) not in (int,float) or not math.isfinite(case[key]) or case[key]<=0:
            raise ValueError('finite positive time required')
    if not case['horizon_seconds']<case['duration_seconds']<=3600:raise ValueError('bounded horizon/window required')
    if (case['distribution'] not in ('pilot','one_symbol') or case['timing'] not in ('uniform','burst')
            or case['expected_stop'] not in ('window_ended','frame_limit')):
        raise ValueError('fixed case enum required')
    return dict(case)


class _SyntheticSocket:
    """필요할 때 한 프레임만 생성하고 실제 수신기에 주입 시각을 전달한다."""
    def __init__(self,case):
        self.case=case;self.index=0;self.seconds=0.;self.closed=False;self.sent=0

    def now(self):return START+timedelta(seconds=self.seconds)
    def clock(self):return self.seconds
    async def send_str(self,text):self.sent+=1
    async def close(self):self.closed=True

    async def receive_str(self):
        c=self.case
        if self.index>=c['frame_count']:
            self.seconds=c['duration_seconds']
            return '{"type":"pong"}'
        index=self.index;self.index+=1
        if index==0:
            return _json(dict(type='subscriptions',id='synthetic-capacity',
                subscribed=[f'orderbook:kr:{s}' for s in SYMBOLS],rejected=[])).decode()
        fraction=index/max(1,c['frame_count']-1)
        span=c['duration_seconds']-0.000001
        self.seconds=fraction*(min(1.,span) if c['timing']=='burst' else span)
        # 전체 기간의 시간 분포와 종목 편중은 합성 가정이며 실제 틱 재생이 아니다.
        selector=((index-1)*7919)%9992
        symbol=SYMBOLS[0 if c['distribution']=='one_symbol' or selector<7128 else 1 if selector<9424 else 2]
        return _json(dict(type='message',topic=f'orderbook:kr:{symbol}',data=dict(
            timestamp=self.now().isoformat(),currency='KRW',
            asks=[dict(price=str(10000+i),volume='100') for i in range(c['levels'])],
            bids=[dict(price=str(9990-i),volume='100') for i in range(c['levels'])]))).decode()


async def run_case(case, *, directory):
    """유한 합성 자료만 임시 저장한다. 자원 예산은 측정 기준이며 강제 격리가 아니다."""
    case=_validate(case)
    directory=Path(directory)
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise ValueError('existing absolute synthetic directory required')
    if tracemalloc.is_tracing():raise ValueError('exclusive memory measurement required')
    socket=_SyntheticSocket(case)
    plan_hash=hashlib.sha256(_json(case)).hexdigest()
    artifact_bytes=0;artifact_sha=None;artifact_error=None;roundtrip=False;serialized_bytes=0
    wall=time.perf_counter();cpu=time.process_time()
    tracemalloc.start()
    try:
        capture=TossOrderbookCapture(symbols=list(SYMBOLS),request_id='synthetic-capacity',
            evaluation_epoch='synthetic-capacity-v1',start_at=START,
            end_at=START+timedelta(seconds=case['duration_seconds']),max_frames=case['max_frames'])
        await receive_orderbooks(socket,capture,exclusive=True,now=socket.now,monotonic=socket.clock)
        result=capture.export()
        serialized_bytes=len(_json(result))
        # 기존 파일을 건드리지 않고 직접 만든 임시 디렉터리만 정리한다.
        with tempfile.TemporaryDirectory(prefix='synthetic-capacity-',dir=directory) as temporary:
            path=Path(temporary)/'capture.jsonl'
            artifact=CaptureArtifact(path,max_bytes=case['artifact_max_bytes'],plan_hash=plan_hash)
            try:
                artifact.open();artifact.finish(result);artifact.close()
                reread=read_capture_artifact(path,max_bytes=case['artifact_max_bytes'],plan_hash=plan_hash)
                roundtrip=reread==result
                del reread
            except (ValueError,OSError):artifact_error='artifact_write_or_read_failed'
            finally:artifact.close()
            if path.exists():
                raw=path.read_bytes();artifact_bytes=len(raw);artifact_sha=hashlib.sha256(raw).hexdigest();del raw
        counts=Counter(r['symbol'] for r in result['records'])
        horizons={s:False for s in SYMBOLS};first=last=None
        for record in result['records']:
            offset=(datetime.fromisoformat(record['received_at'])-START).total_seconds()
            first=offset if first is None else first;last=offset
            if offset>=case['horizon_seconds'] and not record['quality_issues']:horizons[record['symbol']]=True
        metrics=dict(python_peak_bytes=tracemalloc.get_traced_memory()[1],
            wall_seconds=time.perf_counter()-wall,cpu_seconds=time.process_time()-cpu)
    finally:tracemalloc.stop()
    window=(result['stop_reason']=='window_ended' and socket.closed and not result['cleanup_failed'])
    resources=roundtrip and artifact_error is None and all(metrics[k]<=v for k,v in BUDGETS.items())
    return dict(schema_version='toss-capacity-case-v1',synthetic=True,case=case,
        received_frames=result['received_frames'],quote_count=len(result['records']),
        quote_counts_by_symbol={s:counts[s] for s in SYMBOLS},
        first_quote_offset_seconds=first,last_quote_offset_seconds=last,
        horizon_quote_by_symbol=horizons,all_symbols_horizon_quote=all(horizons.values()),
        stop_reason=result['stop_reason'],cleanup_failed=result['cleanup_failed'],
        window_coverage_met=window,resource_budget_met=bool(resources),
        test_expectation_met=result['stop_reason']==case['expected_stop'],
        metrics=metrics,budgets=dict(BUDGETS),serialized_result_bytes=serialized_bytes,
        artifact_bytes=artifact_bytes,artifact_sha256=artifact_sha,artifact_error=artifact_error,
        artifact_roundtrip_verified=roundtrip,production_eligible=False,profit_comparison_available=False,
        stream_complete=None,limitations=[
            'Synthetic envelope assumption; not replayed market ticks or future capacity guarantee.',
            'Python tracked allocation includes export/storage/readback; not process RSS or hard memory isolation.',
            'Window completion does not prove lossless delivery or account profitability.'])


def qualification_cases(suite):
    def make(name,duration,frames,cap=50000,distribution='pilot',timing='uniform'):
        return dict(name=name,duration_seconds=duration,frame_count=frames,max_frames=cap,
            distribution=distribution,timing=timing,levels=10,horizon_seconds=900 if duration>900 else 10,
            expected_stop='frame_limit' if frames>=cap else 'window_ended',artifact_max_bytes=67108864)
    if suite=='smoke':return [make('smoke-window',12,100),make('smoke-cap',12,100,50)]
    if suite!='full':raise ValueError('fixed suite required')
    return [make('pilot-default-cap',1800,math.ceil(RATE*1800),10000),
        make('markout-plus-margin',901,math.ceil(RATE*901)),
        make('ack-to-planned-end',1659.665122,math.ceil(RATE*1659.665122)),
        make('whole-window-mean',1800,math.ceil(RATE*1800)),
        make('whole-window-double',1800,math.ceil(2*RATE*1800)),
        make('whole-window-one-symbol',1800,math.ceil(RATE*1800),distribution='one_symbol'),
        make('whole-window-burst',1800,math.ceil(RATE*1800),timing='burst'),
        make('exact-frame-cap',1800,50000)]


async def qualify(suite, *, directory):
    results=[]
    for case in qualification_cases(suite):results.append(await run_case(case,directory=directory))
    return dict(schema_version='toss-capacity-qualification-v1',suite=suite,synthetic=True,
        assumed_reference_rate=RATE,reference='pilot aggregate 10000 frames / 435.343147 seconds',
        cases=results,test_expectations_met=all(r['test_expectation_met'] for r in results),
        all_cases_window_coverage_met=all(r['window_coverage_met'] for r in results),
        all_cases_resource_budget_met=all(r['resource_budget_met'] for r in results),
        production_eligible=False,profit_comparison_available=False)
