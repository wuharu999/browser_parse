import pytest
from backend.grill_store import GrillStore, MAX_QUESTION_BUDGET


def test_grill_store_lifecycle(tmp_path):
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")

    # 1. Create session
    session, token = store.create_session("Inspect electrical cabinet with quad robot")
    assert session["id"].startswith("grill_")
    assert session["status"] == "intake_pending"
    assert session["question_count"] == 0
    assert session["current_revision"] == 1
    assert "token_hash" not in session

    # 2. Token verification
    assert store.verify_token(session["id"], token) is True
    assert store.verify_token(session["id"], "wrong_token") is False
    assert store.verify_token("unknown_id", token) is False

    # 3. Add file
    f = store.add_file(
        session_id=session["id"],
        name="spec.pdf",
        stored_name="spec_123.pdf",
        size=1024,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        mime_type="application/pdf",
    )
    assert f["name"] == "spec.pdf"
    files = store.get_files(session["id"])
    assert len(files) == 1

    # 4. Worker claims turn 1
    capacity = {"cpu_milli": 2000, "memory_mb": 4096}
    claim = store.worker_claim_grill("worker-1", capacity)
    assert claim is not None
    assert claim["action"] == "turn"
    assert claim["turn_index"] == 1
    assert claim["session_id"] == session["id"]

    # 5. Worker finishes turn 1 with questions
    q1 = {
        "id": "q_001",
        "text": "What is the weight of the inspection payload?",
        "target_ids": ["f_payload"],
        "why": "Determine robot payload sufficiency",
        "options": [
            {"label": "< 2kg", "interpretation": "Lightweight sensor payload"},
            {"label": "2kg - 5kg", "interpretation": "Medium sensor payload"},
            {"label": "> 5kg", "interpretation": "Heavy payload requiring crane or high-capacity arm"},
        ],
        "free_text": True,
        "allow_unknown": True,
    }
    state1 = {
        "schema_version": "1.0",
        "scenario_id": session["id"],
        "revision": 1,
        "summary": "Quad robot inspection",
        "fields": [],
        "root_id": "n_root",
        "nodes": [],
        "alternatives": [],
        "issues": [],
        "questions": [q1],
        "changes": {"added_ids": [], "updated_ids": [], "removed_ids": [], "affected_node_ids": []},
        "checks": {"validation": "passed", "blocking_issue_ids": [], "ready_for_readback": False},
    }
    store.worker_finish_grill(
        task_id=claim["id"],
        worker_id="worker-1",
        status="completed",
        output={"scenario_state": state1, "questions": [q1], "ready_for_readback": False},
    )

    sess = store.get_session(session["id"])
    assert sess["status"] == "interviewing"
    assert len(sess["active_questions"]) == 1
    assert sess["active_questions"][0]["id"] == "q_001"

    # 6. Customer submits answers
    answers = [{"question_id": "q_001", "selected_option": "< 2kg", "free_text": None, "unknown": False}]
    updated_sess = store.submit_answers(session["id"], token, answers)
    assert updated_sess["status"] == "analyzing"
    assert updated_sess["question_count"] == 1

    # 7. Worker claims turn 2
    claim2 = store.worker_claim_grill("worker-1", capacity)
    assert claim2 is not None
    assert claim2["action"] == "turn"
    assert claim2["turn_index"] == 2
    assert len(claim2["customer_answers"]) == 1

    # 8. Worker finishes turn 2 with readback ready
    store.worker_finish_grill(
        task_id=claim2["id"],
        worker_id="worker-1",
        status="completed",
        output={
            "scenario_state": state1,
            "questions": [],
            "ready_for_readback": True,
            "summary": "Scenario verified: Quad robot inspecting cabinet with < 2kg payload.",
        },
    )

    sess2 = store.get_session(session["id"])
    assert sess2["status"] == "ready_for_confirmation"
    assert sess2["readback_summary"] == "Scenario verified: Quad robot inspecting cabinet with < 2kg payload."

    # 9. Customer confirms scenario
    confirmed = store.confirm_scenario(session["id"], token)
    assert confirmed["status"] == "analyzing"

    # 10. Worker claims report task
    claim_report = store.worker_claim_grill("worker-1", capacity)
    assert claim_report is not None
    assert claim_report["action"] == "report"

    # 11. Worker finishes report
    final_rep = {
        "scenario_summary": {"task": "Quad inspection", "target_robot": "Unitree B2"},
        "capabilities": {"claims": []},
        "architecture": {"ros_nodes": []},
        "risk_matrix": {"risks": []},
    }
    store.worker_finish_grill(
        task_id=claim_report["id"],
        worker_id="worker-1",
        status="completed",
        output={"report": final_rep},
    )

    final_sess = store.get_session(session["id"])
    assert final_sess["status"] == "completed"
    assert final_sess["final_report"]["target_robot"] == "Unitree B2"


def test_question_budget_hard_stop(tmp_path):
    assert MAX_QUESTION_BUDGET == 25
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")
    session, token = store.create_session("Long conversation task")

    # Claim turn 1
    claim = store.worker_claim_grill("worker-1", {"cpu_milli": 2000, "memory_mb": 4096})
    # Set question_count right near max (24)
    with store.lock:
        store.db.execute("UPDATE grill_sessions SET question_count = 24 WHERE id = ?", (session["id"],))
        store.db.commit()

    q = [{"id": "q_last", "text": "Final question?", "target_ids": [], "why": "", "options": [], "free_text": True, "allow_unknown": True}]
    store.worker_finish_grill(claim["id"], "worker-1", "completed", {"scenario_state": {}, "questions": q, "ready_for_readback": False})

    # Submitting answer should hit hard stop at 25
    ans = [{"question_id": "q_last", "selected_option": None, "free_text": "Yes", "unknown": False}]
    updated = store.submit_answers(session["id"], token, ans)
    assert updated["question_count"] == 25
    assert updated["status"] == "analyzing"


def test_list_sessions_store(tmp_path):
    store = GrillStore(db_path=":memory:", upload_dir=tmp_path / "uploads")

    # Initially empty
    items, next_cursor = store.list_sessions()
    assert items == []
    assert next_cursor is None

    # Create 3 sessions
    sess1, tok1 = store.create_session("Task 1", referenced_robot="Walker_Tienkung_DEX")
    sess2, tok2 = store.create_session("Task 2", referenced_robot="Walker_C1_EDU")
    sess3, tok3 = store.create_session("Task 3", referenced_robot="TienKung")

    # List all
    items, next_cursor = store.list_sessions(limit=10)
    assert len(items) == 3
    assert next_cursor is None

    # Verify fields
    for it in items:
        assert "id" in it
        assert "token" in it
        assert "status" in it
        assert "question_count" in it
        assert "task_intent" in it
        assert "referenced_robot" in it
        assert "created_at" in it
        assert "updated_at" in it
        assert "finished_at" in it

    # Verify token matches raw token
    id_to_token = {sess1["id"]: tok1, sess2["id"]: tok2, sess3["id"]: tok3}
    for it in items:
        assert it["token"] == id_to_token[it["id"]]

    # Test pagination
    p1, cur1 = store.list_sessions(limit=2)
    assert len(p1) == 2
    assert cur1 is not None

    p2, cur2 = store.list_sessions(limit=2, cursor=int(cur1))
    assert len(p2) == 1
    assert cur2 is None
    assert p2[0]["id"] == items[2]["id"]
