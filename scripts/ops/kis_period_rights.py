#!/usr/bin/env python3
"""KIS 기간별계좌권리현황(CTRGA011R) 읽기 전용 수집 CLI (66차).

python scripts/ops/kis_period_rights.py --start 2025-01-01 --end 2026-10-01 \
    --out-dir ~/.local/share/qwq-private/kis-account-review/<ts>-rights [--max-pages 50] [--min-interval 1.5]

- 장중(09:00~15:30 KST)에는 실행을 거부한다(운영 봇의 계좌 원장 TR 과 초당 1건 한도를 공유). --allow-market-hours 로만 우회.
- 원본 응답·금액·식별자는 out-dir(비공개) 에만 저장한다. 표준 출력은 창별 상태와 행 수만.
- 주문·설정·운영 상태 변경 없음. 실행 원장을 열지 않는 조회 전용 연결이며 토큰은 봇과 같은 캐시를 읽기만 한다(유효하지 않으면 중단, 새 발급 없음).
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analytics.kis_period_rights import SCHEMA_VERSION, collect_window, month_windows, summarize_rights  # noqa: E402
from src.utils import kis_request_metrics  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _in_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 30)


def _bot_active() -> bool:
    """운영 봇(systemd) 가동 여부. 봇은 장외에도 30초/300초 간격으로 계좌 TR 을 부르고 리미터는 프로세스 내부 상태라
    별도 프로세스와 직렬화되지 않는다(3차 리뷰 P1) — 기본은 가동 중 거부, 명시 우회 시 간격 하한을 올린다."""
    import subprocess
    try:
        out = subprocess.run(["systemctl", "is-active", "qwq-ai-trader"], capture_output=True, text=True, timeout=5)
    except Exception:
        return True            # 확인 불가는 가동 중으로 간주(보수적)
    # 확인된 정지(inactive/failed)만 '가동 아님'. activating/deactivating/reloading·알 수 없는 출력·비정상 종료 코드는
    # 전부 가동 중으로 취급한다 — subprocess.run 은 종료 코드로 예외를 내지 않는다(4차 리뷰 P1)
    return not (out.returncode == 3 and out.stdout.strip() in ("inactive", "failed"))


def _http_attempts():
    """브로커 계측에서 CTRGA011R 의 실제 HTTP 전송 횟수(재시도 포함). 계측 불가면 None."""
    try:
        snap = kis_request_metrics.snapshot()
        if not snap.get("available"):
            return None
        return sum(int(r.get("attempts", 0)) for r in snap.get("http", []) if r.get("tr_id") == "CTRGA011R")
    except Exception:
        return None


class ReadOnlyTokenProvider:
    """봇과 같은 토큰 캐시를 **읽기만** 하는 공급자. 발급 POST·캐시 쓰기를 절대 하지 않는다 (재리뷰 P1).

    브로커의 토큰 복구(`_recover_token` → `invalidate` → `_ensure_token`)가 불려도 캐시를 다시 읽어 봇이 회전시킨
    유효 토큰만 돌려주고, 없으면 None 을 돌려 조회가 오류로 끝나게 한다.
    """

    def __init__(self, manager):
        self._manager = manager

    def _reload(self):
        try:
            self._manager._load_cached_tokens()
        except Exception:
            pass

    @property
    def _access_token(self):
        return self._manager._access_token if self._manager._is_token_valid() else None

    def _is_token_valid(self) -> bool:
        return bool(self._manager._is_token_valid())

    async def get_access_token(self):
        self._reload()
        return self._manager._access_token if self._manager._is_token_valid() else None

    async def get_approval_key(self):
        return None                                   # WebSocket 승인키는 조회에 불필요 — 발급하지 않는다

    def invalidate(self) -> None:
        self._reload()                                # 새 발급 대신 캐시 재읽기만


class BotStartedStop(RuntimeError):
    """수집 도중 운영 봇이 기동(또는 상태 확인 불가)해 중단 — 확보한 페이지는 보존 (5차 리뷰 P1)."""


class MarketHoursStop(RuntimeError):
    """장중 진입(또는 진입 임박)으로 전송을 중단했다 — 운영 봇의 계좌 원장 TR 한도와 충돌하지 않기 위해."""


def _blocked_by_market_hours(now: datetime, margin_seconds: float) -> bool:
    """지금부터 margin 초 안에 장중이 되면 전송하지 않는다(내부 재시도 시간을 덮는 여유)."""
    from datetime import timedelta
    for probe in (now, now + timedelta(seconds=margin_seconds)):
        if _in_market_hours(probe):
            return True
    return False


async def _open_readonly_broker():
    """조회 전용 연결. `connect()` 는 운영 실행 원장(ExecutionHistory) 세션을 열어 봇의 기록을 깨뜨릴 수 있어 쓰지 않는다.
    토큰은 봇과 같은 캐시 파일을 **읽기만** 하며 유효하지 않으면 새로 발급하지 않고 중단한다(새 발급은 봇 토큰을 무효화할 수 있다)."""
    import aiohttp
    from src.execution.broker.kis_kr import KISBroker, KISConfig
    from src.utils.token_manager import KISTokenManager
    tm = KISTokenManager()
    if not tm._is_token_valid():
        raise RuntimeError("캐시된 KIS 토큰이 없거나 만료 — 운영 봇 토큰을 보호하기 위해 새 발급을 하지 않는다. 봇이 갱신한 뒤 다시 실행")
    broker = KISBroker(config=KISConfig.from_env(), token_manager=ReadOnlyTokenProvider(tm))
    broker._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=broker.config.timeout_seconds))
    try:
        if not await broker._ensure_token():
            raise RuntimeError("캐시 토큰 적용 실패")
    except Exception:
        await broker._session.close()
        raise
    return broker


def _monotonic() -> float:
    import time
    return time.monotonic()


def _install_send_guard(broker, guard) -> None:
    """`_api_get` 은 타임아웃·401·5xx 에 최대 3회 전송하며 매 시도 직전 `_rate_limit` 을 부른다 — 거기에 가드를 걸어
    **재시도마다** 리미터 대기 뒤·전송 직전에 장중/봇 상태를 재검사한다. 가드가 던진 예외는 `_api_get` 의
    네트워크 예외 처리에 잡히지 않고 그대로 올라와 창을 `aborted:*` 로 끝낸다 (6차 리뷰 P1)."""
    original = broker._rate_limit

    async def guarded(tr_id: str = ""):
        await original(tr_id)
        await guard()
    broker._rate_limit = guarded


async def _close_readonly_broker(broker) -> None:
    session = getattr(broker, "_session", None)
    if session is not None and not session.closed:
        await session.close()


def _prepare_private_dir(path: Path) -> None:
    """출력 디렉터리는 본인 소유·0700 이어야 한다. 기존 디렉터리의 권한은 바꾸지 않고 거부한다(리뷰 P2)."""
    if path.exists():
        st = path.stat()
        if not path.is_dir() or st.st_uid != os.getuid() or (st.st_mode & 0o077):
            raise RuntimeError(f"출력 디렉터리는 본인 소유·0700 이어야 한다: {path}")
        return
    path.mkdir(parents=True)
    path.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


async def _run(args) -> int:
    out_dir = Path(args.out_dir).expanduser()
    try:
        _prepare_private_dir(out_dir)
        broker = await _open_readonly_broker()
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2

    # 내부 재시도(_api_get 최대 3회 전송 + 대기)까지 덮도록 여유를 타임아웃 기준으로 넓힌다 (3차 리뷰 P2)
    effective_margin = max(float(args.market_margin_seconds), 3.0 * float(broker.config.timeout_seconds) + 15.0)

    last_sent = [None]

    async def before_send():
        # `--min-interval` 을 **모든 전송**(내부 5xx/401/타임아웃 재시도 포함)에 적용한다 — 공용 리미터의 기본 간격
        # (약 1.05초)만으로는 봇과 동시 실행 조건(≥2.1초)이 오류 재시도에서 무너진다 (7차 리뷰 P1)
        if last_sent[0] is not None:
            wait = float(args.min_interval) - (_monotonic() - last_sent[0])
            if wait > 0:
                await asyncio.sleep(wait)
        # 대기 뒤 매 전송 직전 재검사 — 08:59 에 시작한 다월·다페이지 조회가 개장 뒤 계속되지 않게 한다 (재리뷰 P1)
        if not args.allow_market_hours and _blocked_by_market_hours(datetime.now(KST), effective_margin):
            raise MarketHoursStop("장중 진입 임박/진입 — 수집 중단(확보한 페이지는 보존)")
        # main() 의 1회 검사만으로는 수집 도중의 자동 재시작·예약 기동을 못 본다 — 명시 우회가 없으면 매 전송 직전
        # 봇 상태를 다시 보고, 가동/확인 불가면 중단한다(남는 경쟁 창은 systemctl 응답~전송 사이 한 요청) (5차·6차 리뷰 P1)
        if not args.allow_bot_running and await asyncio.to_thread(_bot_active):
            raise BotStartedStop("수집 도중 운영 봇 기동/상태 확인 불가 — 수집 중단(확보한 페이지는 보존)")
        last_sent[0] = _monotonic()
    _install_send_guard(broker, before_send)

    async def fetch(start, end, *, fk100, nk100, tr_cont):
        return await broker.get_period_rights(start, end, inqr_dvsn=args.inqr_dvsn, fk100=fk100, nk100=nk100,
                                              tr_cont=tr_cont)

    async def pause():
        await asyncio.sleep(args.min_interval)

    windows = []
    http_before = _http_attempts()
    try:
        for start, end in month_windows(date.fromisoformat(args.start), date.fromisoformat(args.end)):
            if windows:
                await pause()
            result = await collect_window(fetch, start, end, max_pages=args.max_pages, pause=pause)
            windows.append(result)
            _write_private(out_dir / f"rights-{start}-{end}.json", json.dumps(result, ensure_ascii=False, indent=1))
            print(json.dumps({"window": [start, end], "status": result["status"], "pages": result["pages_fetched"],
                              "rows": len(result["rows"])}, ensure_ascii=False))
            if result["status"].startswith("aborted:"):
                print(json.dumps({"stopped": result["status"], "remaining_windows_not_queried": True}, ensure_ascii=False))
                break
    finally:
        await _close_readonly_broker(broker)
    summary = summarize_rights(windows)
    http_after = _http_attempts()
    summary.update(requested=[args.start, args.end], inqr_dvsn=args.inqr_dvsn, collected_at=datetime.now(KST).isoformat(),
                   pages_fetched=sum(w["pages_fetched"] for w in windows),
                   # 실제 HTTP 전송 횟수(내부 재시도 포함) — 계측 불가면 None, 0 으로 채우지 않는다
                   http_attempts=(http_after - http_before) if http_before is not None and http_after is not None else None)
    _write_private(out_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    manifest = {"schema_version": SCHEMA_VERSION, "collected_at": summary["collected_at"], "files": {}}
    for f in sorted(out_dir.glob("*.json")):
        if f.name != "manifest.json":
            manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    _write_private(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"summary": {k: summary[k] for k in ("windows", "window_statuses", "complete_windows", "rows",
                                                            "unparsed_amount_rows", "pages_fetched", "http_attempts")},
                      "right_types": {t: b["rows"] for t, b in summary["by_right_type"].items()},
                      "out_dir": str(out_dir)}, ensure_ascii=False))
    return 0 if summary["complete_windows"] == summary["windows"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--inqr-dvsn", default="03")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--min-interval", type=float, default=1.5)
    parser.add_argument("--allow-market-hours", action="store_true")
    parser.add_argument("--allow-bot-running", action="store_true",
                        help="운영 봇 가동 중에도 실행. 공유 직렬화가 없어 봇의 계좌 조회(장외 300초 간격)와 충돌할 수 있다 — 간격 하한 2.1초")
    parser.add_argument("--market-margin-seconds", type=float, default=90.0,
                        help="개장까지 이 여유보다 가까우면 전송하지 않는다(내부 재시도 시간 포함)")
    args = parser.parse_args()
    if args.max_pages < 1 or not (args.min_interval >= 1.05):
        print(json.dumps({"error": "--max-pages ≥1, --min-interval ≥1.05(원장 TR 초당 1건)"}))
        return 2
    now = datetime.now(KST)
    if _in_market_hours(now) and not args.allow_market_hours:
        print(json.dumps({"error": f"장중({now.isoformat()}) 실행 거부 — 운영 봇의 계좌 원장 TR 한도와 충돌한다"}, ensure_ascii=False))
        return 2
    if args.allow_bot_running and args.min_interval < 2.1:
        # 우회 옵션은 봇 상태와 무관하게 검증한다 — 정지 중 시작했다가 봇이 기동하면 우회 때문에 재검사 없이 계속되므로 (6차 리뷰 P2)
        print(json.dumps({"error": "--allow-bot-running 에서는 --min-interval ≥ 2.1 (잔고 TR 간격과 동일) 이어야 한다"}))
        return 2
    if _bot_active() and not args.allow_bot_running:
        print(json.dumps({"error": "운영 봇 가동 중 — 봇은 장외에도 계좌 TR 을 주기 호출하고 CLI 와 직렬화되지 않는다. "
                                   "--allow-bot-running 으로만 우회(간격 ≥2.1초, 충돌 시 봇은 1회 재시도)"}, ensure_ascii=False))
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
