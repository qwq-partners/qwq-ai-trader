"""Private routine-report storage and durable, at-most-one-attempt slots.

This is not Telegram server idempotency. An interrupted attempt remains consumed;
API acceptance is not proof of delivery to the reader. Urgent alerts bypass it.
"""
from contextlib import contextmanager
from datetime import date, datetime
import os
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
KINDS = {"market": {"morning", "midday", "after"}, "engine": {"daily"}}


def default_store_path() -> Path:
    return Path.home() / ".cache/ai_trader/report_delivery/reports.sqlite3"


def validate_slot(audience: str, kind: str, report_date: str) -> None:
    if kind not in KINDS.get(audience, set()):
        raise ValueError("unsupported report audience/kind")
    if date.fromisoformat(report_date).isoformat() != report_date:
        raise ValueError("report_date must be ISO date")


class ReportStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else default_store_path()

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create before sqlite connects so private text never has a public mode.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("CREATE TABLE IF NOT EXISTS reports (report_date TEXT, kind TEXT, text TEXT, recorded_at TEXT, PRIMARY KEY(report_date,kind))")
            db.execute("CREATE TABLE IF NOT EXISTS slots (audience TEXT, kind TEXT, report_date TEXT, source_snapshot_id TEXT, status TEXT, PRIMARY KEY(audience,kind,report_date))")
            with db:
                yield db
        finally:
            db.close()

    def record(self, kind: str, text: str, now: datetime | None = None) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
            raise ValueError("invalid report kind")
        now = now or datetime.now(KST)
        if now.tzinfo is None:
            raise ValueError("record time must be timezone-aware")
        now = now.astimezone(KST)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?)", (now.date().isoformat(), kind, str(text)[:30000], now.isoformat()))
        return True  # Stored, not sent.

    def records(self, report_date: str) -> list[dict]:
        date.fromisoformat(report_date)
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT kind,text,recorded_at FROM reports WHERE report_date=? ORDER BY recorded_at,kind", (report_date,))]

    def claim(self, audience: str, kind: str, report_date: str, source_snapshot_id: str) -> bool:
        validate_slot(audience, kind, report_date)
        if not source_snapshot_id:
            raise ValueError("source_snapshot_id required")
        with self._db() as db:
            return db.execute("INSERT OR IGNORE INTO slots VALUES (?,?,?,?,?)", (audience, kind, report_date, source_snapshot_id, "attempted")).rowcount == 1

    def finish(self, audience: str, kind: str, report_date: str, status: str) -> None:
        validate_slot(audience, kind, report_date)
        if status not in {"accepted", "unknown", "failed_configuration", "skipped_holiday", "skipped_missing_current_market_basis", "skipped_missing_market_basis"}:
            raise ValueError("invalid terminal status")
        with self._db() as db:
            db.execute("UPDATE slots SET status=? WHERE audience=? AND kind=? AND report_date=? AND status='attempted'", (status, audience, kind, report_date))

    def receipt(self, audience: str, kind: str, report_date: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM slots WHERE audience=? AND kind=? AND report_date=?", (audience, kind, report_date)).fetchone()
            return dict(row) if row else None
