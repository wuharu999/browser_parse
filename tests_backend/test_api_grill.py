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


def test_grill_qa_unavailable_does_not_fabricate_an_answer(client):
    store = client.app.state.grill_store
    session, token = store.create_session('Choose a robot', defer_turn=True)
    store.db.execute("UPDATE grill_sessions SET status = 'completed' WHERE id = ?", (session['id'],))
    store.db.commit()
    client.app.state.chat.key = ''
    response = client.post(f"/api/grill/sessions/{session['id']}/questions", headers={'Authorization': f'Bearer {token}'}, json={'question': 'Is this safe?'})
    assert response.status_code == 503
    assert store.list_followup_questions(session['id'])['items'] == []


def test_grill_free_text_and_unknown_reach_worker(client):
    store = client.app.state.grill_store
    session, token = store.create_session('Clean glass')
    task = store.worker_claim_grill('worker', {'cpu_milli': 2000, 'memory_mb': 4096})
    questions = [{'id': 'q1', 'text': 'Which surface?', 'target_ids': ['f_surface']}, {'id': 'q2', 'text': 'Which robot?', 'target_ids': ['f_robot']}]
    store.worker_finish_grill(task['id'], 'worker', 'completed', {'scenario_state': {}, 'questions': questions})
    response = client.post(f"/api/grill/sessions/{session['id']}/turns", headers={'Authorization': f'Bearer {token}'}, json={'answers': [{'question_id': 'q1', 'free_text_answer': 'Inner glass'}, {'question_id': 'q2', 'is_unknown': True}]})
    assert response.status_code == 200, response.text
    next_task = store.worker_claim_grill('worker', {'cpu_milli': 2000, 'memory_mb': 4096})
    assert next_task['customer_answers'][0]['free_text'] == 'Inner glass'
    assert next_task['customer_answers'][1]['unknown'] is True
    assert next_task['normalized_updates']['resolved_fields'][0]['value'] == 'Inner glass'
    assert next_task['normalized_updates']['unresolved_fields'] == ['f_robot']


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
    assert final_obj["final_report"]["target_robot"] == "Unitree B2"
    assert len(final_obj["turns"]) >= 2


def test_grill_api_defer_turn_and_start(client):
    # 1. Create session with defer_turn=True
    create_resp = client.post(
        "/api/grill/sessions",
        json={
            "task_intent": "Warehouse transport",
            "referenced_robot": "Walker_C1",
            "defer_turn": True,
        },
    )
    assert create_resp.status_code == 201
    data = create_resp.json()
    token = data["token"]
    session_id = data["session"]["id"]

    # Verify no worker task is claimed yet
    worker_headers = {"Authorization": "Bearer worker-test-token", "X-Worker-ID": "worker-1"}
    claim_resp = client.post(
        "/api/worker/claim",
        headers=worker_headers,
        json={"worker_id": "worker-1", "capacity": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 20000}},
    )
    assert claim_resp.json()["job"] is None

    # Upload file
    upload_resp = client.put(
        f"/api/grill/sessions/{session_id}/files?name=notes.txt",
        headers={"Authorization": f"Bearer {token}"},
        content=b"Speed limit: 1.2 m/s",
    )
    assert upload_resp.status_code == 201

    # Start session
    start_resp = client.post(
        f"/api/grill/sessions/{session_id}/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "queued"

    # Now worker claims the turn task
    claim_resp2 = client.post(
        "/api/worker/claim",
        headers=worker_headers,
        json={"worker_id": "worker-1", "capacity": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 20000}},
    )
    job = claim_resp2.json()["job"]
    assert job is not None
    assert job["session_id"] == session_id
    assert len(job["files"]) == 1
    assert job["files"][0]["name"] == "notes.txt"
    # Ensure SMALL_PROFILE has disk_mb matching resources.py (8192)
    assert job["resource_plan"]["disk_mb"] == 8192


def test_grill_list_sessions_endpoint(client):
    # 1. Initially empty
    empty_resp = client.get("/api/grill/sessions")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == {"items": [], "next_cursor": None}

    # 2. Create 3 sessions
    sess1_resp = client.post(
        "/api/grill/sessions",
        json={"task_intent": "Carry 5kg parts across assembly floor", "referenced_robot": "Walker_C1_EDU"},
    )
    assert sess1_resp.status_code == 201
    s1_data = sess1_resp.json()

    sess2_resp = client.post(
        "/api/grill/sessions",
        json={"task_intent": "Precision manipulation in cleanroom", "referenced_robot": "Walker_Tienkung_DEX"},
    )
    assert sess2_resp.status_code == 201
    s2_data = sess2_resp.json()

    sess3_resp = client.post(
        "/api/grill/sessions",
        json={"task_intent": "Outdoor patrol on rough terrain", "referenced_robot": "TienKung"},
    )
    assert sess3_resp.status_code == 201
    s3_data = sess3_resp.json()

    # 3. Query GET /api/grill/sessions
    list_resp = client.get("/api/grill/sessions")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    items = list_data["items"]
    assert len(items) == 3

    # Verify ordering created_at DESC
    assert items[0]["id"] == s3_data["session"]["id"]
    assert items[1]["id"] == s2_data["session"]["id"]
    assert items[2]["id"] == s1_data["session"]["id"]

    # Verify all required metadata fields and tokens
    expected_tokens = {
        s1_data["session"]["id"]: s1_data["token"],
        s2_data["session"]["id"]: s2_data["token"],
        s3_data["session"]["id"]: s3_data["token"],
    }
    for item in items:
        assert item["token"] == expected_tokens[item["id"]]
        assert "id" in item
        assert "status" in item
        assert "question_count" in item
        assert "task_intent" in item
        assert "referenced_robot" in item
        assert "created_at" in item
        assert "updated_at" in item
        assert "finished_at" in item

    # 4. Pagination with limit and cursor
    page1_resp = client.get("/api/grill/sessions?limit=2")
    assert page1_resp.status_code == 200
    page1 = page1_resp.json()
    assert len(page1["items"]) == 2
    cursor = page1["next_cursor"]
    assert cursor is not None

    page2_resp = client.get(f"/api/grill/sessions?limit=2&cursor={cursor}")
    assert page2_resp.status_code == 200
    page2 = page2_resp.json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["id"] == s1_data["session"]["id"]
    assert page2["next_cursor"] is None

    # 5. Invalid query parameters
    assert client.get("/api/grill/sessions?limit=0").status_code == 422
    assert client.get("/api/grill/sessions?limit=101").status_code == 422
    assert client.get("/api/grill/sessions?cursor=not_an_int").status_code == 422
    assert client.get("/api/grill/sessions?cursor=-1").status_code == 422

    # 6. Verify unauthenticated 403 is preserved on /api/grill/sessions/{id}
    detail_unauth = client.get(f"/api/grill/sessions/{items[0]['id']}")
    assert detail_unauth.status_code == 403

    # Authenticated detail with token works
    detail_auth = client.get(
        f"/api/grill/sessions/{items[0]['id']}",
        headers={"Authorization": f"Bearer {items[0]['token']}"},
    )
    assert detail_auth.status_code == 200
    assert detail_auth.json()["id"] == items[0]["id"]


def test_phased_wind_down_prompts():
    from sandbox.run_grill import build_turn_prompt

    # Normal turn under 15 questions
    p_norm = build_turn_prompt("Carry boxes", turn_index=2, question_count=5)
    assert "There are at most 10 more questions" not in p_norm
    assert "There are at most 5 questions" not in p_norm
    assert "Maximum question budget reached" not in p_norm

    # 15 questions: soft wind-down prompt injected
    p_15 = build_turn_prompt("Carry boxes", turn_index=5, question_count=15)
    assert "There are at most 10 more questions you could ask, but you don't have to hit 10 if you don't need it. If information is sufficient, proceed to summarize and finalize." in p_15

    # 20 questions: 5 questions left warning injected
    p_20 = build_turn_prompt("Carry boxes", turn_index=7, question_count=20)
    assert "There are at most 5 questions left to ask. Focus exclusively on critical unresolved decisions and prepare the final readback." in p_20

    # 25 questions: hard ceiling enforced with ready_for_readback and 0 questions
    p_25 = build_turn_prompt("Carry boxes", turn_index=9, question_count=25)
    assert "checks.ready_for_readback: true" in p_25
    assert "0 questions" in p_25

    # Confined to 4 supported robot models
    for prompt in [p_norm, p_15, p_20, p_25]:
        assert "Walker_Tienkung_DEX" in prompt
        assert "Walker_C1_EDU" in prompt
        assert "TienKung" in prompt
        assert "Walker_S2_EDU" in prompt
        assert "Codex must not recommend or assume any external or unsupported robot hardware." in prompt


def test_question_budget_ceiling_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('ROBOT_GRILL_USE_FALLBACK', '1')
    from sandbox import run_grill

    workspace = tmp_path / "workspace_budget"
    workspace.mkdir()

    job = {
        "job_type": "grill",
        "action": "turn",
        "session_id": "grill_ceiling_test",
        "turn_index": 10,
        "task_intent": "Move pallet with Walker_S2_EDU",
        "referenced_robot": "Walker_S2_EDU",
        "scenario_state": None,
        "customer_answers": [],
        "question_count": 25,
    }

    orig_ws = run_grill.WORKSPACE
    try:
        run_grill.WORKSPACE = workspace
        ret = run_grill.run_grill(job)
        assert ret == 0
        res = json.loads((workspace / "result.json").read_text())
        assert res["status"] == "completed"
        assert res["ready_for_readback"] is True
        assert res["questions"] == []
    finally:
        run_grill.WORKSPACE = orig_ws
