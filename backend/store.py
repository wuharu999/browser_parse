from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
TERMINAL = {"completed", "failed", "cancelled"}
EVENT_KINDS = {"progress", "notice", "warning", "error", "analysis", "artifact", "system"}


def now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def clean(value: str, limit: int) -> str:
    value = "".join(c for c in value if c in "\n\t" or ord(c) >= 32).strip()
    value = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)\b(sk[-_][\w-]{8,}|AKIA[0-9A-Z]{12,})\b", "[redacted]", value)
    value = re.sub(r"(?i)\b(password|api[_-]?key|token|secret)\s*([:=])\s*[^\s,;]+", r"\1\2[redacted]", value)
    return value[:limit]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_report(value: str) -> str:
    """Redact report values without consuming JSON quotes or source structure."""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict) and parsed.get("schemaVersion") == "robot-analysis/v1":
            def redact(item):
                if isinstance(item, str): return clean(item, 20000)
                if isinstance(item, list): return [redact(entry) for entry in item]
                if isinstance(item, dict): return {key: redact(entry) for key, entry in item.items()}
                return item
            return json.dumps(redact(parsed), ensure_ascii=False, separators=(",", ":"))[:20000]
    except (ValueError, TypeError):
        pass
    return clean(value, 20000)


class Store:
    # ponytail: a process-wide SQLite lock; move writes behind a DB service only if traffic needs it.
    def __init__(self, db_path: str, upload_dir: str, *, estimate: float = 5, daily_limit: float = 10, max_running: int = 2, max_pending: int = 20, lease_seconds: int = 1800, runtime_seconds: int = 1800):
        if not math.isfinite(estimate) or estimate <= 0 or not math.isfinite(daily_limit) or daily_limit <= 0 or estimate > daily_limit:
            raise ValueError("budget settings must be positive finite numbers")
        if not 1 <= max_running <= 2 or not 1 <= max_pending <= 100 or not 1 <= lease_seconds <= 1800 or not 1 <= runtime_seconds <= 1800:
            raise ValueError("invalid queue, lease, or runtime setting")
        self.path, self.upload_dir = Path(db_path), Path(upload_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.estimate, self.daily_limit = estimate, daily_limit
        self.max_running, self.max_pending = max_running, max_pending
        self.lease_seconds, self.runtime_seconds = lease_seconds, runtime_seconds
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def _schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, description TEXT NOT NULL, language TEXT NOT NULL,
          evidence TEXT, status TEXT NOT NULL, token_hash TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT,
          cancel_requested INTEGER NOT NULL DEFAULT 0, report TEXT, metrics TEXT,
          cost_usd REAL, reservation REAL NOT NULL DEFAULT 0, reservation_day TEXT, upload_reserved INTEGER NOT NULL DEFAULT 0,
          lease_until TEXT, run_deadline TEXT, finished_at TEXT, finished_day TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), name TEXT NOT NULL,
          stored_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
          created_at TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT NOT NULL, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS claims (
          job_id TEXT PRIMARY KEY REFERENCES jobs(id), name TEXT NOT NULL, token_hash TEXT NOT NULL,
          claimed_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
          reviewer_name TEXT NOT NULL, success INTEGER NOT NULL, note TEXT NOT NULL,
          procedure TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id, seq);
        CREATE INDEX IF NOT EXISTS files_job ON files(job_id);
        CREATE INDEX IF NOT EXISTS versions_job ON versions(job_id, id);
        """)
        # Keep local development databases usable after additive schema changes.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, sql in (("run_deadline", "TEXT"), ("finished_day", "TEXT"), ("upload_reserved", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql}")
        self.db.commit()

    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")

    def _event(self, job_id: str, kind: str, agent: str, message: str) -> dict:
        created = stamp()
        cur = self.db.execute("INSERT INTO events(job_id,created_at,kind,agent,message) VALUES(?,?,?,?,?)", (job_id, created, kind, clean(agent, 80) or "system", clean(message, 2000)))
        return {"seq": cur.lastrowid, "created_at": created, "kind": kind, "agent": clean(agent, 80) or "system", "message": clean(message, 2000)}

    def _files(self, job_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT id,name,size,sha256,created_at FROM files WHERE job_id=? ORDER BY rowid", (job_id,))]

    def _public(self, row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        for key in ("token_hash", "evidence", "lease_until", "run_deadline", "reservation_day", "finished_day", "upload_reserved"):
            item.pop(key, None)
        item["cancel_requested"] = bool(item["cancel_requested"])
        claim = self.db.execute("SELECT name,expires_at FROM claims WHERE job_id=? AND expires_at>?", (item["id"], stamp())).fetchone()
        item["review_claim"] = dict(claim) if claim else None
        return item

    def active(self) -> list[dict]:
        with self.lock:
            self._tx(); self._expire_leases(); self.db.commit()
            rows = self.db.execute("SELECT * FROM jobs WHERE status IN ('draft','queued','running') ORDER BY created_at").fetchall()
            items = []
            for row in rows:
                item = self._public(row) or {}
                for field in ("report", "metrics", "files"):
                    item.pop(field, None)
                items.append(item)
            return items

    def _worker(self, row: sqlite3.Row) -> dict:
        item = self._public(row) or {}
        item["files"] = self._files(item["id"])
        item["evidence"] = json.loads(row["evidence"]) if row["evidence"] else None
        return item

    def create(self, description: str, language: str, evidence: object | None) -> tuple[dict, str]:
        description, language = clean(description, 12000), clean(language, 80)
        if not description or not language:
            raise ValueError("description and language are required")
        raw = secrets.token_urlsafe(32)
        job_id, created = secrets.token_hex(16), stamp()
        payload = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) if evidence is not None else None
        with self.lock:
            self._tx()
            pending = self.db.execute("SELECT count(*) n FROM jobs WHERE status IN ('draft','queued')").fetchone()["n"]
            if pending >= self.max_pending:
                self.db.rollback(); raise ValueError("queue is full")
            self.db.execute("INSERT INTO jobs(id,description,language,evidence,status,token_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, description, language, payload, "draft", token_hash(raw), created, created))
            self._event(job_id, "system", "api", "Job created; upload evidence and submit when ready.")
            self.db.commit()
            return self.get(job_id), raw

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            return self._public(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self, limit: int, cursor: int | None) -> tuple[list[dict], str | None]:
        with self.lock:
            query, args = "SELECT rowid AS _cursor,* FROM jobs", []
            if cursor is not None:
                query += " WHERE rowid<?"; args.append(cursor)
            rows = self.db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
            more, rows = len(rows) > limit, rows[:limit]
            items = []
            for row in rows:
                item = self._public(row) or {}
                item.pop("report", None); item.pop("metrics", None); item["file_count"] = len(item.pop("files", []))
                items.append(item)
            return items, str(rows[-1]["_cursor"]) if more and rows else None

    def token_owner(self, job_id: str, token: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT token_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and hmac.compare_digest(row["token_hash"], token_hash(token)))

    def submit(self, job_id: str) -> dict:
        with self.lock:
            self._tx()
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "draft": self.db.rollback(); raise ValueError("only draft jobs can be submitted")
            when = stamp()
            self.db.execute("UPDATE jobs SET status='queued',submitted_at=?,updated_at=? WHERE id=?", (when, when, job_id))
            self._event(job_id, "system", "api", "Job queued for a worker.")
            self.db.commit()
            return self.get(job_id)

    def reserve_upload(self, job_id: str, size: int) -> None:
        with self.lock:
            self._tx()
            row = self.db.execute("SELECT status,upload_reserved FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "draft": self.db.rollback(); raise ValueError("files may only be uploaded before submission")
            total = self.db.execute("SELECT COALESCE(SUM(size),0) n FROM files WHERE job_id=?", (job_id,)).fetchone()["n"]
            if total + row["upload_reserved"] + size > 4 * 1024**3: self.db.rollback(); raise ValueError("job upload limit is 4 GiB")
            self.db.execute("UPDATE jobs SET upload_reserved=upload_reserved+?,updated_at=? WHERE id=?", (size, stamp(), job_id)); self.db.commit()

    def release_upload(self, job_id: str, size: int) -> None:
        with self.lock:
            self._tx(); self.db.execute("UPDATE jobs SET upload_reserved=MAX(0,upload_reserved-?),updated_at=? WHERE id=?", (size, stamp(), job_id)); self.db.commit()

    def add_file(self, job_id: str, name: str, stored_name: str, size: int, sha256: str, reserved: int) -> dict:
        with self.lock:
            self._tx()
            row = self.db.execute("SELECT status,upload_reserved FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "draft": self.db.rollback(); raise ValueError("files may only be uploaded before submission")
            if size > reserved or row["upload_reserved"] < reserved: self.db.rollback(); raise ValueError("upload reservation is invalid")
            artifact_id = secrets.token_hex(16)
            created = stamp()
            self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (artifact_id, job_id, name, stored_name, size, sha256, created))
            self.db.execute("UPDATE jobs SET upload_reserved=upload_reserved-?,updated_at=? WHERE id=?", (reserved, created, job_id))
            self._event(job_id, "system", "api", f"Uploaded evidence file: {name}")
            self.db.commit()
            return {"id": artifact_id, "name": name, "size": size, "sha256": sha256, "created_at": created}

    def events(self, job_id: str, after: int, limit: int = 200, *, latest: bool = False, before: int | None = None) -> tuple[list[dict], int | None]:
        with self.lock:
            if latest or before is not None:
                rows = self.db.execute("SELECT seq,created_at,kind,agent,message FROM events WHERE job_id=? AND seq>? AND seq<? ORDER BY seq DESC LIMIT ?", (job_id, after, before if before is not None else 9223372036854775807, limit)).fetchall()[::-1]
            else:
                rows = self.db.execute("SELECT seq,created_at,kind,agent,message FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, after, limit)).fetchall()
            return [dict(row) for row in rows], rows[-1]["seq"] if rows else None

    def budget(self) -> dict:
        day = datetime.now(SHANGHAI).date().isoformat()
        with self.lock:
            spent = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) n FROM jobs WHERE finished_day=?", (day,)).fetchone()["n"]
            reserved = self.db.execute("SELECT COALESCE(SUM(reservation),0) n FROM jobs WHERE status='running'", ()).fetchone()["n"]
            running = self.db.execute("SELECT count(*) n FROM jobs WHERE status='running'", ()).fetchone()["n"]
        return {"day": day, "timezone": "Asia/Shanghai", "daily_limit_usd": self.daily_limit, "estimated_per_job_usd": self.estimate, "spent_usd": spent, "active_reservations_usd": reserved, "admission_used_usd": spent + reserved, "max_running": self.max_running, "running": running, "max_pending": self.max_pending, "note": "Admission control, not a hard billing ceiling; actual worker cost may overrun its estimate."}

    def _expire_leases(self) -> None:
        expired = self.db.execute("SELECT id,reservation FROM jobs WHERE status='running' AND (lease_until<? OR run_deadline<?)", (stamp(), stamp())).fetchall()
        for row in expired:
            finished = stamp()
            self.db.execute("UPDATE jobs SET status='failed',cancel_requested=1,report=?,cost_usd=?,finished_at=?,finished_day=?,updated_at=?,reservation=0 WHERE id=?", ("Worker lease or maximum runtime expired. The job was not retried automatically.", row["reservation"], finished, datetime.now(SHANGHAI).date().isoformat(), finished, row["id"]))
            self._event(row["id"], "system", "api", "Worker lease or maximum runtime expired; job marked failed without retry.")

    def worker_claim(self) -> tuple[dict | None, dict]:
        with self.lock:
            self._tx(); self._expire_leases()
            budget = self.budget()
            if budget["running"] >= self.max_running or budget["admission_used_usd"] + self.estimate > self.daily_limit:
                self.db.commit(); return None, budget
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY submitted_at,id LIMIT 1").fetchone()
            if not row: self.db.commit(); return None, budget
            until = stamp(now() + timedelta(seconds=self.lease_seconds)); current = stamp(); day = datetime.now(SHANGHAI).date().isoformat()
            deadline = stamp(now() + timedelta(seconds=self.runtime_seconds))
            self.db.execute("UPDATE jobs SET status='running',reservation=?,reservation_day=?,lease_until=?,run_deadline=?,updated_at=? WHERE id=?", (self.estimate, day, until, deadline, current, row["id"]))
            self._event(row["id"], "system", "api", "Worker lease granted.")
            updated = self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            self.db.commit()
            return self._worker(updated), self.budget()

    def worker_job(self, job_id: str) -> dict | None:
        with self.lock:
            self._tx(); self._expire_leases(); self.db.commit()
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._worker(row) if row else None

    def worker_file(self, job_id: str, artifact_id: str) -> tuple[Path, str] | None:
        with self.lock:
            row = self.db.execute("SELECT f.name,f.stored_name FROM files f JOIN jobs j ON j.id=f.job_id WHERE f.job_id=? AND f.id=? AND j.status='running'", (job_id, artifact_id)).fetchone()
            if not row: return None
            path = self.upload_dir / row["stored_name"]
            return (path, row["name"]) if path.is_file() else None

    def worker_event(self, job_id: str, kind: str, agent: str, message: str) -> dict:
        if kind not in EVENT_KINDS - {"system"}: raise ValueError("event kind is not allowed")
        if not clean(message, 2000): raise ValueError("message is required")
        with self.lock:
            self._tx()
            row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "running": self.db.rollback(); raise ValueError("job is not running")
            event = self._event(job_id, kind, agent or "worker", message)
            self.db.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?", (stamp(now() + timedelta(seconds=self.lease_seconds)), stamp(), job_id))
            self.db.commit(); return event

    def finish(self, job_id: str, status: str, report: str, cost: float | None, metrics: object | None) -> dict:
        if status not in TERMINAL: raise ValueError("invalid terminal status")
        if cost is not None and (not math.isfinite(cost) or cost < 0 or cost > 100000): raise ValueError("invalid cost")
        try: metrics_json = json.dumps(metrics or {}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc: raise ValueError("metrics must be finite JSON") from exc
        report = clean_report(report)
        if not report: raise ValueError("report is required")
        with self.lock:
            self._tx(); row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "running": self.db.rollback(); raise ValueError("job is not running")
            if row["cancel_requested"]: status = "cancelled"
            charged = row["reservation"] if cost is None else cost
            finished = stamp(); self.db.execute("UPDATE jobs SET status=?,report=?,metrics=?,cost_usd=?,reservation=0,lease_until=NULL,run_deadline=NULL,finished_at=?,finished_day=?,updated_at=? WHERE id=?", (status, report, metrics_json, charged, finished, datetime.now(SHANGHAI).date().isoformat(), finished, job_id))
            self._event(job_id, "system", "worker", f"Job {status}.")
            self.db.commit(); return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            self._tx(); row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] in TERMINAL or row["cancel_requested"]: self.db.rollback(); raise ValueError("job is already terminal or stopping")
            when = stamp()
            if row["status"] in {"draft", "queued"}:
                self.db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?", (when, when, job_id))
            else:
                # Keep the slot and reservation until worker acknowledgement or the original lease/runtime expiry.
                self.db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (when, job_id))
            self._event(job_id, "system", "api", "Cancellation requested.")
            self.db.commit(); return self.get(job_id)

    def claim_review(self, job_id: str, name: str) -> str:
        name = clean(name, 100)
        if not name: raise ValueError("reviewer name is required")
        raw = secrets.token_urlsafe(32)
        with self.lock:
            self._tx(); row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: self.db.rollback(); raise KeyError(job_id)
            if row["status"] != "completed": self.db.rollback(); raise ValueError("only completed jobs can be reviewed")
            self.db.execute("DELETE FROM claims WHERE job_id=? AND expires_at<?", (job_id, stamp()))
            if self.db.execute("SELECT 1 FROM claims WHERE job_id=?", (job_id,)).fetchone(): self.db.rollback(); raise PermissionError("review is currently claimed")
            created, expires = stamp(), stamp(now() + timedelta(seconds=self.lease_seconds))
            self.db.execute("INSERT INTO claims VALUES(?,?,?,?,?)", (job_id, name, token_hash(raw), created, expires))
            self._event(job_id, "system", "api", "Review claim acquired.")
            self.db.commit(); return raw

    def release_claim(self, job_id: str, raw: str) -> None:
        with self.lock:
            self._tx(); row = self.db.execute("SELECT token_hash FROM claims WHERE job_id=?", (job_id,)).fetchone()
            if not row or not hmac.compare_digest(row["token_hash"], token_hash(raw)): self.db.rollback(); raise PermissionError("claim token is invalid")
            self.db.execute("DELETE FROM claims WHERE job_id=?", (job_id,)); self.db.commit()

    def version(self, job_id: str, raw: str, success: bool, note: str, procedure: str) -> dict:
        note, procedure = clean(note, 5000), clean(procedure, 20000)
        if not note or not procedure: raise ValueError("reason and edited procedure are required")
        with self.lock:
            self._tx(); claim = self.db.execute("SELECT * FROM claims WHERE job_id=?", (job_id,)).fetchone()
            if not claim or claim["expires_at"] < stamp() or not hmac.compare_digest(claim["token_hash"], token_hash(raw)): self.db.rollback(); raise PermissionError("claim token is invalid or expired")
            created = stamp(); cur = self.db.execute("INSERT INTO versions(job_id,reviewer_name,success,note,procedure,created_at) VALUES(?,?,?,?,?,?)", (job_id, claim["name"], int(success), note, procedure, created))
            self.db.execute("DELETE FROM claims WHERE job_id=?", (job_id,)); self._event(job_id, "system", "api", "Immutable review version saved.")
            self.db.commit(); return {"id": cur.lastrowid, "reviewer_name": claim["name"], "success": success, "note": note, "procedure": procedure, "created_at": created}

    def versions(self, job_id: str) -> list[dict]:
        with self.lock:
            return [{**dict(row), "success": bool(row["success"])} for row in self.db.execute("SELECT id,reviewer_name,success,note,procedure,created_at FROM versions WHERE job_id=? ORDER BY id", (job_id,))]
