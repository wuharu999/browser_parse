import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.chat import ChatService, ChatError
from sandbox.analysis_context import validate_context
from sandbox import run_codex


def notes(**kwargs):
    return {"schemaVersion": "robot-analysis-notes/v1", "observations": ["motor stopped [E1]"], "hypotheses_checked": ["Supply fault unresolved [E1]"], "evidence": ["[E1] robot.log lines 10-12: timeout"], "unresolved_questions": [], **kwargs}


class FakeChat(ChatService):
    def __init__(self):
        self.key = "test-chat-key"
        self.calls = []
        self.fail = False

    async def answer(self, context, history, question):
        self.calls.append((context, history, question))
        yield "Saved finding [E1]."
        await asyncio.sleep(0)
        if self.fail: raise RuntimeError("private provider diagnostic test-chat-key")
        yield " No new analysis was run."


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(db_path=str(Path(self.tmp.name) / "jobs.db"), upload_dir=str(Path(self.tmp.name) / "uploads"))
        self.store = self.app.state.store
        self.chat = FakeChat()
        self.app.state.chat = self.chat
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.store.db.close()
        self.tmp.cleanup()

    def job(self, context=None):
        job, _ = self.store.create("Motor stopped", "en", {"private": "raw logs excluded"})
        self.store.submit(job["id"])
        self.store.worker_claim("worker-a", {"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 32768})
        self.store.finish(job["id"], "worker-a", "completed", "Saved report [E1]", 0, {}, context)
        return job["id"]

    def ask(self, jid, request_id="request-a", question="Why did it stop?"):
        return self.client.post(f"/api/jobs/{jid}/questions", json={"request_id": request_id, "question": question})

    def test_context_shared_history_idempotency_and_allowance(self):
        jid = self.job(notes())
        budget = self.store.budget()
        report = self.store.get(jid)["report"]
        response = self.ask(jid)
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([e["type"] for e in events], ["started", "answer", "answer", "done"])
        first = events[1]["item"]["answer"]
        self.assertNotEqual(first, events[-1]["item"]["answer"])
        history = self.client.get(f"/api/jobs/{jid}/questions").json()
        self.assertEqual(history["context_mode"], "report_and_notes")
        self.assertEqual(history["items"][0]["status"], "completed")
        replay = self.ask(jid).json()
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(self.ask(jid, question="Different question").status_code, 409)
        self.assertEqual(budget, self.store.budget())
        self.assertEqual(report, self.store.get(jid)["report"])
        self.assertNotIn("raw logs", json.dumps(self.chat.calls))
        self.assertEqual(self.chat.calls[0][0]["investigation_notes"], notes())

    def test_report_only_invalid_notes_and_history_window(self):
        jid = self.job({"private_reasoning": "discard me"})
        for i in range(14):
            self.assertEqual(self.ask(jid, str(i)).status_code, 200)
        self.assertEqual(len(self.chat.calls[-1][1]), 12)
        page = self.client.get(f"/api/jobs/{jid}/questions?limit=5").json()
        self.assertEqual(page["context_mode"], "report_only")
        self.assertEqual(len(page["items"]), 5)
        earlier = self.client.get(f"/api/jobs/{jid}/questions?limit=100&before={page['next_before']}").json()
        self.assertEqual(len(earlier["items"]), 9)
        self.assertFalse(set(i["id"] for i in page["items"]) & set(i["id"] for i in earlier["items"]))
        self.assertEqual(self.client.get(f"/api/jobs/{jid}/questions?limit=0").status_code, 422)

    def test_context_notes_redacted_and_survive_reopen(self):
        jid = self.job(notes(observations=["api_key=abcdef; motor stopped"]))
        context, _ = self.store.chat_context(jid)
        self.assertNotIn("abcdef", json.dumps(context))
        reopened = create_app(db_path=str(self.store.path), upload_dir=str(self.store.upload_dir))
        self.assertEqual(reopened.state.store.chat_context(jid)[0], context)
        reopened.state.store.db.close()

    def test_missing_key_no_execution_or_budget_change(self):
        jid = self.job()
        self.chat.key = ""
        budget = self.store.budget()
        self.assertEqual(self.ask(jid).status_code, 503)
        self.assertFalse(self.client.get(f"/api/jobs/{jid}/questions").json()["available"])
        self.assertEqual(self.chat.calls, [])
        self.assertEqual(self.store.questions(jid)["items"], [])
        self.assertEqual(self.store.budget(), budget)

    def test_single_active_atomic_duplicate_and_job_isolation(self):
        jid, other = self.job(), self.job()
        def begin(i):
            try: return self.store.begin_question(jid, str(i), "Question")
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(begin, range(4)))
        self.assertEqual(sum(bool(result) for result in results), 1)
        active = self.store.questions(jid)["active"]
        replay, created = self.store.begin_question(jid, active["request_id"], "Question")
        self.assertFalse(created)
        self.assertEqual(replay["id"], active["id"])
        self.assertEqual(self.ask(jid).status_code, 409)
        self.assertEqual(self.ask(other).status_code, 200)
        self.assertEqual(self.chat.calls[-1][1], [])
        self.assertEqual(len(self.store.questions(other)["items"]), 1)

    def test_interrupted_partial_retry_and_restart(self):
        jid = self.job()
        self.chat.fail = True
        response = self.ask(jid)
        self.assertNotIn("private provider", response.text)
        item = self.store.questions(jid)["items"][0]
        self.assertEqual(item["status"], "interrupted")
        self.assertEqual(item["answer"], "Saved finding [E1].")
        self.chat.fail = False
        self.ask(jid, "retry")
        self.assertEqual(len(self.chat.calls), 2)
        self.assertEqual(self.chat.calls[-1][1], [])  # Incomplete answers excluded.
        item, _ = self.store.begin_question(jid, "stale", "Stale")
        self.store.update_question(item["id"], "durable partial")
        with TestClient(create_app(db_path=str(self.store.path), upload_dir=str(self.store.upload_dir))) as restarted:
            saved = restarted.get(f"/api/jobs/{jid}/questions").json()["items"][-1]
            self.assertEqual((saved["answer"], saved["status"], saved["error"]), ("durable partial", "interrupted", "server_restarted"))
        restarted.app.state.store.db.close()

    def test_timeout_and_question_validation(self):
        jid = self.job()
        async def slow(*args):
            yield "Partial"
            await asyncio.sleep(1)
        self.chat.answer = slow
        self.chat.timeout_seconds = 0.02
        response = self.ask(jid)
        self.assertEqual(json.loads(response.text.splitlines()[-1])["type"], "interrupted")
        self.assertEqual(self.ask(jid, "long", "a" * 4001).status_code, 422)
        self.assertEqual(self.ask(jid, "empty", "  ").status_code, 409)
        self.assertEqual(self.ask("missing").status_code, 404)
        draft, _ = self.store.create("draft", "en", None)
        self.assertEqual(self.ask(draft["id"]).status_code, 409)

    def test_worker_context_acknowledged_before_completion_and_immutable(self):
        job, _ = self.store.create("Context before cleanup", "en", None)
        jid = job["id"]
        self.store.submit(jid)
        self.store.worker_claim("worker-a", {"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 32768})
        url = f"/api/worker/jobs/{jid}/analysis-context"
        headers = {"Authorization": "Bearer context-test", "X-Worker-ID": "worker-a"}
        with patch.dict(os.environ, {"ROBOT_WORKER_TOKEN": "context-test"}):
            self.assertEqual(self.client.post(url, json={"analysis_context": notes()}).status_code, 401)
            other = {**headers, "X-Worker-ID": "worker-b"}
            self.assertEqual(self.client.post(url, headers=other, json={"analysis_context": notes()}).status_code, 403)
            self.assertEqual(self.client.post(url, headers=headers, json={"analysis_context": {"invalid": True}}).json(), {"saved": False})
            self.assertEqual(self.client.post(url, headers=headers, json={"analysis_context": notes()}).json(), {"saved": True})
            self.assertEqual(self.store.get(jid)["status"], "running")
            self.assertEqual(self.store.chat_context(jid)[0]["investigation_notes"], notes())
            self.assertEqual(self.client.post(url, headers=headers, json={"analysis_context": notes()}).json(), {"saved": True})
            self.assertEqual(self.client.post(url, headers=headers, json={"analysis_context": notes(observations=["replace"])}).status_code, 409)
            response = self.client.post(f"/api/worker/jobs/{jid}/finish", headers=headers, json={"status": "completed", "report": "Report [E1]", "analysis_context": notes(observations=["replace"])})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.store.chat_context(jid)[0]["investigation_notes"], notes())
            self.assertEqual(self.client.post(url, headers=headers, json={"analysis_context": notes()}).status_code, 409)

    def test_review_labeled_separately_and_analysis_unchanged(self):
        jid = self.job(notes())
        token = self.store.claim_review(jid, "Reviewer")
        self.store.version(jid, token, False, "Needs checking", "Human workflow")
        self.ask(jid)
        context = self.chat.calls[-1][0]
        self.assertEqual(context["original_report"], "Saved report [E1]")
        self.assertEqual(context["latest_human_reviewed_workflow"]["procedure"], "Human workflow")


class ContextTest(unittest.TestCase):
    def test_runner_reads_bounded_factual_notes_and_ignores_invalid(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run_codex, "WORKSPACE", Path(directory)), patch.dict(os.environ, {"CODEX_API_KEY": "private-value"}):
            path = Path(directory) / "analysis-notes.json"
            self.assertIsNone(run_codex._analysis_context())
            path.write_text(json.dumps(notes(observations=["private-value motor stopped"])))
            self.assertNotIn("private-value", json.dumps(run_codex._analysis_context()))
            path.write_text('{')
            self.assertIsNone(run_codex._analysis_context())
            path.write_text('x' * 16385)
            self.assertIsNone(run_codex._analysis_context())
            self.assertIsNone(validate_context(notes(observations=["中" * 2000] * 3)))
            self.assertIsNone(validate_context(notes(observations=[{"reasoning": "no"}])))
            self.assertIsNone(validate_context(notes(extra="no")))


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_provider_payload_and_stream_protocol(self):
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Answer [E1]"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        chat = ChatService()
        chat.key = "test"
        with patch("backend.chat.httpx.AsyncClient", return_value=client):
            chunks = [chunk async for chunk in chat.answer({"report": "finding"}, [], "Explain")]
        self.assertEqual(chunks, ["Answer [E1]"])
        payload = captured[0]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertTrue(payload["stream"])
        self.assertNotIn("tools", payload)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user", "user"])

    async def test_provider_eof_and_tool_calls_are_not_success(self):
        for content in ('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', 'data: {"choices":[{"delta":{"tool_calls":[{"name":"exec"}]}}]}\n\n'):
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=content)))
            with patch("backend.chat.httpx.AsyncClient", return_value=client), self.assertRaises(ChatError):
                _ = [chunk async for chunk in ChatService().answer({}, [], "Explain")]
