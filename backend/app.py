from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from .store import Store


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


class SanitizeJob(BaseModel):
    original_description: str = Field(max_length=12000)
    sanitized_description: str = Field(max_length=12000)


class Finish(BaseModel):
    status: Literal["completed", "failed", "cancelled"]
    report: str = Field(max_length=20000)
    cost_usd: float | None = Field(default=None, ge=0, le=100000)
    metrics: dict[str, Any] = Field(default_factory=dict)


class WorkerCapacity(BaseModel):
    cpu_milli: StrictInt = Field(gt=0)
    memory_mb: StrictInt = Field(gt=0)
    disk_mb: StrictInt = Field(gt=0)


class WorkerClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=80)
    capacity: WorkerCapacity


def bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer ") or not value[7:].strip():
        raise HTTPException(401, "Bearer token required")
    return value[7:].strip()


def create_app(*, db_path: str | None = None, upload_dir: str | None = None) -> FastAPI:
    daily_shots = int(os.getenv("JOB_DAILY_LIMIT_SHOTS", os.getenv("JOB_DAILY_LIMIT_USD", "20")))
    store = Store(db_path or os.getenv("JOB_DB", "data/jobs.sqlite3"), upload_dir or os.getenv("JOB_UPLOAD_DIR", "data/uploads"), daily_limit_shots=daily_shots, max_running=int(os.getenv("JOB_MAX_RUNNING", "2")), max_pending=int(os.getenv("JOB_MAX_PENDING", "20")), lease_seconds=int(os.getenv("JOB_CLAIM_TTL_SECONDS", "1800")), runtime_seconds=int(os.getenv("JOB_MAX_RUNTIME_SECONDS", "1800")), pool_cpu_milli=int(os.getenv("JOB_POOL_CPU_MILLI", "4000")), pool_memory_mb=int(os.getenv("JOB_POOL_MEMORY_MB", "8192")), pool_disk_mb=int(os.getenv("JOB_POOL_DISK_MB", "32768")))
    app = FastAPI(title="Robot Log Workbench API", docs_url=None, redoc_url=None)
    app.state.store = store
    configured_origins = {x.strip() for x in os.getenv("JOB_ALLOWED_ORIGINS", "").split(",") if x.strip()}

    @app.middleware("http")
    async def limits_and_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin != same_origin and origin not in configured_origins:
            return JSONResponse({"detail": "origin not allowed"}, 403)
        if request.url.path.endswith("/files") and request.method == "PUT": limit = 2 * GIB
        elif request.url.path == "/api/jobs" and request.method == "POST": limit = 16 * 1024 * 1024
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
        if not (request.url.path.endswith("/files") and request.method == "PUT"):
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

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, after: int = 0, latest: bool = False, before: int | None = None):
        job_or_404(job_id)
        if not 0 <= after <= 9223372036854775807: raise HTTPException(422, "after is outside the valid range")
        if before is not None and not 1 <= before <= 9223372036854775807: raise HTTPException(422, "before is outside the valid range")
        items, next_after = store.events(job_id, after, latest=latest, before=before)
        return {"events": items, "next": next_after}

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

    @app.post("/api/worker/claim")
    async def worker_claim(body: WorkerClaim, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        identity = worker_identity(authorization, x_worker_id)
        if body.worker_id != identity: raise HTTPException(422, "worker_id must match X-Worker-ID")
        try:
            job, budget_state = store.worker_claim(identity, body.capacity.model_dump())
        except ValueError as exc: raise HTTPException(422, str(exc))
        return {"job": job, "budget": budget_state}

    @app.get("/api/worker/jobs/{job_id}")
    async def worker_get(job_id: str, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        item = store.worker_job(job_id, owned_worker_identity(job_id, authorization, x_worker_id))
        if not item: raise HTTPException(404, "job not found")
        return item

    @app.get("/api/worker/jobs/{job_id}/files/{artifact_id}")
    async def worker_file(job_id: str, artifact_id: str, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        item = store.worker_file(job_id, artifact_id, owned_worker_identity(job_id, authorization, x_worker_id))
        if not item: raise HTTPException(404, "file not available")
        path, name = item; return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post("/api/worker/jobs/{job_id}/events", status_code=201)
    async def worker_event(job_id: str, body: WorkerEvent, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        try: return store.worker_event(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.kind, body.agent, body.message, body.subagent)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/worker/jobs/{job_id}/sanitize")
    async def worker_sanitize(job_id: str, body: SanitizeJob, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        try: return store.sanitize(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.original_description, body.sanitized_description)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/worker/jobs/{job_id}/finish")
    async def worker_finish(job_id: str, body: Finish, authorization: str | None = Header(default=None), x_worker_id: str | None = Header(default=None)):
        try: return store.finish(job_id, owned_worker_identity(job_id, authorization, x_worker_id), body.status, body.report, body.cost_usd, body.metrics)
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    dist = Path(os.getenv("JOB_DIST", "dist"))
    if dist.is_dir(): app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


app = create_app()
