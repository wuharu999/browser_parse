from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.docker_runtime import DockerJob, DockerError, DockerRuntime


class DockerRuntimePreflightTests(unittest.TestCase):
    def runtime(self, directory: Path | None) -> DockerRuntime:
        return DockerRuntime(
            image="sha256:" + "a" * 64,
            network="robot-analysis-jobs",
            proxy_url="http://robot-egress-proxy:3128",
            worker_id="worker-a",
            data_dir=directory,
            disk_reserve_mb=1,
        )

    def responses(self, directory: Path, *, network: dict | None = None) -> dict[str, str]:
        return {
            "info": json.dumps({
                "OSType": "linux", "MemoryLimit": True, "SwapLimit": True,
                "CpuCfsQuota": True, "PidsLimit": True,
                "SecurityOptions": ["name=seccomp,profile=default"],
                "DockerRootDir": str(directory), "NCPU": 4, "MemTotal": 8 * 1024**3,
            }),
            "image": json.dumps([{"Id": "sha256:" + "b" * 64, "Config": {"Volumes": {"/workspace": {}}}}]),
            "network": json.dumps([network or {
                "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
            }]),
        }

    def test_debug_cursor_reads_complete_unicode_records_without_full_file_copy(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "debug.jsonl"
            line = json.dumps({"message": "电机数据" * 500}, ensure_ascii=False) + "\n"
            output.write_text(line * 4 + '{"partial":')
            runtime = self.runtime(root)
            def execute_reader(args, **kwargs):
                program = args[5].replace("'/workspace/codex-debug.jsonl'", repr(str(output)))
                return subprocess.run([sys.executable, "-I", "-c", program, *args[6:]], capture_output=True, text=True)
            with patch.object(runtime, "_run", side_effect=execute_reader):
                text, cursor = runtime.read_activity(DockerJob("c", "v"), 0, 16384)
                self.assertEqual(text, line * 2)
                self.assertEqual(cursor, len(text.encode()))
                text2, next_cursor = runtime.read_activity(DockerJob("c", "v"), cursor, 16384)
                self.assertEqual(text2, line * 2)
                self.assertEqual(next_cursor, len((line * 4).encode()))
                self.assertEqual(runtime.read_activity(DockerJob("c", "v"), next_cursor, 16384), ("", next_cursor))

    def test_preflight_accepts_local_limited_daemon_and_isolated_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            directory = Path(temporary)
            responses = self.responses(directory)

            def fake_run(args, **_kwargs):
                key = args[0]
                if key == "info":
                    text = responses["info"]
                elif key == "image":
                    text = responses["image"]
                elif key == "network":
                    text = responses["network"]
                else:
                    raise AssertionError(args)
                return subprocess.CompletedProcess(args, 0, text, "")

            runtime = self.runtime(directory)
            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                runtime.preflight()
            self.assertEqual(runtime.image, "sha256:" + "b" * 64)
            self.assertEqual(runtime.data_dir, directory.resolve())

    def test_preflight_discovers_relocated_root_when_no_path_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            directory = Path(temporary)
            responses = self.responses(directory)

            def fake_run(args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, responses[args[0]], "")

            runtime = self.runtime(None)
            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                runtime.preflight()
            self.assertEqual(runtime.expected_data_dir, None)
            self.assertEqual(runtime.data_dir, directory.resolve())

    def test_preflight_rejects_mismatched_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as actual, tempfile.TemporaryDirectory() as expected, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            responses = self.responses(Path(actual))

            def fake_run(args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, responses[args[0]], "")

            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(DockerError, "does not match"):
                    self.runtime(Path(expected)).preflight()

    def test_preflight_rejects_missing_or_unavailable_reported_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            responses = self.responses(Path(temporary))
            reported = json.loads(responses["info"])
            reported["DockerRootDir"] = "/not/a/real/docker-root"
            responses["info"] = json.dumps(reported)

            def fake_run(args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, responses[args[0]], "")

            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(DockerError, "unavailable DockerRootDir"):
                    self.runtime(None).preflight()

    def test_preflight_rejects_malformed_reported_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            responses = self.responses(Path(temporary))
            reported = json.loads(responses["info"])
            reported["DockerRootDir"] = ["not-a-path"]
            responses["info"] = json.dumps(reported)

            def fake_run(args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, responses[args[0]], "")

            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(DockerError, "usable DockerRootDir"):
                    self.runtime(None).preflight()

    def test_capacity_uses_detected_root_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            directory = Path(temporary)
            responses = self.responses(directory)
            observed: list[Path] = []

            def fake_run(args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, responses[args[0]], "")

            class DiskUsage:
                free = 100 * 1024**2

            def disk_usage(path):
                observed.append(Path(path))
                return DiskUsage()

            runtime = self.runtime(None)
            with patch.object(DockerRuntime, "_run", side_effect=fake_run), patch("backend.docker_runtime.shutil.disk_usage", side_effect=disk_usage):
                runtime.preflight()
                capacity = runtime.available_capacity({"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 500})
            self.assertEqual(observed, [directory.resolve(), directory.resolve()])
            self.assertEqual(capacity["disk_mb"], 99)

    def test_preflight_rejects_network_that_can_route_or_enable_ipv6(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True):
            directory = Path(temporary)
            responses = self.responses(directory, network={
                "Driver": "bridge", "Internal": True, "EnableIPv6": True,
                "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "nat"},
            })

            def fake_run(args, **_kwargs):
                key = args[0]
                return subprocess.CompletedProcess(args, 0, responses[key], "")

            runtime = self.runtime(directory)
            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(DockerError, "internal bridge"):
                    runtime.preflight()
            self.assertEqual(runtime._info, {})
            self.assertIsNone(runtime.data_dir)

    def test_output_reader_rejects_untrusted_paths_before_docker_execution(self) -> None:
        runtime = self.runtime(Path("/tmp"))
        with self.assertRaisesRegex(DockerError, "invalid job output path"):
            runtime.copy_out_text(type("Job", (), {"container": "x"})(), "/etc/passwd")

    def test_snapshot_workspace_creates_tar_gz(self) -> None:
        import io
        import tarfile
        import gzip

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            target = tmp / "snapshot.tar.gz"

            # Create an uncompressed tar stream that docker cp would output
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                data = b'{"state": "ok"}'
                ti = tarfile.TarInfo(name="scenario_state.json")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

                cdx = b"codex-session"
                ci = tarfile.TarInfo(name=".codex/history.log")
                ci.size = len(cdx)
                tar.addfile(ci, io.BytesIO(cdx))
            uncompressed_tar_bytes = tar_buf.getvalue()

            class FakePopen:
                def __init__(self, *args, **kwargs):
                    self.stdout = io.BytesIO(uncompressed_tar_bytes)
                    self.returncode = 0
                def communicate(self, timeout=None):
                    return b"", b""
                def poll(self):
                    return self.returncode
                def kill(self):
                    pass
                def wait(self):
                    return 0

            runtime = self.runtime(Path("/tmp"))
            with patch("subprocess.Popen", side_effect=FakePopen):
                result = runtime.snapshot_workspace(DockerJob("c1", "v1"), target)
                self.assertEqual(result, target)
                self.assertTrue(target.is_file())

            # Verify target is a valid gzip tarball with expected members
            with tarfile.open(target, mode="r:gz") as tar:
                names = tar.getnames()
                self.assertIn("scenario_state.json", names)
                self.assertIn(".codex/history.log", names)
                extracted = tar.extractfile("scenario_state.json").read()
                self.assertEqual(extracted, b'{"state": "ok"}')

    def test_snapshot_workspace_raises_on_docker_error(self) -> None:
        import io
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            target = tmp / "fail.tar.gz"

            class FailPopen:
                def __init__(self, *args, **kwargs):
                    self.stdout = io.BytesIO(b"")
                    self.returncode = 1
                def communicate(self, timeout=None):
                    return b"", b"Error: No such container: c1"
                def poll(self):
                    return self.returncode
                def kill(self):
                    pass
                def wait(self):
                    return 1

            runtime = self.runtime(Path("/tmp"))
            with patch("subprocess.Popen", side_effect=FailPopen):
                with self.assertRaisesRegex(DockerError, "Docker snapshot copy failed"):
                    runtime.snapshot_workspace(DockerJob("c1", "v1"), target)
            self.assertFalse(target.exists())

    def test_restore_workspace_streams_tar_to_docker_cp(self) -> None:
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            archive = tmp / "source.tar.gz"

            # Create valid .tar.gz archive
            with tarfile.open(archive, mode="w:gz") as tar:
                data = b"print('hello')"
                ti = tarfile.TarInfo(name="script.py")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            captured_input = io.BytesIO()

            class FakeStdin:
                def write(self, b):
                    captured_input.write(b)
                def close(self):
                    pass
                @property
                def closed(self):
                    return False

            class FakeCpPopen:
                def __init__(self, *args, **kwargs):
                    self.stdin = FakeStdin()
                    self.returncode = 0
                def communicate(self, timeout=None):
                    return b"", b""
                def poll(self):
                    return self.returncode
                def kill(self):
                    pass
                def wait(self):
                    return 0

            runtime = self.runtime(Path("/tmp"))
            with patch("subprocess.Popen", side_effect=FakeCpPopen):
                runtime.restore_workspace(DockerJob("c1", "v1"), archive)

            # Check that captured input on stdin is a valid tar with uid 10001
            captured_input.seek(0)
            with tarfile.open(fileobj=captured_input, mode="r:") as tar:
                members = tar.getmembers()
                self.assertEqual(len(members), 1)
                self.assertEqual(members[0].name, "script.py")
                self.assertEqual(members[0].uid, 10001)
                self.assertEqual(members[0].gid, 10001)

    def test_restore_workspace_rejects_missing_file(self) -> None:
        runtime = self.runtime(Path("/tmp"))
        with self.assertRaisesRegex(DockerError, "Snapshot archive not found"):
            runtime.restore_workspace(DockerJob("c1", "v1"), Path("/nonexistent/snap.tar.gz"))

    def test_restore_workspace_rejects_corrupted_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corrupt = Path(temp_dir) / "corrupt.tar.gz"
            corrupt.write_bytes(b"not-a-valid-tar-archive")

            runtime = self.runtime(Path("/tmp"))
            with self.assertRaisesRegex(DockerError, "Corrupted snapshot archive"):
                runtime.restore_workspace(DockerJob("c1", "v1"), corrupt)

    def test_restore_workspace_rejects_path_traversal(self) -> None:
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as temp_dir:
            traversal = Path(temp_dir) / "traversal.tar.gz"
            with tarfile.open(traversal, mode="w:gz") as tar:
                data = b"malicious"
                ti = tarfile.TarInfo(name="../../etc/passwd")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            runtime = self.runtime(Path("/tmp"))
            with self.assertRaisesRegex(DockerError, "Unsafe path"):
                runtime.restore_workspace(DockerJob("c1", "v1"), traversal)

