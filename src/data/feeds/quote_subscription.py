"""명시 주입형 KIS 채널 소유권 조정기. 기본 런타임은 설치하지 않는다.

공식 2026-04-20 공지: appkey당 1세션, 실시간 등록 합산 41건.
등록 단위는 공식 요청 구조의 (tr_id, tr_key)로 예산화한다.
ACK에는 request ID가 없어 같은 연결에서 해제 완료한 key를 재사용하지 않는다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time
from uuid import uuid4

from src.analytics.entry_observation import EntryObservationBuffer

Channel = tuple[str, str]
BOOK = "H0STASP0"
PRICE_IDS = {"H0STCNT0", "H0NXCNT0"}
STATE_CHANNELS = (BOOK, "H0STCNT0", "H0STMKO0")


@dataclass
class Registration:
    pending: str | None
    requested_at: float
    status: str = "requested"
    acknowledged: bool = False
    observed: bool = False
    operational_observed: bool = False
    send_completed: asyncio.Event | None = None


@dataclass(frozen=True)
class Lease:
    symbol: str
    deadline: float
    expires_at: str


class QuoteSubscriptionCoordinator:
    """한 WS의 운영/관측 채널을 직렬화. 송신 완료는 승인·실수신이 아니다."""

    def __init__(self, observer, *, registration_cap: int, external_reserved: int,
                 operational_headroom: int, max_candidates: int, lease_seconds: float,
                 evidence_ref: str, monotonic=time.monotonic, request_interval=0.15,
                 send_timeout=2.0, ack_timeout=15.0, unsubscribe_success_messages=(),
                 unsubscribe_evidence_ref=None, market_state_evidence=False, market_state_max_records=None):
        if not isinstance(observer, EntryObservationBuffer):
            raise ValueError("동일 평가 세대의 EntryObservationBuffer 필요")
        if type(market_state_evidence) is not bool:
            raise ValueError("market_state_evidence: 명시 boolean 필요")
        self.market_state_evidence = market_state_evidence
        if ((market_state_evidence and (type(market_state_max_records) is not int or market_state_max_records <= 0))
                or (not market_state_evidence and market_state_max_records is not None)):
            raise ValueError("market-state 기록 예산은 활성 계약에 명시한 양의 정수 필요")
        self.market_state_max_records = market_state_max_records
        self._market_state_records = 0
        for name, value, minimum in (("registration_cap", registration_cap, 1),
                ("external_reserved", external_reserved, 0),
                ("operational_headroom", operational_headroom, 0),
                ("max_candidates", max_candidates, 1)):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name}: 정수 범위 위반")
        if registration_cap > 41 or external_reserved + operational_headroom > registration_cap:
            raise ValueError("공식 등록 한도/예약 범위 위반")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("세션 독점·외부 등록 예약 근거 필요")
        if (not isinstance(unsubscribe_success_messages, tuple)
                or any(not isinstance(v, str) or not v.strip() or v == "SUBSCRIBE SUCCESS"
                       for v in unsubscribe_success_messages)
                or (unsubscribe_success_messages and (not isinstance(unsubscribe_evidence_ref, str)
                                                       or not unsubscribe_evidence_ref.strip()))):
            raise ValueError("해제 성공 응답은 검증된 문자열 집합·별도 근거가 필요")
        for value in (lease_seconds, send_timeout, ack_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("유한 양수 시간 필요")
        if (type(request_interval) not in (int, float) or not math.isfinite(request_interval)
                or request_interval < 0):
            raise ValueError("유효 요청 간격 필요")
        self.observer = observer
        self.cap = registration_cap
        self.reserved = external_reserved
        self.headroom = operational_headroom
        self.max_candidates = max_candidates
        self.lease_seconds = lease_seconds
        self.evidence_ref = evidence_ref
        # 공식 예제는 UNSUB prefix만 제공한다. 정확한 성공값 미확보 상태는 기본 불승인.
        self.unsubscribe_success_messages = unsubscribe_success_messages
        self.unsubscribe_evidence_ref = unsubscribe_evidence_ref
        self.clock = monotonic
        self.interval = request_interval
        self.send_timeout = send_timeout
        self.ack_timeout = ack_timeout
        self.generation = 0
        self.connection_id = None
        self._send = None
        self._lock = asyncio.Lock()
        self._operational: list[Channel] = []
        self._leases: dict[str, Lease] = {}
        self._states: dict[Channel, Registration] = {}
        self._retired: set[Channel] = set()
        self._tasks: set[asyncio.Task] = set()
        # ACK follow-up sends share the sender lock, not the enrollment queue budget.
        self._ack_reconcile_task: asyncio.Task | None = None
        self._ack_reconcile_dirty = False
        self._ack_reconcile_enabled = False
        self._last_send = float("-inf")
        self._candidate_status: dict[str, str] = {}
        self._observation_ended = False
        self._ready_requests: dict[str, dict] = {}
        self._ready_unresolved: set[str] = set()
        self._static_lease_id = None
        self._operational_initialized = False

    def install_static_lease(self, symbol, *, expires_at):
        """정지 피드의 명시 기술 계약에 한 번만 설치. 운영 수요를 먼저 확인한다."""
        if (not getattr(self.observer, 'static_pilot', False) or not self.market_state_evidence
                or self.max_candidates != 1 or self.generation or self._send is not None
                or self._leases or self._static_lease_id is not None or self._observation_ended
                or not self._operational_initialized):
            raise RuntimeError('정지 상태의 한 번뿐인 파일럿 설치 필요')
        if symbol != self.observer.pilot_symbol or expires_at != self.observer.end_at:
            raise ValueError('파일럿 symbol/종료 계약 불일치')
        remaining = (expires_at-self.observer.current_time()).total_seconds()
        if self.observer.ended or remaining <= 0 or remaining > self.lease_seconds:
            raise ValueError('파일럿 종료까지 충분한 lease 필요')
        missing = [k for k in self._lease_channels(symbol) if k not in self._operational]
        if len(self._operational)+self.reserved+self.headroom+len(missing) > self.cap:
            return False
        cid = self._static_lease_id = 'pilot:' + symbol
        self._leases[cid] = Lease(symbol, self.clock()+remaining, expires_at.isoformat())
        self._record('desired', (BOOK, symbol), candidate_id=cid, expires_at=expires_at.isoformat())
        return True

    def pilot_snapshot(self):
        """논리 설치와 연결/ACK를 구분한다. 무사건 상태를 정상으로 승격하지 않는다."""
        symbol = getattr(self.observer, 'pilot_symbol', '')
        bundle = self._lease_channels(symbol)
        desired = self._desired()
        installed = (self._static_lease_id in self._leases and self._operational_initialized
                     and all(k in desired for k in bundle))
        requested = sum(self.accepts_frame(k, self.generation) for k in bundle)
        acknowledged = sum(self.accepts_frame(k, self.generation) and self._states[k].acknowledged for k in bundle)
        operational_missing = sum(not self.accepts(k, self.generation) or not self._states[k].acknowledged
                                  for k in self._operational)
        return dict(installed=bool(installed), connected=self._send is not None,
                    stream_ready=bool(installed and acknowledged == 3 and operational_missing == 0),
                    requested_channels=requested, acknowledged_channels=acknowledged,
                    observed_channels=sum(self.accepts_frame(k, self.generation) and self._states[k].observed
                                          for k in bundle),
                    missing_from_operational=sum(k not in self._operational for k in bundle),
                    operational_demand=len(self._operational), operational_missing=operational_missing,
                    desired_count=len(desired), occupied=len(self._states), registration_cap=self.cap,
                    external_reserved=self.reserved, operational_headroom=self.headroom,
                    waiting_for_release=any(s.pending == 'unsubscribe' for s in self._states.values()),
                    generation=self.generation)

    def _record(self, status, key=None, *, candidate_id=None, reason=None, expires_at=None, frame_diagnostic=None):
        record = {"kind": "quote_subscription", "observed_at": (self.observer.current_time().isoformat()
                  if (getattr(self.observer, "order_ready_admission", False) or getattr(self.observer, "llm_admission", False)) else datetime.now(timezone.utc).isoformat()),
                  "status": status, "generation": self.generation,
                  "connection_id": self.connection_id, "candidate_id": candidate_id,
                  "tr_id": key[0] if key else None, "symbol": key[1] if key else None,
                  "reason": reason, "expires_at": expires_at}
        identity = self._ready_requests.get(candidate_id)
        if identity is not None:
            record.update(candidate_id=identity['candidate_id'], signal_id=identity['signal_id'])
            if 'allocation_signal_id' in identity:
                record['allocation_signal_id'] = identity['allocation_signal_id']
            else:
                record['order_id'] = identity['order_id']
        if frame_diagnostic is not None:
            record["frame_diagnostic"] = frame_diagnostic
        # 창 안의 용량 실패는 dropped_records, 창 밖의 기록은 의도적 제외다.
        self.observer.publish(record)

    def _gap(self, reason, *, frame_diagnostic=None):
        self.observer.mark_incomplete(reason)
        self._record("connection_gap", reason=reason, frame_diagnostic=frame_diagnostic)

    def frame_gap(self, reason, **frame):
        """진단 실패도 기존 gap을 한 번 보존한다. 원문/예외는 저장하지 않는다."""
        detail = None
        if self.observer.frame_diagnostics_settings is not None:
            try:
                from src.analytics.kis_frame_diagnostics import classify_frame, validate_diagnostic
                detail = validate_diagnostic(classify_frame(owner=self, **frame), reason=reason)
            except Exception:
                self.observer.mark_incomplete("frame_diagnostic_failed")
        self._gap(reason, frame_diagnostic=detail)

    async def start(self, send):
        async with self._lock:
            if self._send is not None:
                raise RuntimeError("이전 소켓 종료 후에만 새 연결 시작 가능")
            self.generation += 1
            self.connection_id = uuid4().hex
            self._states.clear()
            self._retired.clear()
            self._candidate_status.clear()
            self._last_send = float("-inf")
            self._send = send
            self._ack_reconcile_enabled = True
            self._record("connected")
            await self._reconcile()
            return self.generation

    async def stop(self):
        await self._stop_ack_reconcile()
        async with self._lock:
            if self._send is not None:
                self._gap("socket_closed")
            self._send = None
            self._states.clear()

    async def cancel_pending(self):
        """Shutdown only: drain owned work before stop(); ACKs resume at next start()."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await self._stop_ack_reconcile()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_ack_reconcile(self):
        self._ack_reconcile_enabled = False
        self._ack_reconcile_dirty = False
        task = self._ack_reconcile_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            # Never drain a sender while holding the lock it needs.
            await asyncio.gather(task, return_exceptions=True)
            if self._ack_reconcile_task is task:
                self._ack_reconcile_task = None  # Also covers cancellation before first run.

    def _schedule_ack_reconcile(self):
        if not self._ack_reconcile_enabled or self._send is None:
            return
        self._ack_reconcile_dirty = True
        if self._ack_reconcile_task is None or self._ack_reconcile_task.done():
            self._ack_reconcile_task = asyncio.create_task(
                self._reconcile_after_ack(self.generation))

    async def _reconcile_after_ack(self, generation):
        try:
            while self._ack_reconcile_dirty and self._ack_reconcile_enabled:
                async with self._lock:
                    if generation != self.generation or self._send is None:
                        return
                    self._ack_reconcile_dirty = False
                    await self._reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._gap("ack_reconcile_failed")
        finally:
            if self._ack_reconcile_task is asyncio.current_task():
                self._ack_reconcile_task = None

    def end_observation(self):
        """관측 후보 수요만 종료. 운영 등록·미확인 슬롯은 유지한다."""
        if self._ready_unresolved:
            self.observer.mark_incomplete('order_ready_enrollment_unresolved')
        self._observation_ended = True
        self._leases.clear()

    def submit(self, scan_id, symbols):
        """원래 스크리닝을 기다리게 하지 않는 유한 작업 큐. 후보 분모는 별도 scan에 유지."""
        if self._observation_ended or getattr(self.observer, 'static_pilot', False):
            return
        if len(self._tasks) >= 8:
            self._record("unallocated", reason="enrollment_queue_full")
            self._gap("enrollment_queue_full")
            return
        symbols = list(symbols)
        if len(symbols) > self.max_candidates:
            self._record("unallocated", reason="scan_exceeds_lease_capacity")
        task = asyncio.create_task(self.enroll(scan_id, symbols[:self.max_candidates],
                                              requested_at=self.clock()))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _ready_live(self):
        if self._observation_ended:
            return False
        if self.observer.current_time() >= self.observer.end_at or self.observer.ended:
            self.end_observation()
            return False
        return True

    def submit_llm_check(self, admission):
        """신규 종목의 최초 LLM identity만 등록한다. 반복 신호는 원장 배정에 보존한다."""
        if not getattr(self.observer, 'llm_admission', False):
            return
        self.submit_order_ready(admission)

    def submit_order_ready(self, admission):
        """준비 identity당 유한 예약만 한다. 주문 호출자는 송신/lock을 기다리지 않는다."""
        if getattr(self.observer, 'static_pilot', False):
            return
        if not self._ready_live():
            self.observer.mark_incomplete('order_ready_enrollment_unresolved')
            return
        sid = admission['signal_id']
        if not isinstance(sid, str) or not sid:
            self._gap('order_ready_invalid_identity')
            return
        if sid in self._ready_requests:
            return
        if len(self._ready_requests) >= self.max_candidates:
            self._record('unallocated', reason='order_ready_limit')
            self._gap('order_ready_limit')
            return
        self._ready_requests[sid] = dict(admission)
        if len(self._tasks) >= 8:
            self._record('unallocated', (BOOK, admission['symbol']), candidate_id=sid,
                         reason='enrollment_queue_full')
            self._gap('enrollment_queue_full')
            return
        remaining = (self.observer.end_at - self.observer.current_time()).total_seconds()
        deadline = self.clock() + min(self.lease_seconds, max(0, remaining))
        self._ready_unresolved.add(sid)
        task = asyncio.create_task(self._enroll_order_ready(sid, deadline))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _enroll_order_ready(self, sid, deadline):
        try:
            async with self._lock:
                if not self._ready_live():
                    return
                admission = self._ready_requests[sid]
                symbol = admission['symbol']
                if (deadline <= self.clock() or not isinstance(symbol, str) or len(symbol) != 6
                        or not symbol.isascii() or not symbol.isdigit()):
                    self._record('unallocated', (BOOK, symbol), candidate_id=sid,
                                 reason='invalid_or_full_or_expired')
                    self._gap('order_ready_enrollment_failed')
                    return
                self._leases[sid] = Lease(symbol, deadline, self.observer.end_at.isoformat())
                self._record('desired', (BOOK, symbol), candidate_id=sid,
                             expires_at=self.observer.end_at.isoformat())
                await self._reconcile()
        except (Exception, asyncio.CancelledError):
            self.observer.mark_incomplete('order_ready_enrollment_failed')
            raise
        finally:
            self._ready_unresolved.discard(sid)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._gap("enrollment_failed")

    async def enroll(self, scan_id, symbols, *, requested_at=None):
        async with self._lock:
            if self._observation_ended or getattr(self.observer, 'static_pilot', False):
                return
            now = self.clock()
            start = now if requested_at is None else requested_at
            self._expire()
            for symbol in symbols:
                cid = f"{scan_id}:{symbol}"
                if cid in self._leases:
                    continue  # 동일 후보 재요청으로 TTL을 연장하지 않는다.
                if (not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii()
                        or not symbol.isdigit() or len(self._leases) >= self.max_candidates
                        or start + self.lease_seconds <= now):
                    self._record("unallocated", (BOOK, symbol), candidate_id=cid,
                                 reason="invalid_or_full_or_expired")
                    continue
                expires = (datetime.now(timezone.utc) + timedelta(
                    seconds=max(0, start + self.lease_seconds - now))).isoformat()
                self._leases[cid] = Lease(symbol, start + self.lease_seconds, expires)
                self._record("desired", (BOOK, symbol), candidate_id=cid, expires_at=expires)
            await self._reconcile()

    async def set_operational(self, symbols, price_id, book_id):
        async with self._lock:
            # 전달 순서: 보유 PRICE → 보유 BOOK → 나머지 PRICE/BOOK.
            self._operational = list(dict.fromkeys((tr, s) for s in symbols for tr in (price_id, book_id)))
            self._operational_initialized = True
            await self._reconcile()

    def _expire(self):
        for cid, lease in list(self._leases.items()):
            if lease.deadline <= self.clock():
                if cid == self._static_lease_id and self.observer.current_time() < self.observer.end_at:
                    self.observer.mark_incomplete('pilot_lease_expired_early')
                    self.observer.ended = True
                if cid in self._ready_requests and self.observer.current_time() < self.observer.end_at:
                    self.observer.mark_incomplete('order_ready_lease_expired_early')
                self._record("expired", (BOOK, lease.symbol), candidate_id=cid, expires_at=lease.expires_at)
                del self._leases[cid]
                self._candidate_status.pop(cid, None)

    def _desired(self):
        if getattr(self.observer, 'order_ready_admission', False) or getattr(self.observer, 'llm_admission', False) or getattr(self.observer, 'static_pilot', False):
            self._ready_live()
        if self._observation_ended:
            self._leases.clear()
        desired = list(self._operational)
        spare = max(0, self.cap - self.reserved - len(desired) - self.headroom)
        for lease in self._leases.values():
            if lease.deadline <= self.clock():
                continue
            missing = [key for key in self._lease_channels(lease.symbol) if key not in desired]
            # 논리적 배정은 전체 묶음 단위. 실제 요청/ACK/수신은 별도로 기록한다.
            if len(missing) <= spare:
                desired.extend(missing)
                spare -= len(missing)
        return desired

    def _lease_channels(self, symbol):
        return [(tr, symbol) for tr in (STATE_CHANNELS if self.market_state_evidence else (BOOK,))]

    async def _request(self, action, key):
        if getattr(self.observer, 'order_ready_admission', False) or getattr(self.observer, 'llm_admission', False) or getattr(self.observer, 'static_pilot', False):
            self._ready_live()
        if action == "subscribe" and self._observation_ended and key not in self._operational:
            return
        if action == "subscribe":
            state = Registration(action, self.clock())
            self._states[key] = state  # 송신 전 예약: timeout/취소도 점유 불명이다.
        else:
            state = self._states[key]
            state.pending = action
            state.status = "pending_unsubscribe"
            state.requested_at = self.clock()
            if state.observed and any(v.symbol == key[1] for v in self._leases.values()):
                self._gap("observed_candidate_channel_withdrawn")
        # A new event is required even when unsubscribe reuses the Registration.
        completed = state.send_completed = asyncio.Event()
        try:
            delay = self.interval - (self.clock() - self._last_send)
            if delay > 0:
                await asyncio.sleep(delay)
            if getattr(self.observer, 'order_ready_admission', False) or getattr(self.observer, 'llm_admission', False) or getattr(self.observer, 'static_pilot', False):
                self._ready_live()
            if action == "subscribe" and self._observation_ended and key not in self._operational:
                # 아직 송신하지 않은 로컬 예약만 제거. 이미 송신 중인 슬롯은 불명으로 보존.
                if self._states.get(key) is state:
                    del self._states[key]
                return
            self._last_send = self.clock()
            state.requested_at = self.clock()
            await asyncio.wait_for(self._send(action, key), timeout=self.send_timeout)
            self._record("requested" if action == "subscribe" else "unsubscribe_requested", key)
        except (Exception, asyncio.CancelledError) as exc:
            state.status = "unknown"
            state.pending = None
            self._record("unknown", key, reason="send_uncertain")
            self._gap("send_uncertain")
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            # Preserve requested/unknown-before-ACK journal ordering.
            completed.set()

    async def _reconcile(self):
        self._expire()
        desired = self._desired()
        if (self._static_lease_id in self._leases and
                not all(k in desired for k in self._lease_channels(self.observer.pilot_symbol))):
            self._gap('pilot_channel_capacity')
            self.observer.ended = True
            self.end_observation()
            desired = list(self._operational)
        if self._send is not None:
            for key, state in list(self._states.items()):
                if (key not in desired and state.pending is None and state.acknowledged
                        and state.status not in ("unknown", "rejected")):
                    await self._request("unsubscribe", key)
            for key in desired:
                if len(self._states) + self.reserved >= self.cap:
                    break
                if key not in self._states and key not in self._retired:
                    await self._request("subscribe", key)
        for cid, lease in self._leases.items():
            key = (BOOK, lease.symbol)
            bundle = self._lease_channels(lease.symbol)
            allocated = all(channel in desired for channel in bundle)
            shared = all(channel in self._operational for channel in bundle)
            status = ("shared_operational" if shared else "allocated") if allocated else "unallocated"
            if self._candidate_status.get(cid) != status:
                self._candidate_status[cid] = status
                if status == 'unallocated' and cid in self._ready_requests:
                    self.observer.mark_incomplete('order_ready_channel_capacity')
                self._record(status, key, candidate_id=cid, reason=None if allocated else "channel_capacity")

    async def maintain(self):
        async with self._lock:
            for key, state in self._states.items():
                if state.pending and self.clock() - state.requested_at > self.ack_timeout:
                    state.pending = None
                    state.status = "unknown"
                    self._record("unknown", key, reason="ack_timeout")
                    self._gap("ack_timeout")
            await self._reconcile()

    async def handle_ack(self, message, generation):
        if (generation != self.generation or self._send is None
                or not self._ack_reconcile_enabled):
            return
        header, body = message.get("header", {}), message.get("body", {})
        if not isinstance(header, dict) or not isinstance(body, dict):
            return
        key = (header.get("tr_id"), header.get("tr_key"))
        if not all(isinstance(v, str) for v in key):
            return
        state = self._states.get(key)
        if not state or not state.pending:
            return
        msg = body.get("msg1", "")
        if not isinstance(msg, str):
            return
        action = ("unsubscribe" if msg in self.unsubscribe_success_messages else
                  "subscribe" if msg == "SUBSCRIBE SUCCESS" else None)
        rejected = body.get("rt_cd") == "1"
        if not rejected and (body.get("rt_cd") != "0" or action != state.pending):
            return
        completed, pending, sender = state.send_completed, state.pending, self._send
        if completed is not None:
            await completed.wait()  # Only this send, never unrelated pacing/batch sends.
        # No awaits below: state transitions are atomic on the receive event loop.
        if (generation != self.generation or self._send is not sender
                or not self._ack_reconcile_enabled or self._states.get(key) is not state
                or state.send_completed is not completed or state.pending != pending):
            return
        if rejected:
            # 명시 거절도 기존 등록 부재를 증명하지 않는다. 슬롯은 재연결까지 유지.
            state.status, state.pending = "rejected", None
            self._record("rejected", key)
            self._gap("registration_rejected")
            return
        if action == "unsubscribe":
            del self._states[key]
            self._retired.add(key)
            self._record("released", key)
        else:
            state.pending, state.acknowledged, state.status = None, True, "acknowledged"
            self._record("acknowledged", key)
        self._schedule_ack_reconcile()

    def accepts_frame(self, key, generation):
        """현재 연결에서 실제 요청한 운영/관측 채널. 엔진 가격 전달과 별개다."""
        state = self._states.get(key)
        return bool(self._send is not None and generation == self.generation and state
                    and state.pending != "unsubscribe" and state.status not in ("unknown", "rejected")
                    and key in self._desired())

    def accepts(self, key, generation):
        return self.accepts_frame(key, generation) and (key[0] not in PRICE_IDS or key in self._operational)

    def accepts_market_state(self, key, generation):
        if (not self.market_state_evidence or key[0] not in STATE_CHANNELS[1:]
                or not self.accepts_frame(key, generation)):
            return False
        desired = self._desired()
        return (any(lease.symbol == key[1] and lease.deadline > self.clock() for lease in self._leases.values())
                and all(channel in desired for channel in self._lease_channels(key[1])))

    def note_data(self, key, generation):
        if self.accepts(key, generation):
            state = self._states[key]
            if key in self._operational and key[0] in PRICE_IDS:
                state.operational_observed = True
            if not state.observed:
                state.observed = True
                self._record("observed", key)

    def note_market_state(self, key, generation):
        if self.accepts_market_state(key, generation):
            state = self._states[key]
            if not state.observed:
                state.observed = True
                self._record("observed", key)

    def publish_market_state(self, record):
        """상태가 선언한 몫을 넘겨 뒤의 코호트 기록 공간을 소진하지 않는다."""
        if not self.market_state_evidence:
            return False
        if self._market_state_records >= self.market_state_max_records:
            self.observer.mark_incomplete('market_state_record_limit')
            return False
        from src.analytics.entry_observation import _publish
        recorded = _publish(self.observer, record)
        if recorded:
            self._market_state_records += 1
        return recorded

    def operational_coverage(self):
        return {key[1] for key in self._operational if key[0] in PRICE_IDS
                and self.accepts(key, self.generation)
                and self._states[key].acknowledged and self._states[key].operational_observed}

    def snapshot(self):
        return {"generation": self.generation, "connection_id": self.connection_id,
                "registration_cap": self.cap, "external_reserved": self.reserved,
                "operational_headroom": self.headroom, "evidence_ref": self.evidence_ref,
                "market_state_evidence": self.market_state_evidence,
                "market_state_max_records": self.market_state_max_records,
                "market_state_records": self._market_state_records,
                "operational_observed": sum(s.operational_observed for s in self._states.values()),
                "unsubscribe_evidence_ref": self.unsubscribe_evidence_ref,
                "unsubscribe_ack_enabled": bool(self.unsubscribe_success_messages),
                "operational_demand": len(self._operational), "occupied": len(self._states),
                "capacity_error": len(self._operational) + self.reserved > self.cap,
                "waiting_for_release": any(s.pending == "unsubscribe" for s in self._states.values()),
                "acknowledged": sum(s.acknowledged for s in self._states.values()),
                "observed": sum(s.observed for s in self._states.values()),
                "operational_missing": sum(not self.accepts(k, self.generation) or
                    not self._states[k].acknowledged for k in self._operational),
                "leases": len(self._leases), "retired_channels": len(self._retired)}


def observe_screen_candidates(feed, observer, scan_id, stocks):
    """동일 버퍼가 명시 설치된 feed에만 후보 BOOK 요청. 기본 None은 완전한 no-op."""
    if isinstance(observer, EntryObservationBuffer) and observer.scan_scope == 'window':
        return  # Explicit signal-window runtime submits only recorded signal identities.
    owner = getattr(feed, "_quote_subscription_owner", None)
    if owner is not None and observer is owner.observer and scan_id is not None:
        try:
            owner.submit(scan_id, [stock.symbol for stock in stocks])
        except Exception:
            # 관측 실패로 원래 스크리너 결과/후속 진입 처리가 취소되지 않게 한다.
            if isinstance(observer, EntryObservationBuffer):
                observer.mark_incomplete("enrollment_submit_failed")
