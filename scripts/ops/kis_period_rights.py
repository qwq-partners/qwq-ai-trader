#!/usr/bin/env python3
"""KIS 기간별계좌권리현황(CTRGA011R) 읽기 전용 수집 CLI (66차).

python scripts/ops/kis_period_rights.py --start 2025-01-01 --end 2026-10-01 \
    --out-dir ~/.local/share/qwq-private/kis-account-review/<ts>-rights [--max-pages 50] [--min-interval 1.5]

- 장중(09:00~15:30 KST)에는 실행을 거부한다(운영 봇의 계좌 원장 TR 과 초당 1건 한도를 공유). --allow-market-hours 로만 우회.
- 원본 응답·금액·식별자는 out-dir(비공개) 에만 저장한다. 표준 출력은 창별 상태와 행 수만.
- 주문·설정·운영 상태 변경 없음. 토큰은 기존 캐시를 재사용한다.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analytics.kis_period_rights import SCHEMA_VERSION, collect_window, month_windows, summarize_rights  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _in_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 30)


async def _run(args) -> int:
    from src.execution.broker.kis_kr import KISBroker
    broker = KISBroker()
    if not await broker.connect():
        print(json.dumps({"error": "KIS 연결/토큰 실패"}, ensure_ascii=False))
        return 2
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def fetch(start, end, *, fk100, nk100, tr_cont):
        return await broker.get_period_rights(start, end, inqr_dvsn=args.inqr_dvsn, fk100=fk100, nk100=nk100,
                                              tr_cont=tr_cont)

    async def pause():
        await asyncio.sleep(args.min_interval)

    windows = []
    try:
        for start, end in month_windows(date.fromisoformat(args.start), date.fromisoformat(args.end)):
            if windows:
                await pause()
            result = await collect_window(fetch, start, end, max_pages=args.max_pages, pause=pause)
            windows.append(result)
            raw_path = out_dir / f"rights-{start}-{end}.json"
            raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps({"window": [start, end], "status": result["status"], "pages": result["api_calls"],
                              "rows": len(result["rows"])}, ensure_ascii=False))
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass
    summary = summarize_rights(windows)
    summary.update(requested=[args.start, args.end], inqr_dvsn=args.inqr_dvsn, collected_at=datetime.now(KST).isoformat(),
                   api_calls=sum(w["api_calls"] for w in windows))
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"schema_version": SCHEMA_VERSION, "collected_at": summary["collected_at"], "files": {}}
    for f in sorted(out_dir.glob("*.json")):
        if f.name != "manifest.json":
            manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": {k: summary[k] for k in ("windows", "window_statuses", "complete_windows", "rows",
                                                            "unparsed_amount_rows", "api_calls")},
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
    args = parser.parse_args()
    if args.max_pages < 1 or not (args.min_interval >= 1.05):
        print(json.dumps({"error": "--max-pages ≥1, --min-interval ≥1.05(원장 TR 초당 1건)"}))
        return 2
    now = datetime.now(KST)
    if _in_market_hours(now) and not args.allow_market_hours:
        print(json.dumps({"error": f"장중({now.isoformat()}) 실행 거부 — 운영 봇의 계좌 원장 TR 한도와 충돌한다"}, ensure_ascii=False))
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
