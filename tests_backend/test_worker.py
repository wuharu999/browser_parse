from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from backend.worker import CubeWorker, WorkerApi, WorkerConfig, WorkerError, _input_path


class Files:
    def __init__(self) -> None:
        self.values: dict[str, bytes | str] = {}
        self.directories: set[str] = set()

    def exists(self, path: str) -> bool:
        return path in self.directories or path in self.values

    def stat(self, path: str) -> dict[str, str]:
        if path in self.directories:
            return {"type": "FILE_TYPE_DIRECTORY"}
        if path in self.values:
            return {"type": "FILE_TYPE_REGULAR"}
        raise FileNotFoundError(path)

    def make_dir(self, path: str) -> None:
        self.directories.add(path)

    def write(self, path: str, value: bytes | str) -> None:
        self.values[path] = value

    def read(self, path: str) -> bytes | str:
        return self.values[path]


class ExistingRootFiles(Files):
    def __init__(self) -> None:
        super().__init__()
        self.directories = {"/workspace"}

    def exists(self, path: str) -> bool:
        return path in self.directories or path in self.values

    def stat(self, path: str) -> dict[str, str]:
        if path in self.directories:
            return {"type": "FILE_TYPE_DIRECTORY"}
        if path in self.values:
            return {"type": "FILE_TYPE_REGULAR"}
        raise FileNotFoundError(path)

    def make_dir(self, path: str) -> None:
        if self.exists(path):
            raise OSError(f"Filesystem MakeDir failed: already_exists: directory already exists: {path}")
        self.directories.add(path)


class Commands:
    def __init__(self, files: Files) -> None:
        self.files = files
        self.runs: list[str] = []
        self.options: list[dict] = []

    def run(self, command: str, **kwargs) -> None:
        self.runs.append(command)
        self.options.append(kwargs)
        self.files.write("/workspace/result.json", json.dumps({
            "status": "completed", "report": "Evidence-backed conclusion.",
            "metrics": {"peak_rss_bytes": 12, "disk_bytes": 34},
        }))


class SandboxInstance:
    def __init__(self) -> None:
        self.files = Files()
        self.commands = Commands(self.files)
        self.timeout: int | None = None
        self.killed = 0

    def set_timeout(self, value: int) -> None:
        self.timeout = value

    def kill(self) -> None:
        self.killed += 1

    def get_info(self):
        return type("Info", (), {"cpu_milli": 500, "memory_mb": 1024, "disk_size_mb": 4096})()


class Sandbox:
    created: list[tuple[SandboxInstance, object, dict[str, str]]] = []

    @classmethod
    def create(cls, *, config: object, env_vars: dict[str, str], timeout: int) -> SandboxInstance:
        instance = SandboxInstance()
        instance.timeout = timeout
        cls.created.append((instance, config, env_vars))
        return instance


class ExistingRootSandbox(Sandbox):
    @classmethod
    def create(cls, *, config: object, env_vars: dict[str, str], timeout: int) -> SandboxInstance:
        instance = SandboxInstance()
        instance.files = ExistingRootFiles()
        instance.commands = Commands(instance.files)
        instance.timeout = timeout
        cls.created.append((instance, config, env_vars))
        return instance


class Api:
    def __init__(self, job: dict | None, blobs: dict[str, bytes] | None = None) -> None:
        self.job, self.blobs = job, blobs or {}
        self.finished: list[dict] = []
        self.events: list[dict] = []

    def claim(self):
        return self.job

    def download_to(self, _job_id: str, artifact_id: str, destination: Path):
        data = self.blobs[artifact_id]
        destination.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def get_job(self, _job_id: str) -> dict:
        return {"cancel_requested": False}

    def event(self, _job_id: str, kind: str, message: str, agent: str = "worker") -> None:
        self.events.append({"kind": kind, "message": message, "agent": agent})

    def finish(self, _job_id: str, status: str, report: str, cost_usd, metrics: dict) -> None:
        self.finished.append({"status": status, "report": report, "cost_usd": cost_usd, "metrics": metrics})


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        Sandbox.created.clear()
        self.temp = tempfile.TemporaryDirectory()
        runtime = Path(self.temp.name, "runtime")
        runtime.mkdir()
        Path(runtime, "MAIN_PROMPT.md").write_text("safe runtime asset")
        self.config = WorkerConfig(
            api_url="http://127.0.0.1:8000", worker_token="worker-token",
            cube_api_url="http://cube:3000", cube_api_key="cube-key", cube_template_id="template",
            cube_proxy_node_ip=None, cube_proxy_port_http=None, codex_model="gpt-5.6-luna", codex_provider_url=None,
            codex_api_key_env="OPENAI_API_KEY", codex_api_key="model-key", timeout_seconds=1800,
            poll_seconds=0.01, runtime_dir=runtime, wiki_dir=None,
            parallel=2,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claimed_job_uploads_verified_file_and_only_runs_fixed_runner(self) -> None:
        data = b"not extracted on the worker host"
        job = {"id": "job-1", "description": "inspect", "language": "en", "evidence": {"x": 1}, "files": [{
            "id": "opaque", "name": "../../sample.tar.gz", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }]}
        api = Api(job, {"opaque": data})
        self.assertTrue(CubeWorker(self.config, api, Sandbox).run_once())
        instance, _, env = Sandbox.created[0]
        self.assertEqual(instance.commands.runs, ["python3 /opt/sandbox/run_codex.py /workspace/job.json"])
        self.assertEqual(instance.commands.options[0]["cwd"], "/workspace")
        self.assertGreater(instance.commands.options[0]["timeout"], 0)
        self.assertEqual(instance.files.values["/workspace/.upload/opaque/000000"], data)
        runner_job = json.loads(instance.files.values["/workspace/job.json"])
        self.assertEqual(runner_job["files"][0]["local_path"], "inputs/opaque.tar.gz")
        self.assertEqual(runner_job["files"][0]["staging_chunks"], [".upload/opaque/000000"])
        self.assertEqual(instance.timeout, 1800)
        self.assertEqual(env["OPENAI_API_KEY"], "model-key")
        self.assertEqual(api.finished[0]["status"], "completed")
        self.assertIsNone(api.finished[0]["cost_usd"])
        self.assertEqual(api.finished[0]["metrics"]["cost_source"], "unknown")

    def test_run_job_accepts_existing_workspace_and_creates_transfer_directories(self) -> None:
        data = b"synthetic log"
        job = {"id": "job-existing-root", "files": [{
            "id": "log", "name": "robot.log", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }]}
        api = Api(job, {"log": data})

        self.assertTrue(CubeWorker(self.config, api, ExistingRootSandbox).run_once())

        instance = ExistingRootSandbox.created[0][0]
        self.assertEqual(api.finished[0]["status"], "completed")
        self.assertTrue({"/workspace/inputs", "/workspace/.upload", "/workspace/.upload/log"}.issubset(instance.files.directories))

    def test_bad_manifest_hash_never_reaches_codex(self) -> None:
        job = {"id": "job-2", "files": [{"id": "x", "name": "x.log", "size": 1, "sha256": "0" * 64}]}
        api = Api(job, {"x": b"x"})
        CubeWorker(self.config, api, Sandbox).run_once()
        self.assertEqual(Sandbox.created[0][0].commands.runs, [])
        self.assertEqual(api.finished[0]["status"], "failed")

    def test_pre_cancelled_job_never_creates_sandbox(self) -> None:
        api = Api({"id": "job-3", "cancel_requested": True})
        CubeWorker(self.config, api, Sandbox).run_once()
        self.assertEqual(Sandbox.created, [])
        self.assertEqual(api.finished[0]["status"], "cancelled")

    def test_cube_config_passes_optional_forwarded_proxy_port(self) -> None:
        worker = CubeWorker(replace(self.config, cube_proxy_node_ip="127.0.0.1", cube_proxy_port_http=11080), Api(None), Sandbox)
        _, config = worker._cube(90)
        self.assertEqual(config["proxy_node_ip"], "127.0.0.1")
        self.assertEqual(config["proxy_port"], 11080)

    def test_cube_config_omits_proxy_port_to_preserve_sdk_default(self) -> None:
        worker = CubeWorker(self.config, Api(None), Sandbox)
        _, config = worker._cube(90)
        self.assertNotIn("proxy_port", config)

    def test_input_path_preserves_archive_suffix_without_traversal(self) -> None:
        self.assertEqual(_input_path("a/b", "../../data.tar.gz"), "inputs/a_b.tar.gz")

    def test_runtime_transfer_skips_python_cache_files(self) -> None:
        cache = self.config.runtime_dir / "__pycache__"
        cache.mkdir()
        (cache / "ignored.pyc").write_bytes(b"cache")
        (self.config.runtime_dir / "also-ignored.pyc").write_bytes(b"cache")
        files = Files()
        CubeWorker._write_tree(type("Sandbox", (), {"files": files})(), self.config.runtime_dir, "/workspace")
        self.assertIn("/workspace/MAIN_PROMPT.md", files.values)
        self.assertFalse(any("pycache" in path or path.endswith(".pyc") for path in files.values))

    def test_safe_report_keeps_markdown_line_breaks(self) -> None:
        from backend.worker import _safe_text
        self.assertEqual(_safe_text("# Report\n\n- finding\r\n- next"), "# Report\n\n- finding\n- next")

    def test_safe_final_report_preserves_relative_source_provenance(self) -> None:
        from backend.worker import _safe_report
        report = json.dumps({"schemaVersion": "robot-analysis/v1", "source": "archive/folder/log.txt", "findings": ["x" * 8500]})
        cleaned = _safe_report(report, self.config.codex_api_key and (self.config.codex_api_key,))
        self.assertEqual(json.loads(cleaned)["source"], "archive/folder/log.txt")
        self.assertGreater(len(cleaned), 800)

    def test_safe_json_report_redacts_string_values_without_breaking_schema(self) -> None:
        from backend.worker import _safe_report
        report = json.dumps({
            "schemaVersion": "robot-analysis/v1",
            "source": "archive/folder/log.txt",
            "evidence": [{"excerpt": "token=abc123 password=letmein; observed timeout"}],
        })
        cleaned = _safe_report(report)
        parsed = json.loads(cleaned)
        self.assertEqual(parsed["source"], "archive/folder/log.txt")
        self.assertNotIn("abc123", cleaned)
        self.assertNotIn("letmein", cleaned)
        self.assertIn("observed timeout", parsed["evidence"][0]["excerpt"])

    def test_wiki_transfer_accepts_bounded_image_larger_than_markdown_index_cap(self) -> None:
        wiki = Path(self.temp.name, "wiki")
        wiki.mkdir()
        png = b"\x89PNG\r\n\x1a\n" + b"x" * (4 * 1024 * 1024 + 1)
        (wiki / "camera.png").write_bytes(png)
        sandbox = SandboxInstance()
        CubeWorker(replace(self.config, wiki_dir=wiki), Api(None), Sandbox)._upload_wiki(sandbox, "job-wiki", time.monotonic() + 5)
        self.assertEqual(sandbox.files.values["/workspace/wiki/camera.png"], png)

    def test_activity_cursor_uses_monotonic_sequences_after_ring_rotation(self) -> None:
        api = Api(None)
        worker = CubeWorker(self.config, api, Sandbox)
        sandbox = SandboxInstance()
        sandbox.files.write("/workspace/activity.jsonl", '{"seq":401,"agent":"log_investigator","message":"Codex activity: turn.started"}\n')
        seen = worker._activity(sandbox, "job-activity", 400)
        sandbox.files.write("/workspace/activity.jsonl", '{"seq":401,"agent":"log_investigator","message":"Codex activity: turn.started"}\n{"seq":402,"agent":"evidence_reviewer","message":"Codex activity: turn.completed"}\n')
        self.assertEqual(worker._activity(sandbox, "job-activity", seen), 402)
        self.assertEqual([event["agent"] for event in api.events], ["log_investigator", "evidence_reviewer"])

    def test_activity_worker_suppresses_tool_payload_and_redacts_messages(self) -> None:
        api = Api(None)
        worker = CubeWorker(self.config, api, Sandbox)
        sandbox = SandboxInstance()
        sandbox.files.write("/workspace/activity.jsonl", "\n".join([
            json.dumps({"seq": 1, "agent": "codex", "kind": "tool", "message": "rm /private/path --token=model-key"}),
            json.dumps({"seq": 2, "agent": "codex", "kind": "message", "message": "token=model-key; reviewed evidence"}),
        ]) + "\n")
        self.assertEqual(worker._activity(sandbox, "job-activity", 0), 2)
        self.assertEqual(api.events[0]["message"], "Codex tool execution update.")
        self.assertNotIn("model-key", api.events[1]["message"])
        self.assertIn("reviewed evidence", api.events[1]["message"])

    def test_activity_worker_keeps_fixed_tool_completion_status(self) -> None:
        api = Api(None)
        worker = CubeWorker(self.config, api, Sandbox)
        sandbox = SandboxInstance()
        sandbox.files.write("/workspace/activity.jsonl", json.dumps({
            "seq": 1, "agent": "codex", "kind": "tool", "message": "Codex tool execution completed.",
        }) + "\n")
        worker._activity(sandbox, "job-activity", 0)
        self.assertEqual(api.events[0]["message"], "Codex tool execution completed.")

    def test_unconfirmed_cleanup_does_not_terminalize_successful_job(self) -> None:
        api = Api({"id": "job-kill", "files": []})
        with patch.object(CubeWorker, "_kill", return_value=False):
            CubeWorker(self.config, api, Sandbox).run_once()
        self.assertEqual(api.finished, [])
        self.assertEqual(api.events[-1]["kind"], "warning")


class WorkerApiTests(unittest.TestCase):
    def test_private_http_contract_sends_bearer_and_expected_path(self) -> None:
        seen: dict[str, object] = {}

        class Response:
            def read(self):
                return b'{"job":null}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_open(request, timeout):
            seen["url"], seen["auth"], seen["body"], seen["timeout"] = request.full_url, request.headers["Authorization"], request.data, timeout
            return Response()

        with patch("backend.worker.urlopen", fake_open):
            self.assertIsNone(WorkerApi("http://server", "private-token").claim())
        self.assertEqual(seen["url"], "http://server/api/worker/claim")
        self.assertEqual(seen["auth"], "Bearer private-token")
        self.assertEqual(seen["body"], b"{}")

    def test_from_env_fails_closed_without_cube_or_model_secret(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(WorkerError):
                WorkerConfig.from_env()

    def test_from_env_validates_optional_cube_proxy_port(self) -> None:
        required = {
            "ROBOT_WORKER_TOKEN": "worker",
            "CUBE_API_URL": "http://127.0.0.1:13000",
            "CUBE_API_KEY": "local",
            "CUBE_TEMPLATE_ID": "template",
            "OPENAI_API_KEY": "model",
        }
        with patch.dict("os.environ", {**required, "CUBE_PROXY_PORT_HTTP": "11080"}, clear=True):
            self.assertEqual(WorkerConfig.from_env().cube_proxy_port_http, 11080)
        with patch.dict("os.environ", {**required, "CUBE_PROXY_PORT_HTTP": "70000"}, clear=True):
            with self.assertRaisesRegex(WorkerError, "between 1 and 65535"):
                WorkerConfig.from_env()
