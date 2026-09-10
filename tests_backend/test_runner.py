"""Pure runner helpers only: these tests never execute a model or command."""
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox import run_codex


class RunnerTests(unittest.TestCase):
    def test_prompt_keeps_full_evidence_out_and_role_names_match(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)):
            prompt = run_codex._prompt({"description": "Check motor timeout", "language": "zh", "evidence": {"schemaVersion": "robot-log-evidence/v2", "evidence": [{"raw": "private full log body"}]}})
            self.assertIn("log_investigator", prompt)
            self.assertIn("evidence_reviewer", prompt)
            self.assertIn("Language: zh", prompt)
            self.assertNotIn("private full log body", prompt)

    def test_activity_excludes_raw_commands_and_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            event = {"type": "item.completed", "item": {"type": "command_execution", "command": "private shell command", "aggregated_output": "private log body"}}
            run_codex._activity(event, target)
            public = target.read_text()
            self.assertNotIn("private", public)
            self.assertIsNone(run_codex._agent_message({"item": {"type": "reasoning", "text": "private thoughts"}}))
            self.assertEqual(json.loads(public)["message"], "Codex activity: item.completed")

    def test_provider_configuration_stays_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"CODEX_PROVIDER_URL": "https://provider.invalid/v1", "CODEX_PROVIDER_ENV_KEY": "TEST_MODEL_KEY"}):
            run_codex._write_config("test-model")
            config = tomllib.loads(Path(directory, ".codex/config.toml").read_text())
            self.assertEqual(os.environ["CODEX_HOME"], str(Path(directory, ".codex")))
            self.assertEqual(config["model"], "test-model")
            self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 2)
            self.assertEqual(config["model_providers"]["sandbox-provider"]["env_key"], "TEST_MODEL_KEY")


if __name__ == "__main__":
    unittest.main()
