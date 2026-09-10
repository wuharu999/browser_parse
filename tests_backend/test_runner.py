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

    def test_deepseek_catalog_enables_native_subagents_without_claiming_text_model_vision(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"CODEX_PROVIDER_URL": "https://api.deepseek.com", "CODEX_PROVIDER_ENV_KEY": "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY": "test-secret-never-written"}):
            for model, modalities in (("deepseek-v4-flash", ["text"]), ("deepseek-v4-flash-vision-exp", ["text", "image"])):
                run_codex._write_config(model)
                config_path = Path(directory, ".codex/config.toml")
                config = tomllib.loads(config_path.read_text())
                catalog = json.loads(Path(config["model_catalog_json"]).read_text())["models"][0]
                self.assertEqual(catalog["slug"], model)
                self.assertEqual(catalog["input_modalities"], modalities)
                self.assertEqual(catalog["multi_agent_version"], "v2")
                self.assertFalse(config["model_providers"]["sandbox-provider"]["requires_openai_auth"])
                self.assertNotIn("test-secret-never-written", config_path.read_text())


if __name__ == "__main__":
    unittest.main()
