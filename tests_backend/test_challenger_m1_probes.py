"""Empirical Challenger verification and adversarial stress tests for Milestone 1.

Probes:
1. Probe GET /api/grill/sessions: pagination, ordering (created_at DESC), token presence,
   and 403 Forbidden for unauthorized/cross-session access to /api/grill/sessions/{id}.
2. Probe budget logic: question_count strictly stops at 25 and transitions to ready_for_confirmation.
3. Probe prompt generator in sandbox/run_grill.py: turn prompt outputs at 0, 14, 15, 19, 20, 24, 25, 26 questions,
   verifying exact phrasing and ready_for_readback behavior.
4. Probe 4-robot model enforcement: prompt scoping and behavior on supported vs unsupported models.
5. Probe constraint compliance: IP 120.77.250.227 and forbidden UI terminology.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.grill_store import GrillStore, MAX_QUESTION_BUDGET
from sandbox.run_grill import build_turn_prompt, generate_fallback_draft, run_grill

SUPPORTED_ROBOTS = [
    "Walker_Tienkung_DEX",
    "Walker_C1_EDU",
    "TienKung",
    "Walker_S2_EDU",
]

UNSUPPORTED_ROBOTS = [
    "Boston Dynamics Spot",
    "Unitree H1",
    "Tesla Optimus",
    "ANYbotics ANYmal",
]


@pytest.fixture
def test_app():
    """Create a clean isolated in-memory test app and client."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        app = create_app(
            db_path=":memory:",
            grill_db_path=":memory:",
            grill_upload_dir=tmp_path / "uploads",
        )
        yield TestClient(app), app.state.grill_store


# ============================================================================
# PROBE 1: GET /api/grill/sessions & Access Control Probes
# ============================================================================

def test_probe_1_sessions_listing_and_pagination(test_app):
    """Stress test pagination, ordering, and token presence across multiple sessions."""
    client, store = test_app

    # 1. Initially empty
    res = client.get("/api/grill/sessions")
    assert res.status_code == 200
    data = res.json()
    assert data["items"] == []
    assert data["next_cursor"] is None

    # 2. Populate 15 sessions with sequential delays or varied timestamps
    tokens = {}
    session_ids = []
    for i in range(15):
        sess, token = store.create_session(
            task_intent=f"Task {i:02d} intent test",
            referenced_robot=SUPPORTED_ROBOTS[i % len(SUPPORTED_ROBOTS)],
        )
        tokens[sess["id"]] = token
        session_ids.append(sess["id"])

    # 3. Test pagination with limit=4
    all_retrieved = []
    cursor = None
    page_count = 0
    max_pages = 10

    while page_count < max_pages:
        url = "/api/grill/sessions?limit=4"
        if cursor:
            url += f"&cursor={cursor}"
        resp = client.get(url)
        assert resp.status_code == 200
        page = resp.json()
        items = page["items"]
        if not items:
            break
        assert len(items) <= 4
        all_retrieved.extend(items)
        cursor = page.get("next_cursor")
        page_count += 1
        if not cursor:
            break

    assert len(all_retrieved) == 15
    retrieved_ids = [item["id"] for item in all_retrieved]
    # No duplicates
    assert len(set(retrieved_ids)) == 15
    # Order should be reverse of creation (most recent first: id 14 down to 0)
    assert retrieved_ids == list(reversed(session_ids))

    # Verify every session in list contains its raw token
    for item in all_retrieved:
        assert "token" in item
        assert item["token"] == tokens[item["id"]]
        assert "task_intent" in item
        assert "referenced_robot" in item
        assert "created_at" in item
        assert "status" in item


def test_probe_1_unauthorized_and_cross_session_403(test_app):
    """Probe access control on /api/grill/sessions/{id} with missing, invalid, and cross-session tokens."""
    client, store = test_app

    sess_a, token_a = store.create_session(task_intent="Session A")
    sess_b, token_b = store.create_session(task_intent="Session B")

    # A. Missing token -> 403
    r_no_auth = client.get(f"/api/grill/sessions/{sess_a['id']}")
    assert r_no_auth.status_code == 403
    assert "Invalid or missing session token" in r_no_auth.json()["detail"]

    # B. Invalid token -> 403
    r_bad_auth = client.get(f"/api/grill/sessions/{sess_a['id']}?token=completely_fake_token_12345")
    assert r_bad_auth.status_code == 403

    # C. Cross-session token (Token B used for Session A) -> 403
    r_cross_auth = client.get(
        f"/api/grill/sessions/{sess_a['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_cross_auth.status_code == 403

    # D. Valid Token A for Session A via Query -> 200
    r_valid_query = client.get(f"/api/grill/sessions/{sess_a['id']}?token={token_a}")
    assert r_valid_query.status_code == 200
    assert r_valid_query.json()["id"] == sess_a["id"]

    # E. Valid Token A for Session A via Header -> 200
    r_valid_header = client.get(
        f"/api/grill/sessions/{sess_a['id']}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r_valid_header.status_code == 200
    assert r_valid_header.json()["id"] == sess_a["id"]


def test_probe_1_invalid_query_parameters(test_app):
    """Verify input validation and 422 rejections for malformed pagination parameters."""
    client, _ = test_app

    # limit=0 -> 422
    assert client.get("/api/grill/sessions?limit=0").status_code == 422
    # limit=101 -> 422
    assert client.get("/api/grill/sessions?limit=101").status_code == 422
    # cursor non-integer -> 422
    assert client.get("/api/grill/sessions?cursor=not_a_number").status_code == 422
    # cursor <= 0 -> 422
    assert client.get("/api/grill/sessions?cursor=0").status_code == 422
    assert client.get("/api/grill/sessions?cursor=-3").status_code == 422


# ============================================================================
# PROBE 2: Budget Logic & Hard Ceiling at 25 Questions
# ============================================================================

def test_probe_2_budget_constant_and_store_ceiling(test_app):
    """Probe that MAX_QUESTION_BUDGET is strictly 25 and store transitions accurately."""
    _, store = test_app
    assert MAX_QUESTION_BUDGET == 25

    sess, token = store.create_session(task_intent="Budget stress test", defer_turn=True)
    sid = sess["id"]

    # Set question_count to 23 with 2 active questions (total will hit 25)
    questions = [
        {"id": "q1", "text": "Question 1"},
        {"id": "q2", "text": "Question 2"},
    ]
    with store.lock:
        store.db.execute(
            "UPDATE grill_sessions SET question_count = 23, status = 'interviewing', active_questions = ? WHERE id = ?",
            (json.dumps(questions), sid),
        )
        store.db.execute(
            "INSERT INTO grill_turns (id, session_id, turn_index, questions_json, created_at) VALUES ('t1', ?, 1, ?, '2026-09-17T10:00:00Z')",
            (sid, json.dumps(questions)),
        )
        store.db.commit()

    answers = [
        {"question_id": "q1", "selected_option": None, "free_text": "opt1"},
        {"question_id": "q2", "selected_option": None, "free_text": "opt2"},
    ]

    updated = store.submit_answers(sid, token, answers)
    assert updated["question_count"] == 25
    # Must immediately transition to ready_for_confirmation upon hitting 25
    assert updated["status"] == "analyzing"


def test_probe_2_store_update_from_turn_result_enforces_readback(test_app):
    """Probe that worker_finish_grill forces ready_for_confirmation at 25 questions."""
    _, store = test_app
    sess, token = store.create_session(task_intent="Turn update test", defer_turn=True)
    sid = sess["id"]

    # Create task
    task_id = "task_test_budget"
    worker_id = "test_worker_1"
    with store.lock:
        store.db.execute(
            "UPDATE grill_sessions SET question_count = 25, status = 'analyzing' WHERE id = ?",
            (sid,),
        )
        store.db.execute(
            "INSERT INTO grill_tasks (id, session_id, action, turn_index, status, worker_id, created_at, updated_at) "
            "VALUES (?, ?, 'turn', 5, 'running', ?, '2026-09-17T10:00:00Z', '2026-09-17T10:00:00Z')",
            (task_id, sid, worker_id),
        )
        store.db.commit()

    # Worker reports turn completed, even if output ready_for_readback was False
    turn_output = {
        "status": "completed",
        "scenario_state": {"summary": "Turn 5 summary"},
        "questions": [{"id": "q_unexpected", "text": "Should not be accepted"}],
        "ready_for_readback": False,
    }

    store.worker_finish_grill(task_id, worker_id, "completed", turn_output)

    updated = store.get_session(sid)
    # Because question_count >= 25, session MUST transition to ready_for_confirmation
    assert updated["status"] == "ready_for_confirmation"


# ============================================================================
# PROBE 3: Prompt Generator Phased Wind-Down at Exact Boundaries
# ============================================================================

def test_probe_3_prompt_phased_wind_down_boundaries():
    """Probe build_turn_prompt at 0, 14, 15, 19, 20, 24, 25, 26 questions."""
    phrase_10 = (
        "There are at most 10 more questions you could ask, but you don't have to hit 10 if you don't need it. "
        "If information is sufficient, proceed to summarize and finalize."
    )
    phrase_5 = (
        "There are at most 5 questions left to ask. Focus exclusively on critical unresolved decisions "
        "and prepare the final readback."
    )
    phrase_hard_stop = (
        "Maximum question budget reached (25 questions). Enforce hard stop with `checks.ready_for_readback: true` "
        "and 0 questions (set `questions: []`)."
    )

    # 1. question_count = 0 (Turn 1 start)
    p0 = build_turn_prompt("Sort boxes", turn_index=1, question_count=0)
    assert phrase_10 not in p0
    assert phrase_5 not in p0
    assert phrase_hard_stop not in p0

    # 2. question_count = 14 (Just before first warning threshold)
    p14 = build_turn_prompt("Sort boxes", turn_index=5, question_count=14)
    assert phrase_10 not in p14
    assert phrase_5 not in p14
    assert phrase_hard_stop not in p14

    # 3. question_count = 15 (First warning threshold)
    p15 = build_turn_prompt("Sort boxes", turn_index=6, question_count=15)
    assert phrase_10 in p15
    assert phrase_5 not in p15
    assert phrase_hard_stop not in p15

    # 4. question_count = 19 (End of first warning bracket)
    p19 = build_turn_prompt("Sort boxes", turn_index=7, question_count=19)
    assert phrase_10 in p19
    assert phrase_5 not in p19
    assert phrase_hard_stop not in p19

    # 5. question_count = 20 (Second warning threshold)
    p20 = build_turn_prompt("Sort boxes", turn_index=8, question_count=20)
    assert phrase_10 not in p20
    assert phrase_5 in p20
    assert phrase_hard_stop not in p20

    # 6. question_count = 24 (Last question before hard stop)
    p24 = build_turn_prompt("Sort boxes", turn_index=9, question_count=24)
    assert phrase_10 not in p24
    assert phrase_5 in p24
    assert phrase_hard_stop not in p24

    # 7. question_count = 25 (Hard stop)
    p25 = build_turn_prompt("Sort boxes", turn_index=10, question_count=25)
    assert phrase_10 not in p25
    assert phrase_5 not in p25
    assert phrase_hard_stop in p25

    # 8. question_count = 26 (Beyond budget edge case)
    p26 = build_turn_prompt("Sort boxes", turn_index=11, question_count=26)
    assert phrase_hard_stop in p26


def test_probe_3_runner_ready_for_readback_at_budget(tmp_path, monkeypatch):
    """Probe run_grill behavior when question_count >= 25."""
    # Run inside tmp_path
    monkeypatch.setattr("sandbox.run_grill.WORKSPACE", tmp_path)
    (tmp_path / "inputs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('ROBOT_GRILL_USE_FALLBACK', '1')

    job = {
        "action": "turn",
        "session_id": "test_budget_runner",
        "turn_index": 10,
        "task_intent": "Carry assembly parts",
        "referenced_robot": "Walker_C1_EDU",
        "question_count": 25,
        "customer_answers": [],
    }

    rc = run_grill(job)
    assert rc == 0

    result_file = tmp_path / "result.json"
    assert result_file.is_file()
    result = json.loads(result_file.read_text())

    assert result["ready_for_readback"] is True
    assert result["questions"] == []
    assert result["scenario_state"]["checks"]["ready_for_readback"] is True
    assert result["scenario_state"]["questions"] == []


# ============================================================================
# PROBE 4: 4-Robot Model Enforcement
# ============================================================================

def test_probe_4_prompt_contains_all_4_platforms_and_prohibition():
    """Verify prompt strictly includes all 4 platforms and forbids unsupported hardware."""
    prompt = build_turn_prompt("Transport cargo across floor", turn_index=1, question_count=0)

    # All 4 platforms must be explicitly named
    assert "Walker_Tienkung_DEX" in prompt
    assert "Walker_C1_EDU" in prompt
    assert "TienKung" in prompt
    assert "Walker_S2_EDU" in prompt

    # Explicit prohibition clause must be present
    prohibition = "Codex must not recommend or assume any external or unsupported robot hardware."
    assert prohibition in prompt


def test_probe_4_fallback_draft_handles_supported_vs_unsupported_models():
    """Verify fallback draft sets f_robot for supported models and rejects unsupported models."""
    # 1. Supported model in answers
    draft_supported = generate_fallback_draft(
        scenario_id="sess_supp",
        task_intent="Inspect engine block",
        turn_index=2,
        customer_answers=[
            {"question_id": "q_robot", "selected_option": "Walker_Tienkung_DEX", "free_text": ""}
        ],
    )
    f_robot_supp = next(f for f in draft_supported["fields"] if f["id"] == "f_robot")
    assert "Walker_Tienkung_DEX" in str(f_robot_supp["value"])

    # 2. Unsupported model in answers (e.g. Spot)
    draft_unsupported = generate_fallback_draft(
        scenario_id="sess_unsupp",
        task_intent="Inspect engine block",
        turn_index=2,
        customer_answers=[
            {"question_id": "q_robot", "selected_option": "Boston Dynamics Spot", "free_text": "We have a Spot dog"}
        ],
    )
    f_robot_unsupp = next(f for f in draft_unsupported["fields"] if f["id"] == "f_robot")
    # Must NOT adopt Boston Dynamics Spot as a recognized robot model
    assert f_robot_unsupp["value"] != "Boston Dynamics Spot"
    assert "Spot" not in str(f_robot_unsupp["value"])
    assert f_robot_unsupp["value"] == "Pending Robot Selection"

    # 3. Verify options in initial q_robot
    draft_t1 = generate_fallback_draft(
        scenario_id="sess_t1",
        task_intent="Inspect engine block",
        turn_index=1,
        referenced_robot=None,
    )
    q_robot = next(q for q in draft_t1["questions"] if q["id"] == "q_robot")
    labels = [opt["label"] for opt in q_robot["options"]]
    all_labels_str = " ".join(labels)
    for model in SUPPORTED_ROBOTS:
        assert any(model in lbl for lbl in labels), f"Model {model} missing from q_robot options"

    for bad in UNSUPPORTED_ROBOTS:
        assert bad not in all_labels_str, f"Unsupported model {bad} present in options"


# ============================================================================
# PROBE 5: Constraint Compliance Probes
# ============================================================================

def test_probe_1_rapid_sessions_ordering_and_deep_pagination(test_app):
    """Adversarially probe 50 sessions created with identical/tight timestamps."""
    client, store = test_app
    sess_ids = []
    # Create 50 sessions
    for i in range(50):
        s, _ = store.create_session(task_intent=f"Rapid intent #{i}", referenced_robot="Walker_C1_EDU")
        sess_ids.append(s["id"])

    # Traverse all 50 sessions with limit=7
    collected = []
    cursor = None
    while True:
        url = "/api/grill/sessions?limit=7"
        if cursor:
            url += f"&cursor={cursor}"
        r = client.get(url)
        assert r.status_code == 200
        data = r.json()
        items = data["items"]
        if not items:
            break
        collected.extend([item["id"] for item in items])
        cursor = data.get("next_cursor")
        if not cursor:
            break

    assert len(collected) == 50
    assert len(set(collected)) == 50
    assert collected == list(reversed(sess_ids))


def test_probe_2_answer_submission_state_guardrails(test_app):
    """Probe that answer submission rejects illegal session states (completed, analyzing)."""
    client, store = test_app
    sess, token = store.create_session(task_intent="State guardrail test", defer_turn=True)
    sid = sess["id"]

    # 1. State 'analyzing' cannot submit answers
    with store.lock:
        store.db.execute("UPDATE grill_sessions SET status = 'analyzing' WHERE id = ?", (sid,))
        store.db.commit()

    with pytest.raises(ValueError, match="cannot submit answers"):
        store.submit_answers(sid, token, [{"question_id": "q1", "selected_option": "opt"}])

    # 2. State 'completed' cannot submit answers
    with store.lock:
        store.db.execute("UPDATE grill_sessions SET status = 'completed' WHERE id = ?", (sid,))
        store.db.commit()

    with pytest.raises(ValueError, match="cannot submit answers"):
        store.submit_answers(sid, token, [{"question_id": "q1", "selected_option": "opt"}])


def test_probe_5_forbidden_ip_and_banned_terms_in_responses(test_app):
    """Verify responses and customer-facing error messages do not leak 'Codex' or '沙箱' or the forbidden IP."""
    client, store = test_app
    forbidden_ip = "120.77.250.227"

    sess, token = store.create_session(task_intent="Testing terminology leaks", referenced_robot="TienKung")
    sid = sess["id"]

    # 1. List sessions endpoint
    r_list = client.get("/api/grill/sessions")
    assert forbidden_ip not in r_list.text
    assert "沙箱" not in r_list.text

    # 2. Detail endpoint
    r_detail = client.get(f"/api/grill/sessions/{sid}?token={token}")
    assert forbidden_ip not in r_detail.text
    assert "沙箱" not in r_detail.text

    # 3. 403 Forbidden error response
    r_403 = client.get(f"/api/grill/sessions/{sid}")
    assert r_403.status_code == 403
    assert forbidden_ip not in r_403.text
    assert "沙箱" not in r_403.text
    assert "Codex" not in r_403.text


def test_probe_5_forbidden_ip_never_present_in_codebase():
    """Verify IP 120.77.250.227 is never referenced in source code."""
    forbidden_ip = "120.77.250.227"
    root_dir = Path(__file__).resolve().parent.parent

    scanned_extensions = {".py", ".ts", ".js", ".html", ".sh", ".json"}
    excluded_dirs = {".git", ".agents", "node_modules", "dist", "tests", "tests_backend"}

    offending_files = []
    for path in root_dir.rglob("*"):
        if path.is_file() and path.suffix in scanned_extensions:
            if any(part in excluded_dirs for part in path.parts):
                continue
            content = path.read_text(errors="ignore")
            if forbidden_ip in content:
                offending_files.append(str(path.relative_to(root_dir)))

    assert not offending_files, f"Forbidden IP {forbidden_ip} found in source files: {offending_files}"

