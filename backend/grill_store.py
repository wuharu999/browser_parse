"""SQLite storage layer for Grill Bot sessions, turns, files, and worker tasks.

Provides private sessions via unguessable token hashes, turn history,
question budget tracking (hard stop at 30), and worker task dispatching.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.resources import PROFILES

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_GRILL_DB = "data/grill.sqlite3"
DEFAULT_GRILL_UPLOADS = "data/grill_uploads"
MAX_QUESTION_BUDGET = 30
SMALL_PROFILE = {
    "profile": "small",
    **PROFILES["small"],
}


def stamp(dt: datetime | None = None) -> str:
    current = dt or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GrillStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        upload_dir: str | Path | None = None,
        lease_seconds: int = 180,
    ) -> None:
        raw_db = str(db_path or os.getenv("GRILL_DB") or DEFAULT_GRILL_DB)
        self.is_memory = raw_db == ":memory:"
        if not self.is_memory:
            self.db_path = Path(raw_db).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.upload_dir = Path(upload_dir or os.getenv("GRILL_UPLOADS") or DEFAULT_GRILL_UPLOADS).resolve()
            self.upload_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.db_path = Path(":memory:")
            self.upload_dir = Path(upload_dir or "/tmp/grill_uploads").resolve()
            self.upload_dir.mkdir(parents=True, exist_ok=True)

        self.lease_seconds = lease_seconds
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self.db.row_factory = sqlite3.Row
        if not self.is_memory:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS grill_sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'intake_pending',
                    question_count INTEGER NOT NULL DEFAULT 0,
                    current_revision INTEGER NOT NULL DEFAULT 1,
                    task_intent TEXT,
                    referenced_robot TEXT,
                    scenario_state TEXT,
                    active_questions TEXT,
                    readback_summary TEXT,
                    final_report TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS grill_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    questions_json TEXT NOT NULL,
                    answers_json TEXT,
                    normalized_updates_json TEXT,
                    state_snapshot_json TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES grill_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS grill_files (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES grill_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS grill_tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    worker_id TEXT,
                    lease_until TEXT,
                    run_deadline TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES grill_sessions(id) ON DELETE CASCADE
                );
            """)
            try:
                self.db.execute("ALTER TABLE grill_sessions ADD COLUMN error_message TEXT")
            except sqlite3.OperationalError:
                pass

    def create_session(
        self,
        task_intent: str,
        referenced_robot: str | None = None,
        initial_state: dict | None = None,
        defer_turn: bool = False,
    ) -> tuple[dict, str]:
        raw_token = secrets.token_urlsafe(32)
        thash = token_hash(raw_token)
        session_id = f"grill_{secrets.token_hex(12)}"
        now_stamp = stamp()

        with self.lock:
            self.db.execute(
                """
                INSERT INTO grill_sessions (
                    id, token_hash, status, question_count, current_revision,
                    task_intent, referenced_robot, scenario_state, active_questions,
                    readback_summary, final_report, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    thash,
                    "intake_pending",
                    0,
                    1,
                    task_intent,
                    referenced_robot,
                    json.dumps(initial_state or {}) if initial_state is not None else None,
                    json.dumps([]),
                    None,
                    None,
                    now_stamp,
                    now_stamp,
                ),
            )

            if not defer_turn:
                # Queue Turn 1 task for the worker
                task_id = f"gtask_{secrets.token_hex(12)}"
                self.db.execute(
                    """
                    INSERT INTO grill_tasks (
                        id, session_id, action, turn_index, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, session_id, "turn", 1, "queued", now_stamp, now_stamp),
                )
            self.db.commit()

        session = self.get_session(session_id)
        assert session is not None
        return session, raw_token

    def enqueue_turn(self, session_id: str, turn_index: int = 1) -> str:
        now_stamp = stamp()
        with self.lock:
            existing = self.db.execute(
                "SELECT id FROM grill_tasks WHERE session_id = ? AND turn_index = ? AND status IN ('queued', 'running', 'leased')",
                (session_id, turn_index),
            ).fetchone()
            if existing:
                return existing["id"]
            task_id = f"gtask_{secrets.token_hex(12)}"
            self.db.execute(
                """
                INSERT INTO grill_tasks (
                    id, session_id, action, turn_index, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, "turn", turn_index, "queued", now_stamp, now_stamp),
            )
            self.db.commit()
            return task_id

    def verify_token(self, session_id: str, raw_token: str) -> bool:
        if not raw_token or not session_id:
            return False
        with self.lock:
            row = self.db.execute(
                "SELECT token_hash FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return False
            expected = row["token_hash"]
            provided = token_hash(raw_token)
            return hmac.compare_digest(expected, provided)

    def get_session(self, session_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            res.pop("token_hash", None)
            res["scenario_state"] = json.loads(res["scenario_state"]) if res.get("scenario_state") else None
            res["active_questions"] = json.loads(res["active_questions"]) if res.get("active_questions") else []
            res["final_report"] = json.loads(res["final_report"]) if res.get("final_report") else None
            res["files"] = self.get_files(session_id)
            return res

    def get_session_by_token(self, raw_token: str) -> dict | None:
        if not raw_token:
            return None
        th = token_hash(raw_token)
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM grill_sessions WHERE token_hash = ?",
                (th,),
            ).fetchone()
            if not row:
                return None
            return self.get_session(row["id"])

    def get_files(self, session_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, session_id, name, size, sha256, mime_type, created_at FROM grill_files WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_file(
        self,
        session_id: str,
        name: str,
        stored_name: str,
        size: int,
        sha256: str,
        mime_type: str,
    ) -> dict:
        file_id = f"gfile_{secrets.token_hex(10)}"
        now_stamp = stamp()
        with self.lock:
            self.db.execute(
                """
                INSERT INTO grill_files (id, session_id, name, stored_name, size, sha256, mime_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, session_id, name, stored_name, size, sha256, mime_type, now_stamp),
            )
            self.db.commit()
            return {
                "id": file_id,
                "session_id": session_id,
                "name": name,
                "size": size,
                "sha256": sha256,
                "mime_type": mime_type,
                "created_at": now_stamp,
            }

    def get_file_path(self, session_id: str, file_id: str) -> tuple[Path, str] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT name, stored_name FROM grill_files WHERE session_id = ? AND id = ?",
                (session_id, file_id),
            ).fetchone()
            if not row:
                return None
            path = self.upload_dir / row["stored_name"]
            return (path, row["name"]) if path.is_file() else None

    def get_file_by_id(self, file_id: str) -> tuple[Path, str] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT name, stored_name FROM grill_files WHERE id = ?",
                (file_id,),
            ).fetchone()
            if not row:
                return None
            path = self.upload_dir / row["stored_name"]
            return (path, row["name"]) if path.is_file() else None

    def get_turns(self, session_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM grill_turns WHERE session_id = ? ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()
            turns = []
            for r in rows:
                item = dict(r)
                item["questions"] = json.loads(item["questions_json"]) if item.get("questions_json") else []
                item["answers"] = json.loads(item["answers_json"]) if item.get("answers_json") else []
                item["normalized_updates"] = json.loads(item["normalized_updates_json"]) if item.get("normalized_updates_json") else None
                item["state_snapshot"] = json.loads(item["state_snapshot_json"]) if item.get("state_snapshot_json") else None
                turns.append(item)
            return turns

    def submit_answers(
        self,
        session_id: str,
        raw_token: str,
        answers: list[dict],
        normalized_updates: dict | None = None,
    ) -> dict:
        if not self.verify_token(session_id, raw_token):
            raise PermissionError("Invalid session token")

        now_stamp = stamp()
        with self.lock:
            row = self.db.execute(
                "SELECT status, question_count, active_questions FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise ValueError("Session not found")
            if row["status"] not in ("interviewing", "ready_for_confirmation"):
                raise ValueError(f"Session is in status '{row['status']}', cannot submit answers")

            active_q = json.loads(row["active_questions"]) if row["active_questions"] else []
            new_count = row["question_count"] + len(active_q)

            # Update latest turn with answers
            latest_turn = self.db.execute(
                "SELECT id, turn_index FROM grill_turns WHERE session_id = ? ORDER BY turn_index DESC LIMIT 1",
                (session_id,),
            ).fetchone()

            if latest_turn:
                self.db.execute(
                    """
                    UPDATE grill_turns
                    SET answers_json = ?, normalized_updates_json = ?, answered_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(answers, ensure_ascii=False),
                        json.dumps(normalized_updates or {}, ensure_ascii=False),
                        now_stamp,
                        latest_turn["id"],
                    ),
                )
                next_turn_index = latest_turn["turn_index"] + 1
            else:
                next_turn_index = 1

            # Check question limit
            if new_count >= MAX_QUESTION_BUDGET:
                new_status = "ready_for_confirmation"
            else:
                new_status = "analyzing"

            self.db.execute(
                """
                UPDATE grill_sessions
                SET question_count = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_count, new_status, now_stamp, session_id),
            )

            # Queue next worker turn task
            task_id = f"gtask_{secrets.token_hex(12)}"
            self.db.execute(
                """
                INSERT INTO grill_tasks (
                    id, session_id, action, turn_index, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, "turn", next_turn_index, "queued", now_stamp, now_stamp),
            )
            self.db.commit()

        return self.get_session(session_id) or {}

    def confirm_scenario(
        self,
        session_id: str,
        raw_token: str,
        confirmation_note: str | None = None,
    ) -> dict:
        if not self.verify_token(session_id, raw_token):
            raise PermissionError("Invalid session token")

        now_stamp = stamp()
        with self.lock:
            row = self.db.execute(
                "SELECT status, readback_summary, scenario_state FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise ValueError("Session not found")
            if row["status"] not in ("ready_for_confirmation", "interviewing"):
                raise ValueError(f"Session cannot be confirmed in status '{row['status']}'")

            self.db.execute(
                """
                UPDATE grill_sessions
                SET status = 'analyzing', updated_at = ?
                WHERE id = ?
                """,
                (now_stamp, session_id),
            )

            # Queue specialist report synthesis task
            task_id = f"gtask_{secrets.token_hex(12)}"
            self.db.execute(
                """
                INSERT INTO grill_tasks (
                    id, session_id, action, turn_index, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, "report", 0, "queued", now_stamp, now_stamp),
            )
            self.db.commit()

        return self.get_session(session_id) or {}

    def _expire_leases(self) -> None:
        now_iso = stamp()
        self.db.execute(
            """
            UPDATE grill_tasks
            SET status = 'queued', worker_id = NULL, lease_until = NULL, updated_at = ?
            WHERE status = 'running' AND lease_until < ?
            """,
            (now_iso, now_iso),
        )

    def worker_claim_grill(self, worker_id: str, capacity: dict) -> dict | None:
        # Grill tasks require small container profile
        if capacity.get("cpu_milli", 0) < SMALL_PROFILE["cpu_milli"] or \
           capacity.get("memory_mb", 0) < SMALL_PROFILE["memory_mb"]:
            return None

        with self.lock:
            self._expire_leases()

            # Find next queued task
            task = self.db.execute(
                """
                SELECT * FROM grill_tasks
                WHERE status = 'queued'
                ORDER BY created_at ASC LIMIT 1
                """
            ).fetchone()
            if not task:
                self.db.commit()
                return None

            session = self.db.execute(
                "SELECT * FROM grill_sessions WHERE id = ?",
                (task["session_id"],),
            ).fetchone()
            if not session:
                self.db.execute("UPDATE grill_tasks SET status = 'failed', error_message = 'Session missing' WHERE id = ?", (task["id"],))
                self.db.commit()
                return None

            now_dt = datetime.now(timezone.utc)
            lease_until = stamp(now_dt + timedelta(seconds=self.lease_seconds))
            deadline = stamp(now_dt + timedelta(seconds=300))
            now_iso = stamp(now_dt)

            self.db.execute(
                """
                UPDATE grill_tasks
                SET status = 'running', worker_id = ?, lease_until = ?, run_deadline = ?, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_until, deadline, now_iso, task["id"]),
            )

            # Gather session files and last turn answers
            files = self.get_files(task["session_id"])
            last_turn = self.db.execute(
                "SELECT answers_json, normalized_updates_json FROM grill_turns WHERE session_id = ? ORDER BY turn_index DESC LIMIT 1",
                (task["session_id"],),
            ).fetchone()

            customer_answers = json.loads(last_turn["answers_json"]) if last_turn and last_turn["answers_json"] else []
            normalized_updates = json.loads(last_turn["normalized_updates_json"]) if last_turn and last_turn["normalized_updates_json"] else {}

            self.db.commit()

            return {
                "id": task["id"],
                "job_type": "grill",
                "session_id": task["session_id"],
                "action": task["action"],
                "turn_index": task["turn_index"],
                "resource_plan": SMALL_PROFILE.copy(),
                "files": files,
                "task_intent": session["task_intent"],
                "referenced_robot": session["referenced_robot"],
                "scenario_state": json.loads(session["scenario_state"]) if session["scenario_state"] else None,
                "active_questions": json.loads(session["active_questions"]) if session["active_questions"] else [],
                "customer_answers": customer_answers,
                "normalized_updates": normalized_updates,
                "question_count": session["question_count"],
                "timeout_seconds": 300,
            }

    def worker_finish_grill(
        self,
        task_id: str,
        worker_id: str,
        status: str,
        output: dict,
        error_message: str | None = None,
    ) -> None:
        now_stamp = stamp()
        with self.lock:
            task = self.db.execute(
                "SELECT * FROM grill_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not task:
                return

            self.db.execute(
                """
                UPDATE grill_tasks
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error_message, now_stamp, task_id),
            )

            session_id = task["session_id"]
            if status != "completed":
                err = error_message or (output.get("report") if isinstance(output, dict) else str(output))
                self.db.execute(
                    """
                    UPDATE grill_sessions
                    SET status = 'failed', error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (err, now_stamp, session_id),
                )
                self.db.commit()
                return

            if task["action"] == "turn":
                # Output from turn runner contains updated scenario_state, questions, and ready_for_readback
                new_state = output.get("scenario_state") or {}
                new_questions = output.get("questions") or []
                ready_for_readback = bool(output.get("ready_for_readback", False))
                readback_summary = output.get("summary") or (new_state.get("summary") if isinstance(new_state, dict) else None)

                # Record the new turn questions in grill_turns
                turn_id = f"gturn_{secrets.token_hex(10)}"
                self.db.execute(
                    """
                    INSERT INTO grill_turns (
                        id, session_id, turn_index, questions_json, state_snapshot_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        session_id,
                        task["turn_index"],
                        json.dumps(new_questions, ensure_ascii=False),
                        json.dumps(new_state, ensure_ascii=False),
                        now_stamp,
                    ),
                )

                # Check if ready for customer readback or question budget reached
                sess_row = self.db.execute("SELECT question_count FROM grill_sessions WHERE id = ?", (session_id,)).fetchone()
                curr_q_count = sess_row["question_count"] if sess_row else 0

                if ready_for_readback or curr_q_count >= MAX_QUESTION_BUDGET:
                    sess_status = "ready_for_confirmation"
                else:
                    sess_status = "interviewing"

                self.db.execute(
                    """
                    UPDATE grill_sessions
                    SET status = ?, scenario_state = ?, active_questions = ?,
                        readback_summary = COALESCE(?, readback_summary),
                        current_revision = current_revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        sess_status,
                        json.dumps(new_state, ensure_ascii=False),
                        json.dumps(new_questions, ensure_ascii=False),
                        readback_summary,
                        now_stamp,
                        session_id,
                    ),
                )

            elif task["action"] == "report":
                # Final report from 3 specialist subagents
                final_report = output.get("report") or output
                self.db.execute(
                    """
                    UPDATE grill_sessions
                    SET status = 'completed', final_report = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(final_report, ensure_ascii=False),
                        now_stamp,
                        now_stamp,
                        session_id,
                    ),
                )

            self.db.commit()
