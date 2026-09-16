import json
import tempfile
from pathlib import Path
from backend.grill_store import GrillStore
from sandbox import run_grill


def test_grill_turn_runner_execution(tmp_path):
    # Test run_grill execution directly in a simulated workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    job = {
        "job_type": "grill",
        "action": "turn",
        "session_id": "grill_runner_test",
        "turn_index": 1,
        "task_intent": "Move pallet from bay 1 to bay 5 with quad robot",
        "referenced_robot": "Unitree B2",
        "scenario_state": None,
        "customer_answers": [],
    }

    original_workspace = run_grill.WORKSPACE
    try:
        run_grill.WORKSPACE = workspace

        ret = run_grill.run_grill(job)
        assert ret == 0
        assert (workspace / "result.json").is_file()
        assert (workspace / "scenario_state.json").is_file()

        res = json.loads((workspace / "result.json").read_text())
        assert res["status"] == "completed"
        assert "scenario_state" in res
        assert len(res["questions"]) > 0
        assert res["questions"][0]["free_text"] is True
        assert res["questions"][0]["allow_unknown"] is True
        assert len(res["questions"][0]["options"]) == 3

        # Turn 2 execution with customer answer
        saved_state = res["scenario_state"]
        job_turn2 = {
            "job_type": "grill",
            "action": "turn",
            "session_id": "grill_runner_test",
            "turn_index": 2,
            "task_intent": "Move pallet from bay 1 to bay 5 with quad robot",
            "referenced_robot": "Unitree B2",
            "scenario_state": saved_state,
            "customer_answers": [
                {
                    "question_id": res["questions"][0]["id"],
                    "selected_option": res["questions"][0]["options"][0]["label"],
                }
            ],
        }

        ret2 = run_grill.run_grill(job_turn2)
        assert ret2 == 0
        res2 = json.loads((workspace / "result.json").read_text())
        assert res2["status"] == "completed"
        assert res2["ready_for_readback"] is True

        # Action: report
        job_report = {
            "job_type": "grill",
            "action": "report",
            "session_id": "grill_runner_test",
            "task_intent": "Move pallet from bay 1 to bay 5 with quad robot",
            "scenario_state": res2["scenario_state"],
        }
        ret3 = run_grill.run_grill(job_report)
        assert ret3 == 0
        res3 = json.loads((workspace / "result.json").read_text())
        assert res3["status"] == "completed"
        assert "report" in res3
        report = res3["report"]
        assert "scenario_summary" in report
        assert "capabilities" in report
        assert "architecture" in report
        assert "risk_matrix" in report
        assert len(report["capabilities"]["claims"]) > 0
        assert len(report["risk_matrix"]["risks"]) > 0
    finally:
        run_grill.WORKSPACE = original_workspace
