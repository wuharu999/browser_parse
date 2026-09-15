"""Pure runner helpers only: these tests never execute a model or command."""
import json
import os
import sqlite3
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

    def test_native_collaboration_tracks_all_children_without_exposing_payloads(self):
        event = {"type": "item.completed", "item": {"type": "collab_tool_call",
                 "tool": "spawn_agent", "status": "completed", "sender_thread_id": "parent",
                 "receiver_thread_ids": ["child-a", "child-b"], "prompt": "private task",
                 "agents_states": {"child-a": {"status": "running", "message": "private result"},
                                   "child-b": {"status": "pending_init"}}}}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            children = set()
            run_codex._activity(event, target, children)
            self.assertEqual(children, {"child-a", "child-b"})
            records = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual([r["seq"] for r in records], [1, 2])
            self.assertEqual([r["subagent"]["status"] for r in records], ["running", "pending_init"])
            self.assertTrue(all(r["subagent"]["parent_thread_id"] == "parent" for r in records))
            self.assertTrue(all(r["agent"] == "subagent" for r in records))
            self.assertNotIn("private", target.read_text())
            event["type"] = "item.updated"
            event["item"]["tool"] = "wait"
            event["item"]["agents_states"]["child-a"]["status"] = "completed"
            run_codex._activity(event, target, children)
            self.assertEqual(json.loads(target.read_text().splitlines()[-2])["subagent"]["status"], "completed")

    def test_native_collaboration_rejects_invalid_identity_and_does_not_invent_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "activity.jsonl")
            event = {"type": "item.started", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "status": "in_progress", "receiver_thread_ids": []}}
            run_codex._activity(event, target)
            self.assertFalse(target.exists())
            event["item"]["receiver_thread_ids"] = [None, {}, "bad id", "x" * 121, "good-id", "good-id"]
            event["item"]["agents_states"] = {"good-id": {"status": "unexpected private text"}}
            run_codex._activity(event, target)
            records = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["subagent"], {"thread_id": "good-id", "parent_thread_id": None, "status": "unknown", "tool": "spawn_agent"})
            self.assertNotIn("private", target.read_text())

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

    # -------------------------------------------------------------------------
    # Requirement R3: Sandbox Python Virtualenv Auto-Sourcing (_setup_virtualenv)
    # -------------------------------------------------------------------------

    def test_setup_virtualenv_detects_and_activates_environment(self):
        """R3.1 & R3.2: Verify virtualenv detection, PATH prepending, VIRTUAL_ENV export,
        .bashrc activation snippet generation, and BASH_ENV configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mock_venv = temp_path / "opt" / "analysis-venv"
            mock_bin = mock_venv / "bin"
            mock_bin.mkdir(parents=True, exist_ok=True)
            mock_workspace = temp_path / "workspace"
            mock_workspace.mkdir(parents=True, exist_ok=True)

            original_path = "/usr/local/bin:/usr/bin:/bin"
            clean_env = {"PATH": original_path}
            with patch.dict(os.environ, clean_env, clear=True):
                activated = run_codex._setup_virtualenv(venv_path=mock_venv, workspace=mock_workspace)
                self.assertTrue(activated, "Expected _setup_virtualenv to return True for valid venv with bin/")

                # 1. Check current process os.environ updates
                self.assertEqual(os.environ.get("VIRTUAL_ENV"), str(mock_venv))
                self.assertTrue(os.environ.get("PATH", "").startswith(f"{mock_bin}:"))
                self.assertIn(original_path, os.environ.get("PATH", ""))

                # 2. Check /workspace/.bashrc profile snippet
                bashrc_file = mock_workspace / ".bashrc"
                self.assertTrue(bashrc_file.is_file(), ".bashrc should be created in workspace")
                bashrc_content = bashrc_file.read_text()
                self.assertIn("Auto-activate analysis virtualenv", bashrc_content)
                self.assertIn(f'export VIRTUAL_ENV="{mock_venv}"', bashrc_content)
                self.assertIn(f'export PATH="{mock_bin}:$PATH"', bashrc_content)

                # 3. Check BASH_ENV is exported pointing to .bashrc for non-interactive sub-shells
                self.assertEqual(os.environ.get("BASH_ENV"), str(bashrc_file))

    def test_setup_virtualenv_nonexistent_returns_false_without_env_mutation(self):
        """R3.1: Verify non-existent virtualenv path returns False gracefully without altering environment."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            nonexistent_venv = temp_path / "nonexistent" / "venv"
            mock_workspace = temp_path / "workspace"
            mock_workspace.mkdir(parents=True, exist_ok=True)

            initial_path = "/usr/bin:/bin"
            with patch.dict(os.environ, {"PATH": initial_path}, clear=True):
                # Test non-existent path
                activated = run_codex._setup_virtualenv(venv_path=nonexistent_venv, workspace=mock_workspace)
                self.assertFalse(activated, "Non-existent venv path should return False")
                self.assertNotIn("VIRTUAL_ENV", os.environ)
                self.assertEqual(os.environ.get("PATH"), initial_path)
                self.assertNotIn("BASH_ENV", os.environ)

                # Test existing directory lacking bin/ subdir
                empty_dir = temp_path / "empty_dir"
                empty_dir.mkdir(parents=True, exist_ok=True)
                activated_empty = run_codex._setup_virtualenv(venv_path=empty_dir, workspace=mock_workspace)
                self.assertFalse(activated_empty, "Venv directory lacking bin/ subdir should return False")
                self.assertNotIn("VIRTUAL_ENV", os.environ)
                self.assertEqual(os.environ.get("PATH"), initial_path)

    def test_setup_virtualenv_idempotent_and_preserves_existing_bashrc(self):
        """R3.2: Verify .bashrc pre-existing contents are preserved, no duplicate snippets added, and PATH is not duplicated."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mock_venv = temp_path / "venv"
            mock_bin = mock_venv / "bin"
            mock_bin.mkdir(parents=True, exist_ok=True)
            mock_workspace = temp_path / "workspace"
            mock_workspace.mkdir(parents=True, exist_ok=True)

            # Pre-seed .bashrc with existing user aliases
            bashrc_file = mock_workspace / ".bashrc"
            bashrc_file.write_text("# Existing user configuration\nalias ll='ls -la'\n")

            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
                # First run
                self.assertTrue(run_codex._setup_virtualenv(venv_path=mock_venv, workspace=mock_workspace))
                first_content = bashrc_file.read_text()
                self.assertIn("# Existing user configuration", first_content)
                self.assertIn("alias ll='ls -la'", first_content)
                self.assertEqual(first_content.count("Auto-activate analysis virtualenv"), 1)

                # Second run (idempotency check)
                self.assertTrue(run_codex._setup_virtualenv(venv_path=mock_venv, workspace=mock_workspace))
                second_content = bashrc_file.read_text()
                # Snippet must not be duplicated
                self.assertEqual(second_content.count("Auto-activate analysis virtualenv"), 1)
                # PATH must not have duplicate mock_bin entries
                path_parts = os.environ.get("PATH", "").split(":")
                self.assertEqual(path_parts.count(str(mock_bin)), 1)

    # -------------------------------------------------------------------------
    # Requirement R4.1: Preprocessing Pipeline Bridge (_compact_evidence)
    # -------------------------------------------------------------------------

    def test_compact_evidence_preserves_envelope_and_totals(self):
        """R4.1: Verify _compact_evidence retains standard envelope keys and totals dictionary from LogPackage."""
        package = {
            "schemaVersion": "robot-log-evidence/v2",
            "summary": "Critical thermal throttle in joint actuator 3",
            "coverage": {"totalFiles": 12, "scannedFiles": 12},
            "omissions": ["sensor_raw.bin"],
            "source": "upload_tarball_123.tar.gz",
            "manifest": {"hash": "sha256:abcd1234efgh"},
            "totals": {
                "files": 12,
                "lines": 45000,
                "expandedBytes": 3200000,
                "severityCounts": {"error": 14, "warning": 68, "info": 44918},
            },
        }
        compacted_json = run_codex._compact_evidence(package)
        result = json.loads(compacted_json)

        # Envelope keys preserved
        self.assertEqual(result["schemaVersion"], "robot-log-evidence/v2")
        self.assertEqual(result["summary"], "Critical thermal throttle in joint actuator 3")
        self.assertEqual(result["coverage"], {"totalFiles": 12, "scannedFiles": 12})
        self.assertEqual(result["omissions"], ["sensor_raw.bin"])
        self.assertEqual(result["source"], "upload_tarball_123.tar.gz")
        self.assertEqual(result["manifest"], {"hash": "sha256:abcd1234efgh"})

        # Totals dictionary preserved
        self.assertEqual(result["totals"]["files"], 12)
        self.assertEqual(result["totals"]["lines"], 45000)
        self.assertEqual(result["totals"]["expandedBytes"], 3200000)
        self.assertEqual(result["totals"]["severityCounts"]["error"], 14)

    def test_compact_evidence_retains_files_metadata(self):
        """R4.1: Verify _compact_evidence formats files with diagnostic metadata, caps at 30, and strips extraneous keys."""
        files = [
            {
                "path": f"inputs/syslog_{i}.log",
                "sizeBytes": 1024 * (i + 1),
                "lines": 100 * (i + 1),
                "subsystem": "motion_control",
                "status": "active",
                "severityCounts": {"error": i, "warning": i * 2},
                "firstTimestamp": "2026-09-11T08:00:00Z",
                "lastTimestamp": "2026-09-11T08:30:00Z",
                # Unrelated internal fields that should be filtered out:
                "rawBuffer": "secret internal buffer",
                "internalAst": {"depth": 3},
            }
            for i in range(35)  # 35 files (exceeds cap of 30)
        ]

        package = {"files": files}
        compacted_json = run_codex._compact_evidence(package)
        result = json.loads(compacted_json)

        # Files capped at 30 items
        self.assertEqual(len(result["files"]), 30)
        # Expected diagnostic keys preserved, extraneous keys stripped
        for f in result["files"]:
            self.assertIn("path", f)
            self.assertIn("sizeBytes", f)
            self.assertIn("lines", f)
            self.assertIn("subsystem", f)
            self.assertIn("status", f)
            self.assertIn("severityCounts", f)
            self.assertIn("firstTimestamp", f)
            self.assertIn("lastTimestamp", f)
            self.assertNotIn("rawBuffer", f)
            self.assertNotIn("internalAst", f)

    def test_compact_evidence_retains_patterns_metadata(self):
        """R4.1: Verify _compact_evidence formats patterns with diagnostic metadata, caps at 20, and strips extraneous keys."""
        patterns = [
            {
                "subsystem": "canbus",
                "signature": f"CAN_BUS_TIMEOUT_ID_{i}",
                "severity": "error",
                "count": 5 + i,
                "countComplete": True,
                "example": f"Frame timeout for ID {i}",
                # Unrelated fields to filter out:
                "regexCompiled": ".*",
            }
            for i in range(25)  # 25 patterns (exceeds cap of 20)
        ]

        package = {"patterns": patterns}
        compacted_json = run_codex._compact_evidence(package)
        result = json.loads(compacted_json)

        # Patterns capped at 20 items
        self.assertEqual(len(result["patterns"]), 20)
        for p in result["patterns"]:
            self.assertIn("subsystem", p)
            self.assertIn("signature", p)
            self.assertIn("severity", p)
            self.assertIn("count", p)
            self.assertIn("countComplete", p)
            self.assertIn("example", p)
            self.assertNotIn("regexCompiled", p)

    def test_compact_evidence_strictly_strips_raw_log_payloads(self):
        """R4.1: Verify _compact_evidence strictly excludes raw log payloads (rawLine, raw, contextBefore, contextAfter)."""
        evidence_items = [
            {
                "file": f"inputs/module_{i}.log",
                "line": 100 + i,
                "severity": "error",
                "timestamp": f"2026-09-11T08:14:{i:02d}Z",
                "subsystem": "chassis",
                "message": f"Error event {i}",
                # Raw payloads that must never be emitted into the Turn 1 prompt:
                "rawLine": f"2026-09-11T08:14:{i:02d}Z ERROR [chassis] Error event {i} (RAW_LINE_PAYLOAD_LEAK_{i})",
                "raw": f"RAW_BLOB_LEAK_{i}_SECRET",
                "contextBefore": [
                    f"2026-09-11T08:14:{i:02d}Z INFO [chassis] context line before {i}",
                ],
                "contextAfter": [
                    f"2026-09-11T08:14:{i:02d}Z INFO [chassis] context line after {i}",
                ],
            }
            for i in range(30)  # 30 items (exceeds cap of 25)
        ]
        package = {"evidence": evidence_items}
        compacted_json = run_codex._compact_evidence(package)
        result = json.loads(compacted_json)

        # Capped at 25 evidence items
        self.assertEqual(len(result["evidence"]), 25)

        # Structured diagnostic fields are preserved
        item = result["evidence"][0]
        self.assertEqual(item["file"], "inputs/module_0.log")
        self.assertEqual(item["line"], 100)
        self.assertEqual(item["severity"], "error")
        self.assertEqual(item["timestamp"], "2026-09-11T08:14:00Z")
        self.assertEqual(item["subsystem"], "chassis")
        self.assertEqual(item["message"], "Error event 0")

        # Raw log lines, blobs, and surrounding context arrays are strictly absent
        for ev in result["evidence"]:
            self.assertNotIn("rawLine", ev)
            self.assertNotIn("raw", ev)
            self.assertNotIn("contextBefore", ev)
            self.assertNotIn("contextAfter", ev)

        # Verify raw leak strings do not exist anywhere in the serialized prompt string
        self.assertNotIn("RAW_LINE_PAYLOAD_LEAK", compacted_json)
        self.assertNotIn("RAW_BLOB_LEAK", compacted_json)
        self.assertNotIn("context line before", compacted_json)
        self.assertNotIn("context line after", compacted_json)

    def test_compact_evidence_length_bounding_and_non_dict_inputs(self):
        """R4.1: Verify _compact_evidence bounds output to <= 10,000 characters and handles non-dict inputs safely."""
        # Test non-dict inputs
        self.assertEqual(json.loads(run_codex._compact_evidence("string_input")), {"type": "str"})
        self.assertEqual(json.loads(run_codex._compact_evidence(42)), {"type": "int"})
        self.assertEqual(json.loads(run_codex._compact_evidence(["a", "b"])), {"type": "list"})
        self.assertEqual(json.loads(run_codex._compact_evidence(None)), {"type": "NoneType"})

        # Test length bounding: large summary that would normally exceed 10,000 characters
        oversized_package = {
            "summary": "x" * 20000,
            "manifest": {"hash": "abc"},
        }
        compacted = run_codex._compact_evidence(oversized_package)
        self.assertLessEqual(len(compacted), 10000, "Compacted evidence must not exceed 10,000 characters")

    # -------------------------------------------------------------------------
    # Requirement R4.2: ROS2 SQLite .db3 Pre-Scan & Formatting
    # -------------------------------------------------------------------------

    def _create_sample_db3(self, db_path: Path):
        """Helper to create a realistic ROS2 SQLite .db3 bag database."""
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE topics (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                serialization_format TEXT,
                offered_qos_profiles TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                data BLOB
            )
        """)
        # Insert sample topics
        cursor.execute("INSERT INTO topics VALUES (1, '/odom', 'nav_msgs/msg/Odometry', 'cdr', '')")
        cursor.execute("INSERT INTO topics VALUES (2, '/sbus_data', 'sensor_msgs/msg/Joy', 'cdr', '')")
        cursor.execute("INSERT INTO topics VALUES (3, '/joint_states', 'sensor_msgs/msg/JointState', 'cdr', '')")
        cursor.execute("INSERT INTO topics VALUES (4, '/imu_seconds', 'sensor_msgs/msg/Imu', 'cdr', '')")
        cursor.execute("INSERT INTO topics VALUES (5, '/unused_topic', 'std_msgs/msg/String', 'cdr', '')")

        # Insert messages with nanosecond timestamps (ROS2 standard: > 1e18)
        # /odom: 10 second duration (1726050000000000000 -> 1726050010000000000)
        cursor.execute("INSERT INTO messages VALUES (1, 1, 1726050000000000000, X'0102')")
        cursor.execute("INSERT INTO messages VALUES (2, 1, 1726050005000000000, X'0103')")
        cursor.execute("INSERT INTO messages VALUES (3, 1, 1726050010000000000, X'0104')")

        # /sbus_data: 2 messages, 4.5s duration
        cursor.execute("INSERT INTO messages VALUES (4, 2, 1726050001000000000, X'0201')")
        cursor.execute("INSERT INTO messages VALUES (5, 2, 1726050005500000000, X'0202')")

        # /joint_states: 1 message (duration 0.0s)
        cursor.execute("INSERT INTO messages VALUES (6, 3, 1726050000000000000, X'0301')")

        # /imu_seconds: timestamps in seconds (> 1e9 and <= 1e18) to test seconds branch
        cursor.execute("INSERT INTO messages VALUES (7, 4, 1726050000, X'0401')")
        cursor.execute("INSERT INTO messages VALUES (8, 4, 1726050020, X'0402')")

        # /unused_topic has no messages (id=5)

        conn.commit()
        conn.close()

    def test_scan_db3_telemetry_extracts_topics_counts_and_timestamps(self):
        """R4.2: Verify _scan_db3_telemetry extracts topic names, message counts, types, ISO dates, and durations."""
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs_dir = Path(temp_dir) / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)
            db3_file = inputs_dir / "flight_telemetry.db3"
            self._create_sample_db3(db3_file)

            results = run_codex._scan_db3_telemetry(inputs_dir)
            self.assertEqual(len(results), 1, "Should discover exactly one .db3 file")

            entry = results[0]
            self.assertEqual(entry["file"], "inputs/flight_telemetry.db3")
            topics = {t["topic"]: t for t in entry["topics"]}

            # 1. Check /odom (nanosecond timestamps)
            odom = topics["/odom"]
            self.assertEqual(odom["type"], "nav_msgs/msg/Odometry")
            self.assertEqual(odom["message_count"], 3)
            self.assertEqual(odom["start_timestamp_ns"], 1726050000000000000)
            self.assertEqual(odom["end_timestamp_ns"], 1726050010000000000)
            self.assertEqual(odom["duration_seconds"], 10.0)
            self.assertEqual(odom["start_iso"], "2024-09-11 10:20:00Z")
            self.assertEqual(odom["end_iso"], "2024-09-11 10:20:10Z")

            # 2. Check /sbus_data
            sbus = topics["/sbus_data"]
            self.assertEqual(sbus["type"], "sensor_msgs/msg/Joy")
            self.assertEqual(sbus["message_count"], 2)
            self.assertEqual(sbus["duration_seconds"], 4.5)

            # 3. Check /joint_states (single message)
            joints = topics["/joint_states"]
            self.assertEqual(joints["message_count"], 1)
            self.assertEqual(joints["duration_seconds"], 0.0)

            # 4. Check /imu_seconds (seconds-scale timestamps)
            imu = topics["/imu_seconds"]
            self.assertEqual(imu["message_count"], 2)
            self.assertEqual(imu["start_iso"], "2024-09-11 10:20:00Z")
            self.assertEqual(imu["end_iso"], "2024-09-11 10:20:20Z")

            # 5. Check /unused_topic (0 messages)
            unused = topics["/unused_topic"]
            self.assertEqual(unused["type"], "std_msgs/msg/String")
            self.assertEqual(unused["message_count"], 0)
            self.assertIsNone(unused["start_timestamp_ns"])
            self.assertIsNone(unused["end_timestamp_ns"])
            self.assertIsNone(unused["duration_seconds"])
            self.assertIsNone(unused["start_iso"])
            self.assertIsNone(unused["end_iso"])

    def test_scan_db3_telemetry_robustness_against_corrupt_empty_missing_tables(self):
        """R4.2: Verify _scan_db3_telemetry gracefully ignores corrupt files, empty files, and databases lacking required tables."""
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs_dir = Path(temp_dir) / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)

            # 1. Non-existent directory returns []
            self.assertEqual(run_codex._scan_db3_telemetry(Path(temp_dir) / "nonexistent"), [])

            # 2. 0-byte file (empty)
            (inputs_dir / "empty.db3").write_bytes(b"")

            # 3. Corrupt file with non-SQLite random bytes
            (inputs_dir / "corrupted.db3").write_bytes(b"NOT_A_SQLITE_DATABASE_HEADER_GARBAGE_BYTES_12345678")

            # 4. SQLite file missing 'messages' table
            missing_table_db = inputs_dir / "missing_messages.db3"
            conn = sqlite3.connect(str(missing_table_db))
            conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT)")
            conn.commit()
            conn.close()

            # 5. Valid SQLite file alongside faulty ones
            valid_db = inputs_dir / "valid.db3"
            self._create_sample_db3(valid_db)

            # Execution must not throw; must skip the faulty files and only parse valid.db3
            results = run_codex._scan_db3_telemetry(inputs_dir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["file"], "inputs/valid.db3")

    def test_format_db3_telemetry_output(self):
        """R4.2: Verify _format_db3_telemetry produces the expected Turn 1 prompt block."""
        # Empty telemetry returns empty string
        self.assertEqual(run_codex._format_db3_telemetry([]), "")

        sample_telemetry = [
            {
                "file": "inputs/robot_run.db3",
                "topics": [
                    {
                        "topic": "/odom",
                        "type": "nav_msgs/msg/Odometry",
                        "message_count": 500,
                        "start_iso": "2024-09-11 10:00:00Z",
                        "end_iso": "2024-09-11 10:05:00Z",
                        "duration_seconds": 300.0,
                    },
                    {
                        "topic": "/unused_camera",
                        "type": "sensor_msgs/msg/Image",
                        "message_count": 0,
                        "start_iso": None,
                        "end_iso": None,
                        "duration_seconds": None,
                    },
                ],
            }
        ]

        formatted = run_codex._format_db3_telemetry(sample_telemetry)
        self.assertIn("Pre-scanned ROS2 Telemetry (.db3):", formatted)
        self.assertIn("- File: inputs/robot_run.db3", formatted)
        self.assertIn("  - Topic: /odom | Type: nav_msgs/msg/Odometry | Count: 500 | Time: 2024-09-11 10:00:00Z -> 2024-09-11 10:05:00Z (300.0s)", formatted)
        self.assertIn("  - Topic: /unused_camera | Type: sensor_msgs/msg/Image | Count: 0 | Time: no messages", formatted)

    def test_prompt_integrates_compact_evidence_and_db3_telemetry(self):
        """R4.1 & R4.2: Verify _prompt integrates both compacted evidence and pre-scanned telemetry."""
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)):
            job = {
                "description": "Diagnose chassis drive motor stalls",
                "evidence": {
                    "schemaVersion": "robot-log-evidence/v2",
                    "totals": {"files": 2, "lines": 500},
                    "evidence": [{"file": "inputs/chassis.log", "message": "Motor overcurrent", "rawLine": "SECRET_RAW"}],
                },
            }
            telemetry = [
                {
                    "file": "inputs/run.db3",
                    "topics": [{"topic": "/odom", "type": "nav_msgs/msg/Odometry", "message_count": 100, "duration_seconds": 12.5}],
                }
            ]
            prompt = run_codex._prompt(job, telemetry=telemetry)
            # Evidence included in compacted form without raw lines
            self.assertIn("robot-log-evidence/v2", prompt)
            self.assertIn("Motor overcurrent", prompt)
            self.assertNotIn("SECRET_RAW", prompt)
            # Pre-scanned telemetry included
            self.assertIn("Pre-scanned ROS2 Telemetry (.db3):", prompt)
            self.assertIn("inputs/run.db3", prompt)
            self.assertIn("/odom", prompt)


if __name__ == "__main__":
    unittest.main()
