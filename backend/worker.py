"""Fail-closed worker that runs each claimed job in one constrained Docker container.

This module deliberately has no local subprocess fallback.  The worker is a
small HTTP client; the API owns queue admission, the two-job global limit, and
the daily budget reservation/settlement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import fcntl
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .lifecycle import validate_subagent
from .guard import (
    GuardError,
    GuardVerdict,
    SecurityGuard,
    extract_non_log_attachments,
    is_non_log_attachment,
)
from .resources import PROFILES
from sandbox.analysis_context import validate_context

from .docker_runtime import DockerCleanupError, DockerError, DockerJob, DockerRuntime


TRANSFER_CHUNK_BYTES = 8 * 1024 * 1024
MAX_WIKI_FILE_BYTES = 16 * 1024 * 1024


class WorkerError(RuntimeError):
    pass


class ApiError(WorkerError):
    pass


class JobCancelled(WorkerError):
    pass


class JobTimedOut(WorkerError):
    pass


class CleanupUnconfirmed(WorkerError):
    pass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise WorkerError(f"missing required environment variable: {name}")
    return value or ""


def _safe_text(value: object, secrets: tuple[str, ...] = (), *, preserve: bool = False) -> str:
    """Bound public event/report text and remove known credential values."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;\"'{}\[\]]+", r"\1=[redacted]", text)
    text = re.sub(r"\b(?:sk|pk|rk|AKIA)[-_A-Za-z0-9]{12,}\b", "[redacted]", text)
    if preserve:
        return text[:12000]
    # Keep Markdown paragraphs/lists readable; only normalize horizontal space.
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()[:12_000]


def _safe_report(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Redact JSON report string values without changing its syntax or provenance."""
    if not isinstance(value, str):
        return _safe_text(value, secrets)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _safe_text(value, secrets)

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return _safe_text(item, secrets)
        if isinstance(item, list):
            return [clean(part) for part in item]
        if isinstance(item, dict):
            return {key: clean(part) for key, part in item.items()}
        return item

    return json.dumps(clean(parsed), ensure_ascii=False, separators=(",", ":"))[:12_000]


def _input_path(file_id: str, name: str) -> str:
    """Make a flat, non-traversing archive-preserving path for a manifest file."""
    leaf = PurePosixPath(name.replace("\\", "/")).name
    suffix = "".join(PurePosixPath(leaf).suffixes[-2:])[:24]
    suffix = re.sub(r"[^A-Za-z0-9.]", "_", suffix)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(file_id))[:80] or "file"
    return f"inputs/{safe_id}{suffix}"


# ---------------------------------------------------------------------------
# Extensions that require a vision endpoint (image or PDF content)
_VISION_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".pdf",
})


def _needs_vision(files: list[dict]) -> bool:
    """Return True when any uploaded file is an image or PDF that requires vision."""
    for item in files:
        name = str(item.get("name", "")).strip().lower()
        ext = PurePosixPath(name).suffix
        if ext in _VISION_EXTENSIONS:
            return True
    return False




@dataclass(frozen=True)
class WorkerConfig:
    api_url: str
    worker_token: str
    worker_id: str
    docker_image: str
    docker_network: str
    egress_proxy_url: str
    docker_data_dir: Path | None
    codex_model: str
    codex_provider_url: str | None
    codex_api_key_env: str
    codex_api_key: str
    timeout_seconds: int
    poll_seconds: float
    runtime_dir: Path
    wiki_dir: Path | None
    cpu_milli: int
    memory_mb: int
    disk_mb: int
    disk_reserve_mb: int
    guard_model: str | None = None
    guard_provider_url: str | None = None
    guard_api_key: str | None = None
    guard_enabled: bool = False

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        key_env = _env("ROBOT_CODEX_API_KEY_ENV", "OPENAI_API_KEY")
        timeout = int(_env("ROBOT_JOB_TIMEOUT_SECONDS", "1800"))
        if not 1 <= timeout <= 3600:
            raise WorkerError("ROBOT_JOB_TIMEOUT_SECONDS must be between 1 and 3600")
        runtime = Path(_env("ROBOT_SANDBOX_RUNTIME", str(Path(__file__).resolve().parents[1] / "sandbox" / "runtime")))
        wiki = os.environ.get("ROBOT_WIKI_DIR")
        default_wiki = Path(__file__).resolve().parents[1] / "knowledge" / "wiki"
        worker_id = _env("ROBOT_WORKER_ID", required=True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", worker_id):
            raise WorkerError("ROBOT_WORKER_ID has invalid characters")
        if _env("ROBOT_WORKER_PARALLEL", "1") != "1":
            raise WorkerError("ROBOT_WORKER_PARALLEL must be 1")
        codex_model = _env("ROBOT_CODEX_MODEL", "gpt-5.6-luna")
        codex_provider_url = os.environ.get("ROBOT_CODEX_PROVIDER_URL") or None
        codex_key = os.environ.get("ROBOT_CODEX_API_KEY") or _env(key_env, required=True)
        guard_model = os.environ.get("ROBOT_GUARD_MODEL") or None
        guard_provider_url = os.environ.get("ROBOT_GUARD_PROVIDER_URL") or codex_provider_url
        guard_api_key = os.environ.get("ROBOT_GUARD_API_KEY") or os.environ.get("ROBOT_CODEX_API_KEY") or codex_key
        # R1.3: Enable the guard by default in worker runtime, defaulting to ROBOT_CODEX_* settings
        guard_enabled = os.environ.get("ROBOT_GUARD_ENABLED", "").lower() not in {"0", "false", "no", "off"}
        configured_data_dir = os.environ.get("ROBOT_DOCKER_DATA_DIR")
        # /var/lib/docker was the previous implicit default.  Preserve it only
        # as a compatibility value, not an assertion that blocks a relocated
        # daemon. Any other explicit path must match DockerRootDir exactly.
        expected_data_dir = (
            Path(configured_data_dir)
            if configured_data_dir and configured_data_dir != "/var/lib/docker"
            else None
        )
        config = cls(
            api_url=_env("ROBOT_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            worker_token=_env("ROBOT_WORKER_TOKEN", required=True),
            worker_id=worker_id,
            docker_image=_env("ROBOT_DOCKER_IMAGE", required=True),
            docker_network=_env("ROBOT_DOCKER_NETWORK", required=True),
            egress_proxy_url=_env("ROBOT_EGRESS_PROXY_URL", required=True),
            docker_data_dir=expected_data_dir,
            codex_model=codex_model,
            codex_provider_url=codex_provider_url,
            codex_api_key_env=key_env,
            codex_api_key=codex_key,
            timeout_seconds=timeout,
            poll_seconds=max(0.25, float(_env("ROBOT_POLL_SECONDS", "3"))),
            runtime_dir=runtime,
            wiki_dir=Path(wiki).resolve() if wiki else (default_wiki.resolve() if default_wiki.is_dir() else None),
            cpu_milli=int(_env("ROBOT_WORKER_CPU_MILLI", "4000")),
            memory_mb=int(_env("ROBOT_WORKER_MEMORY_MB", "8192")),
            disk_mb=int(_env("ROBOT_WORKER_DISK_MB", "24576")),
            disk_reserve_mb=int(_env("ROBOT_HOST_DISK_RESERVE_MB", "8192")),
            guard_model=guard_model,
            guard_provider_url=guard_provider_url,
            guard_api_key=guard_api_key,
            guard_enabled=guard_enabled,
        )
        if any(value <= 0 for value in (config.cpu_milli, config.memory_mb, config.disk_mb, config.disk_reserve_mb)):
            raise WorkerError("Docker worker capacities and disk reserve must be positive")
        return config


class WorkerApi:
    """The private worker API.  It never emits credentials or command output."""

    def __init__(self, base_url: str, token: str, worker_id: str, timeout: float = 30) -> None:
        self.base_url, self.token, self.worker_id, self.timeout = base_url.rstrip("/"), token, worker_id, timeout

    def _request(self, method: str, path: str, body: object | None = None, *, raw: bool = False) -> Any:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self.token}", "X-Worker-ID": self.worker_id}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310: configured API endpoint
                payload = response.read()
        except HTTPError as exc:
            raise ApiError(f"worker API {method} {path} returned {exc.code}") from exc
        except URLError as exc:
            raise ApiError(f"worker API {method} {path} is unavailable") from exc
        if raw:
            return payload
        try:
            return json.loads(payload.decode() or "null")
        except json.JSONDecodeError as exc:
            raise ApiError(f"worker API {method} {path} returned invalid JSON") from exc

    def claim(self, capacity: dict[str, int]) -> dict[str, Any] | None:
        response = self._request("POST", "/api/worker/claim", {"worker_id": self.worker_id, "capacity": capacity})
        if not isinstance(response, dict):
            raise ApiError("claim response is not an object")
        job = response.get("job")
        if job is None:
            return None
        if not isinstance(job, dict) or not job.get("id"):
            raise ApiError("claim response has an invalid job")
        return job

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/api/worker/jobs/{job_id}")
        if not isinstance(response, dict):
            raise ApiError("job state response is not an object")
        return response

    def download_to(self, job_id: str, artifact_id: str, destination: Path) -> tuple[int, str]:
        request = Request(f"{self.base_url}/api/worker/jobs/{job_id}/files/{artifact_id}", headers={"Authorization": f"Bearer {self.token}", "X-Worker-ID": self.worker_id})
        digest, size = hashlib.sha256(), 0
        try:
            with urlopen(request, timeout=self.timeout) as response, destination.open("xb") as output:  # nosec B310: configured API endpoint
                while chunk := response.read(TRANSFER_CHUNK_BYTES):
                    size += len(chunk); digest.update(chunk); output.write(chunk)
        except HTTPError as exc:
            raise ApiError(f"worker file download returned {exc.code}") from exc
        except URLError as exc:
            raise ApiError("worker file download is unavailable") from exc
        return size, digest.hexdigest()

    def event(self, job_id: str, kind: str, message: str, agent: str = "worker", subagent: dict[str, Any] | None = None) -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/events", {
            "kind": kind, "agent": agent, "message": message, "subagent": subagent,
        })

    def events_batch(self, job_id: str, records: list[dict]) -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/events/batch", {"events": records})

    def sanitize(self, job_id: str, original_description: str, sanitized_description: str) -> dict[str, Any]:
        return self._request("POST", f"/api/worker/jobs/{job_id}/sanitize", {
            "original_description": original_description,
            "sanitized_description": sanitized_description,
        })

    def save_analysis_context(self, job_id: str, context: dict) -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/analysis-context", {"analysis_context": context})

    def finish(self, job_id: str, status: str, report: str, cost_usd: float | None, metrics: dict[str, Any], analysis_context: dict | None = None) -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/finish", {
            "status": status, "report": report, "cost_usd": cost_usd, "metrics": metrics, "analysis_context": analysis_context,
        })


class DockerWorker:
    """Serial executor.  API queue admission is the global two-job limiter."""

    def __init__(self, config: WorkerConfig, api: WorkerApi | None = None, runtime: DockerRuntime | None = None, guard: Any = None) -> None:
        self.config = config
        self.api = api or WorkerApi(config.api_url, config.worker_token, config.worker_id)
        self.runtime = runtime or DockerRuntime(image=config.docker_image, network=config.docker_network, proxy_url=config.egress_proxy_url, worker_id=config.worker_id, data_dir=config.docker_data_dir, disk_reserve_mb=config.disk_reserve_mb)
        secrets_list = [config.worker_token, config.codex_api_key]
        if config.guard_api_key:
            secrets_list.append(config.guard_api_key)
        self._secrets = tuple(s for s in secrets_list if s)
        self.guard = guard
        self._capacity_profiles: tuple[str, ...] | None = None
        guard_disabled = os.environ.get("ROBOT_GUARD_ENABLED", "").lower() in {"0", "false", "no", "off"}
        if self.guard is None and not guard_disabled and (
            config.guard_enabled
            or config.guard_provider_url
            or config.codex_provider_url
            or config.guard_model
            or os.environ.get("ROBOT_GUARD_ENABLED", "").lower() in {"1", "true", "yes", "on"}
        ):
            provider_url = config.guard_provider_url or config.codex_provider_url
            model = config.guard_model or config.codex_model
            api_key = config.guard_api_key or config.codex_api_key
            self.guard = SecurityGuard(model=model, provider_url=provider_url, api_key=api_key)

    def _container_env(self, timeout_seconds: int, files: list[dict] | None = None) -> dict[str, str]:
        # Auto-select: if configured model is a vision variant but no file requires
        # vision, use the plain flash model instead (faster, same pricing tier).
        model = self.config.codex_model
        if model == "deepseek-v4-flash-vision-exp" and not _needs_vision(files or []):
            model = "deepseek-v4-flash"
        values = {
            self.config.codex_api_key_env: self.config.codex_api_key,
            "CODEX_API_KEY": self.config.codex_api_key,
            "CODEX_MODEL": model,
            "CODEX_PROVIDER_ENV_KEY": self.config.codex_api_key_env,
            "ROBOT_RUN_TIMEOUT_SECONDS": str(timeout_seconds),
            # Pre-installed virtual environment auto-sourcing (R3.1, R3.2):
            # Prioritize the analysis virtualenv binary directory so child processes
            # and subagent tool invocations have immediate access to preinstalled
            # analysis packages (rosbags, mcap, numpy, pandas, pypdf, h5py, etc.).
            "VIRTUAL_ENV": "/opt/analysis-venv",
            "PATH": "/opt/analysis-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        if self.config.codex_provider_url:
            values["CODEX_PROVIDER_URL"] = self.config.codex_provider_url
        return values


    def _checkpoint(self, job_id: str, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise JobTimedOut("sandbox time limit reached during preparation")
        if self.api.get_job(job_id).get("cancel_requested"):
            raise JobCancelled("cancelled during preparation")

    def _stage_job(self, job: dict[str, Any], root: Path, deadline: float) -> None:
        self._write_local_tree(self.config.runtime_dir, root, str(job["id"]), deadline)
        (root / "inputs").mkdir(parents=True, exist_ok=True)
        if self.config.wiki_dir:
            self._write_local_tree(self.config.wiki_dir, root / "wiki", str(job["id"]), deadline, MAX_WIKI_FILE_BYTES)
        files = job.get("files", [])
        if not isinstance(files, list): raise WorkerError("job file manifest is invalid")
        for item in files:
            self._checkpoint(str(job["id"]), deadline)
            if not isinstance(item, dict): raise WorkerError("job file manifest has an invalid entry")
            file_id, name = str(item.get("id", "")), str(item.get("name", ""))
            expected_size, expected_hash = item.get("size"), str(item.get("sha256", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id) or not name or not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash): raise WorkerError("job file manifest is missing id, name, size, or sha256")
            staged = root / ".downloads" / file_id; staged.parent.mkdir(parents=True, exist_ok=True)
            received_size, received_hash = self.api.download_to(str(job["id"]), file_id, staged)
            if received_size != expected_size or received_hash != expected_hash.lower(): raise WorkerError(f"file integrity check failed for artifact {file_id}")
            chunks: list[str] = []; upload = root / ".upload" / file_id; upload.mkdir(parents=True, exist_ok=True)
            with staged.open("rb") as stream:
                for index, chunk in enumerate(iter(lambda: stream.read(TRANSFER_CHUNK_BYTES), b"")):
                    self._checkpoint(str(job["id"]), deadline); relative = f".upload/{file_id}/{index:06d}"; (root / relative).write_bytes(chunk); chunks.append(relative)
            staged.unlink(); item["local_path"], item["staging_chunks"] = _input_path(file_id, name), chunks
        (root / "job.json").write_text(json.dumps(job, separators=(",", ":")))
        if job.get("scenario_state"):
            (root / "scenario_state.json").write_text(json.dumps(job["scenario_state"], ensure_ascii=False, separators=(",", ":")))
        if job.get("customer_answers"):
            (root / "customer_answers.json").write_text(json.dumps(job["customer_answers"], ensure_ascii=False, separators=(",", ":")))

    def _write_local_tree(self, source: Path, destination: Path, job_id: str, deadline: float, maximum: int | None = None) -> None:
        if not source.is_dir(): raise WorkerError(f"runtime directory is unavailable: {source}")
        root = source.resolve()
        for path in sorted(root.rglob("*")):
            self._checkpoint(job_id, deadline)
            if not path.is_file() or path.is_symlink(): continue
            if maximum is not None and path.stat().st_size > maximum: raise WorkerError(f"wiki file exceeds {maximum} byte transfer cap: {path.name}")
            relative = path.resolve().relative_to(root)
            if "__pycache__" in relative.parts or relative.suffix == ".pyc": continue
            target = destination / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(path.read_bytes())

    def _event(self, job_id: str, kind: str, message: str, agent: str = "worker", subagent: dict[str, Any] | None = None) -> None:
        if subagent is None:
            self.api.event(job_id, kind, _safe_text(message, self._secrets), agent)
        else:
            self.api.event(job_id, kind, _safe_text(message, self._secrets), agent, subagent)

    def _timeout_for(self, job: dict[str, Any]) -> int:
        """The API may make a short approved benchmark; large jobs get 1.5x allowance (up to 2700s/45m)."""
        plan = job.get("resource_plan")
        is_large = isinstance(plan, dict) and plan.get("profile") == "large"
        max_allowed = int(1800 * 1.5) if is_large else 1800
        default_timeout = int(self.config.timeout_seconds * 1.5) if is_large else self.config.timeout_seconds

        requested = job.get("timeout_seconds")
        if requested is None:
            requested = default_timeout
        elif not isinstance(requested, int):
            raise WorkerError("job timeout_seconds must be an integer")
        elif is_large and requested == 1800:
            requested = int(1800 * 1.5)

        if job.get("benchmark"):
            requested = min(requested, 600)
        return max(1, min(requested, max_allowed))

    def _activity(self, container: DockerJob, job_id: str, seen: int) -> int:
        reader = getattr(self.runtime, "read_activity", None)
        if callable(reader):
            page = reader(container, seen)
            if page is not None:
                text, next_offset = page
                records = []
                for line in text.splitlines():
                    event = json.loads(line)
                    kind = event.get("kind")
                    if kind not in {"debug", "agent_started", "subagent"}:
                        continue
                    record = {"kind": kind, "agent": "subagent" if event.get("agent") == "subagent" else "codex",
                              "message": _safe_text(event.get("message", ""), self._secrets, preserve=True)}
                    if event.get("subagent") is not None:
                        record["subagent"] = validate_subagent(event["subagent"])
                    records.append(record)
                for start in range(0, len(records), 100):
                    self.api.events_batch(job_id, records[start:start + 100])
                return next_offset
        # Compatibility with workers still using the older, filtered image.
        try:
            text = self.runtime.copy_out_text(container, "/workspace/activity.jsonl") or ""
        except Exception:
            return seen
        newest = seen
        for line in text.splitlines()[-256:]:
            try:
                event = json.loads(line)
                sequence = event.get("seq")
                if not isinstance(sequence, int) or sequence <= seen:
                    continue
                # Safe agent whitelist for activity event streaming (R2.3).
                # Must match SAFE_AGENTS in sandbox/run_codex.py to prevent downgrading subagent events to orchestrator.
                agent = event.get("agent") if event.get("agent") in {"codex", "log_investigator", "telemetry_investigator", "evidence_reviewer", "subagent"} else "codex"
                message = event.get("message") if isinstance(event.get("message"), str) else "Codex activity."
                activity_kind = event.get("kind")
                if activity_kind not in {None, "message", "tool", "subagent", "lifecycle"}:
                    continue
                child: dict[str, Any] | None = None
                if activity_kind == "tool":
                    safe_prefixes = (
                        "Codex tool execution",
                        "Inspecting",
                        "Inspected",
                        "Checking",
                        "Checked",
                        "Analyzing",
                        "Analyzed",
                        "Scanning",
                        "Scanned",
                    )
                    if not any(message.startswith(p) for p in safe_prefixes):
                        message = "Codex tool execution update."
                elif activity_kind == "subagent":
                    try:
                        child = validate_subagent(event.get("subagent"))
                    except ValueError:
                        child = None
                    message = "Subagent lifecycle update." if child else f"Subagent {agent} update."
                public = _safe_text(message, self._secrets)[:800]
                if public:
                    self._event(job_id, "subagent" if activity_kind == "subagent" and child else "progress", public, agent, child)
                newest = max(newest, sequence)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return newest

    def _unconfirmed_kill(self, job_id: str) -> None:
        self._event(job_id, "warning", "Docker cleanup was not confirmed; keeping the job active until lease expiry.")
        raise CleanupUnconfirmed("Docker cleanup was not confirmed; worker stops before claiming another job")

    def _result(self, container: DockerJob) -> dict[str, Any]:
        raw = self.runtime.copy_out_text(container, "/workspace/result.json", 96 * 1024)
        if raw is None: raise WorkerError("Docker runner result is unavailable")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise WorkerError("sandbox runner result is invalid")
        return value

    def run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        timeout_seconds = self._timeout_for(job)
        if job.get("cancel_requested"):
            self.api.finish(job_id, "cancelled", "Cancelled before sandbox execution.", None, {"cost_source": "unknown"})
            return

        # R1: Pre-execution security inspection pipeline
        # Inspect user prompt and non-log attachments (up to 32 KiB) before creating sandbox or calling Codex.
        if self.guard is not None and job.get("job_type") != "grill":
            with tempfile.TemporaryDirectory(prefix="robot-guard-") as guard_temp:
                non_log_staged: list[tuple[str, Path]] = []
                for index, item in enumerate(job.get("files", [])):
                    name = str(item.get("name", ""))
                    if is_non_log_attachment(name):
                        file_id = str(item.get("id", ""))
                        safe_file_id = f"{index}_{re.sub(r'[^A-Za-z0-9_-]', '_', file_id)[:60]}"
                        staged = Path(guard_temp, safe_file_id)
                        try:
                            self.api.download_to(job_id, file_id, staged)
                            non_log_staged.append((name, staged))
                        except Exception:
                            pass

                supports_vision_fn = getattr(self.guard, "supports_vision", None)
                include_images = supports_vision_fn() if callable(supports_vision_fn) else False
                attachments_text, images = extract_non_log_attachments(
                    non_log_staged,
                    max_total_bytes=32 * 1024,
                    include_images=include_images,
                )

                try:
                    result = self.guard.inspect(
                        description=str(job.get("description") or ""),
                        attachments_text=attachments_text,
                        image_attachments=images,
                    )
                except (GuardError, Exception) as exc:
                    # R3: Safe failure handling (fail closed)
                    # If guard LLM call fails after retry, terminate without sandbox provisioning.
                    try:
                        self._event(job_id, "warning", f"Security pre-check failed: {exc}", "guard")
                    except Exception:
                        pass
                    self.api.finish(job_id, "failed", "Security pre-check failed", 0.0, {"cost_source": "unknown", "security_verdict": "ERROR"})
                    return

                # R2.3: INJECTION verdict -> immediate hard stop, mark failed, zero sandboxes, budget not decremented
                if result.verdict == GuardVerdict.INJECTION:
                    try:
                        self._event(job_id, "warning", "Prompt injection detected in inputs", "guard")
                    except Exception:
                        pass
                    self.api.finish(job_id, "failed", "Prompt injection detected in inputs", 0.0, {"cost_source": "unknown", "security_verdict": "INJECTION"})
                    return
                # R2.2: SUSPICIOUS verdict -> sanitize prompt, record both in DB, emit warning, dispatch sanitized prompt to Codex
                elif result.verdict == GuardVerdict.SUSPICIOUS:
                    original = str(job.get("description", ""))
                    sanitized = result.sanitized_description or "Diagnose reported incident from available logs."
                    job["original_description"] = original
                    job["sanitized_description"] = sanitized
                    job["description"] = sanitized
                    if hasattr(self.api, "sanitize"):
                        try:
                            self.api.sanitize(job_id, original, sanitized)
                        except Exception as exc:
                            self._event(job_id, "warning", f"Failed to record sanitized prompt in store: {exc}", "guard")
                    self._event(job_id, "warning", "User incident prompt was refined for security.", "guard")
                # R2.1: CLEAN verdict -> proceed normally into sandbox execution

        container: DockerJob | None = None
        started = time.monotonic()
        deadline = started + timeout_seconds
        try:
            plan = job.get("resource_plan") or {"profile": "small", **PROFILES["small"]}
            profile = plan.get("profile") if isinstance(plan, dict) else None
            if plan is not None and (profile not in PROFILES or
                                     plan.get("cpu_milli") != PROFILES[profile]["cpu_milli"] or
                                     plan.get("disk_mb") != PROFILES[profile]["disk_mb"] or
                                     not (min(PROFILES[profile]["memory_mb"], 7168) <= plan.get("memory_mb", 0) <= max(PROFILES[profile]["memory_mb"], 8192))):
                raise WorkerError("Job resource plan does not match a supported profile")
            if not isinstance(plan, dict): raise WorkerError("job resource plan is invalid")
            capacity = self.runtime.available_capacity({"cpu_milli": self.config.cpu_milli, "memory_mb": self.config.memory_mb, "disk_mb": self.config.disk_mb})
            if any(plan[key] > capacity.get(key, 0) for key in ("cpu_milli", "memory_mb", "disk_mb")): raise WorkerError("Docker capacity changed after claim")
            container = self.runtime.create(job_id, plan, self._container_env(timeout_seconds, job.get("files") or []))
            with tempfile.TemporaryDirectory(prefix="robot-docker-stage-") as directory:
                self._stage_job(job, Path(directory), deadline); self.runtime.copy_in(container, Path(directory))
            self._checkpoint(job_id, deadline); self.runtime.start(container)
            self._event(job_id, "progress", "Docker container created; Codex execution started.")
            command = self.runtime.exec_runner(container)
            seen_activity = 0
            while command.poll() is None:
                if time.monotonic() - started >= timeout_seconds:
                    if not self.runtime.remove(container):
                        self._unconfirmed_kill(job_id)
                        return
                    container = None; self.api.finish(job_id, "failed", "Docker time limit reached.", None, {"runtime_seconds": timeout_seconds, "cost_source": "unknown"})
                    return
                state = self.api.get_job(job_id)
                if state.get("cancel_requested"):
                    if not self.runtime.remove(container):
                        self._unconfirmed_kill(job_id)
                        return
                    container = None; self.api.finish(job_id, "cancelled", "Cancelled during Docker execution.", None, {"runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown"})
                    return
                if not self.runtime.disk_healthy(container, plan["disk_mb"]): raise WorkerError("Docker workspace or host disk limit reached")
                seen_activity = self._activity(container, job_id, seen_activity)
                self._event(job_id, "heartbeat", "Docker execution remains active.")
                time.sleep(min(2, self.config.poll_seconds))
            # The runner can flush lifecycle records immediately before exit.
            while (next_activity := self._activity(container, job_id, seen_activity)) != seen_activity:
                seen_activity = next_activity
                self._checkpoint(job_id, deadline)
            if command.returncode: raise WorkerError("Docker runner did not complete successfully")
            self._checkpoint(job_id, deadline)
            if not self.runtime.disk_healthy(container, plan["disk_mb"]): raise WorkerError("Docker workspace or host disk limit reached")

            result = self._result(container)
            analysis_context = validate_context(result.get("analysis_context"), lambda text: _safe_text(text, self._secrets))
            metrics = dict(result.get("metrics") or {})
            runtime_s = round(time.monotonic() - started, 3)
            codex_usage = metrics.get("codex_usage") or {}
            metrics.update({
                "runtime_seconds": runtime_s,
                "model": self.config.codex_model,
                # Preserve raw token counts for display/audit
                "input_tokens":  codex_usage.get("input_tokens",  0),
                "output_tokens": codex_usage.get("output_tokens", 0),
            })
            metrics["docker_workspace_bytes"] = self.runtime.workspace_bytes(container)
            status = "completed" if result.get("status") == "completed" else "failed"
            if job.get("job_type") == "grill":
                report = json.dumps(result, ensure_ascii=False) if isinstance(result.get("scenario_state"), dict) or isinstance(result.get("report"), dict) else (result.get("report") or "Grill run completed")
            else:
                report = _safe_report(result.get("report") or "Docker run produced no final report.", self._secrets)
            if analysis_context:
                self.api.save_analysis_context(job_id, analysis_context)
            if not self.runtime.remove(container):
                self._unconfirmed_kill(job_id)
                return
            container = None
            self.api.finish(job_id, status, report, 0.0, metrics, analysis_context)
        except JobCancelled:
            if container is not None and not self.runtime.remove(container):
                self._unconfirmed_kill(job_id)
                return
            self.api.finish(job_id, "cancelled", "Cancelled during Docker preparation.", None, {"runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown"})
        except JobTimedOut:
            if container is not None and not self.runtime.remove(container):
                self._unconfirmed_kill(job_id)
                return
            self.api.finish(job_id, "failed", "Docker time limit reached during preparation.", None, {"runtime_seconds": timeout_seconds, "cost_source": "unknown"})
        except CleanupUnconfirmed:
            raise
        except Exception as exc:
            if isinstance(exc, DockerCleanupError):
                self._unconfirmed_kill(job_id)
                return
            if container is not None and not self.runtime.remove(container):
                self._unconfirmed_kill(job_id)
                return
            err_text = str(exc)
            if "deadline_exceeded" in err_text.lower() or "context deadline exceeded" in err_text.lower() or (time.monotonic() - started >= timeout_seconds):
                msg = f"Docker time limit reached ({int(timeout_seconds)}s timeout exceeded)."
            else:
                msg = f"Worker failed: {exc}"
            self.api.finish(job_id, "failed", _safe_text(msg, self._secrets), None, {
                "runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown",
            })
        finally:
            if container is not None:
                self.runtime.remove(container)

    def run_once(self) -> bool:
        capacity = self.runtime.available_capacity({"cpu_milli": self.config.cpu_milli, "memory_mb": self.config.memory_mb, "disk_mb": self.config.disk_mb})
        fitting = tuple(name for name, plan in PROFILES.items()
                        if all(capacity.get(key, 0) >= needed for key, needed in plan.items()))
        if fitting != self._capacity_profiles:
            available = ", ".join(f"{key}={value}" for key, value in capacity.items())
            minimum = ", ".join(f"{key}={value}" for key, value in PROFILES["small"].items())
            message = (f"worker capacity: {available}; fits={','.join(fitting) or 'none'}; "
                       f"Docker data={self.runtime.data_dir}; disk reserve={self.config.disk_reserve_mb} MiB. "
                       f"Small requires {minimum}. "
                       "Capacity includes configured maxima and host reserves; only fitting jobs can be claimed.")
            print(_safe_text(message, self._secrets), file=sys.stderr, flush=True)
            self._capacity_profiles = fitting
        if not fitting:
            return False
        job = self.api.claim(capacity)
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_forever(self) -> None:
        self.runtime.preflight(); self.runtime.cleanup_orphans()
        while True:
            if not self.run_once(): time.sleep(self.config.poll_seconds)


def main() -> int:
    try:
        config = WorkerConfig.from_env()
        lock = Path(tempfile.gettempdir(), f"robot-worker-{config.worker_id}.lock").open("w")
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise WorkerError("another worker process already owns this ROBOT_WORKER_ID")
        DockerWorker(config).run_forever()
    except WorkerError as exc:
        print(f"worker refused to start: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
