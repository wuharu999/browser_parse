"""Fail-closed worker that runs a claimed job only inside CubeSandbox.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise WorkerError(f"missing required environment variable: {name}")
    return value or ""


def _safe_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Bound public event/report text and remove known credential values."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[redacted]", text)
    # Keep Markdown paragraphs/lists readable; only normalize horizontal space.
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()[:12_000]


def _input_path(file_id: str, name: str) -> str:
    """Make a flat, non-traversing archive-preserving path for a manifest file."""
    leaf = PurePosixPath(name.replace("\\", "/")).name
    suffix = "".join(PurePosixPath(leaf).suffixes[-2:])[:24]
    suffix = re.sub(r"[^A-Za-z0-9.]", "_", suffix)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(file_id))[:80] or "file"
    return f"inputs/{safe_id}{suffix}"


@dataclass(frozen=True)
class WorkerConfig:
    api_url: str
    worker_token: str
    cube_api_url: str
    cube_api_key: str
    cube_template_id: str
    cube_proxy_node_ip: str | None
    codex_model: str
    codex_provider_url: str | None
    codex_api_key_env: str
    codex_api_key: str
    timeout_seconds: int
    poll_seconds: float
    runtime_dir: Path
    wiki_dir: Path | None
    parallel: int

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        key_env = _env("ROBOT_CODEX_API_KEY_ENV", "OPENAI_API_KEY")
        timeout = int(_env("ROBOT_JOB_TIMEOUT_SECONDS", "1800"))
        if not 1 <= timeout <= 1800:
            raise WorkerError("ROBOT_JOB_TIMEOUT_SECONDS must be between 1 and 1800")
        runtime = Path(_env("ROBOT_SANDBOX_RUNTIME", str(Path(__file__).resolve().parents[1] / "sandbox" / "runtime")))
        wiki = os.environ.get("ROBOT_WIKI_DIR")
        default_wiki = Path(__file__).resolve().parents[1] / "knowledge" / "wiki"
        return cls(
            api_url=_env("ROBOT_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            worker_token=_env("ROBOT_WORKER_TOKEN", required=True),
            cube_api_url=_env("CUBE_API_URL", required=True),
            cube_api_key=_env("CUBE_API_KEY", required=True),
            cube_template_id=_env("CUBE_TEMPLATE_ID", required=True),
            cube_proxy_node_ip=os.environ.get("CUBE_PROXY_NODE_IP") or None,
            codex_model=_env("ROBOT_CODEX_MODEL", "gpt-5.6-luna"),
            codex_provider_url=os.environ.get("ROBOT_CODEX_PROVIDER_URL") or None,
            codex_api_key_env=key_env,
            codex_api_key=_env(key_env, required=True),
            timeout_seconds=timeout,
            poll_seconds=max(0.25, float(_env("ROBOT_POLL_SECONDS", "3"))),
            runtime_dir=runtime,
            wiki_dir=Path(wiki).resolve() if wiki else (default_wiki.resolve() if default_wiki.is_dir() else None),
            parallel=max(1, min(2, int(_env("ROBOT_WORKER_PARALLEL", "2")))),
        )


class WorkerApi:
    """The private worker API.  It never emits credentials or command output."""

    def __init__(self, base_url: str, token: str, timeout: float = 30) -> None:
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def _request(self, method: str, path: str, body: object | None = None, *, raw: bool = False) -> Any:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self.token}"}
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

    def claim(self) -> dict[str, Any] | None:
        response = self._request("POST", "/api/worker/claim", {})
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
        request = Request(f"{self.base_url}/api/worker/jobs/{job_id}/files/{artifact_id}", headers={"Authorization": f"Bearer {self.token}"})
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

    def event(self, job_id: str, kind: str, message: str, agent: str = "worker") -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/events", {
            "kind": kind, "agent": agent, "message": message,
        })

    def finish(self, job_id: str, status: str, report: str, cost_usd: float | None, metrics: dict[str, Any]) -> None:
        self._request("POST", f"/api/worker/jobs/{job_id}/finish", {
            "status": status, "report": report, "cost_usd": cost_usd, "metrics": metrics,
        })


class CubeWorker:
    """Serial executor.  API queue admission is the global two-job limiter."""

    def __init__(self, config: WorkerConfig, api: WorkerApi | None = None, sandbox_class: Any = None) -> None:
        self.config = config
        self.api = api or WorkerApi(config.api_url, config.worker_token)
        self._sandbox_class = sandbox_class
        self._secrets = (config.worker_token, config.cube_api_key, config.codex_api_key)

    def _cube(self, timeout_seconds: int) -> tuple[Any, Any]:
        if self._sandbox_class is not None:
            return self._sandbox_class, self._config_object(timeout_seconds)
        try:
            from cubesandbox import Config, Sandbox
        except ImportError as exc:  # installation is an explicit deployment prerequisite
            raise WorkerError("cubesandbox SDK is not installed; refusing host execution") from exc
        values: dict[str, Any] = {
            "api_url": self.config.cube_api_url,
            "api_key": self.config.cube_api_key,
            "template_id": self.config.cube_template_id,
            "timeout": timeout_seconds,
            # The e2b-connect code path keeps a distinct request timeout.
            "request_timeout": timeout_seconds + 30,
        }
        if self.config.cube_proxy_node_ip:
            values["proxy_node_ip"] = self.config.cube_proxy_node_ip
        return Sandbox, Config(**values)

    def _config_object(self, timeout_seconds: int) -> Any:
        """Tests can replace this with a harmless object; production imports Config."""
        return {
            "api_url": self.config.cube_api_url,
            "api_key": self.config.cube_api_key,
            "template_id": self.config.cube_template_id,
            "proxy_node_ip": self.config.cube_proxy_node_ip,
            "timeout": timeout_seconds,
            "request_timeout": timeout_seconds + 30,
        }

    def _sandbox_env(self, timeout_seconds: int) -> dict[str, str]:
        values = {
            self.config.codex_api_key_env: self.config.codex_api_key,
            "CODEX_API_KEY": self.config.codex_api_key,
            "CODEX_MODEL": self.config.codex_model,
            "CODEX_PROVIDER_ENV_KEY": self.config.codex_api_key_env,
            "ROBOT_RUN_TIMEOUT_SECONDS": str(timeout_seconds),
        }
        if self.config.codex_provider_url:
            values["CODEX_PROVIDER_URL"] = self.config.codex_provider_url
        return values

    @staticmethod
    def _write_tree(sandbox: Any, source: Path, destination: str) -> None:
        if not source.is_dir():
            raise WorkerError(f"sandbox runtime directory is unavailable: {source}")
        root = source.resolve()
        made: set[str] = {destination}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.resolve().relative_to(root)
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            remote = PurePosixPath(destination, *relative.parts)
            if str(remote.parent) not in made:
                sandbox.files.make_dir(str(remote.parent)); made.add(str(remote.parent))
            sandbox.files.write(str(remote), path.read_bytes())

    def _checkpoint(self, job_id: str, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise JobTimedOut("sandbox time limit reached during preparation")
        if self.api.get_job(job_id).get("cancel_requested"):
            raise JobCancelled("cancelled during preparation")

    def _upload_inputs(self, sandbox: Any, job: dict[str, Any], deadline: float) -> None:
        files = job.get("files", [])
        if not isinstance(files, list):
            raise WorkerError("job file manifest is invalid")
        sandbox.files.make_dir("/workspace/inputs")
        sandbox.files.make_dir("/workspace/.upload")
        with tempfile.TemporaryDirectory(prefix="robot-worker-") as temporary:
          for item in files:
            self._checkpoint(str(job["id"]), deadline)
            if not isinstance(item, dict):
                raise WorkerError("job file manifest has an invalid entry")
            file_id, name = str(item.get("id", "")), str(item.get("name", ""))
            expected_size, expected_hash = item.get("size"), str(item.get("sha256", ""))
            if not file_id or not name or not isinstance(expected_size, int) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
                raise WorkerError("job file manifest is missing id, name, size, or sha256")
            staged = Path(temporary, file_id)
            received_size, received_hash = self.api.download_to(str(job["id"]), file_id, staged)
            if received_size != expected_size or received_hash != expected_hash.lower():
                raise WorkerError(f"file integrity check failed for artifact {file_id}")
            local_path = _input_path(file_id, name)
            item["local_path"] = local_path
            chunks: list[str] = []
            sandbox.files.make_dir(f"/workspace/.upload/{file_id}")
            with staged.open("rb") as stream:
                for index, chunk in enumerate(iter(lambda: stream.read(TRANSFER_CHUNK_BYTES), b"")):
                    self._checkpoint(str(job["id"]), deadline)
                    chunk_path = f".upload/{file_id}/{index:06d}"
                    sandbox.files.write(f"/workspace/{chunk_path}", chunk)
                    chunks.append(chunk_path)
            item["staging_chunks"] = chunks

    def _upload_wiki(self, sandbox: Any, job_id: str, deadline: float) -> None:
        if not self.config.wiki_dir:
            return
        if not self.config.wiki_dir.is_dir():
            raise WorkerError(f"ROBOT_WIKI_DIR is unavailable: {self.config.wiki_dir}")
        root = self.config.wiki_dir.resolve()
        sandbox.files.make_dir("/workspace/wiki")
        made = {"/workspace/wiki"}
        for path in sorted(root.rglob("*")):
            self._checkpoint(job_id, deadline)
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > MAX_WIKI_FILE_BYTES:
                raise WorkerError(f"wiki file exceeds {MAX_WIKI_FILE_BYTES} byte transfer cap: {path.name}")
            relative = path.resolve().relative_to(root)
            remote = PurePosixPath("/workspace/wiki", *relative.parts)
            if str(remote.parent) not in made:
                sandbox.files.make_dir(str(remote.parent)); made.add(str(remote.parent))
            sandbox.files.write(str(remote), path.read_bytes())

    def _event(self, job_id: str, kind: str, message: str, agent: str = "worker") -> None:
        self.api.event(job_id, kind, _safe_text(message, self._secrets), agent)

    def _timeout_for(self, job: dict[str, Any]) -> int:
        """The API may make a short approved benchmark; no job can exceed 30 min."""
        requested = job.get("timeout_seconds", self.config.timeout_seconds)
        if not isinstance(requested, int):
            raise WorkerError("job timeout_seconds must be an integer")
        if job.get("benchmark"):
            requested = min(requested, 600)
        return max(1, min(requested, 1800))

    @staticmethod
    def _kill(sandbox: Any) -> bool:
        for _ in range(3):
            try:
                sandbox.kill()
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _activity(self, sandbox: Any, job_id: str, seen: int) -> int:
        try:
            raw = sandbox.files.read("/workspace/activity.jsonl")
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        except Exception:
            return seen
        newest = seen
        for line in text.splitlines()[-256:]:
            try:
                event = json.loads(line)
                sequence = event.get("seq")
                if not isinstance(sequence, int) or sequence <= seen:
                    continue
                agent = event.get("agent") if event.get("agent") in {"codex", "log_investigator", "evidence_reviewer"} else "codex"
                message = event.get("message") if isinstance(event.get("message"), str) else "Codex activity."
                self._event(job_id, "progress", message[:200], agent)
                newest = max(newest, sequence)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return newest

    def _unconfirmed_kill(self, job_id: str) -> None:
        # Do not release the API admission slot while an isolated VM may live.
        self._event(job_id, "warning", "Sandbox cleanup was not confirmed; keeping the job active until lease expiry.")

    def _result(self, sandbox: Any) -> dict[str, Any]:
        raw = sandbox.files.read("/workspace/result.json")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
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
        sandbox = None
        executor: ThreadPoolExecutor | None = None
        started = time.monotonic()
        deadline = started + timeout_seconds
        try:
            Sandbox, cube_config = self._cube(timeout_seconds)
            sandbox = Sandbox.create(config=cube_config, env_vars=self._sandbox_env(timeout_seconds), timeout=timeout_seconds)
            self._event(job_id, "progress", "Sandbox created; preparing evidence.")
            sandbox.files.make_dir("/workspace")
            self._write_tree(sandbox, self.config.runtime_dir, "/workspace")
            self._checkpoint(job_id, deadline)
            self._upload_wiki(sandbox, job_id, deadline)
            self._upload_inputs(sandbox, job, deadline)
            self._checkpoint(job_id, deadline)
            sandbox.files.write("/workspace/job.json", json.dumps(job, separators=(",", ":")))
            self._event(job_id, "progress", "Evidence transferred; Codex execution started.")

            executor = ThreadPoolExecutor(max_workers=1)
            command = executor.submit(sandbox.commands.run, "python3 /opt/sandbox/run_codex.py /workspace/job.json", cwd="/workspace", timeout=max(1, deadline - time.monotonic()))
            seen_activity = 0
            while not command.done():
                if time.monotonic() - started >= timeout_seconds:
                    killed = self._kill(sandbox)
                    executor.shutdown(wait=False, cancel_futures=True)
                    if not killed:
                        self._unconfirmed_kill(job_id)
                        return
                    self.api.finish(job_id, "failed", "Sandbox time limit reached.", None, {"runtime_seconds": timeout_seconds, "cost_source": "unknown"})
                    return
                state = self.api.get_job(job_id)
                if state.get("cancel_requested"):
                    killed = self._kill(sandbox)
                    executor.shutdown(wait=False, cancel_futures=True)
                    if not killed:
                        self._unconfirmed_kill(job_id)
                        return
                    self.api.finish(job_id, "cancelled", "Cancelled during sandbox execution.", None, {"runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown"})
                    return
                seen_activity = self._activity(sandbox, job_id, seen_activity)
                self._event(job_id, "progress", "Sandbox execution remains active.")
                time.sleep(min(15, self.config.poll_seconds))
            command.result()  # only command status is consumed; never publish stdout/stderr
            executor.shutdown(wait=True)

            result = self._result(sandbox)
            metrics = dict(result.get("metrics") or {})
            metrics.update({"runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown"})
            info = sandbox.get_info()
            for name in ("cpu_milli", "memory_mb", "disk_size_mb"):
                value = getattr(info, name, None)
                if value is not None:
                    metrics[f"sandbox_{name}"] = value
            status = "completed" if result.get("status") == "completed" else "failed"
            report = _safe_text(result.get("report") or "Sandbox run produced no final report.", self._secrets)
            if not self._kill(sandbox):
                self._unconfirmed_kill(job_id)
                return
            sandbox = None
            self.api.finish(job_id, status, report, None, metrics)
        except JobCancelled:
            if sandbox is not None and not self._kill(sandbox):
                self._unconfirmed_kill(job_id)
                return
            self.api.finish(job_id, "cancelled", "Cancelled during sandbox preparation.", None, {"runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown"})
        except JobTimedOut:
            if sandbox is not None and not self._kill(sandbox):
                self._unconfirmed_kill(job_id)
                return
            self.api.finish(job_id, "failed", "Sandbox time limit reached during preparation.", None, {"runtime_seconds": timeout_seconds, "cost_source": "unknown"})
        except Exception as exc:
            if sandbox is not None and not self._kill(sandbox):
                self._unconfirmed_kill(job_id)
                return
            self.api.finish(job_id, "failed", _safe_text(f"Worker failed: {exc}", self._secrets), None, {
                "runtime_seconds": round(time.monotonic() - started, 3), "cost_source": "unknown",
            })
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            if sandbox is not None:
                self._kill(sandbox)

    def run_once(self) -> bool:
        job = self.api.claim()
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_forever(self) -> None:
        with ThreadPoolExecutor(max_workers=self.config.parallel) as executor:
            active: set[Any] = set()
            while True:
                active = {future for future in active if not future.done()}
                claimed = False
                while len(active) < self.config.parallel:
                    job = self.api.claim()
                    if job is None:
                        break
                    active.add(executor.submit(self.run_job, job))
                    claimed = True
                if not claimed:
                    time.sleep(self.config.poll_seconds)


def main() -> int:
    try:
        CubeWorker(WorkerConfig.from_env()).run_forever()
    except WorkerError as exc:
        print(f"worker refused to start: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
