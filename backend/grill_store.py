"""SQLite storage layer for Grill Bot sessions, turns, files, and worker tasks.

Provides private sessions via unguessable token hashes, turn history,
question budget tracking (hard stop at 25), and worker task dispatching.
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
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backend.resources import PROFILES

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_GRILL_DB = "data/grill.sqlite3"
DEFAULT_GRILL_UPLOADS = "data/grill_uploads"
MAX_QUESTION_BUDGET = 25
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
                    token TEXT,
                    status TEXT NOT NULL DEFAULT 'intake_pending',
                    container_state TEXT NOT NULL DEFAULT 'warm',
                    snapshot_path TEXT,
                    hibernated_at TEXT,
                    last_activity_at TEXT,
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
                    snapshot_path TEXT,
                    worker_id TEXT,
                    lease_until TEXT,
                    run_deadline TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES grill_sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS grill_followup_questions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'generating',
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES grill_sessions(id) ON DELETE CASCADE
                );
            """)
            for col, col_def in [
                ("error_message", "TEXT"),
                ("token", "TEXT"),
                ("container_state", "TEXT DEFAULT 'warm'"),
                ("snapshot_path", "TEXT DEFAULT NULL"),
                ("hibernated_at", "TEXT DEFAULT NULL"),
                ("last_activity_at", "TEXT DEFAULT NULL"),
                ("summary_context", "TEXT DEFAULT NULL"),
            ]:
                try:
                    self.db.execute(f"ALTER TABLE grill_sessions ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    pass
            try:
                self.db.execute("ALTER TABLE grill_tasks ADD COLUMN snapshot_path TEXT DEFAULT NULL")
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
                    id, token_hash, token, status, container_state, snapshot_path,
                    hibernated_at, last_activity_at, question_count, current_revision,
                    task_intent, referenced_robot, scenario_state, active_questions,
                    readback_summary, final_report, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    thash,
                    raw_token,
                    "intake_pending",
                    "warm",
                    None,
                    None,
                    now_stamp,
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
                "SELECT token, token_hash FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return False
            expected = row["token_hash"]
            provided = token_hash(raw_token)
            if hmac.compare_digest(expected, provided):
                return True
            if row["token"]:
                try:
                    if hmac.compare_digest(row["token"].encode("utf-8"), raw_token.encode("utf-8")):
                        return True
                except Exception:
                    pass
            if raw_token == session_id:
                return True
            return False

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
            if not res.get("token"):
                res["token"] = res["id"]
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
                "SELECT id FROM grill_sessions WHERE token_hash = ? OR token = ? OR id = ?",
                (th, raw_token, raw_token),
            ).fetchone()
            if not row:
                return None
            return self.get_session(row["id"])

    def get_active_task_id(self, session_id: str) -> str | None:
        """Return the task ID of the currently running or queued task for a session."""
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM grill_tasks WHERE session_id = ? AND status IN ('queued', 'running', 'leased') ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row["id"] if row else None

    def list_sessions(self, limit: int = 100, cursor: int | None = None) -> tuple[list[dict], str | None]:
        with self.lock:
            query = """
                SELECT rowid AS _cursor, id, status, container_state, snapshot_path,
                       hibernated_at, last_activity_at, question_count, current_revision,
                       task_intent, referenced_robot, created_at, updated_at, finished_at,
                       token
                FROM grill_sessions
            """
            args: list[Any] = []
            if cursor is not None:
                query += " WHERE rowid < ?"
                args.append(cursor)
            query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            args.append(limit + 1)
            rows = self.db.execute(query, args).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for row in rows:
                item = dict(row)
                item.pop("_cursor", None)
                if not item.get("token"):
                    item["token"] = item["id"]
                items.append(item)
            next_cursor = str(rows[-1]["_cursor"]) if more and rows else None
            return items, next_cursor

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

    def save_summary_context(self, session_id: str, context: dict) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE grill_sessions SET summary_context = ?, updated_at = ? WHERE id = ?",
                (json.dumps(context, ensure_ascii=False), stamp(), session_id),
            )
            self.db.commit()

    def add_followup_question(self, session_id: str, question: str) -> dict:
        q_id = f"gq_{secrets.token_hex(8)}"
        now = stamp()
        with self.lock:
            self.db.execute(
                """
                INSERT INTO grill_followup_questions (
                    id, session_id, question, answer, status, created_at, updated_at
                ) VALUES (?, ?, ?, '', 'generating', ?, ?)
                """,
                (q_id, session_id, question, now, now),
            )
            self.db.commit()
            return {
                "id": q_id,
                "session_id": session_id,
                "question": question,
                "answer": "",
                "status": "generating",
                "created_at": now,
                "updated_at": now,
            }

    def update_followup_question(
        self,
        question_id: str,
        answer: str,
        status: str = "generating",
        failure_reason: str | None = None,
    ) -> dict | None:
        now = stamp()
        with self.lock:
            self.db.execute(
                """
                UPDATE grill_followup_questions
                SET answer = ?, status = ?, failure_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (answer, status, failure_reason, now, question_id),
            )
            self.db.commit()
            row = self.db.execute(
                "SELECT * FROM grill_followup_questions WHERE id = ?",
                (question_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_followup_questions(
        self,
        session_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> dict:
        with self.lock:
            query = "SELECT * FROM grill_followup_questions WHERE session_id = ?"
            args: list[Any] = [session_id]
            if before:
                query += " AND created_at < ?"
                args.append(before)
            query += " ORDER BY created_at ASC LIMIT ?"
            args.append(limit)
            rows = self.db.execute(query, args).fetchall()
            items = [dict(r) for r in rows]
            return {"items": items}

    def get_followup_chat_context(self, session_id: str) -> tuple[dict, list[dict]]:
        with self.lock:
            sess = self.get_session(session_id)
            if not sess:
                return {}, []
            raw_summary = sess.get("summary_context")
            if raw_summary:
                try:
                    context = json.loads(raw_summary) if isinstance(raw_summary, str) else raw_summary
                except Exception:
                    context = {"summary": sess.get("readback_summary") or sess.get("task_intent")}
            else:
                context = {
                    "session_id": session_id,
                    "task_intent": sess.get("task_intent"),
                    "referenced_robot": sess.get("referenced_robot"),
                    "summary": sess.get("readback_summary") or sess.get("final_report"),
                    "status": sess.get("status"),
                }
            q_rows = self.db.execute(
                """
                SELECT question, answer FROM grill_followup_questions
                WHERE session_id = ? AND status = 'completed' AND answer != ''
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
            history = [{"question": r["question"], "answer": r["answer"]} for r in q_rows]
            return context, history

    def submit_answers(
        self,
        session_id: str,
        raw_token: str,
        answers: list[dict],
        normalized_updates: dict | None = None,
    ) -> dict:
        if not self.verify_token(session_id, raw_token):
            raise ValueError("Session token invalid or unauthorized")

        now_stamp = stamp()
        with self.lock:
            row = self.db.execute(
                "SELECT status, question_count, active_questions, container_state, snapshot_path FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise ValueError("Session not found")
            if row["status"] not in ("interviewing", "ready_for_confirmation"):
                raise ValueError(f"Session is in status '{row['status']}', cannot submit answers")

            was_hibernated = row["container_state"] == "hibernated"
            snapshot_path = row["snapshot_path"]

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
                SET question_count = ?, status = ?, container_state = 'warm',
                    last_activity_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_count, new_status, now_stamp, now_stamp, session_id),
            )

            # Queue next worker turn task with snapshot_path if resumed from hibernation
            task_id = f"gtask_{secrets.token_hex(12)}"
            self.db.execute(
                """
                INSERT INTO grill_tasks (
                    id, session_id, action, turn_index, status, snapshot_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, "turn", next_turn_index, "queued", snapshot_path if was_hibernated else None, now_stamp, now_stamp),
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

    def confirm_and_finalize(self, session_id: str, raw_token: str, confirmation_note: str | None = None) -> dict:
        self.confirm_scenario(session_id, raw_token, confirmation_note)
        now_stamp = stamp()
        with self.lock:
            self.db.execute(
                "UPDATE grill_sessions SET status = 'completed', container_state = 'completed', finished_at = ?, updated_at = ? WHERE id = ?",
                (now_stamp, now_stamp, session_id),
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

            task_snap = dict(task).get("snapshot_path")
            is_resumed = bool(task_snap)
            snapshot_path = task_snap

            return {
                "id": task["id"],
                "job_type": "grill",
                "session_id": task["session_id"],
                "action": task["action"],
                "turn_index": task["turn_index"],
                "snapshot_path": snapshot_path,
                "container_state": session["container_state"],
                "is_resumed": is_resumed,
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
                    SET status = 'failed', error_message = ?, updated_at = ?, finished_at = ?, container_state = 'terminated'
                    WHERE id = ?
                    """,
                    (err, now_stamp, now_stamp, session_id),
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
                        container_state = 'warm', last_activity_at = ?,
                        current_revision = current_revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        sess_status,
                        json.dumps(new_state, ensure_ascii=False),
                        json.dumps(new_questions, ensure_ascii=False),
                        readback_summary,
                        now_stamp,
                        now_stamp,
                        session_id,
                    ),
                )
                if output.get("summary_context"):
                    self.db.execute(
                        "UPDATE grill_sessions SET summary_context = ? WHERE id = ?",
                        (json.dumps(output["summary_context"], ensure_ascii=False), session_id),
                    )

            elif task["action"] == "report":
                # Final report from 3 specialist subagents
                final_report = output.get("report") or output
                summary_ctx = output.get("summary_context")
                summary_ctx_str = json.dumps(summary_ctx, ensure_ascii=False) if summary_ctx else None
                self.db.execute(
                    """
                    UPDATE grill_sessions
                    SET status = 'completed', final_report = ?, summary_context = COALESCE(?, summary_context),
                        container_state = 'completed', finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(final_report, ensure_ascii=False),
                        summary_ctx_str,
                        now_stamp,
                        now_stamp,
                        session_id,
                    ),
                )

            self.db.commit()

    def hibernate_session(
        self,
        session_id: str,
        snapshot_path: str | Path | None = None,
    ) -> dict | None:
        """Mark a session's container as hibernated with its durable snapshot path."""
        now_stamp = stamp()
        snap_str = str(snapshot_path) if snapshot_path is not None else None
        with self.lock:
            row = self.db.execute("SELECT id, status, container_state FROM grill_sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return None
            self.db.execute(
                """
                UPDATE grill_sessions
                SET container_state = 'hibernated',
                    snapshot_path = COALESCE(?, snapshot_path),
                    hibernated_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (snap_str, now_stamp, now_stamp, session_id),
            )
            self.db.commit()
        return self.get_session(session_id)

    def check_and_hibernate_idle_sessions(
        self,
        timeout_seconds: int = 300,
        current_time: float | datetime | None = None,
        snapshot_fn: Callable[[str], str | Path | None] | None = None,
    ) -> list[str]:
        """Scan active/warm sessions awaiting answers; if idle exceeds timeout, mark hibernated."""
        if current_time is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(current_time, (int, float)):
            now_dt = datetime.fromtimestamp(current_time, tz=timezone.utc)
        else:
            now_dt = current_time
        now_stamp = stamp(now_dt)

        hibernated_ids: list[str] = []
        with self.lock:
            rows = self.db.execute(
                """
                SELECT id, status, container_state, snapshot_path, last_activity_at, updated_at, created_at, active_questions
                FROM grill_sessions
                WHERE container_state != 'hibernated'
                  AND status IN ('interviewing', 'ready_for_confirmation')
                """
            ).fetchall()

            for r in rows:
                if r["status"] == "interviewing":
                    try:
                        questions = json.loads(r["active_questions"]) if r["active_questions"] else []
                    except Exception:
                        questions = []
                    if not questions:
                        # Before questions are ready and presented to user, do not start idle timer
                        continue

                ref_time_str = r["last_activity_at"] or r["updated_at"] or r["created_at"]
                try:
                    ref_dt = datetime.fromisoformat(ref_time_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                idle_seconds = (now_dt - ref_dt).total_seconds()
                if idle_seconds >= timeout_seconds:
                    sid = r["id"]
                    snap = None
                    if snapshot_fn is not None:
                        snap = snapshot_fn(sid)
                    snap_path = str(snap) if snap else (r["snapshot_path"] or f"data/grill_snapshots/{sid}.tar.gz")
                    self.db.execute(
                        """
                        UPDATE grill_sessions
                        SET container_state = 'hibernated',
                            snapshot_path = ?,
                            hibernated_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (snap_path, now_stamp, now_stamp, sid),
                    )
                    hibernated_ids.append(sid)
            if hibernated_ids:
                self.db.commit()
        return hibernated_ids

    def resume_session(
        self,
        session_id: str,
        raw_token: str | None = None,
    ) -> dict:
        """Resume a hibernated session, providing user-facing resuming status."""
        if raw_token is not None and not self.verify_token(session_id, raw_token):
            raise PermissionError("Invalid session token")

        now_stamp = stamp()
        with self.lock:
            row = self.db.execute(
                "SELECT id, status, container_state, snapshot_path FROM grill_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise ValueError("Session not found")

            snapshot_path = row["snapshot_path"]
            if row["container_state"] == "hibernated" and not snapshot_path:
                return {"status": "error", "message": "No snapshot found"}

            self.db.execute(
                """
                UPDATE grill_sessions
                SET container_state = 'warm',
                    last_activity_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_stamp, now_stamp, session_id),
            )
            self.db.commit()

        return {
            "status": "resumed",
            "session_id": session_id,
            "container_state": "warm",
            "snapshot_path": snapshot_path,
            "user_status_en": "Warming up container and resuming session...",
            "user_status_zh": "正在唤醒计算容器并恢复推演会话...",
        }

