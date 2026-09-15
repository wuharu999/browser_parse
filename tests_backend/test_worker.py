from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from backend.docker_runtime import DockerCleanupError, DockerJob, DockerRuntime
from backend.resources import PROFILES
from backend.worker import CleanupUnconfirmed, DockerWorker, WorkerApi, WorkerConfig, WorkerError, _input_path


class FinishedProcess:
    returncode = 0

    def poll(self) -> int:
        return self.returncode


class FakeDockerRuntime:
    def __init__(self) -> None:
        self.data_dir = Path("/fake/docker-root")
        self.created: list[tuple[str, dict, dict]] = []
        self.copied: dict[str, bytes] = {}
        self.started: list[DockerJob] = []
        self.removed: list[DockerJob] = []
        self.remove_result = True
        self.capacity = {"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 24576}
        self.result = json.dumps({"status": "completed", "report": "Evidence-backed conclusion.", "metrics": {}})
        self.activity = ""
        self.disk_ok = True
        self.raise_on_create: Exception | None = None

    def available_capacity(self, maximum: dict[str, int]) -> dict[str, int]:
        return {key: min(maximum[key], self.capacity[key]) for key in maximum}

    def create(self, job_id: str, plan: dict, env: dict) -> DockerJob:
        if self.raise_on_create:
            raise self.raise_on_create
        self.created.append((job_id, plan, env))
        return DockerJob("container-" + job_id, "volume-" + job_id)

    def copy_in(self, _job: DockerJob, source: Path) -> None:
        for path in source.rglob("*"):
            if path.is_file():
                self.copied[str(path.relative_to(source))] = path.read_bytes()

    def start(self, job: DockerJob) -> None:
        self.started.append(job)

    def exec_runner(self, _job: DockerJob) -> FinishedProcess:
        return FinishedProcess()

    def copy_out_text(self, _job: DockerJob, path: str, _maximum: int = 256 * 1024) -> str | None:
        if path == "/workspace/result.json":
            return self.result
        if path == "/workspace/activity.jsonl":
            return self.activity
        raise AssertionError(path)

    def disk_healthy(self, _job: DockerJob, _limit_mb: int) -> bool:
        return self.disk_ok

    def workspace_bytes(self, _job: DockerJob) -> int:
        return 42

    def remove(self, job: DockerJob) -> bool:
        self.removed.append(job)
        return self.remove_result


class FakeApi:
    def __init__(self, job: dict | None, blobs: dict[str, bytes] | None = None) -> None:
        self.job = job
        self.blobs = blobs or {}
        self.finished: list[dict] = []
        self.events: list[dict] = []
        self.cancel_requested = False
        self.claimed_capacity: dict | None = None

    def claim(self, capacity: dict) -> dict | None:
        self.claimed_capacity = capacity
        return self.job

    def download_to(self, _job_id: str, artifact_id: str, destination: Path) -> tuple[int, str]:
        data = self.blobs[artifact_id]
        destination.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def get_job(self, _job_id: str) -> dict:
        return {"cancel_requested": self.cancel_requested}

    def event(self, _job_id: str, kind: str, message: str, agent: str = "worker") -> None:
        self.events.append({"kind": kind, "message": message, "agent": agent})

    def finish(self, _job_id: str, status: str, report: str, cost_usd, metrics: dict) -> None:
        self.finished.append({"status": status, "report": report, "cost_usd": cost_usd, "metrics": metrics})


class DockerWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        runtime_dir = Path(self.temporary.name, "runtime")
        runtime_dir.mkdir()
        (runtime_dir / "MAIN_PROMPT.md").write_text("safe runtime asset")
        self.config = WorkerConfig(
            api_url="http://127.0.0.1:8000",
            worker_token="worker-token",
            worker_id="worker-a",
            docker_image="sha256:" + "a" * 64,
            docker_network="robot-analysis-jobs",
            egress_proxy_url="http://robot-egress-proxy:3128",
            docker_data_dir=Path(self.temporary.name),
            codex_model="gpt-5.6-luna",
            codex_provider_url=None,
            codex_api_key_env="OPENAI_API_KEY",
            codex_api_key="model-key",
            timeout_seconds=1800,
            poll_seconds=0.01,
            runtime_dir=runtime_dir,
            wiki_dir=None,
            cpu_milli=4000,
            memory_mb=8192,
            disk_mb=24576,
            disk_reserve_mb=1024,
            guard_enabled=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def worker(self, job: dict | None, blobs: dict[str, bytes] | None = None) -> tuple[DockerWorker, FakeApi, FakeDockerRuntime]:
        api, runtime = FakeApi(job, blobs), FakeDockerRuntime()
        return DockerWorker(self.config, api, runtime, guard=None), api, runtime

    def test_verified_input_is_staged_then_fixed_runner_completes(self) -> None:
        data = b"not extracted on the worker host"
        job = {"id": "job-1", "description": "inspect", "language": "en", "files": [{
            "id": "opaque", "name": "../../sample.tar.gz", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }], "resource_plan": {"profile": "small", **PROFILES["small"]}}
        worker, api, runtime = self.worker(job, {"opaque": data})

        self.assertTrue(worker.run_once())
        self.assertEqual(runtime.created[0][0], "job-1")
        self.assertEqual(runtime.copied[".upload/opaque/000000"], data)
        runner_job = json.loads(runtime.copied["job.json"])
        self.assertEqual(runner_job["files"][0]["local_path"], "inputs/opaque.tar.gz")
        self.assertEqual(runtime.created[0][2]["OPENAI_API_KEY"], "model-key")
        self.assertEqual(api.finished[0]["status"], "completed")
        self.assertEqual(runtime.removed[0].container, "container-job-1")

    def test_bad_manifest_hash_never_creates_container(self) -> None:
        job = {"id": "job-2", "files": [{"id": "x", "name": "x.log", "size": 1, "sha256": "0" * 64}], "resource_plan": {"profile": "small", **PROFILES["small"]}}
        worker, api, runtime = self.worker(job, {"x": b"x"})
        worker.run_once()
        self.assertEqual(len(runtime.created), 1)
        self.assertEqual(runtime.started, [])
        self.assertEqual(api.finished[0]["status"], "failed")

    def test_pre_cancelled_job_never_creates_container(self) -> None:
        worker, api, runtime = self.worker({"id": "job-3", "cancel_requested": True})
        worker.run_once()
        self.assertEqual(runtime.created, [])
        self.assertEqual(api.finished[0]["status"], "cancelled")

    def test_resource_plan_must_match_profile(self) -> None:
        job = {"id": "bad-plan", "resource_plan": {"profile": "small", **PROFILES["standard"]}}
        worker, api, runtime = self.worker(job)
        worker.run_once()
        self.assertEqual(runtime.created, [])
        self.assertIn("does not match", api.finished[0]["report"])

    def test_capacity_drop_after_claim_fails_without_container(self) -> None:
        job = {"id": "capacity", "resource_plan": {"profile": "small", **PROFILES["small"]}}
        worker, api, runtime = self.worker(job)
        runtime.capacity = {"cpu_milli": 500, "memory_mb": 1024, "disk_mb": 1024}
        self.assertFalse(worker.run_once())
        self.assertEqual(runtime.created, [])
        self.assertIsNone(api.claimed_capacity)

    def test_low_docker_disk_is_explained_once_and_recovers_without_bypassing_limits(self) -> None:
        config = replace(self.config, docker_data_dir=Path("/var/lib/docker"), disk_reserve_mb=8192)
        api = FakeApi(None)
        worker = DockerWorker(config, api=api)
        # Exercise the real capacity calculation with the remote screenshot's
        # 10169 MiB free, rather than a fake pre-computed capacity dictionary.
        worker.runtime._info = {"NCPU": 8, "MemTotal": 16 * 1024**3}
        worker.runtime.data_dir = Path("/var/lib/docker")
        output = io.StringIO()
        with patch("backend.docker_runtime.Path.read_text", return_value="MemAvailable: 12582912 kB\n"), patch(
            "backend.docker_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=10169 * 1024**2)
        ) as disk, redirect_stderr(output):
            self.assertFalse(worker.run_once())
            self.assertIsNone(api.claimed_capacity)
            blocked = output.getvalue()
            self.assertIn("disk_mb=1977", blocked)
            self.assertIn("fits=none", blocked)
            self.assertIn("/var/lib/docker", blocked)
            self.assertIn("8192", blocked)
            self.assertFalse(worker.run_once())
            self.assertEqual(output.getvalue(), blocked)
            # A larger storage filesystem restores claims at the unchanged
            # profile limits and reserve; no Docker daemon is started here.
            disk.return_value = SimpleNamespace(free=82 * 1024**3)
            self.assertFalse(worker.run_once())
            self.assertEqual(api.claimed_capacity["disk_mb"], 24576)
            self.assertIn("fits=small,standard,large", output.getvalue())
        self.assertNotIn(config.worker_token, output.getvalue())
        self.assertNotIn(config.codex_api_key, output.getvalue())

    def test_vision_variant_downgrades_only_without_visual_input(self) -> None:
        worker, _, _ = self.worker(None)
        worker.config = replace(self.config, codex_model="deepseek-v4-flash-vision-exp")
        self.assertEqual(worker._container_env(90, [])["CODEX_MODEL"], "deepseek-v4-flash")
        self.assertEqual(worker._container_env(90, [{"name": "photo.png"}])["CODEX_MODEL"], "deepseek-v4-flash-vision-exp")

    def test_benchmark_timeout_is_limited_to_ten_minutes(self) -> None:
        worker, _, _ = self.worker(None)
        self.assertEqual(worker._timeout_for({"timeout_seconds": 1800, "benchmark": True}), 600)
        with self.assertRaisesRegex(WorkerError, "must be an integer"):
            worker._timeout_for({"timeout_seconds": "600"})

    def test_activity_redacts_and_suppresses_unsafe_tool_payload(self) -> None:
        worker, api, runtime = self.worker(None)
        runtime.activity = "\n".join([
            json.dumps({"seq": 1, "agent": "codex", "kind": "tool", "message": "rm /private --token=model-key"}),
            json.dumps({"seq": 2, "agent": "codex", "kind": "message", "message": "token=model-key; reviewed evidence"}),
        ])
        self.assertEqual(worker._activity(DockerJob("c", "v"), "job", 0), 2)
        self.assertEqual(api.events[0]["message"], "Codex tool execution update.")
        self.assertNotIn("model-key", api.events[1]["message"])
        self.assertIn("reviewed evidence", api.events[1]["message"])

    def test_cleanup_uncertainty_keeps_job_non_terminal(self) -> None:
        job = {"id": "cleanup", "resource_plan": {"profile": "small", **PROFILES["small"]}}
        worker, api, runtime = self.worker(job)
        runtime.remove_result = False
        with self.assertRaises(CleanupUnconfirmed):
            worker.run_once()
        self.assertEqual(api.finished, [])
        self.assertEqual(api.events[-1]["kind"], "warning")

    def test_runtime_creation_cleanup_uncertainty_keeps_job_non_terminal(self) -> None:
        job = {"id": "create-cleanup", "resource_plan": {"profile": "small", **PROFILES["small"]}}
        worker, api, runtime = self.worker(job)
        runtime.raise_on_create = DockerCleanupError("unconfirmed")
        with self.assertRaises(CleanupUnconfirmed):
            worker.run_once()
        self.assertEqual(api.finished, [])
        self.assertEqual(api.events[-1]["kind"], "warning")

    def test_input_path_preserves_archive_suffix_without_traversal(self) -> None:
        self.assertEqual(_input_path("a/b", "../../data.tar.gz"), "inputs/a_b.tar.gz")

    def test_safe_report_preserves_json_provenance_and_redacts_values(self) -> None:
        report = json.dumps({"schemaVersion": "robot-analysis/v1", "source": "archive/folder/log.txt", "evidence": [{"excerpt": "token=abc password=letmein"}]})
        runtime = FakeDockerRuntime()
        runtime.result = json.dumps({"status": "completed", "report": report, "metrics": {}})
        worker, api, _ = self.worker({"id": "report", "resource_plan": {"profile": "small", **PROFILES["small"]}})
        worker.runtime = runtime
        worker.run_once()
        output = json.loads(api.finished[0]["report"])
        self.assertEqual(output["source"], "archive/folder/log.txt")
        self.assertNotIn("letmein", api.finished[0]["report"])


class WorkerApiTests(unittest.TestCase):
    def test_claim_sends_worker_identity_and_capacity(self) -> None:
        seen: dict[str, object] = {}

        class Response:
            def read(self) -> bytes:
                return b'{"job":null}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_open(request, timeout):
            seen.update(url=request.full_url, auth=request.headers["Authorization"], worker=request.headers["X-worker-id"], body=request.data, timeout=timeout)
            return Response()

        with patch("backend.worker.urlopen", fake_open):
            self.assertIsNone(WorkerApi("http://server", "private-token", "worker-a").claim({"cpu_milli": 1, "memory_mb": 2, "disk_mb": 3}))
        self.assertEqual(seen["url"], "http://server/api/worker/claim")
        self.assertEqual(seen["auth"], "Bearer private-token")
        self.assertEqual(seen["worker"], "worker-a")
        self.assertEqual(json.loads(seen["body"]), {"worker_id": "worker-a", "capacity": {"cpu_milli": 1, "memory_mb": 2, "disk_mb": 3}})

    def test_from_env_requires_docker_and_worker_identity(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(WorkerError):
                WorkerConfig.from_env()
