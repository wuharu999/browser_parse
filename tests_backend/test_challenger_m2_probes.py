"""Empirical Verification Challenger Probes for Milestone 2.

Adversarially probes:
1. Snapshot tarball generation: gzip compression, full workspace asset inclusion (subdirs, empty, hidden, binary), atomic write (.tmp.<uuid> -> final).
2. Restoration: UID/GID 10001 remapping on all members, corrupted tarball rejection (raises DockerError, no unhandled crash).
3. Path traversal: zip-slip / tar-slip payloads (relative, absolute, backslash, nested) rejected with DockerError before execution.
4. 5-minute inactivity idle check: exact timing boundary at 299.0s/299.99s (stays warm) vs 300.0s (hibernates, snapshot written, container/volume cleaned).
5. Cold resumption: hibernated turn execution provisions fresh container, restores snapshot, stages answers, executes cleanly; multi-turn alternating warm/cold lifecycle.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from backend.docker_runtime import DockerCleanupError, DockerError, DockerJob, DockerRuntime
from backend.grill_store import GrillStore, MAX_QUESTION_BUDGET
from backend.worker import DockerWorker, WorkerConfig, WorkerError


class FakePopen:
    """Configurable fake subprocess.Popen for DockerRuntime testing."""

    def __init__(
        self,
        stdout_data: bytes = b"",
        stderr_data: bytes = b"",
        returncode: int = 0,
        raise_on_read: Exception | None = None,
    ) -> None:
        self.stdout_data = stdout_data
        self.stderr_data = stderr_data
        self.returncode = returncode
        self.raise_on_read = raise_on_read
        self.killed = False
        self.stdin_captured = io.BytesIO()

        class Stream(io.BytesIO):
            def __init__(self, data: bytes, err: Exception | None):
                super().__init__(data)
                self.err = err

            def read(self, size: int = -1):
                if self.err:
                    raise self.err
                return super().read(size)

        self.stdout = Stream(stdout_data, raise_on_read)
        self.stderr = Stream(stderr_data, None)
        self.stdin = self.stdin_captured

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return self.stdout_data, self.stderr_data

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def create_sample_runtime(data_dir: Path | None = None) -> DockerRuntime:
    return DockerRuntime(
        image="sha256:" + "a" * 64,
        network="robot-analysis-jobs",
        proxy_url="http://robot-egress-proxy:3128",
        worker_id="worker-challenger-m2",
        data_dir=data_dir or Path("/tmp"),
        disk_reserve_mb=1,
    )


class FinishedProcess:
    returncode = 0

    def poll(self) -> int:
        return self.returncode


class ProbingDockerRuntime:
    """Mock Docker runtime with deep tracking for adversarial verification."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or Path("/fake/docker-root")
        self.capacity = {"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 24576}
        self.created: list[tuple[str, dict, dict]] = []
        self.active_containers: set[str] = set()
        self.active_volumes: set[str] = set()
        self.started_containers: list[str] = []
        self.removed_containers: list[str] = []
        self.removed_volumes: list[str] = []
        self.snapshots_created: dict[str, Path] = {}
        self.restores_performed: list[tuple[str, Path]] = []
        self.copied_files: dict[str, dict[str, bytes]] = {}
        self.container_counter = 0

    def available_capacity(self, maximum: dict[str, int]) -> dict[str, int]:
        return {key: min(maximum[key], self.capacity[key]) for key in maximum}

    def create(self, job_id: str, plan: dict, env: dict) -> DockerJob:
        self.container_counter += 1
        cid = f"robot-container-{job_id}-c{self.container_counter}"
        vid = f"robot-workspace-{job_id}-v{self.container_counter}"
        self.created.append((job_id, plan, env))
        self.active_containers.add(cid)
        self.active_volumes.add(vid)
        self.copied_files[cid] = {}
        return DockerJob(cid, vid)

    def copy_in(self, job: DockerJob, source: Path) -> None:
        if job.container not in self.copied_files:
            self.copied_files[job.container] = {}
        for path in source.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(source))
                self.copied_files[job.container][rel] = path.read_bytes()

    def start(self, job: DockerJob) -> None:
        self.started_containers.append(job.container)

    def exec_runner(self, _job: DockerJob) -> FinishedProcess:
        return FinishedProcess()

    def copy_out_text(self, job: DockerJob, path: str, _maximum: int = 256 * 1024) -> str | None:
        if path == "/workspace/result.json":
            files = self.copied_files.get(job.container, {})
            scenario_bytes = files.get("scenario_state.json", b"{}")
            try:
                state = json.loads(scenario_bytes.decode("utf-8"))
            except Exception:
                state = {}
            # Increment turn in state for probe tracking
            rev = state.get("revision", 0) + 1
            state["revision"] = rev
            return json.dumps({
                "status": "completed",
                "scenario_state": state,
                "questions": [{"id": f"q_turn_{rev}", "text": f"Question for turn {rev}?", "options": []}],
                "ready_for_readback": (rev >= 25),
                "metrics": {},
            })
        if path == "/workspace/activity.jsonl":
            return ""
        return None

    def snapshot_workspace(self, job: DockerJob, target_path: Path) -> Path:
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot files in container
        with tarfile.open(target, mode="w:gz") as tar:
            files = self.copied_files.get(job.container, {})
            for name, data in files.items():
                ti = tarfile.TarInfo(name=name)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
            if "scenario_state.json" not in files:
                state_data = b'{"revision": 1, "summary": "Probe snapshot state"}'
                ti = tarfile.TarInfo(name="scenario_state.json")
                ti.size = len(state_data)
                tar.addfile(ti, io.BytesIO(state_data))
            if ".codex/session.log" not in files:
                cdx = b"codex-session-history-data"
                ci = tarfile.TarInfo(name=".codex/session.log")
                ci.size = len(cdx)
                tar.addfile(ci, io.BytesIO(cdx))
        self.snapshots_created[job.container] = target
        return target

    def restore_workspace(self, job: DockerJob, source_tarball: Path) -> None:
        source = Path(source_tarball).resolve()
        if not source.is_file():
            raise DockerError(f"Snapshot archive not found: {source}")
        if job.container not in self.copied_files:
            self.copied_files[job.container] = {}
        try:
            with tarfile.open(source, mode="r:*") as tar:
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        raise DockerError(f"Unsafe path in snapshot archive: {member.name}")
                    if member.isreg():
                        f = tar.extractfile(member)
                        if f:
                            self.copied_files[job.container][member.name] = f.read()
        except Exception as exc:
            if isinstance(exc, DockerError):
                raise
            raise DockerError(f"Corrupted snapshot archive: {exc}") from exc
        self.restores_performed.append((job.container, source))

    def disk_healthy(self, _job: DockerJob, _limit_mb: int) -> bool:
        return True

    def workspace_bytes(self, _job: DockerJob) -> int:
        return 1024

    def remove(self, job: DockerJob) -> bool:
        if job.container in self.active_containers:
            self.active_containers.remove(job.container)
            self.removed_containers.append(job.container)
        if job.volume in self.active_volumes:
            self.active_volumes.remove(job.volume)
            self.removed_volumes.append(job.volume)
        return True


class MockApiForProbes:
    def __init__(self, store: GrillStore) -> None:
        self.store = store
        self.finished: list[dict] = []
        self.events: list[dict] = []

    def claim(self, capacity: dict) -> dict | None:
        return self.store.worker_claim_grill("worker-challenger-m2", capacity)

    def get_job(self, _job_id: str) -> dict:
        return {"cancel_requested": False}

    def download_to(self, session_id: str, artifact_id: str, destination: Path) -> tuple[int, str]:
        item = self.store.get_file_by_id(artifact_id)
        if item:
            path, _ = item
            data = path.read_bytes()
            destination.write_bytes(data)
            import hashlib
            return len(data), hashlib.sha256(data).hexdigest()
        destination.write_bytes(b"mock-data")
        return 9, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def event(self, job_id: str, kind: str, message: str, agent: str = "worker", subagent: Any = None) -> None:
        self.events.append({"job_id": job_id, "kind": kind, "message": message, "agent": agent})

    def events_batch(self, job_id: str, records: list[dict]) -> None:
        for r in records:
            self.events.append({"job_id": job_id, **r})

    def save_analysis_context(self, _job_id: str, _context: dict) -> None:
        pass

    def finish(self, job_id: str, status: str, report: str, cost_usd: float | None, metrics: dict, analysis_context: Any = None) -> None:
        try:
            output = json.loads(report)
        except Exception:
            output = {"report": report}
        self.store.worker_finish_grill(job_id, "worker-challenger-m2", status, output)
        self.finished.append({"job_id": job_id, "status": status, "output": output})

    def hibernate_grill(self, session_id: str, snapshot_path: str) -> None:
        self.store.hibernate_session(session_id, snapshot_path)


def create_probing_worker(tmp_path: Path, store: GrillStore, runtime: ProbingDockerRuntime) -> DockerWorker:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "MAIN_PROMPT.md").write_text("safe prompt")

    config = WorkerConfig(
        api_url="http://127.0.0.1:8000",
        worker_token="test-token",
        worker_id="worker-challenger-m2",
        docker_image="sha256:" + "a" * 64,
        docker_network="robot-analysis-jobs",
        egress_proxy_url="http://proxy:3128",
        docker_data_dir=tmp_path / "docker-root",
        codex_model="deepseek-v4-flash",
        codex_provider_url=None,
        codex_api_key_env="CODEX_API_KEY",
        codex_api_key="key",
        timeout_seconds=300,
        poll_seconds=1.0,
        runtime_dir=runtime_dir,
        wiki_dir=None,
        cpu_milli=4000,
        memory_mb=8192,
        disk_mb=24576,
        disk_reserve_mb=1024,
    )
    api = MockApiForProbes(store)
    return DockerWorker(
        config=config,
        api=api,
        runtime=runtime,
        grill_store=store,
        snapshot_dir=tmp_path / "snapshots",
        idle_timeout_seconds=300,
    )


# ============================================================================
# PROBE 1: Snapshot Tarball Generation & Atomic Write
# ============================================================================

def test_probe_1_snapshot_generates_valid_gzip_tarball_with_diverse_assets(tmp_path: Path):
    """Probe 1.1: Verify valid .tar.gz archive generation with diverse assets:

    nested paths, hidden directories, empty files, binary content.
    """
    runtime = create_sample_runtime(tmp_path)
    target = tmp_path / "snapshots" / "session_test.tar.gz"

    # Construct uncompressed tar stream representing docker cp stdout
    assets = {
        "scenario_state.json": json.dumps({"robot": "Walker_C1_EDU", "rev": 3}).encode(),
        ".codex/session.log": b"line1\nline2\nhidden record",
        ".codex/state.db": b"\x00\x01\x02\x03\xff\xfe binary db",
        "inputs/manual.pdf": b"%PDF-1.4 sample raw binary content",
        "inputs/nested/deep/spec.yaml": b"model: TienKung\npayload: 10kg",
        "empty_flag.txt": b"",
    }

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:") as tar:
        for name, data in assets.items():
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    uncompressed_stream = tar_buf.getvalue()

    fake_popen = FakePopen(stdout_data=uncompressed_stream, returncode=0)

    with patch("subprocess.Popen", return_value=fake_popen):
        res = runtime.snapshot_workspace(DockerJob("c_test", "v_test"), target)
        assert res == target
        assert target.is_file()

    # Verify archive is valid gzip
    with gzip.open(target, "rb") as gz:
        decompressed_header = gz.read(1024)
        assert len(decompressed_header) > 0

    # Verify tar contents match verbatim
    with tarfile.open(target, mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members.keys()) == set(assets.keys())
        for name, expected_bytes in assets.items():
            member = members[name]
            assert member.size == len(expected_bytes)
            if member.isreg():
                extracted = tar.extractfile(member).read()
                assert extracted == expected_bytes


def test_probe_1_snapshot_atomic_write_preserves_existing_on_failure(tmp_path: Path):
    """Probe 1.2: Verify atomic write semantics (.tmp.<uuid> -> final).

    If snapshot fails midway or Docker cp fails:
    1. The .tmp file must be deleted.
    2. Any pre-existing snapshot must NOT be overwritten or deleted.
    """
    runtime = create_sample_runtime(tmp_path)
    target = tmp_path / "snapshots" / "session_atomic.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)

    # 1. Pre-create an original valid snapshot
    original_content = b"original-valid-snapshot-data"
    target.write_bytes(original_content)
    assert target.exists()

    # 2. Simulate failure during snapshot (docker cp exits code 1)
    fail_popen = FakePopen(stdout_data=b"partial data", stderr_data=b"Docker daemon connection lost", returncode=1)

    with patch("subprocess.Popen", return_value=fail_popen):
        with pytest.raises(DockerError, match="Docker snapshot copy failed"):
            runtime.snapshot_workspace(DockerJob("c_fail", "v_fail"), target)

    # Original snapshot MUST remain intact and untampered
    assert target.exists()
    assert target.read_bytes() == original_content

    # No leftover .tmp files in directory
    tmp_files = list(target.parent.glob("*.tmp.*"))
    assert tmp_files == [], f"Found orphaned temp files: {tmp_files}"


def test_probe_1_snapshot_atomic_write_cleans_tmp_on_stream_exception(tmp_path: Path):
    """Probe 1.3: Verify that an unhandled stream reading exception unlinks .tmp file."""
    runtime = create_sample_runtime(tmp_path)
    target = tmp_path / "snapshots" / "session_stream_err.tar.gz"

    err_popen = FakePopen(stdout_data=b"some bytes", raise_on_read=IOError("Broken pipe / disk full"))

    with patch("subprocess.Popen", return_value=err_popen):
        with pytest.raises(DockerError, match="Failed to create workspace snapshot"):
            runtime.snapshot_workspace(DockerJob("c_err", "v_err"), target)

    assert not target.exists()
    tmp_files = list(target.parent.glob("*.tmp.*"))
    assert tmp_files == []


# ============================================================================
# PROBE 2: Restoration & Non-Root UID 10001 Remapping
# ============================================================================

def test_probe_2_restore_remaps_all_members_to_uid_gid_10001(tmp_path: Path):
    """Probe 2.1: Verify that every restored member (files, directories, empty files,

    symlinks) is explicitly remapped to UID 10001, GID 10001, with cleared user/group names.
    """
    runtime = create_sample_runtime(tmp_path)
    archive = tmp_path / "input_archive.tar.gz"

    # Create archive with root UID 0 and custom UIDs
    with tarfile.open(archive, mode="w:gz") as tar:
        # Directory
        d_info = tarfile.TarInfo(name="sub_dir")
        d_info.type = tarfile.DIRTYPE
        d_info.uid = 0
        d_info.gid = 0
        d_info.uname = "root"
        d_info.gname = "root"
        tar.addfile(d_info)

        # File with UID 0
        f1_data = b"root owned data"
        f1_info = tarfile.TarInfo(name="sub_dir/root_file.txt")
        f1_info.size = len(f1_data)
        f1_info.uid = 0
        f1_info.gid = 0
        f1_info.uname = "root"
        f1_info.gname = "root"
        tar.addfile(f1_info, io.BytesIO(f1_data))

        # File with UID 1000
        f2_data = b"user data"
        f2_info = tarfile.TarInfo(name="user_file.json")
        f2_info.size = len(f2_data)
        f2_info.uid = 1000
        f2_info.gid = 1000
        f2_info.uname = "ubuntu"
        f2_info.gname = "ubuntu"
        tar.addfile(f2_info, io.BytesIO(f2_data))

    # Capture what restore_workspace streams to docker cp's stdin
    captured_stdin = io.BytesIO()

    class FakeStdinWrapper:
        def write(self, b):
            captured_stdin.write(b)
        def close(self):
            pass
        @property
        def closed(self):
            return False

    class FakeDockerCpPopen:
        def __init__(self, *args, **kwargs):
            self.stdin = FakeStdinWrapper()
            self.returncode = 0
        def communicate(self, timeout=None):
            return b"", b""
        def poll(self):
            return self.returncode
        def kill(self):
            pass
        def wait(self):
            return 0

    with patch("subprocess.Popen", side_effect=FakeDockerCpPopen):
        runtime.restore_workspace(DockerJob("c_restore", "v_restore"), archive)

    # Read back the tar stream from captured stdin
    captured_stdin.seek(0)
    with tarfile.open(fileobj=captured_stdin, mode="r:") as streamed_tar:
        streamed_members = streamed_tar.getmembers()
        assert len(streamed_members) == 3

        for member in streamed_members:
            assert member.uid == 10001, f"Member {member.name} has UID {member.uid} != 10001"
            assert member.gid == 10001, f"Member {member.name} has GID {member.gid} != 10001"
            assert member.uname == "", f"Member {member.name} has uname {member.uname} != ''"
            assert member.gname == "", f"Member {member.name} has gname {member.gname} != ''"

        names = {m.name for m in streamed_members}
        assert "sub_dir" in names
        assert "sub_dir/root_file.txt" in names
        assert "user_file.json" in names


@pytest.mark.parametrize(
    "corrupt_kind, content, expected_msg",
    [
        ("zero_byte", b"", "Corrupted snapshot archive"),
        ("random_garbage", b"NOT_A_VALID_TAR_OR_GZIP_FILE_AT_ALL", "Corrupted snapshot archive"),
        ("truncated_gzip", gzip.compress(b"some tar content")[:15], "Corrupted snapshot archive"),
        ("valid_gzip_bad_tar", gzip.compress(b"just random bytes inside gzip"), "Corrupted snapshot archive"),
    ],
)
def test_probe_2_restore_handles_corrupted_tarball_variants(tmp_path: Path, corrupt_kind: str, content: bytes, expected_msg: str):
    """Probe 2.2: Test that all corrupted tarball variations raise DockerError and

    never cause unhandled crashes or undefined behavior.
    """
    runtime = create_sample_runtime(tmp_path)
    corrupted_file = tmp_path / f"corrupt_{corrupt_kind}.tar.gz"
    corrupted_file.write_bytes(content)

    with pytest.raises(DockerError, match=expected_msg):
        runtime.restore_workspace(DockerJob("c_corrupt", "v_corrupt"), corrupted_file)


def test_probe_2_restore_missing_file_raises_docker_error(tmp_path: Path):
    """Probe 2.3: Verify non-existent file raises DockerError with explicit message."""
    runtime = create_sample_runtime(tmp_path)
    missing = tmp_path / "non_existent_file.tar.gz"

    with pytest.raises(DockerError, match="Snapshot archive not found"):
        runtime.restore_workspace(DockerJob("c_miss", "v_miss"), missing)


# ============================================================================
# PROBE 3: Path Traversal (Zip-Slip / Tar-Slip)
# ============================================================================

@pytest.mark.parametrize(
    "malicious_path",
    [
        "../../../etc/passwd",
        "../../../../root/.ssh/id_rsa",
        "/etc/shadow",
        "/usr/local/bin/malicious",
        "inputs/../../../etc/crontab",
        "a/b/../../../../var/log/syslog",
        "..",
        "..\\..\\etc\\passwd",
    ],
)
def test_probe_3_restore_rejects_path_traversal_payloads(tmp_path: Path, malicious_path: str):
    """Probe 3: Verify path traversal payloads are caught by archive pre-scan

    and raise DockerError BEFORE invoking subprocess docker cp.
    """
    runtime = create_sample_runtime(tmp_path)
    traversal_tar = tmp_path / "traversal_attack.tar.gz"

    with tarfile.open(traversal_tar, mode="w:gz") as tar:
        payload_data = b"malicious content injection"
        ti = tarfile.TarInfo(name=malicious_path)
        ti.size = len(payload_data)
        tar.addfile(ti, io.BytesIO(payload_data))

    # Mock Popen to ensure it is NEVER called when path traversal is detected
    with patch("subprocess.Popen") as mock_popen:
        with pytest.raises(DockerError, match="Unsafe path in snapshot archive"):
            runtime.restore_workspace(DockerJob("c_traversal", "v_traversal"), traversal_tar)

        mock_popen.assert_not_called()


# ============================================================================
# PROBE 4: 5-Minute Inactivity Idle Check (299s vs 300s)
# ============================================================================

def test_probe_4_idle_check_exact_boundary_timing(tmp_path: Path):
    """Probe 4.1: Adversarial timing probe.

    - 0s, 150s, 298.9s, 299.0s, 299.99s idle -> container STAYS warm.
    - 300.0s, 300.01s idle -> container HIBERNATES cleanly:
      snapshot written, container & volume removed, store state updated.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = ProbingDockerRuntime()
    worker = create_probing_worker(tmp_path, store, runtime)

    session, token = store.create_session("Assembly floor test", referenced_robot="Walker_C1_EDU")
    sid = session["id"]

    # Run Turn 1
    ran = worker.run_once()
    assert ran is True
    assert sid in worker.grill_containers

    warm_info = worker.grill_containers[sid]
    cid = warm_info["container"].container
    vid = warm_info["container"].volume
    t0 = warm_info["last_activity"]

    assert cid in runtime.active_containers
    assert vid in runtime.active_volumes
    assert store.get_session(sid)["container_state"] == "warm"

    # Test sub-threshold timings
    for sub_delay in [0.0, 150.0, 298.9, 299.0, 299.99]:
        hib = worker.check_idle_containers(current_time=t0 + sub_delay)
        assert hib == [], f"Prematurely hibernated at delay {sub_delay}s"
        assert sid in worker.grill_containers
        assert cid in runtime.active_containers
        assert vid in runtime.active_volumes
        assert store.get_session(sid)["container_state"] == "warm"

    # Test threshold at exactly 300.0s
    hib_300 = worker.check_idle_containers(current_time=t0 + 300.0)
    assert hib_300 == [sid]
    assert sid not in worker.grill_containers

    # Verify clean removal of container and volume
    assert cid in runtime.removed_containers
    assert vid in runtime.removed_volumes
    assert cid not in runtime.active_containers
    assert vid not in runtime.active_volumes

    # Verify snapshot archive
    snapshot_file = worker.grill_snapshot_dir / f"{sid}.tar.gz"
    assert snapshot_file.is_file()

    # Verify store status
    sess_after = store.get_session(sid)
    assert sess_after["container_state"] == "hibernated"
    assert sess_after["snapshot_path"] == str(snapshot_file)
    assert sess_after["hibernated_at"] is not None


def test_probe_4_store_level_scanner_boundary_and_status_filtering(tmp_path: Path):
    """Probe 4.2: Verify GrillStore.check_and_hibernate_idle_sessions boundaries

    and verify it only hibernates active turns ('interviewing', 'ready_for_confirmation')
    and ignores 'completed' or 'failed' sessions.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")

    # Session 1: interviewing (should hibernate at 300s)
    s1, _ = store.create_session("Task 1")
    claim1 = store.worker_claim_grill("worker-test", {"cpu_milli": 2000, "memory_mb": 4096})
    store.worker_finish_grill(claim1["id"], "worker-test", "completed", {
        "scenario_state": {"rev": 1}, "questions": [{"id": "q1", "text": "?"}], "ready_for_readback": False
    })

    # Session 2: completed (should NOT hibernate even after 500s)
    s2, tok2 = store.create_session("Task 2")
    claim2 = store.worker_claim_grill("worker-test", {"cpu_milli": 2000, "memory_mb": 4096})
    store.worker_finish_grill(claim2["id"], "worker-test", "completed", {
        "scenario_state": {"rev": 25}, "questions": [], "ready_for_readback": True
    })
    store.confirm_and_finalize(s2["id"], tok2)
    assert store.get_session(s2["id"])["status"] == "completed"

    t_base = time.time()

    # At 299s: 0 hibernated
    res_299 = store.check_and_hibernate_idle_sessions(timeout_seconds=300, current_time=t_base + 299.0)
    assert res_299 == []

    # At 300s: only s1 hibernates, s2 remains untouched
    res_300 = store.check_and_hibernate_idle_sessions(timeout_seconds=300, current_time=t_base + 300.0)
    assert res_300 == [s1["id"]]

    assert store.get_session(s1["id"])["container_state"] == "hibernated"
    assert store.get_session(s2["id"])["container_state"] != "hibernated"


# ============================================================================
# PROBE 5: Cold Resumption & Alternating Lifecycle
# ============================================================================

def test_probe_5_cold_resumption_provisions_fresh_container_and_restores(tmp_path: Path):
    """Probe 5.1: Cold resumption verification:

    1. Turn 1 executes -> warm container C1.
    2. Idle 300s -> C1 hibernates to snapshot S1, C1 cleanly removed.
    3. User submits answers on hibernated session -> session marked analyzing, warm container_state.
    4. Worker claims turn -> creates fresh container C2 (C2 != C1), restores S1 to C2,
       stages customer_answers.json, executes runner, finishes Turn 2.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = ProbingDockerRuntime()
    worker = create_probing_worker(tmp_path, store, runtime)

    session, token = store.create_session("Autonomous pallet moving", referenced_robot="TienKung")
    sid = session["id"]

    # Turn 1
    worker.run_once()
    c1 = worker.grill_containers[sid]["container"].container
    t0 = worker.grill_containers[sid]["last_activity"]

    # Hibernate at 300s
    worker.check_idle_containers(current_time=t0 + 300.0)
    assert sid not in worker.grill_containers
    assert c1 in runtime.removed_containers

    snap_file = worker.grill_snapshot_dir / f"{sid}.tar.gz"
    assert snap_file.is_file()

    # User submits Turn 2 answers
    answers = [{"question_id": "q_turn_1", "selected_option": "Option A", "free_text": "Proceed with 24V supply", "unknown": False}]
    updated = store.submit_answers(sid, token, answers)
    assert updated["status"] == "analyzing"
    assert updated["container_state"] == "warm"

    # Worker executes Turn 2
    ran_turn2 = worker.run_once()
    assert ran_turn2 is True

    # Fresh container verification
    assert sid in worker.grill_containers
    c2 = worker.grill_containers[sid]["container"].container
    assert c2 != c1, f"Container was not fresh! Reused old {c1}"
    assert c2 in runtime.active_containers

    # Workspace restoration verification
    assert len(runtime.restores_performed) == 1
    restored_container, restored_snapshot = runtime.restores_performed[0]
    assert restored_container == c2
    assert restored_snapshot == snap_file

    # Staged answers verification
    copied = runtime.copied_files[c2]
    assert "customer_answers.json" in copied
    parsed_answers = json.loads(copied["customer_answers.json"].decode())
    assert parsed_answers[0]["question_id"] == "q_turn_1"
    assert parsed_answers[0]["free_text"] == "Proceed with 24V supply"


def test_probe_5_alternating_warm_and_cold_multi_turn_lifecycle(tmp_path: Path):
    """Probe 5.2: Multi-turn stress harness alternating between warm reuse (<5m)

    and cold resumption (>5m).
    Turn 1 (cold start) -> Turn 2 (warm reuse) -> Hibernate -> Turn 3 (cold resume)
    -> Turn 4 (warm reuse) -> Hibernate -> Turn 5 (cold resume).
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = ProbingDockerRuntime()
    worker = create_probing_worker(tmp_path, store, runtime)

    session, token = store.create_session("Multi-turn alternating lifecycle probe", referenced_robot="Walker_S2_EDU")
    sid = session["id"]

    # --- Turn 1: Initial cold creation ---
    worker.run_once()
    c1 = worker.grill_containers[sid]["container"].container
    assert len(runtime.created) == 1

    # --- Turn 2: Warm reuse within 60s (<300s) ---
    store.submit_answers(sid, token, [{"question_id": "q_turn_1", "selected_option": "Opt1", "free_text": None, "unknown": False}])
    worker.run_once()
    assert len(runtime.created) == 1  # Reused c1
    assert worker.grill_containers[sid]["container"].container == c1

    # --- Hibernate after 300s ---
    t2 = worker.grill_containers[sid]["last_activity"]
    worker.check_idle_containers(current_time=t2 + 300.0)
    assert sid not in worker.grill_containers
    assert c1 in runtime.removed_containers

    # --- Turn 3: Cold resumption ---
    store.submit_answers(sid, token, [{"question_id": "q_turn_2", "selected_option": "Opt2", "free_text": None, "unknown": False}])
    worker.run_once()
    assert len(runtime.created) == 2  # New container c2
    c2 = worker.grill_containers[sid]["container"].container
    assert c2 != c1
    assert len(runtime.restores_performed) == 1

    # --- Turn 4: Warm reuse within 120s (<300s) ---
    store.submit_answers(sid, token, [{"question_id": "q_turn_3", "selected_option": "Opt3", "free_text": None, "unknown": False}])
    worker.run_once()
    assert len(runtime.created) == 2  # Reused c2
    assert worker.grill_containers[sid]["container"].container == c2

    # --- Hibernate after 300s ---
    t4 = worker.grill_containers[sid]["last_activity"]
    worker.check_idle_containers(current_time=t4 + 300.0)
    assert sid not in worker.grill_containers
    assert c2 in runtime.removed_containers

    # --- Turn 5: Cold resumption ---
    store.submit_answers(sid, token, [{"question_id": "q_turn_4", "selected_option": "Opt4", "free_text": None, "unknown": False}])
    worker.run_once()
    assert len(runtime.created) == 3  # New container c3
    c3 = worker.grill_containers[sid]["container"].container
    assert c3 not in (c1, c2)
    assert len(runtime.restores_performed) == 2


def test_probe_5_cold_resumption_unauthorized_token_rejection(tmp_path: Path):
    """Probe 5.3: Submitting answers with invalid/unauthorized token on hibernated

    session must be rejected and must NOT resume container.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = ProbingDockerRuntime()
    worker = create_probing_worker(tmp_path, store, runtime)

    session, token = store.create_session("Security token probe", referenced_robot="Walker_Tienkung_DEX")
    sid = session["id"]

    worker.run_once()
    t0 = worker.grill_containers[sid]["last_activity"]
    worker.check_idle_containers(current_time=t0 + 300.0)

    # Attempt submit with wrong token
    with pytest.raises(ValueError, match="Session token invalid or unauthorized"):
        store.submit_answers(sid, "malicious-invalid-token", [{"question_id": "q1", "selected_option": "Opt", "free_text": None, "unknown": False}])

    # Session MUST remain hibernated
    sess = store.get_session(sid)
    assert sess["container_state"] == "hibernated"
    assert sess["status"] == "interviewing"
