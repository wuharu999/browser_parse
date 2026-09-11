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
            self.assertIn("telemetry_investigator", prompt)
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
            self.assertEqual(json.loads(public)["message"], "Codex tool execution completed.")

    def test_activity_includes_only_completed_public_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            run_codex._activity({"type": "item.completed", "item": {"type": "agent_message", "channel": "final", "text": "Reviewed 3 records."}}, target)
            run_codex._activity({"type": "item.completed", "item": {"type": "reasoning", "text": "hidden chain of thought"}}, target)
            records = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual(records[0]["message"], "Reviewed 3 records.")
            self.assertEqual(len(records), 1)

    def test_activity_redacts_sensitive_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEST_API_TOKEN": "very-secret-value"}):
            target = Path(directory, "activity.jsonl")
            run_codex._activity({"type": "item.completed", "item": {"type": "agent_message", "channel": "commentary", "text": "token=very-secret-value Bearer abc.def.ghi sk-abcdefghijklmnop"}}, target)
            public = target.read_text()
            self.assertNotIn("very-secret-value", public)
            self.assertNotIn("abc.def.ghi", public)
            self.assertNotIn("sk-abcdefghijklmnop", public)

    def test_activity_ring_is_64kib_and_contains_complete_jsonl_records(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            for number in range(200):
                run_codex._activity({"type": "item.completed", "item": {"type": "agent_message", "channel": "commentary", "text": f"{number}: " + "x" * 790}}, target)
            raw = target.read_bytes()
            self.assertLessEqual(len(raw), 64 * 1024)
            records = [json.loads(line) for line in raw.decode().splitlines()]
            self.assertGreater(len(records), 1)
            self.assertEqual(records[-1]["seq"], 200)

    def test_final_report_keeps_long_structured_json_and_source_path(self):
        report = json.dumps({"schemaVersion": "robot-analysis/v1", "source": "archive/folder/log.txt", "findings": ["x" * 8500]})
        event = {"type": "item.completed", "item": {"type": "agent_message", "channel": "final", "text": report}}
        final = run_codex._agent_message(event)
        self.assertIsNotNone(final)
        self.assertGreater(len(final or ""), 800)
        self.assertEqual(json.loads(final or "") ["source"], "archive/folder/log.txt")

    def test_commentary_is_activity_only_and_unknown_channels_are_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            commentary = {"type": "item.completed", "item": {"type": "agent_message", "channel": "commentary", "text": "Public progress."}}
            unknown = {"type": "item.completed", "item": {"type": "agent_message", "channel": "other", "text": "must hide"}}
            run_codex._activity(commentary, target)
            run_codex._activity(unknown, target)
            self.assertIsNone(run_codex._agent_message(commentary))
            self.assertEqual([json.loads(line)["message"] for line in target.read_text().splitlines()], ["Public progress."])

    def test_installed_codex_json_agent_message_without_channel_is_public(self):
        # Matches the locally installed Codex SDK's documented JSONL item shape.
        event = {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hi!"}}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            run_codex._activity(event, target)
            self.assertEqual(json.loads(target.read_text())["message"], "Hi!")
        self.assertEqual(run_codex._agent_message(event), "Hi!")

    def test_provider_configuration_stays_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"CODEX_PROVIDER_URL": "https://provider.invalid/v1", "CODEX_PROVIDER_ENV_KEY": "TEST_MODEL_KEY"}):
            run_codex._write_config("test-model")
            config = tomllib.loads(Path(directory, ".codex/config.toml").read_text())
            self.assertEqual(os.environ["CODEX_HOME"], str(Path(directory, ".codex")))
            self.assertEqual(config["model"], "test-model")
            self.assertEqual(config["model_reasoning_effort"], "high")
            self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 3)
            self.assertEqual(config["model_providers"]["sandbox-provider"]["env_key"], "TEST_MODEL_KEY")

    def test_safe_agents_includes_telemetry_investigator(self):
        # R2.3: Safe agent whitelist must include telemetry_investigator for activity streaming and child metrics
        self.assertIn("telemetry_investigator", run_codex.SAFE_AGENTS)
        self.assertEqual(run_codex.SAFE_AGENTS, {"codex", "log_investigator", "telemetry_investigator", "evidence_reviewer"})

    def test_reasoning_effort_configuration_and_env_override(self):
        # R1.1: Default reasoning effort is "high", overridable via ROBOT_CODEX_REASONING_EFFORT
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)):
            run_codex._write_config("gpt-5.6-luna")
            config = tomllib.loads(Path(directory, ".codex/config.toml").read_text())
            self.assertEqual(config["model_reasoning_effort"], "high")

        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"ROBOT_CODEX_REASONING_EFFORT": "low"}):
            run_codex._write_config("gpt-5.6-luna")
            config = tomllib.loads(Path(directory, ".codex/config.toml").read_text())
            self.assertEqual(config["model_reasoning_effort"], "low")

        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"ROBOT_CODEX_REASONING_EFFORT": "invalid_value"}):
            run_codex._write_config("gpt-5.6-luna")
            config = tomllib.loads(Path(directory, ".codex/config.toml").read_text())
            self.assertEqual(config["model_reasoning_effort"], "high")

    def test_subagent_toml_configurations(self):
        # R1.1, R1.2, R2.1: Verify all 3 subagents exist, use low reasoning effort, and contain inlined diagnostic commands
        agents_dir = Path(__file__).parents[1] / "sandbox/runtime/.codex/agents"
        expected_agents = {
            "log-investigator.toml": "log_investigator",
            "telemetry-investigator.toml": "telemetry_investigator",
            "evidence-reviewer.toml": "evidence_reviewer",
        }
        for filename, expected_name in expected_agents.items():
            agent_file = agents_dir / filename
            self.assertTrue(agent_file.is_file(), f"Missing agent file {filename}")
            agent_data = tomllib.loads(agent_file.read_text())
            self.assertEqual(agent_data["name"], expected_name)
            self.assertEqual(agent_data["model_reasoning_effort"], "low")
            instructions = agent_data["developer_instructions"]
            self.assertIn("without reading SKILL.md", instructions)
            self.assertIn("rg", instructions)
            self.assertIn("head", instructions)
            self.assertIn("tail", instructions)

    def test_deepseek_catalog_has_current_vision_model_and_accurate_legacy_aliases(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"CODEX_PROVIDER_URL": "https://api.deepseek.com", "CODEX_PROVIDER_ENV_KEY": "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY": "test-secret-never-written"}):
            for model, modalities in (("deepseek-flash", ["text", "image"]), ("deepseek-v4-flash", ["text"]), ("deepseek-v4-pro", ["text"]), ("deepseek-v4-flash-vision-exp", ["text", "image"])):
                run_codex._write_config(model)
                config_path = Path(directory, ".codex/config.toml")
                config = tomllib.loads(config_path.read_text())
                catalog = json.loads(Path(config["model_catalog_json"]).read_text())["models"][0]
                self.assertEqual(catalog["slug"], model)
                self.assertIsInstance(catalog["base_instructions"], str)
                self.assertTrue(catalog["base_instructions"].strip())
                self.assertEqual(catalog["input_modalities"], modalities)
                self.assertEqual(catalog["supports_image_detail_original"], "image" in modalities)
                self.assertEqual(catalog["multi_agent_version"], "v2")
                self.assertFalse(config["model_providers"]["sandbox-provider"]["requires_openai_auth"])
                self.assertNotIn("test-secret-never-written", config_path.read_text())


if __name__ == "__main__":
    unittest.main()
