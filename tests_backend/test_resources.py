import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.resources import GIB, MIB, PROFILES, estimate_resources
from backend.store import Store


class SizingTests(unittest.TestCase):
    def test_small_text_standard_media_and_large_volume(self):
        for name, size, profile in (("robot.log", 5000, "small"), ("scan.pdf", 5000, "standard"), ("robot.mcap", 5000, "standard"), ("robot.log", 600 * MIB, "large")):
            self.assertEqual(estimate_resources([{"name": name, "size": size}], None)["profile"], profile)

    def test_expansion_and_count_hints_only_raise_tier(self):
        evidence = {"schemaVersion": "robot-log-evidence/v2", "totals": {"expandedBytes": 3 * GIB, "files": 1}}
        self.assertEqual(estimate_resources([{"name": "logs.tgz", "size": 1000}], evidence)["profile"], "large")
        evidence["totals"]["expandedBytes"] = -999
        self.assertEqual(estimate_resources([{"name": "logs.tgz", "size": 600 * MIB}], evidence)["profile"], "large")
        evidence["totals"] = {"expandedBytes": 10**100, "files": 10**100}
        result = estimate_resources([], evidence)
        self.assertEqual((result["expanded_bytes_hint"], result["entry_count_hint"]), (8 * GIB, 10000))
        self.assertEqual(estimate_resources([], {"totals": "bad"})["profile"], "small")

    def test_unknown_archive_and_embedded_binary_use_standard(self):
        self.assertEqual(estimate_resources([{"name": "x.zip", "size": 10}], None)["profile"], "standard")
        evidence = {"schemaVersion": "robot-log-evidence/v2", "totals": {}, "files": [{"path": "x.tgz!/data.mcap", "status": "skipped"}]}
        self.assertEqual(estimate_resources([], evidence)["profile"], "standard")


class ResourceQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(str(root / "jobs.sqlite3"), str(root / "uploads"), estimate=1)

    def tearDown(self):
        self.store.db.close(); self.temp.cleanup()

    def job(self, name="robot.log", size=100):
        job, _ = self.store.create("synthetic sizing test", "en", None)
        self.store.reserve_upload(job["id"], size)
        self.store.add_file(job["id"], name, "fixture", size, "0" * 64, size)
        return self.store.submit(job["id"])

    def test_large_runs_alone_and_cancellation_keeps_resource_reservation(self):
        large = self.job(size=600 * MIB); small = self.job()
        running, _ = self.store.worker_claim()
        self.assertEqual(running["id"], large["id"])
        self.assertIsNone(self.store.worker_claim()[0])
        self.assertEqual(self.store.budget()["resources_used"], PROFILES["large"])
        self.assertTrue(self.store.budget()["resource_wait"])
        self.store.cancel(large["id"])
        self.assertIsNone(self.store.worker_claim()[0])
        self.store.finish(large["id"], "cancelled", "test stopped", 0, {})
        self.assertEqual(self.store.worker_claim()[0]["id"], small["id"])

    def test_concurrent_claims_cannot_overallocate_two_standard_jobs(self):
        for _ in range(3): self.job("scan.pdf")
        with ThreadPoolExecutor(max_workers=3) as pool:
            claimed = list(pool.map(lambda _: self.store.worker_claim()[0], range(3)))
        self.assertEqual(sum(job is not None for job in claimed), 2)
        self.assertEqual(self.store.budget()["resources_used"], {"cpu_milli": 4000, "memory_mb": 8192, "disk_mb": 32768})

    def test_fifo_does_not_starve_large_job_behind_small(self):
        first = self.job(); large = self.job(size=600 * MIB); self.job()
        self.assertEqual(self.store.worker_claim()[0]["id"], first["id"])
        self.assertIsNone(self.store.worker_claim()[0])
        self.store.finish(first["id"], "completed", "test", 0, {})
        self.assertEqual(self.store.worker_claim()[0]["id"], large["id"])

    def test_cpu_and_disk_are_gates_and_plan_persists(self):
        job = self.job("scan.pdf")
        self.store.resource_pool["cpu_milli"] = 2000
        self.store.worker_claim(); self.job()
        self.assertIsNone(self.store.worker_claim()[0])
        self.assertEqual(self.store.get(job["id"])["resource_plan"]["profile"], "standard")
        self.store.resource_pool["disk_mb"] = 8192
        with self.assertRaisesRegex(ValueError, "larger than this worker"):
            self.job("scan.pdf")

    def test_legacy_running_job_is_not_counted_as_free(self):
        job = self.job(); self.store.worker_claim()
        self.store.db.execute("UPDATE jobs SET resource_plan=NULL WHERE id=?", (job["id"],)); self.store.db.commit()
        self.assertEqual(self.store.budget()["resources_used"], PROFILES["standard"])
