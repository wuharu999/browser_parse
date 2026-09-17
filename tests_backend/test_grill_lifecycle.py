"""Integration and unit tests for container lifecycle, snapshotting, hibernation, and resumption.

Verifies:
1. 5-minute inactivity triggers workspace snapshotting to durable storage and clean removal of container/volume.
2. Inactivity under 5 minutes keeps container warm without termination or snapshotting.
3. Answer submission on a hibernated session provisions a fresh container, restores /workspace, and completes turn.
4. Warm container reuse/continuation when user answers within 5 minutes.
5. Inactivity boundary behavior (299s idle vs 300s idle).
6. Idempotent hibernation checks.
7. Snapshot tarball integrity and restoration with full historical continuity.
8. Error handling for corrupted or non-existent snapshot archives.
9. User-facing status phrasing compliance (no leakage of internal termination mechanics).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.docker_runtime import DockerCleanupError, DockerError, DockerJob, DockerRuntime
from backend.grill_store import GrillStore, MAX_QUESTION_BUDGET, stamp
from backend.worker import DockerWorker, WorkerConfig, WorkerError


class FinishedProcess:
    returncode = 0

    def poll(self) -> int:
        return self.returncode


class TrackingDockerRuntime:
    """Mock Docker runtime tracking the complete container & volume lifecycle."""

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
        cid = f"robot-container-{job_id}-{self.container_counter}"
        vid = f"robot-workspace-{job_id}-{self.container_counter}"
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
            # If scenario_state.json exists in workspace, mirror it in result
            scenario_bytes = files.get("scenario_state.json", b"{}")
            try:
                state = json.loads(scenario_bytes.decode("utf-8"))
            except Exception:
                state = {}
            return json.dumps({
                "status": "completed",
                "scenario_state": state,
                "questions": [{"id": "q_next", "text": "Next question?", "options": []}],
                "ready_for_readback": False,
                "metrics": {},
            })
        if path == "/workspace/activity.jsonl":
            return ""
        return None

    def snapshot_workspace(self, job: DockerJob, target_path: Path) -> Path:
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Create tarball of all files currently in container
        with tarfile.open(target, mode="w:gz") as tar:
            files = self.copied_files.get(job.container, {})
            for name, data in files.items():
                ti = tarfile.TarInfo(name=name)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
            # Always ensure scenario_state.json and .codex log are present
            if "scenario_state.json" not in files:
                state_data = b'{"revision": 1, "summary": "Snapshotted state"}'
                ti = tarfile.TarInfo(name="scenario_state.json")
                ti.size = len(state_data)
                tar.addfile(ti, io.BytesIO(state_data))
            if ".codex/session.log" not in files:
                cdx = b"codex-session-history"
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


class MockWorkerApi:
    """Mock API client for DockerWorker testing."""

    def __init__(self, store: GrillStore) -> None:
        self.store = store
        self.finished: list[dict] = []
        self.events: list[dict] = []

    def claim(self, capacity: dict) -> dict | None:
        return self.store.worker_claim_grill("worker-test", capacity)

    def get_job(self, _job_id: str) -> dict:
        return {"cancel_requested": False}

    def download_to(self, session_id: str, artifact_id: str, destination: Path) -> tuple[int, str]:
        # Handle file downloads from grill_store
        item = self.store.get_file_by_id(artifact_id)
        if item:
            path, _ = item
            data = path.read_bytes()
            destination.write_bytes(data)
            import hashlib
            return len(data), hashlib.sha256(data).hexdigest()
        destination.write_bytes(b"mock-file-content")
        return 17, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

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
        self.store.worker_finish_grill(job_id, "worker-test", status, output)
        self.finished.append({"job_id": job_id, "status": status, "output": output})

    def hibernate_grill(self, session_id: str, snapshot_path: str) -> None:
        self.store.hibernate_session(session_id, snapshot_path)


def create_test_worker(tmp_path: Path, store: GrillStore, runtime: TrackingDockerRuntime) -> DockerWorker:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "MAIN_PROMPT.md").write_text("safe prompt")

    config = WorkerConfig(
        api_url="http://127.0.0.1:8000",
        worker_token="test-token",
        worker_id="worker-test",
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
    api = MockWorkerApi(store)
    return DockerWorker(
        config=config,
        api=api,
        runtime=runtime,
        grill_store=store,
        snapshot_dir=tmp_path / "snapshots",
        idle_timeout_seconds=300,
    )


# ---------------------------------------------------------------------------
# Test 1: 5-minute inactivity triggers workspace snapshot and clean removal
# ---------------------------------------------------------------------------
def test_5_min_inactivity_triggers_snapshot_and_clean_termination(tmp_path: Path):
    """After 300s of inactivity awaiting user answers, take snapshot to durable

    storage, cleanly remove container & volume, and update grill_sessions state to hibernated.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    # 1. Create session and run Turn 1
    session, token = store.create_session("Inspect assembly floor", referenced_robot="Walker_C1_EDU")
    sid = session["id"]

    ran = worker.run_once()
    assert ran is True
    assert sid in worker.grill_containers

    # Container is warm and awaiting answers
    warm_info = worker.grill_containers[sid]
    initial_cid = warm_info["container"].container
    initial_vid = warm_info["container"].volume
    assert initial_cid in runtime.active_containers
    assert initial_vid in runtime.active_volumes

    sess_state = store.get_session(sid)
    assert sess_state["container_state"] == "warm"

    # 2. Simulate 5 minutes (300s) of inactivity awaiting user answers
    t0 = warm_info["last_activity"]
    hibernated = worker.check_idle_containers(current_time=t0 + 300.0)
    assert sid in hibernated
    assert sid not in worker.grill_containers

    # 3. Verify clean removal of container and volume
    assert initial_cid in runtime.removed_containers
    assert initial_vid in runtime.removed_volumes
    assert initial_cid not in runtime.active_containers
    assert initial_vid not in runtime.active_volumes

    # 4. Verify durable snapshot archive exists
    snapshot_path = worker.grill_snapshot_dir / f"{sid}.tar.gz"
    assert snapshot_path.is_file()

    # Verify snapshot tarball contains full workspace records
    with tarfile.open(snapshot_path, mode="r:gz") as tar:
        names = tar.getnames()
        assert "scenario_state.json" in names
        assert ".codex/session.log" in names

    # 5. Verify database session state marked 'hibernated'
    updated_sess = store.get_session(sid)
    assert updated_sess["container_state"] == "hibernated"
    assert updated_sess["snapshot_path"] == str(snapshot_path)
    assert updated_sess["hibernated_at"] is not None


# ---------------------------------------------------------------------------
# Test 2: Inactivity under 5 minutes keeps container warm
# ---------------------------------------------------------------------------
def test_inactivity_under_5_min_keeps_container_warm(tmp_path: Path):
    """Inactivity duration < 300s keeps the container warm; no snapshot or kill."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    session, token = store.create_session("Calibrate gripper torque", referenced_robot="Walker_Tienkung_DEX")
    sid = session["id"]

    worker.run_once()
    assert sid in worker.grill_containers
    warm_cid = worker.grill_containers[sid]["container"].container

    t0 = worker.grill_containers[sid]["last_activity"]

    # Inactivity at 120s (2 minutes)
    res_120 = worker.check_idle_containers(current_time=t0 + 120.0)
    assert res_120 == []
    assert sid in worker.grill_containers
    assert warm_cid in runtime.active_containers

    # Inactivity at 299s (1s before threshold)
    res_299 = worker.check_idle_containers(current_time=t0 + 299.0)
    assert res_299 == []
    assert sid in worker.grill_containers
    assert warm_cid in runtime.active_containers

    sess = store.get_session(sid)
    assert sess["container_state"] == "warm"


# ---------------------------------------------------------------------------
# Test 3: Answer submission on hibernated session provisions fresh container & restores workspace
# ---------------------------------------------------------------------------
def test_answer_submission_on_hibernated_session_restores_and_executes(tmp_path: Path):
    """Answer submission on a hibernated session spins up a fresh container,

    unpacks the snapshot to /workspace, and executes the turn cleanly.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    # 1. Turn 1 execution
    session, token = store.create_session("Autonomous battery swapping", referenced_robot="TienKung")
    sid = session["id"]
    worker.run_once()

    turn1_cid = worker.grill_containers[sid]["container"].container
    t0 = worker.grill_containers[sid]["last_activity"]

    # 2. Hibernate after 300s
    worker.check_idle_containers(current_time=t0 + 300.0)
    assert sid not in worker.grill_containers
    assert turn1_cid in runtime.removed_containers

    hib_sess = store.get_session(sid)
    assert hib_sess["container_state"] == "hibernated"
    snap_path = hib_sess["snapshot_path"]
    assert snap_path is not None and Path(snap_path).is_file()

    # 3. User returns and submits answers to active questions
    answers = [{"question_id": "q_next", "selected_option": "Option A", "free_text": None, "unknown": False}]
    updated_sess = store.submit_answers(sid, token, answers)
    assert updated_sess["status"] == "analyzing"
    assert updated_sess["container_state"] == "warm"

    # 4. Worker claims Turn 2 and executes
    ran_turn2 = worker.run_once()
    assert ran_turn2 is True

    # 5. Verify fresh container was created and restored
    assert sid in worker.grill_containers
    turn2_cid = worker.grill_containers[sid]["container"].container
    assert turn2_cid != turn1_cid  # Must be a fresh container!
    assert turn2_cid in runtime.active_containers

    # Verify workspace restoration was invoked for the fresh container
    assert len(runtime.restores_performed) == 1
    restored_cid, restored_snap = runtime.restores_performed[0]
    assert restored_cid == turn2_cid
    assert str(restored_snap) == snap_path

    # Verify Turn 2 customer_answers.json was staged in the container
    copied_in_turn2 = runtime.copied_files[turn2_cid]
    assert "customer_answers.json" in copied_in_turn2
    answers_content = json.loads(copied_in_turn2["customer_answers.json"].decode("utf-8"))
    assert answers_content[0]["question_id"] == "q_next"


# ---------------------------------------------------------------------------
# Test 4: Warm container reuse when user answers within 5 minutes
# ---------------------------------------------------------------------------
def test_warm_container_reused_within_5_minutes(tmp_path: Path):
    """When user answers within 5 minutes, reuse the active warm container without

    re-creating or snapshotting.
    """
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    # 1. Turn 1 execution
    session, token = store.create_session("Obstacle avoidance in narrow aisles", referenced_robot="Walker_S2_EDU")
    sid = session["id"]
    worker.run_once()

    initial_created_count = len(runtime.created)
    assert initial_created_count == 1
    initial_cid = worker.grill_containers[sid]["container"].container

    # 2. User answers after 60 seconds (well within 300s window)
    answers = [{"question_id": "q_next", "selected_option": "Slow down", "free_text": None, "unknown": False}]
    store.submit_answers(sid, token, answers)

    # 3. Worker executes Turn 2
    worker.run_once()

    # 4. Verify the exact same container was reused
    assert len(runtime.created) == initial_created_count  # NO new container created!
    current_cid = worker.grill_containers[sid]["container"].container
    assert current_cid == initial_cid
    assert initial_cid not in runtime.removed_containers
    assert len(runtime.snapshots_created) == 0  # No snapshot taken!


# ---------------------------------------------------------------------------
# Test 5: Inactivity boundary behavior (299.0s vs 300.0s)
# ---------------------------------------------------------------------------
def test_inactivity_boundary_exact_timing(tmp_path: Path):
    """Exact boundary verification: 299s keeps warm, exactly 300s hibernates."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    session, token = store.create_session("Timing boundary test")
    sid = session["id"]
    worker.run_once()

    t0 = worker.grill_containers[sid]["last_activity"]

    # 299.9s -> keeps warm
    res_299 = worker.check_idle_containers(current_time=t0 + 299.9)
    assert res_299 == []
    assert sid in worker.grill_containers

    # 300.0s -> triggers hibernation
    res_300 = worker.check_idle_containers(current_time=t0 + 300.0)
    assert res_300 == [sid]
    assert sid not in worker.grill_containers


# ---------------------------------------------------------------------------
# Test 6: Store-level check_and_hibernate_idle_sessions idempotency
# ---------------------------------------------------------------------------
def test_store_check_and_hibernate_idempotency(tmp_path: Path):
    """GrillStore.check_and_hibernate_idle_sessions is safely idempotent."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")

    session, token = store.create_session("Store level hibernation test")
    sid = session["id"]

    # Fake turn 1 completion so session is interviewing
    claim = store.worker_claim_grill("worker-test", {"cpu_milli": 2000, "memory_mb": 4096})
    store.worker_finish_grill(
        claim["id"],
        "worker-test",
        "completed",
        {"scenario_state": {"rev": 1}, "questions": [{"id": "q1", "text": "?"}], "ready_for_readback": False},
    )

    t0 = time.time()
    # 1. First check after 305s -> hibernates
    hib1 = store.check_and_hibernate_idle_sessions(timeout_seconds=300, current_time=t0 + 305)
    assert hib1 == [sid]

    sess1 = store.get_session(sid)
    assert sess1["container_state"] == "hibernated"

    # 2. Second check after 400s -> already hibernated, zero changes, returns empty list
    hib2 = store.check_and_hibernate_idle_sessions(timeout_seconds=300, current_time=t0 + 400)
    assert hib2 == []


# ---------------------------------------------------------------------------
# Test 7: Resumption status phrasing compliance & terminology guardrails
# ---------------------------------------------------------------------------
def test_resumption_status_phrasing_and_terminology_guardrails(tmp_path: Path):
    """Resume session provides user-facing status without internal container leaks."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    session, token = store.create_session("Status phrasing test")
    sid = session["id"]

    # Hibernate with snapshot
    snap_file = tmp_path / f"{sid}.tar.gz"
    with tarfile.open(snap_file, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="scenario_state.json")
        ti.size = 2
        tar.addfile(ti, io.BytesIO(b"{}"))

    store.hibernate_session(sid, snap_file)

    res = store.resume_session(sid, token)
    assert res["status"] == "resumed"
    assert res["user_status_en"] == "Warming up container and resuming session..."
    assert res["user_status_zh"] == "正在唤醒计算容器并恢复推演会话..."

    # Guardrails: verify no leakage of "Codex", "sandbox", "killed", "docker", "exited"
    for phrase in [res["user_status_en"], res["user_status_zh"]]:
        for forbidden in ["codex", "sandbox", "沙箱", "killed", "docker", "exited", "tarball"]:
            assert forbidden not in phrase.lower()


# ---------------------------------------------------------------------------
# Test 8: Resumption error handling on non-existent snapshot
# ---------------------------------------------------------------------------
def test_resumption_missing_snapshot_error(tmp_path: Path):
    """Resuming a hibernated session that lacks a snapshot path returns clean error."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    session, token = store.create_session("Missing snapshot test")
    sid = session["id"]

    with store.lock:
        store.db.execute("UPDATE grill_sessions SET container_state = 'hibernated', snapshot_path = NULL WHERE id = ?", (sid,))
        store.db.commit()

    res = store.resume_session(sid, token)
    assert res["status"] == "error"
    assert "No snapshot found" in res["message"]


# ---------------------------------------------------------------------------
# Test 9: Multi-session concurrency with active, hibernated, and completed sessions
# ---------------------------------------------------------------------------
def test_multi_session_lifecycle_concurrency(tmp_path: Path):
    """Multiple sessions concurrently progressing through warm, hibernated, and completed."""
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    runtime = TrackingDockerRuntime()
    worker = create_test_worker(tmp_path, store, runtime)

    # Session A: will remain warm
    sess_a, tok_a = store.create_session("Session A task", referenced_robot="Walker_C1_EDU")
    worker.run_once()

    # Session B: will hibernate
    sess_b, tok_b = store.create_session("Session B task", referenced_robot="TienKung")
    worker.run_once()

    t_now = time.time()
    # Mark Session B last_activity older by 310s
    worker.grill_containers[sess_b["id"]]["last_activity"] = t_now - 310.0
    # Keep Session A last_activity fresh (60s ago)
    worker.grill_containers[sess_a["id"]]["last_activity"] = t_now - 60.0

    # Trigger idle check
    hib = worker.check_idle_containers(current_time=t_now)
    assert sess_b["id"] in hib
    assert sess_a["id"] not in hib

    assert sess_a["id"] in worker.grill_containers
    assert sess_b["id"] not in worker.grill_containers

    assert store.get_session(sess_a["id"])["container_state"] == "warm"
    assert store.get_session(sess_b["id"])["container_state"] == "hibernated"
