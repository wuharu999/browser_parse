import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.resources import GIB, MIB, estimate_resources
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

    @staticmethod
    def capacity(cpu=4000, memory=8192, disk=32768):
        return {"cpu_milli": cpu, "memory_mb": memory, "disk_mb": disk}

    def test_two_large_jobs_fit_on_two_capable_workers_and_cancellation_keeps_reservation(self):
        large = self.job(size=600 * MIB); second_large = self.job(size=600 * MIB)
        small = self.job()
        running, _ = self.store.worker_claim("machine-a", self.capacity())
        self.assertEqual(running["id"], large["id"])
        another, _ = self.store.worker_claim("machine-b", self.capacity())
        self.assertEqual(another["id"], second_large["id"])
        self.assertEqual(self.store.budget()["running"], 2)
        self.store.cancel(large["id"])
        self.assertIsNone(self.store.worker_claim("machine-c", self.capacity())[0])
        self.store.finish(large["id"], "machine-a", "cancelled", "test stopped", 0, {})
        self.assertEqual(self.store.worker_claim("machine-c", self.capacity())[0]["id"], small["id"])

    def test_concurrent_distinct_claimers_cannot_overallocate_global_two_slots(self):
        for _ in range(3): self.job("scan.pdf")
        with ThreadPoolExecutor(max_workers=3) as pool:
            claimed = list(pool.map(lambda index: self.store.worker_claim(f"machine-{index}", self.capacity())[0], range(3)))
        self.assertEqual(sum(job is not None for job in claimed), 2)

    def test_one_worker_has_one_active_job(self):
        first, second = self.job(), self.job()
        self.assertEqual(self.store.worker_claim("machine-a", self.capacity())[0]["id"], first["id"])
        self.assertIsNone(self.store.worker_claim("machine-a", self.capacity())[0])
        self.assertEqual(self.store.worker_claim("machine-b", self.capacity())[0]["id"], second["id"])

    def test_capacity_skips_unsuitable_older_job_and_keeps_fifo_among_fit_jobs(self):
        large = self.job(size=600 * MIB)
        small = self.job()
        claimed, _ = self.store.worker_claim("small-host", self.capacity(cpu=1000, memory=2048, disk=8192))
        self.assertEqual(claimed["id"], small["id"])
        self.assertEqual(self.store.get(large["id"])["status"], "queued")
        self.assertEqual(self.store.get(small["id"])["resource_plan"]["profile"], "small")

    def test_malformed_capacity_is_rejected(self):
        self.job()
        for capacity in ({}, {"cpu_milli": 1, "memory_mb": 1, "disk_mb": 0}, {"cpu_milli": True, "memory_mb": 1, "disk_mb": 1}):
            with self.assertRaisesRegex(ValueError, "worker capacity"):
                self.store.worker_claim("machine-a", capacity)

    def test_hard_limit_job_allocated_8gb_tier_and_claimed_by_8gb_worker(self):
        # A 1.1 GB multi-file upload hits the large hard-limit tier.
        job = self.job(name="archive.zip", size=1100 * MIB)
        plan = self.store.get(job["id"])["resource_plan"]
        self.assertEqual(plan["profile"], "large")
        self.assertEqual(plan["cpu_milli"], 4000)
        self.assertEqual(plan["memory_mb"], 7168)
        self.assertEqual(plan["disk_mb"], 24576)

        # An 8 GB physical host (MemTotal ~8192-8744 MB minus 1024 MB OS reserve)
        # advertises ~7720 MB RAM. It must claim the large job by default.
        eight_gb_worker_capacity = {"cpu_milli": 4000, "memory_mb": 7720, "disk_mb": 24576}
        claimed, _ = self.store.worker_claim("worker-8gb-node", eight_gb_worker_capacity)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["resource_plan"]["profile"], "large")

