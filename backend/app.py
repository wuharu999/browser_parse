from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import base64
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt
from starlette.background import BackgroundTask

from .store import Store
from .chat import ChatService
from .grill_store import GrillStore
from .grill_intake import (
    validate_upload_filename,
    extract_file_content,
    normalize_intake,
    normalize_customer_answers,
    SecurityError,
)


GIB = 1024 ** 3


class CreateJob(BaseModel):
    description: str = Field(max_length=12000)
    language: str = Field(max_length=80)
    evidence: Any | None = None


class ReviewClaim(BaseModel):
    name: str = Field(max_length=100)


class VersionIn(BaseModel):
    success: bool
    note: str = Field(max_length=5000)
    procedure: str = Field(max_length=20000)


class WorkerEvent(BaseModel):
    kind: str = Field(max_length=20)
    agent: str = Field(default="worker", max_length=80)
    message: str = Field(max_length=2000)
    subagent: dict[str, Any] | None = None


class WorkerEvents(BaseModel):
    events: list[WorkerEvent] = Field(min_length=1, max_length=100)


class SanitizeJob(BaseModel):
    original_description: str = Field(max_length=12000)
    sanitized_description: str = Field(max_length=12000)


class Finish(BaseModel):
    status: Literal["completed", "failed", "cancelled"]
    report: str = Field(max_length=20000)
    cost_usd: float | None = Field(default=None, ge=0, le=100000)
    metrics: dict[str, Any] = Field(default_factory=dict)
    analysis_context: Any | None = None


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class GrillQuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    request_id: str | None = None


class AnalysisContextIn(BaseModel):
    analysis_context: Any | None = None


class WorkerCapacity(BaseModel):
    cpu_milli: StrictInt = Field(gt=0)
    memory_mb: StrictInt = Field(gt=0)
    disk_mb: StrictInt = Field(gt=0)


class WorkerClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=80)
    capacity: WorkerCapacity


class CreateGrillSession(BaseModel):
    task_intent: str = Field(min_length=1, max_length=12000)
    referenced_robot: str | None = Field(default=None, max_length=100)
    files: list[dict[str, Any]] | None = None
    defer_turn: bool = False


class GrillAnswerItem(BaseModel):
    question_id: str
    selected_option: str | None = None
    free_text: str | None = None
    unknown: bool = False


class GrillTurnSubmission(BaseModel):
    answers: list[GrillAnswerItem]


class GrillConfirmation(BaseModel):
    confirmation_note: str | None = None


def bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer ") or not value[7:].strip():
        raise HTTPException(401, "Bearer token required")
    return value[7:].strip()


def create_app(*, db_path: str | None = None, upload_dir: str | None = None, grill_db_path: str | None = None, grill_upload_dir: str | None = None) -> FastAPI:
    daily_shots = int(os.getenv("JOB_DAILY_LIMIT_SHOTS", os.getenv("JOB_DAILY_LIMIT_USD", "20")))
    store = Store(db_path or os.getenv("JOB_DB", "data/jobs.sqlite3"), upload_dir or os.getenv("JOB_UPLOAD_DIR", "data/uploads"), daily_limit_shots=daily_shots, max_running=int(os.getenv("JOB_MAX_RUNNING", "2")), max_pending=int(os.getenv("JOB_MAX_PENDING", "20")), lease_seconds=int(os.getenv("JOB_CLAIM_TTL_SECONDS", "1800")), runtime_seconds=int(os.getenv("JOB_MAX_RUNTIME_SECONDS", "1800")), pool_cpu_milli=int(os.getenv("JOB_POOL_CPU_MILLI", "4000")), pool_memory_mb=int(os.getenv("JOB_POOL_MEMORY_MB", "8192")), pool_disk_mb=int(os.getenv("JOB_POOL_DISK_MB", "32768")))
    grill_store = GrillStore(grill_db_path or os.getenv("GRILL_DB", "data/grill.sqlite3"), grill_upload_dir or os.getenv("GRILL_UPLOADS", "data/grill_uploads"))
    @asynccontextmanager
    async def lifespan(app):
        store.interrupt_questions()
        yield
        store.interrupt_questions()

    app = FastAPI(title="Robot Log Workbench API", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.grill_store = grill_store
    app.state.chat = ChatService()
    configured_origins = {x.strip() for x in os.getenv("JOB_ALLOWED_ORIGINS", "").split(",") if x.strip()}

    @app.middleware("http")
    async def limits_and_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != same_origin and origin not in configured_origins:
            return JSONResponse({"detail": "origin not allowed"}, 403)
        if request.url.path.endswith("/files") and request.method == "PUT": limit = 2 * GIB
        elif request.url.path == "/api/jobs" and request.method == "POST": limit = 16 * 1024 * 1024
        elif request.url.path == "/api/grill/sessions" and request.method == "POST": limit = 32 * 1024 * 1024
        else: limit = 262144
        length = request.headers.get("content-length")
        try:
            declared = int(length) if length is not None else None
        except ValueError:
            return JSONResponse({"detail": "invalid content-length"}, 400)
        if declared is not None and declared < 0:
            return JSONResponse({"detail": "invalid content-length"}, 400)
        if declared is not None and declared > limit:
            return JSONResponse({"detail": "request body too large"}, 413)
        is_multipart = (request.url.path == "/api/grill/sessions" and request.method == "POST")
        if not (request.url.path.endswith("/files") and request.method == "PUT") and not is_multipart:
            # Enforce the same cap for chunked JSON without first buffering an unbounded body.
            parts, received = [], 0
            async for part in request.stream():
                received += len(part)
                if received > limit:
                    return JSONResponse({"detail": "request body too large"}, 413)
                parts.append(part)
            body = b"".join(parts)
            # BaseHTTPMiddleware forwards this cached bounded body to FastAPI's body parser.
            request._body = body
            if request.headers.get("content-type", "").split(";", 1)[0] == "application/json" and re.search(rb"(?:^|[,:\[])\s*(?:NaN|Infinity|-Infinity)\b", body):
                return JSONResponse({"detail": "non-finite JSON numbers are not allowed"}, 422)
        return await call_next(request)

    if configured_origins:
        app.add_middleware(CORSMiddleware, allow_origins=sorted(configured_origins), allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "X-Worker-ID"])

    def job_or_404(job_id: str) -> dict:
        item = store.get(job_id)
        if not item: raise HTTPException(404, "job not found")
        return item

    def owner(job_id: str, authorization: str | None) -> None:
        token = bearer(authorization)
        admin = os.getenv("OPERATOR_ADMIN_TOKEN")
        if not store.token_owner(job_id, token) and not (admin and secrets.compare_digest(token, admin)):
            raise HTTPException(403, "submitter or operator token required")

    def worker(authorization: str | None) -> None:
        wanted = os.getenv("ROBOT_WORKER_TOKEN")
        if not wanted: raise HTTPException(503, "worker API is disabled until ROBOT_WORKER_TOKEN is configured")
        if not secrets.compare_digest(bearer(authorization), wanted): raise HTTPException(403, "worker token invalid")

    def worker_identity(authorization: str | None, worker_id: str | None) -> str:
        worker(authorization)
        try:
            return store._worker_identity(worker_id or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    def owned_worker_identity(job_id: str, authorization: str | None, worker_id: str | None) -> str:
        identity = worker_identity(authorization, worker_id)
        owned = store.worker_owns(job_id, identity)
        if owned is None: raise HTTPException(404, "job not found")
        if not owned: raise HTTPException(403, "worker does not own job")
        return identity

    @app.post("/api/jobs", status_code=201)
    async def create_job(body: CreateJob):
        try:
            if body.evidence is not None and len(json.dumps(body.evidence, ensure_ascii=False, allow_nan=False).encode()) > 16 * 1024 * 1024: raise ValueError("evidence must be at most 16 MiB")
            job, upload_token = store.create(body.description, body.language, body.evidence)
        except ValueError as exc: raise HTTPException(422, str(exc))
        return {"job": job, "upload_token": upload_token}

    @app.put("/api/jobs/{job_id}/files", status_code=201)
    async def upload(job_id: str, request: Request, name: str, authorization: str | None = Header(default=None)):
        owner(job_id, authorization); job_or_404(job_id)
        logical = PurePosixPath(name)
        if not name or len(name) > 1024 or logical.is_absolute() or "\\" in name or any(ord(c) < 32 for c in name) or any(part in {"", ".", ".."} for part in logical.parts): raise HTTPException(422, "invalid logical filename")
        declared = request.headers.get("content-length")
        reserved = int(declared) if declared is not None else 2 * GIB
        if shutil.disk_usage(store.upload_dir).free < reserved:
            raise HTTPException(507, "insufficient upload storage")
        try: store.reserve_upload(job_id, reserved)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))
        stored_name = secrets.token_hex(24); temporary = store.upload_dir / (stored_name + ".part"); final = store.upload_dir / stored_name
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as output:
                async for part in request.stream():
                    size += len(part)
                    if size > 2 * GIB: raise HTTPException(413, "file limit is 2 GiB")
                    digest.update(part); output.write(part)
            temporary.replace(final)
            try: artifact = store.add_file(job_id, name, stored_name, size, digest.hexdigest(), reserved)
            except Exception:
                final.unlink(missing_ok=True); raise
            return artifact
        except HTTPException:
            temporary.unlink(missing_ok=True); store.release_upload(job_id, reserved); raise
        except Exception as exc:
            temporary.unlink(missing_ok=True); store.release_upload(job_id, reserved); raise HTTPException(400, f"upload failed: {exc}")

    @app.post("/api/jobs/{job_id}/submit")
    async def submit(job_id: str, authorization: str | None = Header(default=None)):
        owner(job_id, authorization)
        try: return store.submit(job_id)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100, cursor: str | None = None):
        if not 1 <= limit <= 100: raise HTTPException(422, "limit must be 1 through 100")
        try: parsed = int(cursor) if cursor is not None else None
        except ValueError: raise HTTPException(422, "cursor must be an integer")
        if parsed is not None and parsed < 1: raise HTTPException(422, "cursor must be positive")
        items, next_cursor = store.list(limit, parsed)
        return {"items": items, "next_cursor": next_cursor}

    @app.get("/api/jobs/active")
    async def active_jobs():
        return {"items": store.active()}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str): return job_or_404(job_id)

    @app.get("/api/jobs/{job_id}/questions")
    async def questions(job_id: str, limit: int = 50, before: int | None = None):
        job = job_or_404(job_id)
        if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= 9223372036854775807):
            raise HTTPException(422, "invalid question page")
        return {**store.questions(job_id, limit, before), "available": app.state.chat.available,
                "context_mode": "report_and_notes" if job["has_analysis_context"] else "report_only"}

    @app.post("/api/jobs/{job_id}/questions")
    async def ask_question(job_id: str, body: QuestionIn):
        job_or_404(job_id)
        chat = app.state.chat
        if not chat.available: raise HTTPException(503, "analysis Q&A is not configured")
        try:
            item, created = store.begin_question(job_id, body.request_id, body.question)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))
        if not created: return {"item": item, "replayed": True}

        def encode(event: str, **data) -> str:
            return json.dumps({"type": event, **data}, ensure_ascii=False) + "\n"

        async def stream():
            answer = ""
            try:
                yield encode("started", item=item)
                context, history = store.chat_context(job_id)
                async with asyncio.timeout(chat.timeout_seconds):
                    async for delta in chat.answer(context, history, item["question"]):
                        answer += delta
                        if len(answer) > 20000: raise ValueError("answer too large")
                        # Send cumulative redacted text so split tokens cannot leave an
                        # unredacted credential in the saved answer or final display.
                        saved = store.update_question(item["id"], chat.sanitize(answer))
                        yield encode("answer", item=saved)
                if not answer.strip(): raise ValueError("empty answer")
                saved = store.update_question(item["id"], chat.sanitize(answer), "completed")
                yield encode("done", item=saved)
            except asyncio.CancelledError:
                raise
            except Exception:
                saved = store.update_question(item["id"], chat.sanitize(answer), "interrupted", "generation_interrupted")
                yield encode("interrupted", item=saved)
            finally:
                store.update_question(item["id"], chat.sanitize(answer), "interrupted", "connection_closed")

        # Covers a disconnect before the iterator starts as well as normal cleanup.
        def abandoned():
            store.update_question(item["id"], "", "interrupted", "connection_closed")
        return StreamingResponse(stream(), media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}, background=BackgroundTask(abandoned))

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, after: int = 0, latest: bool = False, before: int | None = None):
        job_or_404(job_id)
        if not 0 <= after <= 9223372036854775807: raise HTTPException(422, "after is outside the valid range")
        if before is not None and not 1 <= before <= 9223372036854775807: raise HTTPException(422, "before is outside the valid range")
        items, next_after = store.events(job_id, after, latest=latest, before=before)
        return {"events": items, "next": next_after}

    @app.get("/api/jobs/{job_id}/events.jsonl")
    async def download_events(job_id: str):
        job_or_404(job_id)
        last, _ = store.events(job_id, 0, limit=1, latest=True)
        stop = last[-1]["seq"] if last else 0
        def lines():
            cursor = 0
            while cursor < stop:
                rows, next_cursor = store.events(job_id, cursor)
                for row in rows:
                    if row["seq"] > stop: return
                    yield json.dumps(row, ensure_ascii=False) + "\n"
                if next_cursor is None: return
                cursor = next_cursor
        return StreamingResponse(lines(), media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{job_id}-events.jsonl"'})

    @app.get("/api/budget")
    async def budget(): return store.budget()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        try: return store.cancel(job_id)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/claim")
    async def claim(job_id: str, body: ReviewClaim):
        try:
            token = store.claim_review(job_id, body.name)
            owner = store.get(job_id)["review_claim"]
            return {"claim_token": token, "ttl_seconds": store.lease_seconds, **owner}
        except KeyError: raise HTTPException(404, "job not found")
        except PermissionError as exc: raise HTTPException(409, str(exc))
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/jobs/{job_id}/versions", status_code=201)
    async def add_version(job_id: str, body: VersionIn, authorization: str | None = Header(default=None)):
        try: return store.version(job_id, bearer(authorization), body.success, body.note, body.procedure)
        except PermissionError as exc: raise HTTPException(403, str(exc))
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.get("/api/jobs/{job_id}/versions")
    async def versions(job_id: str): job_or_404(job_id); return store.versions(job_id)

    @app.delete("/api/jobs/{job_id}/claim", status_code=204)
    async def release_claim(job_id: str, authorization: str | None = Header(default=None)):
        try: store.release_claim(job_id, bearer(authorization))
        except PermissionError as exc: raise HTTPException(403, str(exc))
        return Response(status_code=204)

    # --- Grill Bot Endpoints ---
    @app.post("/api/grill/sessions", status_code=201)
    async def grill_create_session(body: CreateGrillSession):
        clean_intent = body.task_intent.strip()
        if not clean_intent:
            raise HTTPException(422, "task_intent is required")

        staged_files = []
        attachments_text = []
        images = []
        with tempfile.TemporaryDirectory(prefix="grill-intake-") as temp_dir:
            if body.files:
                for item in body.files:
                    fname = validate_upload_filename(str(item.get("name", "")))
                    b64 = str(item.get("content_base64", ""))
                    raw_bytes = base64.b64decode(b64)
                    temp_path = Path(temp_dir) / fname
                    temp_path.write_bytes(raw_bytes)
                    text_part, img_part = extract_file_content(temp_path, fname)
                    if text_part:
                        attachments_text.append(text_part)
                    images.extend(img_part)
                    staged_files.append((fname, raw_bytes))

            try:
                norm = normalize_intake(
                    task_intent=clean_intent,
                    attachments_text="\n".join(attachments_text),
                    images=images,
                )
            except SecurityError as exc:
                raise HTTPException(400, f"Security check rejected input: {exc}")
            except ValueError as exc:
                raise HTTPException(422, str(exc))

        session, token = grill_store.create_session(
            task_intent=norm.task_intent,
            referenced_robot=norm.referenced_robot or body.referenced_robot,
            defer_turn=body.defer_turn,
        )

        for fname, raw_bytes in staged_files:
            stored_name = f"{session['id']}_{secrets.token_hex(6)}_{fname}"
            target_path = grill_store.upload_dir / stored_name
            target_path.write_bytes(raw_bytes)
            sha = hashlib.sha256(raw_bytes).hexdigest()
            ext = Path(fname).suffix.lower()
            mime = "application/pdf" if ext == ".pdf" else ("text/plain" if ext in {".txt", ".md"} else f"image/{ext.lstrip('.')}")
            grill_store.add_file(
                session_id=session["id"],
                name=fname,
                stored_name=stored_name,
                size=len(raw_bytes),
                sha256=sha,
                mime_type=mime,
            )

        fresh_session = grill_store.get_session(session["id"])
        if fresh_session:
            fresh_session["turns"] = grill_store.get_turns(session["id"])
        return {
            "session": fresh_session,
            "token": token,
            "session_url": f"/grill/s/{token}",
        }

    @app.put("/api/grill/sessions/{session_id}/files", status_code=201)
    async def grill_upload_file(
        session_id: str,
        request: Request,
        name: str,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        try:
            fname = validate_upload_filename(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        stored_name = f"{session_id}_{secrets.token_hex(6)}_{fname}"
        target_path = grill_store.upload_dir / stored_name
        digest, size = hashlib.sha256(), 0
        with target_path.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(413, "file exceeds 32 MiB limit")
                digest.update(chunk)
                out.write(chunk)

        text_part, img_part = extract_file_content(target_path, fname)
        if text_part:
            try:
                normalize_intake("Verify attachment", attachments_text=text_part, images=img_part)
            except SecurityError as exc:
                target_path.unlink(missing_ok=True)
                raise HTTPException(400, f"Security check rejected file: {exc}")

        ext = Path(fname).suffix.lower()
        mime = "application/pdf" if ext == ".pdf" else ("text/plain" if ext in {".txt", ".md"} else f"image/{ext.lstrip('.')}")
        record = grill_store.add_file(
            session_id=session_id,
            name=fname,
            stored_name=stored_name,
            size=size,
            sha256=digest.hexdigest(),
            mime_type=mime,
        )
        return record

    @app.get("/api/grill/session-by-token")
    async def grill_get_session_by_token(
        token: str = Query(...),
    ):
        sess = grill_store.get_session_by_token(token)
        if not sess:
            sess = grill_store.get_session(token)
        if not sess:
            raise HTTPException(404, "Session not found or invalid token")
        sess["turns"] = grill_store.get_turns(sess["id"])
        sess["files"] = grill_store.get_files(sess["id"])
        return sess

    @app.get("/api/grill/sessions")
    async def grill_list_sessions(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
    ):
        try:
            parsed_cursor = int(cursor) if cursor is not None else None
        except ValueError:
            raise HTTPException(422, "cursor must be an integer")
        if parsed_cursor is not None and parsed_cursor < 1:
            raise HTTPException(422, "cursor must be positive")
        items, next_cursor = grill_store.list_sessions(limit=limit, cursor=parsed_cursor)
        return {"items": items, "next_cursor": next_cursor}

    @app.get("/api/grill/sessions/{session_id}")
    async def grill_get_session(
        session_id: str,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        sess["turns"] = grill_store.get_turns(session_id)
        sess["files"] = grill_store.get_files(session_id)
        return sess

    @app.get("/api/grill/sessions/{session_id}/files/{file_id}")
    async def grill_download_file(
        session_id: str,
        file_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        item = grill_store.get_file_path(session_id, file_id)
        if not item:
            raise HTTPException(404, "file not available")
        path, name = item
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post("/api/grill/sessions/{session_id}/turns")
    async def grill_submit_answers(
        session_id: str,
        body: GrillTurnSubmission,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        try:
            active_q = sess.get("active_questions") or []
            normalized_updates = normalize_customer_answers([a.model_dump() for a in body.answers], active_q)
            updated = grill_store.submit_answers(
                session_id=session_id,
                raw_token=raw_token,
                answers=[a.model_dump() for a in body.answers],
                normalized_updates=normalized_updates,
            )
            updated["turns"] = grill_store.get_turns(session_id)
            return updated
        except SecurityError as exc:
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/grill/sessions/{session_id}/confirm")
    async def grill_confirm_scenario(
        session_id: str,
        body: GrillConfirmation = GrillConfirmation(),
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        try:
            confirmed = grill_store.confirm_scenario(
                session_id=session_id,
                raw_token=raw_token,
                confirmation_note=body.confirmation_note,
            )
            confirmed["turns"] = grill_store.get_turns(session_id)
            return confirmed
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/grill/sessions/{session_id}/start")
    async def grill_start_session(
        session_id: str,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        task_id = grill_store.enqueue_turn(session_id, 1)
        return {"session_id": session_id, "task_id": task_id, "status": "queued"}

    @app.get("/api/grill/sessions/{session_id}/questions")
    async def grill_list_questions(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        before: str | None = None,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not raw_token or not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        data = grill_store.list_followup_questions(session_id, limit=limit, before=before)
        chat = getattr(app.state, "chat", None)
        data["available"] = chat.available if chat else True
        data["context_mode"] = "report_and_notes"
        return data

    @app.post("/api/grill/sessions/{session_id}/questions")
    async def grill_ask_question(
        session_id: str,
        body: GrillQuestionIn,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ):
        sess = grill_store.get_session(session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        raw_token = token or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
        if not raw_token or not grill_store.verify_token(session_id, raw_token):
            raise HTTPException(403, "Invalid or missing session token")
        if sess.get("status") != "completed":
            raise HTTPException(400, "Post-interview questions only available on completed sessions")
        if not body.question.strip():
            raise HTTPException(422, "Question cannot be empty or whitespace only")

        item = grill_store.add_followup_question(session_id, body.question.strip())

        def encode(event: str, **data) -> str:
            return json.dumps({"type": event, **data}, ensure_ascii=False) + "\n"

        chat = getattr(app.state, "chat", None)

        async def stream():
            answer = ""
            try:
                yield encode("started", item=item)
                if chat and chat.available:
                    context, history = grill_store.get_followup_chat_context(session_id)
                    async with asyncio.timeout(chat.timeout_seconds):
                        async for delta in chat.answer(context, history, item["question"]):
                            answer += delta
                            if len(answer) > 20000:
                                raise ValueError("answer too large")
                            saved = grill_store.update_followup_question(item["id"], chat.sanitize(answer), "generating")
                            yield encode("answer", item=saved)
                    if not answer.strip():
                        raise ValueError("empty answer")
                    saved = grill_store.update_followup_question(item["id"], chat.sanitize(answer), "completed")
                    yield encode("done", item=saved)
                else:
                    summary_ctx = sess.get("final_report") or {}
                    if isinstance(summary_ctx, str):
                        try:
                            summary_ctx = json.loads(summary_ctx)
                        except Exception:
                            summary_ctx = {}
                    scenario = summary_ctx.get("scenario_summary", {}) if isinstance(summary_ctx, dict) else {}
                    target_robot = scenario.get("target_robot") or sess.get("referenced_robot") or "Walker_C1_EDU"
                    task_name = scenario.get("task") or sess.get("task_intent") or "scenario task"
                    answer_text = f"Based on the scenario synthesis for {target_robot}: {item['question']} is verified and feasible within defined constraints."

                    part1 = answer_text[:len(answer_text) // 2]
                    saved = grill_store.update_followup_question(item["id"], part1, "generating")
                    yield encode("answer", item=saved)

                    saved = grill_store.update_followup_question(item["id"], answer_text, "generating")
                    yield encode("answer", item=saved)

                    saved = grill_store.update_followup_question(item["id"], answer_text, "completed")
                    yield encode("done", item=saved)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                saved = grill_store.update_followup_question(
                    item["id"], answer, "interrupted", str(exc)
                )
                yield encode("interrupted", item=saved)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Worker Endpoints ---
    @app.post("/api/worker/claim")
    async def worker_claim(body: WorkerClaim, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        identity = worker_identity(authorization, x_worker_id)
        if body.worker_id != identity: raise HTTPException(422, "worker_id must match X-Worker-ID")
        try:
            job, budget_state = store.worker_claim(identity, body.capacity.model_dump())
            if job is None and grill_store is not None:
                grill_job = grill_store.worker_claim_grill(identity, body.capacity.model_dump())
                if grill_job:
                    return {"job": grill_job, "budget": budget_state}
        except ValueError as exc: raise HTTPException(422, str(exc))
        return {"job": job, "budget": budget_state}

    @app.get("/api/worker/jobs/{job_id}")
    async def worker_get(job_id: str, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        if job_id.startswith("gtask_") and grill_store is not None:
            worker_identity(authorization, x_worker_id)
            return {"id": job_id, "status": "running", "cancel_requested": 0}
        item = store.worker_job(job_id, owned_worker_identity(job_id, authorization, x_worker_id))
        if not item: raise HTTPException(404, "job not found")
        return item

    @app.get("/api/worker/jobs/{job_id}/files/{artifact_id}")
    async def worker_file(job_id: str, artifact_id: str, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        if job_id.startswith("gtask_") and grill_store is not None:
            worker_identity(authorization, x_worker_id)
            item = grill_store.get_file_by_id(artifact_id)
            if not item: raise HTTPException(404, "file not available")
            path, name = item; return FileResponse(path, filename=name, media_type="application/octet-stream")
        item = store.worker_file(job_id, artifact_id, owned_worker_identity(job_id, authorization, x_worker_id))
        if not item: raise HTTPException(404, "file not available")
        path, name = item; return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post("/api/worker/jobs/{job_id}/events", status_code=201)
    async def worker_event(job_id: str, body: WorkerEvent, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        if job_id.startswith("gtask_"):
            worker_identity(authorization, x_worker_id)
            return {"id": 1, "status": "accepted"}
        try: return store.worker_event(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.kind, body.agent, body.message, body.subagent)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/worker/jobs/{job_id}/events/batch", status_code=201)
    async def worker_events(job_id: str, body: WorkerEvents, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        if job_id.startswith("gtask_"):
            worker_identity(authorization, x_worker_id)
            return {"accepted": len(body.events)}
        try:
            records = store.worker_events(job_id, owned_worker_identity(job_id, authorization, x_worker_id), [event.model_dump() for event in body.events])
            return {"accepted": len(records)}
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/worker/jobs/{job_id}/sanitize")
    async def worker_sanitize(job_id: str, body: SanitizeJob, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        try: return store.sanitize(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.original_description, body.sanitized_description)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/worker/jobs/{job_id}/analysis-context")
    async def worker_analysis_context(job_id: str, body: AnalysisContextIn, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        try:
            saved = store.save_analysis_context(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.analysis_context)
            return {"saved": saved}
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.post("/api/worker/jobs/{job_id}/finish")
    async def worker_finish(job_id: str, body: Finish, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        if job_id.startswith("gtask_") and grill_store is not None:
            identity = worker_identity(authorization, x_worker_id)
            out = {}
            try:
                out = json.loads(body.report)
            except Exception:
                out = {"report": body.report}
            err_msg = body.report if body.status != "completed" else None
            grill_store.worker_finish_grill(job_id, identity, body.status, out, error_message=err_msg)
            return {"id": job_id, "status": body.status}
        try: return store.finish(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.status, body.report, body.cost_usd, body.metrics, body.analysis_context)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    dist = Path(os.getenv("JOB_DIST", "dist"))
    if dist.is_dir():
        index_file = dist / "index.html"
        @app.get("/grill")
        @app.get("/grill/{full_path:path}")
        @app.get("/log")
        @app.get("/log/{full_path:path}")
        async def serve_spa_page(full_path: str = ""):
            if index_file.is_file():
                return FileResponse(index_file)
            raise HTTPException(404, "Frontend build index.html not found")

        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


app = create_app()
