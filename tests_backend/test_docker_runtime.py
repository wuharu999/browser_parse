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
    def runtime(self, directory: Path) -> DockerRuntime:
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

            with patch.object(DockerRuntime, "_run", side_effect=fake_run):
                with self.assertRaisesRegex(DockerError, "internal bridge"):
                    self.runtime(directory).preflight()

    def test_output_reader_rejects_untrusted_paths_before_docker_execution(self) -> None:
        runtime = self.runtime(Path("/tmp"))
        with self.assertRaisesRegex(DockerError, "invalid job output path"):
            runtime.copy_out_text(type("Job", (), {"container": "x"})(), "/etc/passwd")
