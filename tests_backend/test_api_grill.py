import os
import json
import pytest
from fastapi.testclient import TestClient

os.environ["ROBOT_WORKER_TOKEN"] = "worker-test-token"
from backend.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(
        db_path=":memory:",
        upload_dir=str(tmp_path / "uploads"),
        grill_db_path=":memory:",
        grill_upload_dir=str(tmp_path / "grill_uploads"),
    )
    return TestClient(app)


def test_grill_api_full_flow(client):
    # 1. Create session
    create_resp = client.post(
        "/api/grill/sessions",
        json={"task_intent": "Carry warehouse parts with quadruped robot", "referenced_robot": "Unitree B2"},
    )
    assert create_resp.status_code == 201, create_resp.text
    data = create_resp.json()
    assert "token" in data
    assert "session" in data
    token = data["token"]
    session_id = data["session"]["id"]
    assert data["session_url"] == f"/grill/s/{token}"

    # 2. Get session without token -> 403
    unauth_resp = client.get(f"/api/grill/sessions/{session_id}")
    assert unauth_resp.status_code == 403

    # Get session with Bearer token
    auth_resp = client.get(
        f"/api/grill/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert auth_resp.status_code == 200
    assert auth_resp.json()["id"] == session_id

    # 3. Upload a supporting markdown file
    upload_resp = client.put(
        f"/api/grill/sessions/{session_id}/files?name=spec.md",
        headers={"Authorization": f"Bearer {token}"},
        content=b"# Pallet Spec\nWeight: 4kg",
    )
    assert upload_resp.status_code == 201
    file_info = upload_resp.json()
    assert file_info["name"] == "spec.md"

    # Verify invalid file extension is rejected
    bad_upload = client.put(
        f"/api/grill/sessions/{session_id}/files?name=malicious.exe",
        headers={"Authorization": f"Bearer {token}"},
        content=b"binary",
    )
    assert bad_upload.status_code == 422

    worker_headers = {"Authorization": "Bearer worker-test-token", "X-Worker-ID": "worker-1"}

    # 4. Worker claims turn 1
    worker_claim_resp = client.post(
        "/api/worker/claim",
        headers=worker_headers,
        json={"worker_id": "worker-1", "capacity": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 20000}},
    )
    assert worker_claim_resp.status_code == 200
    claim_data = worker_claim_resp.json()
    assert claim_data["job"] is not None
    assert claim_data["job"]["job_type"] == "grill"
    assert claim_data["job"]["action"] == "turn"
    task_id = claim_data["job"]["id"]

    # 5. Worker finishes turn 1 with questions
    q1 = {
        "id": "q_mass",
        "text": "What is the weight range?",
        "target_ids": ["f_mass"],
        "why": "Determine payload requirements",
        "options": [
            {"label": "< 5kg", "interpretation": "Light payload"},
            {"label": "5-15kg", "interpretation": "Medium payload"},
            {"label": "> 15kg", "interpretation": "Heavy payload"},
        ],
        "free_text": True,
        "allow_unknown": True,
    }
    turn1_out = {
        "status": "completed",
        "scenario_state": {
            "schema_version": "1.0",
            "scenario_id": session_id,
            "revision": 1,
            "summary": "Carry warehouse parts draft",
            "fields": [],
            "root_id": "n_root",
            "nodes": [],
            "alternatives": [],
            "issues": [],
            "questions": [q1],
            "changes": {"added_ids": [], "updated_ids": [], "removed_ids": [], "affected_node_ids": []},
            "checks": {"validation": "passed", "blocking_issue_ids": [], "ready_for_readback": False},
        },
        "questions": [q1],
        "ready_for_readback": False,
        "summary": "Carry warehouse parts draft",
    }
    finish_resp = client.post(
        f"/api/worker/jobs/{task_id}/finish",
        headers=worker_headers,
        json={"status": "completed", "report": json.dumps(turn1_out)},
    )
    assert finish_resp.status_code == 200

    # 6. Customer checks active questions
    sess_resp = client.get(
        f"/api/grill/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    sess_obj = sess_resp.json()
    assert sess_obj["status"] == "interviewing"
    assert len(sess_obj["active_questions"]) == 1
    assert sess_obj["active_questions"][0]["id"] == "q_mass"

    # 7. Customer submits answers
    turn_resp = client.post(
        f"/api/grill/sessions/{session_id}/turns",
        headers={"Authorization": f"Bearer {token}"},
        json={"answers": [{"question_id": "q_mass", "selected_option": "< 5kg", "free_text": None, "unknown": False}]},
    )
    assert turn_resp.status_code == 200
    assert turn_resp.json()["status"] == "analyzing"

    # 8. Worker claims turn 2
    claim2_resp = client.post(
        "/api/worker/claim",
        headers=worker_headers,
        json={"worker_id": "worker-1", "capacity": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 20000}},
    )
    assert claim2_resp.status_code == 200
    claim2 = claim2_resp.json()["job"]
    assert claim2["action"] == "turn"
    assert claim2["turn_index"] == 2

    # Worker finishes turn 2 indicating ready for readback
    turn2_out = {
        "status": "completed",
        "scenario_state": turn1_out["scenario_state"],
        "questions": [],
        "ready_for_readback": True,
        "summary": "Scenario summary ready for customer confirmation.",
    }
    client.post(
        f"/api/worker/jobs/{claim2['id']}/finish",
        headers=worker_headers,
        json={"status": "completed", "report": json.dumps(turn2_out)},
    )

    # 9. Session is now ready_for_confirmation
    check_conf = client.get(
        f"/api/grill/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert check_conf.json()["status"] == "ready_for_confirmation"
    assert "ready for customer confirmation" in check_conf.json()["readback_summary"]

    # 10. Customer confirms scenario
    conf_resp = client.post(
        f"/api/grill/sessions/{session_id}/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirmation_note": "Confirmed without issues"},
    )
    assert conf_resp.status_code == 200
    assert conf_resp.json()["status"] == "analyzing"

    # 11. Worker claims report generation task
    claim_rep_resp = client.post(
        "/api/worker/claim",
        headers=worker_headers,
        json={"worker_id": "worker-1", "capacity": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 20000}},
    )
    claim_rep = claim_rep_resp.json()["job"]
    assert claim_rep["action"] == "report"

    # Worker finishes report
    final_report = {
        "scenario_summary": {"task": "Carry parts", "target_robot": "Unitree B2"},
        "capabilities": {"summary": "Capabilities verified", "claims": []},
        "architecture": {"summary": "ROS 2 Humble", "ros_nodes": []},
        "risk_matrix": {"summary": "Low risk", "risks": []},
    }
    client.post(
        f"/api/worker/jobs/{claim_rep['id']}/finish",
        headers=worker_headers,
        json={"status": "completed", "report": json.dumps(final_report)},
    )

    # 12. Customer checks completed session with final report
    final_sess_resp = client.get(
        f"/api/grill/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    final_obj = final_sess_resp.json()
    assert final_obj["status"] == "completed"
    assert final_obj["final_report"]["scenario_summary"]["target_robot"] == "Unitree B2"
    assert len(final_obj["turns"]) >= 2
