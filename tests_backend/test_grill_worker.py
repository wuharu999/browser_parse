import json
import tempfile
import pytest
from pathlib import Path
from backend.grill_store import GrillStore
from sandbox import run_grill


@pytest.fixture(autouse=True)
def demo_runner(monkeypatch):
    monkeypatch.setenv('ROBOT_GRILL_USE_FALLBACK', '1')


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
        assert "system_architecture" in report
        assert "risk_matrix" in report
        assert report["assessment_status"] == "incomplete"
        assert report["capabilities"]["claims"] == []
        assert report["risk_matrix"]["risks"] == []
    finally:
        run_grill.WORKSPACE = original_workspace


def test_grill_turn_runner_writes_codex_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace_cfg"
    workspace.mkdir()

    monkeypatch.setenv("CODEX_PROVIDER_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ROBOT_CODEX_MODEL", "deepseek-flash")
    monkeypatch.setenv("CODEX_PROVIDER_ENV_KEY", "DEEPSEEK_API_KEY")

    job = {
        "job_type": "grill",
        "action": "turn",
        "session_id": "grill_cfg_test",
        "turn_index": 1,
        "task_intent": "Inspect warehouse",
        "referenced_robot": "Walker_Tienkung_DEX",
        "scenario_state": None,
        "customer_answers": [],
    }

    original_workspace = run_grill.WORKSPACE
    try:
        run_grill.WORKSPACE = workspace
        ret = run_grill.run_grill(job)
        assert ret == 0

        config_path = workspace / ".codex" / "config.toml"
        assert config_path.is_file()
        content = config_path.read_text()
        assert 'model_provider = "sandbox-provider"' in content
        assert "https://api.deepseek.com" in content
        assert 'model = "deepseek-flash"' in content

        # DeepSeek catalog should be generated
        models_path = workspace / ".codex" / "models.json"
        assert models_path.is_file()
        models_data = json.loads(models_path.read_text())
        assert any(m["slug"] == "deepseek-flash" for m in models_data.get("models", []))
    finally:
        run_grill.WORKSPACE = original_workspace


def test_run_codex_grill_writes_config(tmp_path, monkeypatch):
    from sandbox import run_codex
    workspace = tmp_path / "workspace_rc"
    workspace.mkdir()

    monkeypatch.setenv("CODEX_PROVIDER_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ROBOT_CODEX_MODEL", "deepseek-flash")
    monkeypatch.setenv("CODEX_PROVIDER_ENV_KEY", "DEEPSEEK_API_KEY")

    job = {
        "job_type": "grill",
        "action": "turn",
        "session_id": "codex_grill_test",
        "turn_index": 1,
        "task_intent": "Move pallet",
        "referenced_robot": "Walker_Tienkung_DEX",
        "scenario_state": None,
        "customer_answers": [],
    }
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))

    monkeypatch.setattr("sys.argv", ["run_codex.py", str(job_file)])
    orig_rc_ws = run_codex.WORKSPACE
    orig_rg_ws = run_grill.WORKSPACE
    try:
        run_codex.WORKSPACE = workspace
        run_grill.WORKSPACE = workspace
        ret = run_codex.main()
        assert ret == 0
        config_path = workspace / ".codex" / "config.toml"
        assert config_path.is_file()
        content = config_path.read_text()
        assert 'model_provider = "sandbox-provider"' in content
        assert "https://api.deepseek.com" in content
    finally:
        run_codex.WORKSPACE = orig_rc_ws
        run_grill.WORKSPACE = orig_rg_ws
