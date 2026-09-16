"""Debug output regressions; synthetic events only, no model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from sandbox import run_codex


class DebugOutputTests(unittest.TestCase):
    def test_commands_results_and_in_progress_messages_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'activity.jsonl'
            for event in [
                {'type': 'item.updated', 'item': {'type': 'agent_message', 'text': 'Still checking motor 7'}},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'python /workspace/check.py', 'aggregated_output': 'motor 7 stalled', 'exit_code': 1}},
                {'type': 'error', 'message': 'provider retry in 5 seconds'},
            ]:
                run_codex._activity(event, output)
            content = output.read_text()
            for expected in ('Still checking motor 7', '/workspace/check.py', 'motor 7 stalled', 'provider retry in 5 seconds'):
                self.assertIn(expected, content)

    def test_debug_history_does_not_rotate_away_or_truncate_large_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'activity.jsonl'
            content = 'BEGIN_OUTPUT' + 'x' * 90000 + 'END_OUTPUT'
            run_codex._activity({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': content}}, output)
            messages = ''.join(json.loads(line)['message'] for line in output.read_text().splitlines())
            self.assertIn('BEGIN_OUTPUT', messages)
            self.assertIn('END_OUTPUT', messages)
            self.assertGreater(len(messages), 90000)

    def test_child_rollout_reports_actual_role_progress_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / 'sessions'; sessions.mkdir()
            rollout = sessions / 'child.jsonl'
            output = root / 'debug.jsonl'
            meta = {'type': 'session_meta', 'payload': {'id': 'child-1', 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'parent', 'agent_role': 'telemetry_investigator'}}}}}
            progress = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Checked 12 telemetry records'}}
            rollout.write_text(json.dumps(meta) + '\n' + json.dumps(progress)[:-2])
            observed = set()
            collector = run_codex.ChildOutput(root, output, observed)
            collector.poll()
            self.assertEqual(observed, {'child-1'})
            self.assertEqual(len(output.read_text().splitlines()), 1)
            with rollout.open('a') as sink:
                sink.write(json.dumps(progress)[-2:] + '\n')
                sink.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': 'All records checked'}}) + '\n')
            collector.poll(drain=True)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertTrue(all(record['subagent']['role'] == 'telemetry_investigator' for record in records))
            self.assertIn('Checked 12 telemetry records', output.read_text())
            self.assertEqual([record for record in records if record.get('subagent')][-1]['subagent']['status'], 'completed')
            previous = output.read_text()
            collector.poll()
            self.assertEqual(output.read_text(), previous)

    def test_secrets_are_redacted_before_large_output_is_split(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'MODEL_API_KEY': 'fixture-credential-value'}):
            output = Path(directory) / 'debug.jsonl'
            run_codex._activity({'type': 'error', 'authorization': 'Bearer abc', 'message': 'x' * 1180 + 'fixture-credential-value'}, output)
            content = output.read_text()
            self.assertNotIn('fixture-credential-value', content)
            self.assertNotIn('Bearer abc', content)
            self.assertIn('[redacted]', content)

    def test_runner_captures_stderr_large_json_and_final_child_flush(self):
        import os
        import sys
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'codex'
            binary.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.stdin.read()
print('fixture retry diagnostic', file=sys.stderr, flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','aggregated_output':'START'+'x'*90000+'END'}}), flush=True)
sessions=Path(os.environ['CODEX_HOME'])/'sessions'; sessions.mkdir()
with (sessions/'child.jsonl').open('w') as output:
    output.write(json.dumps({'type':'session_meta','payload':{'id':'fixture-child','parent_thread_id':'fixture-parent','agent_role':'log_investigator'}})+'\\n')
    output.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','last_agent_message':'Child checked logs'}})+'\\n')
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('Fixture report')
''')
            binary.chmod(0o700)
            job = root / 'job.json'; job.write_text(json.dumps({'description': 'fixture', 'files': []}))
            with patch.object(run_codex, 'WORKSPACE', root), patch.object(run_codex, 'RESULT', root / 'result.json'), \
                 patch.object(run_codex, '_setup_virtualenv'), patch.object(sys, 'argv', ['runner', str(job)]), \
                 patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'ROBOT_RUN_TIMEOUT_SECONDS': '10'}):
                self.assertEqual(run_codex.main(), 0)
            records = [json.loads(line) for line in (root / 'codex-debug.jsonl').read_text().splitlines()]
            self.assertEqual(records[0]['kind'], 'agent_started')
            messages = ''.join(record['message'] for record in records)
            self.assertIn('fixture retry diagnostic', messages)
            self.assertIn('START' + 'x'*90000 + 'END', messages)
            self.assertEqual([record for record in records if record.get('subagent')][-1]['subagent']['status'], 'completed')
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['metrics']['observed_child_thread_ids'], ['fixture-child'])
