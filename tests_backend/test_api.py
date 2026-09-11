import os
import json
import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

os.environ["ROBOT_WORKER_TOKEN"] = "worker-test-token"
from backend.app import create_app  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client = TestClient(create_app(db_path=str(root / "jobs.sqlite3"), upload_dir=str(root / "uploads")))
        self.worker = {"Authorization": "Bearer worker-test-token"}

    def tearDown(self): self.temp.cleanup()

    def job(self, description="find failure"):
        response = self.client.post("/api/jobs", json={"description": description, "language": "English", "evidence": {"full": "private raw evidence"}})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def submit(self, created):
        job, token = created["job"], created["upload_token"]
        response = self.client.post(f"/api/jobs/{job['id']}/submit", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        return job["id"]

    def claim_worker(self):
        response = self.client.post("/api/worker/claim", headers=self.worker)
        self.assertEqual(response.status_code, 200)
        return response.json()["job"]

    def test_upload_private_and_restart(self):
        made = self.job(); jid, token = made["job"]["id"], made["upload_token"]
        response = self.client.put(f"/api/jobs/{jid}/files?name=robot.log", content=b"private log", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("evidence", self.client.get(f"/api/jobs/{jid}").json())
        self.assertNotIn("files", self.client.get(f"/api/jobs/{jid}").json())
        self.submit(made); running = self.claim_worker(); self.assertEqual(running["id"], jid)
        artifact = running["files"][0]["id"]
        self.assertEqual(self.client.get(f"/api/worker/jobs/{jid}/files/{artifact}", headers=self.worker).content, b"private log")
        self.assertEqual(self.client.get(f"/api/worker/jobs/{jid}/files/{artifact}").status_code, 401)
        db, uploads = self.client.app.state.store.path, self.client.app.state.store.upload_dir
        restarted = TestClient(create_app(db_path=str(db), upload_dir=str(uploads)))
        self.assertEqual(restarted.get(f"/api/jobs/{jid}").json()["status"], "running")

    def test_upload_manifest_keeps_same_second_insertion_order_and_relative_name(self):
        made = self.job(); jid, token = made["job"]["id"], made["upload_token"]
        headers = {"Authorization": f"Bearer {token}"}
        for name in ("archive/a.log", "archive/b.log"):
            self.assertEqual(self.client.put(f"/api/jobs/{jid}/files?name={name}", content=name.encode(), headers=headers).status_code, 201)
        self.submit(made); running = self.claim_worker()
        self.assertEqual([item["name"] for item in running["files"]], ["archive/a.log", "archive/b.log"])
        self.assertEqual(self.client.put(f"/api/jobs/{jid}/files?name=../bad.log", content=b"x", headers=headers).status_code, 422)

    def test_budget_is_atomic_and_running_is_limited(self):
        jobs = [self.submit(self.job(f"job {n}")) for n in range(3)]
        def claim(_): return self.client.post("/api/worker/claim", headers=self.worker).json()["job"]
        with ThreadPoolExecutor(max_workers=3) as pool: claimed = list(pool.map(claim, jobs))
        running = [j for j in claimed if j]
        self.assertEqual(len(running), 2)
        budget = self.client.get("/api/budget").json()
        self.assertEqual(budget["active_reservations_usd"], 10)
        self.assertEqual(budget["running"], 2)

    def test_budget_exposes_pending_slots_and_shanghai_reset(self):
        initial = self.client.get("/api/budget").json()
        self.assertEqual((initial["pending"], initial["queued"]), (0, 0))
        self.assertTrue(initial["resets_at"].endswith("T00:00:00+08:00"))
        self.assertGreater(initial["resets_at"][:10], initial["day"])
        made = self.job()
        draft = self.client.get("/api/budget").json()
        self.assertEqual((draft["pending"], draft["queued"]), (1, 0))
        self.submit(made)
        queued = self.client.get("/api/budget").json()
        self.assertEqual((queued["pending"], queued["queued"]), (1, 1))
        self.claim_worker()
        running = self.client.get("/api/budget").json()
        self.assertEqual((running["pending"], running["queued"], running["running"]), (0, 0, 1))

    def test_public_cancel_and_worker_sees_it(self):
        jid = self.submit(self.job())
        running = self.claim_worker(); self.assertEqual(running["id"], jid)
        cancelled = self.client.post(f"/api/jobs/{jid}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "running")
        seen = self.client.get(f"/api/worker/jobs/{jid}", headers=self.worker).json()
        self.assertTrue(seen["cancel_requested"])
        finish = self.client.post(f"/api/worker/jobs/{jid}/finish", headers=self.worker, json={"status": "cancelled", "report": "Stopped safely", "cost_usd": 1, "metrics": {}})
        self.assertEqual(finish.status_code, 200)
        self.assertEqual(finish.json()["cost_usd"], 1)
        self.assertEqual(self.client.post(f"/api/worker/jobs/{jid}/finish", headers=self.worker, json={"status": "cancelled", "report": "duplicate", "metrics": {}}).status_code, 409)

    def test_cancel_keeps_admission_reservation_and_expiry_charges_it(self):
        first, second, third = self.submit(self.job("one")), self.submit(self.job("two")), self.submit(self.job("three"))
        a, b = self.claim_worker(), self.claim_worker()
        self.assertEqual(len({a["id"], b["id"]}), 2)
        stopping = a["id"]
        self.assertEqual(self.client.post(f"/api/jobs/{stopping}/cancel").status_code, 200)
        budget = self.client.get("/api/budget").json()
        self.assertEqual((budget["running"], budget["active_reservations_usd"]), (2, 10))
        self.assertIsNone(self.claim_worker())
        store = self.client.app.state.store
        with store.lock:
            store.db.execute("UPDATE jobs SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00+00:00", stopping)); store.db.commit()
        self.claim_worker()  # expires the cancelled running job; third still cannot pass the charged daily cap.
        expired = self.client.get(f"/api/jobs/{stopping}").json()
        self.assertEqual((expired["status"], expired["cancel_requested"], expired["cost_usd"]), ("failed", True, 5))
        self.assertIsNone(self.claim_worker())

    def test_active_reservation_counts_even_with_old_reservation_day(self):
        jid = self.submit(self.job()); self.claim_worker()
        store = self.client.app.state.store
        with store.lock:
            store.db.execute("UPDATE jobs SET reservation_day='2000-01-01' WHERE id=?", (jid,)); store.db.commit()
        self.assertEqual(self.client.get("/api/budget").json()["active_reservations_usd"], 5)

    def test_paged_summaries_and_bounded_events(self):
        made = self.job(); jid, token = made["job"]["id"], made["upload_token"]
        store = self.client.app.state.store
        with store.lock:
            store._tx()
            for n in range(505): store._event(jid, "notice", "worker", f"event {n}")
            store.db.commit()
        events = self.client.get(f"/api/jobs/{jid}/events?after=0").json()
        self.assertEqual(len(events["events"]), 500)
        self.assertEqual(self.client.get("/api/jobs?limit=1").json()["items"][0]["id"], jid)

    def test_public_worker_text_redacts_common_secrets_and_rejects_nonfinite_cost(self):
        jid = self.submit(self.job()); self.claim_worker()
        self.assertEqual(self.client.post(f"/api/worker/jobs/{jid}/events", headers=self.worker, json={"kind": "notice", "agent": "worker", "message": "password=known-test-password Bearer abc.def.ghi"}).status_code, 201)
        event = self.client.get(f"/api/jobs/{jid}/events").json()["events"][-1]["message"]
        self.assertNotIn("known-test-password", event)
        self.assertNotIn("abc.def.ghi", event)
        headers = {**self.worker, "Content-Type": "application/json"}
        self.assertEqual(self.client.post(f"/api/worker/jobs/{jid}/finish", headers=headers, content=b'{"status":"completed","report":"done","cost_usd":NaN,"metrics":{}}').status_code, 422)

    def test_active_jobs_are_independent_of_history_page_and_hide_private_fields(self):
        old = self.submit(self.job("older queued analysis"))
        recent = self.job("new upload")
        page = self.client.get("/api/jobs?limit=1").json()["items"]
        self.assertEqual(page[0]["id"], recent["job"]["id"])
        active = self.client.get("/api/jobs/active").json()["items"]
        self.assertEqual({item["id"] for item in active}, {old, recent["job"]["id"]})
        for item in active:
            for private in ("evidence", "token_hash", "files", "metrics", "report"):
                self.assertNotIn(private, item)
        self.client.post(f"/api/jobs/{old}/cancel")
        self.assertNotIn(old, {item["id"] for item in self.client.get("/api/jobs/active").json()["items"]})

    def test_session_history_latest_and_earlier_pages_are_bounded_and_ordered(self):
        jid = self.job()["job"]["id"]
        store = self.client.app.state.store
        with store.lock:
            store._tx()
            for index in range(1005): store._event(jid, "notice", "worker", f"fixture {index}")
            store.db.commit()
        latest = self.client.get(f"/api/jobs/{jid}/events?latest=true").json()["events"]
        self.assertEqual(len(latest), 500)
        self.assertEqual(latest[-1]["message"], "fixture 1004")
        older = self.client.get(f"/api/jobs/{jid}/events?before={latest[0]['seq']}").json()["events"]
        self.assertEqual(len(older), 500)
        self.assertLess(older[-1]["seq"], latest[0]["seq"])
        self.assertEqual([event["seq"] for event in latest], sorted(event["seq"] for event in latest))
        for query in ("before=-1", "before=9999999999999999999999", "after=9999999999999999999999"):
            self.assertEqual(self.client.get(f"/api/jobs/{jid}/events?{query}").status_code, 422)

    def test_public_review_claim_shows_name_not_token_and_expires(self):
        jid = self.submit(self.job()); self.claim_worker()
        self.client.post(f"/api/worker/jobs/{jid}/finish", headers=self.worker, json={"status": "completed", "report": "done", "cost_usd": 0, "metrics": {}})
        token = self.client.post(f"/api/jobs/{jid}/claim", json={"name": "Reviewer A"}).json()["claim_token"]
        public = self.client.get(f"/api/jobs/{jid}").json()
        self.assertEqual(set(public["review_claim"]), {"name", "expires_at"})
        self.assertEqual(public["review_claim"]["name"], "Reviewer A")
        self.assertNotIn(token, str(public))
        store = self.client.app.state.store
        with store.lock:
            store.db.execute("UPDATE claims SET expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (jid,)); store.db.commit()
        self.assertIsNone(self.client.get(f"/api/jobs/{jid}").json()["review_claim"])

    def test_structured_report_redaction_preserves_json_and_evidence_source(self):
        jid = self.submit(self.job()); self.claim_worker()
        report = {"schemaVersion": "robot-analysis/v1", "summary": "Observed timeout", "evidenceChain": [{"source": "archive/folder/robot.log", "excerpt": "token=synthetic-secret"}], "workflow": ["Inspect E1"]}
        finished = self.client.post(f"/api/worker/jobs/{jid}/finish", headers=self.worker, json={"status": "completed", "report": json.dumps(report), "cost_usd": 0, "metrics": {}})
        self.assertEqual(finished.status_code, 200)
        parsed = json.loads(finished.json()["report"])
        self.assertEqual(parsed["evidenceChain"][0]["source"], "archive/folder/robot.log")
        self.assertEqual(parsed["evidenceChain"][0]["excerpt"], "token=[redacted]")

    def test_chunked_json_over_limit_is_rejected_before_downstream_parsing(self):
        app = self.client.app
        chunks = iter([b'{"description":"', b"x" * (16 * 1024 * 1024), b'","language":"en"}'])
        sent = []
        async def receive():
            try: return {"type": "http.request", "body": next(chunks), "more_body": True}
            except StopIteration: return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message): sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/jobs", "raw_path": b"/api/jobs", "query_string": b"", "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")], "client": ("127.0.0.1", 1234), "server": ("testserver", 80), "root_path": ""}
        asyncio.run(app(scope, receive, send))
        self.assertEqual(next(message for message in sent if message["type"] == "http.response.start")["status"], 413)

    def test_review_claim_versions_and_owner_isolation(self):
        first, other = self.job(), self.job()
        self.assertEqual(self.client.post(f"/api/jobs/{first['job']['id']}/submit", headers={"Authorization": f"Bearer {other['upload_token']}"}).status_code, 403)
        jid = self.submit(first); running = self.claim_worker()
        self.client.post(f"/api/worker/jobs/{jid}/finish", headers=self.worker, json={"status": "completed", "report": "sanitized report", "cost_usd": 5, "metrics": {}})
        claim = self.client.post(f"/api/jobs/{jid}/claim", json={"name": "Reviewer"}).json()["claim_token"]
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/claim", json={"name": "Other"}).status_code, 409)
        version = self.client.post(f"/api/jobs/{jid}/versions", headers={"Authorization": f"Bearer {claim}"}, json={"success": True, "note": "accepted", "procedure": "edited procedure"})
        self.assertEqual(version.status_code, 201)
        versions = self.client.get(f"/api/jobs/{jid}/versions").json()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["reviewer_name"], "Reviewer")


if __name__ == "__main__": unittest.main()
