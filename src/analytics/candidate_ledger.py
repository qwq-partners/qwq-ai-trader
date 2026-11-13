"""장중 스캔 후보 전수 원장 — V02 (2026-11-13, 측정 전용).

스크리너가 반환한 후보 **전체**를 현금·레짐·점수 게이트보다 앞에서 하루 한 파일(JSONL)에 덧붙인다.
현금이 없거나 매수 중지여도 "그 시각 엔진이 고른 후보와 점수 구성"이 남아, 일봉 사후 수익을 붙여
선정 능력을 잴 수 있다(판독은 오프라인 — scripts/research/candidate_forward_excess.py 계열).

- 경로: ~/.cache/ai_trader/candidate_ledger/candidates_YYYYMMDD.jsonl (0600)
- 행: {"kind":"scan", scan_id, at, session, n, basis_n, candidates:[…]}, {"kind":"gate", scan_id, at, …}, {"kind":"error", at, where, error}
- 매매 경로(스케줄러)는 행을 만들어 크기 제한 대기열에 **넣기만** 한다(파일 I/O·대기 없음). 전용 스레드 하나가
  순서대로 파일에 쓴다. 디스크가 멈추면 대기열이 차고, 그 뒤 행은 버려진다(경고) — 스캔·신호 발행은 기다리지 않는다.
- 매매 경로에서는 로그도 쓰지 않는다(운영 로그 sink 는 동기 파일 I/O 다). 버린 행 수는 세어 두었다가 기록 스레드가
  회복했을 때 한 줄로 경고한다. 매매 경로 쪽 행 구성 오류는 {"kind":"error"} 행으로 원장에 남기고, 기록 스레드의
  파일 I/O 오류(디렉터리·열기·쓰기)는 경고만 남긴다.
- 반환값·예외로 선정·주문 흐름을 바꾸지 않는다. 프로세스 종료 시 대기열에 남은 행은 사라질 수 있다.
- 끄기: CANDIDATE_LEDGER=0 (재시작 필요)
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "candidate_ledger"
SCHEMA = "candidate-ledger-v1"
MAX_REASONS = 8
QUEUE_SIZE = 64           # 스캔당 최대 3행 — 20스캔(약 100분) 분량이 밀리면 그 뒤는 버린다
_NUM_FIELDS = ("score", "price", "change_pct", "volume", "volume_ratio", "rsi", "atr_pct", "per", "pbr")
_BASIS_FIELDS = (("merge_score_sum", "merge"), ("post_merge_adjustment", "adj"), ("occurrence_count", "occ"),
                 ("occurrence_bonus", "bonus"), ("pre_normalization_score", "pre"))

_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_SIZE)
_writer: Optional[threading.Thread] = None
_writer_lock = threading.Lock()
_dropped = 0   # 대기열이 가득 차 버린 행 수 — 기록 스레드가 회복하면 요약 경고 뒤 0 으로(_dropped_lock 아래에서만 바꾼다)
_dropped_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("CANDIDATE_LEDGER", "1") != "0"


def _num(value: Any) -> Optional[float]:
    """유한한 수만 남긴다. bool·NaN·문자열·지나치게 큰 정수는 None(미관측) — 0 으로 바꾸지 않는다. 예외를 올리지 않는다."""
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        return value
    except (OverflowError, TypeError, ValueError):   # 예: float 로 못 바꾸는 큰 정수
        return None


def _candidate(stock: Any) -> dict:
    row = {"symbol": str(stock.symbol), **{k: _num(getattr(stock, k, None)) for k in _NUM_FIELDS}}
    for attr, key in (("has_foreign_buying", "foreign"), ("has_inst_buying", "inst")):
        v = getattr(stock, attr, None)
        row[key] = v if isinstance(v, bool) else None
    reasons = getattr(stock, "reasons", None)
    row["reasons"] = [str(r)[:80] for r in reasons[:MAX_REASONS]] if isinstance(reasons, list) else []
    basis = getattr(stock, "selection_basis", None)
    if isinstance(basis, dict):   # 원천별 가중 점수와 병합 뒤 보정 — 점수 구성요소 판독용
        terms = basis.get("source_terms")
        row["basis"] = {
            "terms": [[t.get("source_id"), _num(t.get("weighted_score"))] for t in terms if isinstance(t, dict)]
            if isinstance(terms, list) else [],
            **{key: _num(basis.get(name)) for name, key in _BASIS_FIELDS},
            "cache": basis.get("cache_fallback") is True,
        }
    return row


def _append(record: dict, now: datetime, ledger_dir: Optional[Path]) -> None:
    base = Path(ledger_dir) if ledger_dir is not None else LEDGER_DIR
    base.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(base / f"candidates_{now:%Y%m%d}.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        start = os.fstat(fd).st_size   # 쓰는 스레드는 하나다 — 실패하면 이 위치로 되돌려 잘린 줄이 다음 행에 붙지 않게 한다
        if start > 0 and os.pread(fd, 1, start - 1) != b"\n":
            # 앞 프로세스가 쓰다가 죽어 마지막 줄이 잘려 있다 — 개행으로 끊어 이번 행이 그 줄에 붙지 않게 한다.
            # 잘린 줄은 지우지 않는다(판독은 JSON 이 아닌 줄을 건너뛴다).
            line = b"\n" + line
            logger.warning("[후보원장] 끝이 잘린 줄을 개행으로 끊고 이어 쓴다(앞 프로세스의 미완료 쓰기)")
        try:
            view = memoryview(line)
            while view:   # 짧은 쓰기는 이어 쓰고, 진전이 없으면 실패로 올린다(잘린 줄을 성공으로 치지 않는다)
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("후보 원장 쓰기 진전 없음")
                view = view[n:]
        except BaseException:
            os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)


def _run_writer() -> None:
    """경고는 전부 이 스레드에서 남긴다 — 매매 경로는 로그 I/O 도 하지 않는다."""
    global _dropped
    while True:
        record, now, ledger_dir = _queue.get()
        try:
            _append(record, now, ledger_dir)
            kind = record.get("kind")
            if kind == "scan" and record["basis_n"] < len(record["candidates"]):   # 점수 구성은 V02 의 핵심 자료다
                logger.warning(f"[후보원장] 점수 구성 누락: {len(record['candidates']) - record['basis_n']}/{len(record['candidates'])} 후보")
            elif kind == "error":
                logger.warning(f"[후보원장] 기록 실패(선정 흐름 유지): {record.get('where')} {record.get('error')}")
            with _dropped_lock:
                n, _dropped = _dropped, 0
            if n:
                logger.warning(f"[후보원장] 기록 대기열이 가득 차 {n}행을 버렸다")
        except Exception as e:   # 기록은 부가 기능 — 다음 행을 계속 쓴다
            logger.warning(f"[후보원장] 쓰기 실패({record.get('kind')}): {type(e).__name__}")
        finally:
            _queue.task_done()


def _submit(record: dict, now: datetime, ledger_dir: Optional[Path]) -> bool:
    """행을 대기열에 넣기만 한다(대기·파일/로그 I/O 없음). 가득 차 있으면 세기만 하고 버린 뒤 False."""
    global _writer, _dropped
    if _writer is None or not _writer.is_alive():
        with _writer_lock:
            if _writer is None or not _writer.is_alive():
                _writer = threading.Thread(target=_run_writer, name="candidate-ledger-writer", daemon=True)
                _writer.start()
    try:
        _queue.put_nowait((record, now, ledger_dir))
        return True
    except queue.Full:
        with _dropped_lock:   # 메모리 연산뿐 — 기록 스레드는 이 잠금을 쥔 채 I/O 를 하지 않는다
            _dropped += 1
        return False


def _note_error(where: str, error: Exception, ledger_dir: Optional[Path]) -> None:
    """매매 경로의 기록 오류를 원장 행으로 넘긴다(경고는 기록 스레드가 남긴다). 여기서도 예외를 올리지 않는다."""
    try:
        at = datetime.now()
        _submit({"kind": "error", "at": at.isoformat(timespec="seconds"), "where": where,
                 "error": type(error).__name__}, at, ledger_dir)
    except Exception:
        pass


def drain(timeout: float = 5.0) -> bool:
    """대기열이 비고 쓰기가 끝날 때까지 기다린다(검사·종료용). 시한 안에 끝나면 True."""
    done = threading.Event()
    threading.Thread(target=lambda: (_queue.join(), done.set()), daemon=True).start()
    return done.wait(timeout)


def record_scan(stocks: Any, session: str, *, now: Optional[datetime] = None,
                ledger_dir: Optional[Path] = None) -> Optional[str]:
    """스캔 1회의 반환 후보 전체를 기록 대기열에 넣고 scan_id 를 돌려준다. 꺼져 있거나 실패하면 None(예외 없음)."""
    if not enabled():
        return None
    try:
        at = now if now is not None else datetime.now()
        items = list(stocks)
        rows = [_candidate(s) for s in items]   # 자르지 않는다 — 반환은 점수 내림차순이라 자르면 하위 후보만 빠진다
        basis_n = sum("basis" in r for r in rows)   # 누락은 행에 남기고, 경고는 기록 스레드가 스캔당 한 줄
        scan_id = uuid4().hex[:16]
        ok = _submit({"kind": "scan", "schema": SCHEMA, "scan_id": scan_id, "at": at.isoformat(timespec="seconds"),
                      "session": str(session), "n": len(items), "basis_n": basis_n, "candidates": rows}, at, ledger_dir)
        return scan_id if ok else None
    except Exception as e:   # 기록은 부가 기능 — 선정·주문 흐름을 막지 않는다
        _note_error("scan", e, ledger_dir)
        return None


def record_gate(scan_id: Optional[str], *, now: Optional[datetime] = None, ledger_dir: Optional[Path] = None,
                **fields: Any) -> bool:
    """그 스캔에 적용된 스캔 단위 게이트 결과(레짐·현금 등)를 기록 대기열에 넣는다. scan_id 가 없으면 아무것도 하지 않는다."""
    if scan_id is None or not enabled():
        return False
    try:
        at = now if now is not None else datetime.now()
        clean = {k: (v if isinstance(v, (bool, str)) or v is None else _num(v)) for k, v in fields.items()}
        return _submit({"kind": "gate", "scan_id": scan_id, "at": at.isoformat(timespec="seconds"), **clean}, at, ledger_dir)
    except Exception as e:
        _note_error("gate", e, ledger_dir)
        return False


def _change(quote: Any) -> Optional[float]:
    return _num(quote.get("change_pct")) if isinstance(quote, dict) else None


def record_regime(scan_id: Optional[str], ok: Any, kospi_quote: Any, kosdaq_quote: Any, **kw: Any) -> bool:
    """약세장 필터 결과와 **관측된** 지수 등락률을 남긴다. 조회 실패는 null 이다 — 정책이 쓰는 0.0 대체값을 적지 않는다."""
    try:
        kospi, kosdaq = _change(kospi_quote), _change(kosdaq_quote)
        return record_gate(scan_id, gate="regime", ok=ok if isinstance(ok, bool) else None,
                           observed=kospi is not None and kosdaq is not None,
                           kospi_chg=kospi, kosdaq_chg=kosdaq, **kw)
    except Exception as e:   # 시세 객체가 이상해도(예: get 이 예외) 자동진입 흐름으로 예외를 올리지 않는다
        _note_error("regime", e, kw.get("ledger_dir"))
        return False
