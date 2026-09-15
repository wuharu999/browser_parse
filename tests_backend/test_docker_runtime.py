from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.docker_runtime import DockerError, DockerRuntime


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
