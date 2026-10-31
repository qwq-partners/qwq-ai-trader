"""Private routine-report storage and durable, at-most-one-attempt slots.

This is not Telegram server idempotency. An interrupted attempt remains consumed;
API acceptance is not proof of delivery to the reader. Urgent alerts bypass it.
"""
from contextlib import contextmanager
import asyncio
from datetime import date, datetime, timedelta
import hashlib
from io import BytesIO
import os
import fcntl
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
KINDS = {"market": {"morning", "midday", "after"}, "engine": {"daily"}, "web": {"morning", "midday", "after"}}
MAX_REPORT_PHOTO_BYTES = 4 * 1024 * 1024


def validate_report_body(body: str, *, source: bool = False) -> None:
    """Bound complete messages, including Telegram's UTF-16 transport limit."""
    if (not isinstance(body, str) or not body.strip()
            or len(body) > (1800 if source else 3000)
            or len(body.splitlines()) > (35 if source else 60)):
        raise ValueError("invalid report body")
    try:
        size = len(body.encode("utf-16-le")) // 2
    except UnicodeError:
        raise ValueError("invalid report encoding") from None
    if size > 4096:
        raise ValueError("report exceeds transport limit")


def encode_public_evidence(bundle: dict) -> tuple[str, str]:
    """Canonical storage encoding of an already allowlisted public DTO."""
    if not isinstance(bundle, dict):
        raise ValueError("invalid public evidence")
    encoded = json.dumps(bundle, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    raw = encoded.encode("utf-8")
    if len(raw) > 48 * 1024:
        raise ValueError("public evidence exceeds input limit")
    return encoded, hashlib.sha256(raw).hexdigest()


async def durable_thread(function, *args, **kwargs):
    """Drain a started worker before cancellation releases an enclosing lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def validate_report_photo(photo: bytes, caption: str) -> None:
    """Reject unsupported media before the slot or transport is attempted."""
    if not isinstance(photo, bytes) or not 0 < len(photo) <= MAX_REPORT_PHOTO_BYTES:
        raise ValueError("report photo must be bounded immutable bytes")
    if not photo.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("report photo must be PNG")
    if not isinstance(caption, str) or not caption.strip() or len(caption.encode("utf-16-le")) // 2 > 900:
        raise ValueError("report caption exceeds plain-text budget")
    # Optional image dependency is never imported by the ordinary text path.
    from PIL import Image
    with Image.open(BytesIO(photo)) as image:
        width, height = image.size
        if (image.format != "PNG" or width < 1 or height < 1
                or width * height > 16_000_000 or width + height > 10000
                or max(width, height) > 20 * min(width, height)):
            raise ValueError("unsupported report image dimensions")
        image.verify()


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
            db.execute("CREATE TABLE IF NOT EXISTS report_events (id INTEGER PRIMARY KEY, report_date TEXT, kind TEXT, text TEXT, recorded_at TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS report_events_day ON report_events(report_date)")
            db.execute("CREATE TABLE IF NOT EXISTS slots (audience TEXT, kind TEXT, report_date TEXT, source_snapshot_id TEXT, status TEXT, PRIMARY KEY(audience,kind,report_date))")
            db.execute("CREATE TABLE IF NOT EXISTS payloads (audience TEXT, kind TEXT, report_date TEXT, body TEXT, PRIMARY KEY(audience,kind,report_date))")
            db.execute("CREATE TABLE IF NOT EXISTS media_payloads (audience TEXT, kind TEXT, report_date TEXT, media_type TEXT, caption TEXT, image_sha256 TEXT, png BLOB, PRIMARY KEY(audience,kind,report_date))")
            db.execute("CREATE TABLE IF NOT EXISTS market_inputs (report_date TEXT PRIMARY KEY, owner TEXT, status TEXT, rows_json TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS report_analyses (audience TEXT, kind TEXT, report_date TEXT, owner TEXT NOT NULL, source_snapshot_id TEXT NOT NULL, source_body TEXT NOT NULL, source_sha256 TEXT NOT NULL, prompt_version TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, final_body TEXT, metadata_json TEXT, PRIMARY KEY(audience,kind,report_date))")
            db.execute("CREATE TABLE IF NOT EXISTS report_analysis_sources (audience TEXT, kind TEXT, report_date TEXT, evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, PRIMARY KEY(audience,kind,report_date))")
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
        # Fixed 21:00 cutoff: later cron output belongs to tomorrow's digest.
        report_day = (now + timedelta(hours=3)).date().isoformat()
        with self._db() as db:
            db.execute("INSERT INTO report_events (report_date,kind,text,recorded_at) VALUES (?,?,?,?)", (report_day, kind, str(text)[:30000], now.isoformat()))
        return True  # Stored, not sent.

    def records(self, report_date: str) -> list[dict]:
        date.fromisoformat(report_date)
        with self._db() as db:
            # Preserve legacy latest-only records too; all new inputs are append-only.
            return [dict(r) for r in db.execute("SELECT kind,text,recorded_at FROM (SELECT kind,text,recorded_at,0 AS sequence FROM reports WHERE report_date=? UNION ALL SELECT kind,text,recorded_at,id AS sequence FROM report_events WHERE report_date=?) ORDER BY recorded_at,sequence", (report_date, report_date))]

    def delivery_issues(self, report_date: str) -> list[dict]:
        """Recent non-success receipts, without exposing stored private bodies."""
        previous = (date.fromisoformat(report_date)-timedelta(days=1)).isoformat()
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT audience,kind,report_date,status FROM slots WHERE report_date BETWEEN ? AND ? AND status NOT IN ('accepted','skipped_holiday') ORDER BY CASE WHEN status LIKE 'skipped_%' THEN 1 ELSE 0 END,report_date DESC,kind", (previous, report_date))]

    def claim(self, audience: str, kind: str, report_date: str, source_snapshot_id: str, text: str | None = None, *, photo: bytes | None = None, caption: str | None = None) -> bool:
        validate_slot(audience, kind, report_date)
        if audience == "web":
            raise ValueError("web rows are analyses, not deliveries")
        if not source_snapshot_id:
            raise ValueError("source_snapshot_id required")
        if photo is not None:
            if audience != "market" or text is None:
                raise ValueError("report photos require a market report body")
            validate_report_photo(photo, caption)
        elif caption is not None:
            raise ValueError("caption requires report photo")
        with self._db() as db:
            claimed = db.execute("INSERT OR IGNORE INTO slots VALUES (?,?,?,?,?)", (audience, kind, report_date, source_snapshot_id, "attempted")).rowcount == 1
            if claimed and text is not None:
                db.execute("INSERT INTO payloads VALUES (?,?,?,?)", (audience, kind, report_date, text))
            if claimed and photo is not None:
                db.execute("INSERT INTO media_payloads VALUES (?,?,?,?,?,?,?)", (audience, kind, report_date, "image/png", caption, hashlib.sha256(photo).hexdigest(), photo))
            return claimed

    def finish(self, audience: str, kind: str, report_date: str, status: str) -> None:
        validate_slot(audience, kind, report_date)
        if audience == "web":
            raise ValueError("web rows are analyses, not deliveries")
        if status not in {"accepted", "rejected", "unknown", "failed_validation", "failed_configuration", "skipped_holiday", "skipped_missing_current_market_basis", "skipped_missing_market_basis"}:
            raise ValueError("invalid terminal status")
        with self._db() as db:
            db.execute("UPDATE slots SET status=? WHERE audience=? AND kind=? AND report_date=? AND status='attempted'", (status, audience, kind, report_date))

    def receipt(self, audience: str, kind: str, report_date: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT slots.*,payloads.body,COALESCE(media_payloads.media_type,'text/html') AS media_type,media_payloads.caption,media_payloads.image_sha256 FROM slots LEFT JOIN payloads USING(audience,kind,report_date) LEFT JOIN media_payloads USING(audience,kind,report_date) WHERE audience=? AND kind=? AND report_date=?", (audience, kind, report_date)).fetchone()
            return dict(row) if row else None

    def market_days(self, limit: int = 120) -> list[dict]:
        """Newest-first market slot statuses per day for the public archive page."""
        with self._db() as db:
            rows = db.execute("SELECT report_date,kind,status FROM slots WHERE audience='market' ORDER BY report_date DESC", ).fetchall()
        days: dict[str, dict] = {}
        for row in rows:
            entry = days.setdefault(row["report_date"], {"report_date": row["report_date"], "statuses": {}})
            entry["statuses"][row["kind"]] = row["status"]
        return list(days.values())[:limit]

    def archived_photo(self, audience: str, kind: str, report_date: str) -> bytes | None:
        """Explicitly load the immutable image; ordinary receipts omit the blob."""
        validate_slot(audience, kind, report_date)
        with self._db() as db:
            row = db.execute("SELECT png FROM media_payloads WHERE audience=? AND kind=? AND report_date=?", (audience, kind, report_date)).fetchone()
            return bytes(row["png"]) if row else None

    @contextmanager
    def input_guard(self, report_date: str):
        """Compatibility entrypoint: the after-slot guard keeps its original inode."""
        with self.slot_guard("market", "after", report_date) as acquired:
            yield acquired

    @contextmanager
    def slot_guard(self, audience: str, kind: str, report_date: str):
        """Nonblocking cross-process ownership; never unlink an inode with waiters."""
        validate_slot(audience, kind, report_date)
        if audience != "market":
            raise ValueError("analysis guard is market-only")
        canonical = self.path.resolve()
        canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Keep the established after lock name for old/new process contention.
        lock = canonical.with_name(f"{canonical.name}.{kind}-{report_date}.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def claim_analysis(self, audience: str, kind: str, report_date: str,
                       source_snapshot_id: str, source_body: str,
                       prompt_version: str, *, owner: str, public_evidence: dict | None = None) -> bool:
        """Spend the logical analysis once, durably, before any manager call."""
        validate_slot(audience, kind, report_date)
        if audience not in ("market", "web"):
            raise ValueError("analysis is market/web-only")
        if any(not isinstance(value, str) or not value or len(value) > 128
               for value in (source_snapshot_id, prompt_version, owner)):
            raise ValueError("invalid analysis identity")
        if not isinstance(source_body, str) or not source_body.strip() or len(source_body) > 6000:
            raise ValueError("invalid analysis source")
        digest = hashlib.sha256(source_body.encode("utf-8")).hexdigest()
        evidence = encode_public_evidence(public_evidence) if public_evidence is not None else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM slots WHERE audience=? AND kind=? AND report_date=?", (audience, kind, report_date)).fetchone():
                return False
            claimed = db.execute("INSERT OR IGNORE INTO report_analyses (audience,kind,report_date,owner,source_snapshot_id,source_body,source_sha256,prompt_version,status) VALUES (?,?,?,?,?,?,?,?, 'attempted')",
                                 (audience, kind, report_date, owner, source_snapshot_id, source_body, digest, prompt_version)).rowcount == 1
            if claimed and evidence is not None:
                db.execute("INSERT INTO report_analysis_sources VALUES (?,?,?,?,?)", (audience, kind, report_date, *evidence))
            return claimed

    def complete_analysis(self, audience: str, kind: str, report_date: str, *,
                          owner: str, status: str, final_body: str,
                          result: dict | None = None, metadata: dict | None = None) -> bool:
        """Atomically freeze validated content and safe metadata; never overwrite."""
        validate_slot(audience, kind, report_date)
        if audience not in ("market", "web") or status not in {"ready", "failed", "unknown", "abandoned"}:
            raise ValueError("invalid analysis outcome")
        validate_report_body(final_body)
        # Web rows keep their observed facts in result_json even when the model step failed.
        if (status == "ready" and not isinstance(result, dict)) or (status != "ready" and result is not None and audience != "web"):
            raise ValueError("invalid analysis result")
        result_json = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False)
        result_limit = 65536 if audience == "web" else 16384
        if (result_json is not None and len(result_json.encode()) > result_limit) or len(metadata_json.encode()) > 16384:
            raise ValueError("analysis record too large")
        with self._db() as db:
            return db.execute("UPDATE report_analyses SET status=?,result_json=?,final_body=?,metadata_json=? WHERE audience=? AND kind=? AND report_date=? AND owner=? AND status='attempted'",
                              (status, result_json, final_body, metadata_json, audience, kind, report_date, owner)).rowcount == 1

    def analysis_receipt(self, audience: str, kind: str, report_date: str) -> dict | None:
        validate_slot(audience, kind, report_date)
        with self._db() as db:
            row = db.execute("SELECT report_analyses.*, report_analysis_sources.evidence_json, report_analysis_sources.evidence_sha256 FROM report_analyses LEFT JOIN report_analysis_sources USING(audience,kind,report_date) WHERE audience=? AND kind=? AND report_date=?", (audience, kind, report_date)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json") or "null")
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["public_evidence"] = json.loads(result.pop("evidence_json") or "null")
        return result

    def record_validation_failure(self, audience: str, kind: str, report_date: str,
                                  source_snapshot_id: str, now: datetime) -> bool:
        """Consume invalid delivery and append its quality event atomically once."""
        validate_slot(audience, kind, report_date)
        if audience != "market" or not source_snapshot_id or now.tzinfo is None:
            raise ValueError("invalid report validation failure")
        message = f"시장 {kind} 보고 보류: 보고 입력 또는 저장된 분석의 형식·분량 검증 실패."
        with self._db() as db:
            claimed = db.execute("INSERT OR IGNORE INTO slots VALUES (?,?,?,?,?)",
                                 (audience, kind, report_date, source_snapshot_id, "failed_validation")).rowcount == 1
            if claimed:
                db.execute("INSERT INTO report_events (report_date,kind,text,recorded_at) VALUES (?,?,?,?)",
                           (report_date, "report_quality", message, now.astimezone(KST).isoformat()))
            return claimed

    def claim_inputs(self, report_date: str, owner: str) -> bool:
        """Spend the entire day's report GET allowance before network activity."""
        validate_slot("market", "after", report_date)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM slots WHERE audience='market' AND kind='after' AND report_date=?", (report_date,)).fetchone():
                return False
            return db.execute("INSERT OR IGNORE INTO market_inputs VALUES (?,?,'collecting',NULL)", (report_date, owner)).rowcount == 1

    def complete_inputs(self, report_date: str, owner: str, rows) -> None:
        validate_slot("market", "after", report_date)
        if not isinstance(rows, (list, tuple)) or len(rows) > 2:
            raise ValueError("report observations must contain at most two indices")
        fields = {"code", "label", "price", "source_observed_at", "received_at", "source", "observation_kind", "finality"}
        if any(not isinstance(row, dict) or set(row) != fields for row in rows):
            raise ValueError("unsupported observation fields")
        data = json.dumps(rows, allow_nan=False, ensure_ascii=False)
        if len(data) > 4096:
            raise ValueError("oversize report observations")
        with self._db() as db:
            db.execute("UPDATE market_inputs SET status='completed',rows_json=? WHERE report_date=? AND owner=? AND status='collecting'", (data, report_date, owner))

    def load_inputs(self, report_date: str) -> tuple:
        """Only completed inputs are reusable. Abandoned collecting is never reset."""
        validate_slot("market", "after", report_date)
        with self._db() as db:
            row = db.execute("SELECT status,rows_json FROM market_inputs WHERE report_date=?", (report_date,)).fetchone()
        return tuple(json.loads(row["rows_json"])) if row and row["status"] == "completed" else ()
