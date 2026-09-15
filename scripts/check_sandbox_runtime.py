"""Validate a restricted Docker analysis runtime without model or API credentials.

Run after Docker infrastructure has created the internal job network and proxy:
```
ROBOT_DOCKER_IMAGE=sha256:... \
ROBOT_EGRESS_PROXY_URL=http://robot-egress-proxy:3128 \
python scripts/check_sandbox_runtime.py standard
```
The check creates one synthetic job volume/container and removes both on exit.
It does not contact the API, a model provider, or any external service.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.docker_runtime import DockerRuntime
from backend.resources import PROFILES


ROOT = Path(__file__).resolve().parents[1]
CHECK_PROGRAM = """\
import json
import os
import subprocess
from pathlib import Path

for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ROBOT_WORKER_TOKEN", "CODEX_API_KEY"):
    assert not os.environ.get(name), f"unexpected credential: {name}"

for name in ("robot-analysis-context", "robot-evidence", "pdf-evidence"):
    assert Path("/workspace/.agents/skills", name, "SKILL.md").is_file(), name

subprocess.run(["python3", "/workspace/evidence.py", "index"], check=True, capture_output=True)
context = json.loads(subprocess.check_output(["python3", "/workspace/evidence.py", "context"]))
assert context["wiki_indexed_pages"] == 1
assert context["job_id"] == "offline-runtime-check"

limits = {}
for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
    path = Path("/sys/fs/cgroup", name)
    limits[name] = path.read_text().strip() if path.is_file() else None

environment = json.loads(Path("/opt/sandbox/environment.json").read_text())
Path("/workspace/result.json").write_text(
    json.dumps({"context": context, "environment": environment, "cgroup": limits})
)
"""


def _stage_runtime(destination: Path, job: dict[str, object]) -> None:
    runtime = ROOT / "sandbox" / "runtime"
    if not runtime.is_dir():
        raise RuntimeError(f"runtime directory is unavailable: {runtime}")
    shutil.copytree(
        runtime,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )
    wiki = destination / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "fixture.md").write_text("# Synthetic test\nMotor diagnostic context only.\n")
    (destination / "job.json").write_text(json.dumps(job, separators=(",", ":")))
    (destination / "runtime_check.py").write_text(CHECK_PROGRAM)


def _inspect_limits(runtime: DockerRuntime, container: str, plan: dict[str, int]) -> None:
    result = runtime._run(["inspect", container])
    details = json.loads(result.stdout)[0]
    host = details["HostConfig"]
    expected_memory = plan["memory_mb"] * 1024 * 1024
    expected_cpu = plan["cpu_milli"] * 1_000_000
    if host.get("Memory") != expected_memory or host.get("MemorySwap") != expected_memory:
        raise RuntimeError("Docker memory/swap limit differs from the requested profile")
    if host.get("NanoCpus") != expected_cpu:
        raise RuntimeError("Docker CPU limit differs from the requested profile")
    if host.get("PidsLimit") != runtime.pids_limit:
        raise RuntimeError("Docker pids limit differs from the configured limit")
    if not host.get("ReadonlyRootfs") or host.get("NetworkMode") != runtime.network:
        raise RuntimeError("Docker root filesystem or job network restriction is missing")
    if "ALL" not in (host.get("CapDrop") or []) or "no-new-privileges" not in (host.get("SecurityOpt") or []):
        raise RuntimeError("Docker capability or no-new-privileges restriction is missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--image", default=os.environ.get("ROBOT_DOCKER_IMAGE"))
    parser.add_argument("--network", default=os.environ.get("ROBOT_DOCKER_NETWORK", "robot-analysis-jobs"))
    parser.add_argument("--proxy-url", default=os.environ.get("ROBOT_EGRESS_PROXY_URL"))
    parser.add_argument("--worker-id", default="runtime-check")
    configured_data_dir = os.environ.get("ROBOT_DOCKER_DATA_DIR")
    parser.add_argument(
        "--docker-data-dir",
        type=Path,
        default=(
            Path(configured_data_dir)
            if configured_data_dir and configured_data_dir != "/var/lib/docker"
            else None
        ),
        help="optional expected DockerRootDir; omit to discover it from the local daemon",
    )
    parser.add_argument("--disk-reserve-mb", type=int, default=int(os.environ.get("ROBOT_HOST_DISK_RESERVE_MB", "8192")))
    parser.add_argument("--pids-limit", type=int, default=int(os.environ.get("ROBOT_JOB_PIDS_LIMIT", "512")))
    args = parser.parse_args()
    if not args.image:
        raise SystemExit("missing --image or ROBOT_DOCKER_IMAGE")
    if not args.proxy_url:
        raise SystemExit("missing --proxy-url or ROBOT_EGRESS_PROXY_URL")

    plan = {"profile": args.profile, **PROFILES[args.profile]}
    runtime = DockerRuntime(
        image=args.image,
        network=args.network,
        proxy_url=args.proxy_url,
        worker_id=args.worker_id,
        data_dir=args.docker_data_dir,
        disk_reserve_mb=args.disk_reserve_mb,
        pids_limit=args.pids_limit,
    )
    runtime.preflight()
    job = runtime.create("offline-runtime-check", plan, {})
    removed = False
    try:
        with tempfile.TemporaryDirectory(prefix="robot-runtime-check-") as temporary:
            _stage_runtime(Path(temporary), {
                "id": "offline-runtime-check",
                "description": "Synthetic context check; no model run.",
                "language": "en",
                "files": [],
                "resource_plan": plan,
            })
            runtime.copy_in(job, Path(temporary))
        runtime.start(job)
        _inspect_limits(runtime, job.container, plan)
        runtime._run(
            ["exec", job.container, "python3", "-I", "/workspace/runtime_check.py"],
            timeout=120,
        )
        report = runtime.copy_out_text(job, "/workspace/result.json", 64 * 1024)
        if report is None:
            raise RuntimeError("Docker runtime check did not produce a readable result")
        print(json.dumps(json.loads(report), ensure_ascii=False))
        removed = runtime.remove(job)
        if not removed:
            raise RuntimeError("Docker runtime check cleanup was not confirmed")
    finally:
        if not removed and not runtime.remove(job):
            raise RuntimeError("Docker runtime check cleanup was not confirmed")


if __name__ == "__main__":
    main()
