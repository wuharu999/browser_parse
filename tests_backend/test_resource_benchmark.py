from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import benchmark_resources


def stat_line(pid: int, ppid: int, rss_pages: int, utime: int, stime: int) -> str:
    # state plus fields 4..24: ppid index 1, utime 11, stime 12, rss 21.
    fields = ["S", "0"] + ["0"] * 22
    fields[1], fields[11], fields[12], fields[21] = str(ppid), str(utime), str(stime), str(rss_pages)
    return f"{pid} (worker with spaces) " + " ".join(fields)


class ResourceBenchmarkTests(unittest.TestCase):
    def test_proc_stat_parser_handles_parentheses_and_expected_fields(self) -> None:
        self.assertEqual(benchmark_resources.parse_stat(stat_line(10, 1, 7, 11, 13)), (10, 1, 7, 24))
        self.assertIsNone(benchmark_resources.parse_stat("not a stat line"))

    def test_process_tree_sums_root_and_descendant_not_other_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, line in (("10", stat_line(10, 1, 2, 5, 1)), ("11", stat_line(11, 10, 3, 7, 2)), ("12", stat_line(12, 1, 99, 0, 0))):
                target = root / pid; target.mkdir(); (target / "stat").write_text(line)
            snapshot = benchmark_resources.process_tree_snapshot(10, root)
            self.assertEqual(snapshot and snapshot["processes"], 2)
            self.assertEqual(snapshot and snapshot["cpu_ticks"], 15)
            self.assertEqual(snapshot and snapshot["rss_bytes_summed"], 5 * benchmark_resources.os.sysconf("SC_PAGE_SIZE"))

    def test_meminfo_fixture_and_missing_pid_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "meminfo").write_text("MemTotal: 16384 kB\nMemAvailable: 4096 kB\n")
            self.assertEqual(benchmark_resources.meminfo(root / "meminfo"), {"total_bytes": 16384 * 1024, "available_bytes": 4096 * 1024})
            self.assertIsNone(benchmark_resources.process_tree_snapshot(999, root))

    def test_directory_snapshots_label_apparent_and_allocated_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.bin").write_bytes(b"x" * 37)
            self.assertEqual(benchmark_resources.directory_apparent_bytes(root), 37)
            allocated = benchmark_resources.directory_allocated_bytes(root)
            self.assertIsNotNone(allocated)
            self.assertGreaterEqual(allocated or 0, 37)


if __name__ == "__main__":
    unittest.main()
