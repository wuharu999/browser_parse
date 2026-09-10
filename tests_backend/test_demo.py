from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.store import Store
from scripts.seed_demo import DEMOS, seed


class DemoSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "jobs.sqlite3"
        self.uploads = self.root / "uploads"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_seeds_exactly_three_completed_contract_reports(self) -> None:
        self.assertEqual(seed(self.db, self.uploads), 3)
        store = Store(str(self.db), str(self.uploads))
        rows = store.db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        self.assertEqual([row["id"] for row in rows], sorted(demo["id"] for demo in DEMOS))
        for row in rows:
            self.assertEqual(row["status"], "completed")
            self.assertTrue(row["description"].startswith("[DEMO]"))
            self.assertEqual(row["cost_usd"], 0)
            report = json.loads(row["report"])
            self.assertEqual(report["schemaVersion"], "robot-analysis/v1")
            self.assertTrue(report["demo"])
            self.assertIsInstance(report["summary"], str)
            self.assertTrue(report["summary"])
            self.assertTrue(report["workflow"])
            self.assertTrue(report["uncertainties"])
            ids = {item["id"] for item in report["evidenceChain"]}
            self.assertTrue(ids)
            for item in report["evidenceChain"]:
                self.assertEqual(set(item), {"id", "observation", "source", "lines", "excerpt", "reasoning"})
                self.assertTrue(all(item.values()))
            self.assertTrue(all(any(evidence_id in step for evidence_id in ids) for step in report["workflow"]))
        events = store.db.execute("SELECT agent,message FROM events ORDER BY seq").fetchall()
        self.assertEqual(len(events), 3)
        self.assertTrue(all(row["agent"] == "demo-seeder" and "no agent or model" in row["message"] for row in events))
        store.db.close()

    def test_rerun_preserves_existing_reviews_and_demo_content(self) -> None:
        seed(self.db, self.uploads)
        store = Store(str(self.db), str(self.uploads))
        store.db.execute(
            "INSERT INTO versions(job_id,reviewer_name,success,note,procedure,created_at) VALUES(?,?,?,?,?,?)",
            ("demo-motor", "Human", 1, "keep", "reviewed steps", "2026-01-01T00:00:00+00:00"),
        )
        store.db.execute("UPDATE jobs SET description=? WHERE id=?", ("[DEMO] Human-customized title", "demo-motor"))
        store.db.commit()
        store.db.close()

        self.assertEqual(seed(self.db, self.uploads), 0)
        reopened = Store(str(self.db), str(self.uploads))
        self.assertEqual(reopened.get("demo-motor")["description"], "[DEMO] Human-customized title")
        self.assertEqual(len(reopened.versions("demo-motor")), 1)
        self.assertEqual(reopened.db.execute("SELECT count(*) n FROM events").fetchone()["n"], 3)
        reopened.db.close()

    def test_does_not_interfere_with_real_queued_job(self) -> None:
        store = Store(str(self.db), str(self.uploads))
        job, _ = store.create("Real queued analysis", "en", {"source": "real"})
        store.submit(job["id"])
        store.db.close()

        self.assertEqual(seed(self.db, self.uploads), 3)
        reopened = Store(str(self.db), str(self.uploads))
        real = reopened.get(job["id"])
        self.assertEqual(real["status"], "queued")
        claimed, _ = reopened.worker_claim()
        self.assertEqual(claimed["id"], job["id"])
        self.assertNotIn(claimed["id"], {demo["id"] for demo in DEMOS})
        reopened.db.close()


if __name__ == "__main__":
    unittest.main()
