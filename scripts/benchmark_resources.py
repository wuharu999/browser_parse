#!/usr/bin/env python3
"""Bounded local resource sampler for a sandbox-run analysis process tree.

It never starts workloads or calls an API. Invoke it alongside an explicitly
started local/container workload with a PID visible to this Linux host.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_SECONDS = 1800


def parse_stat(text: str) -> tuple[int, int, int, int] | None:
    """Return pid, ppid, rss-pages, total CPU ticks from Linux /proc/<pid>/stat."""
    end = text.rfind(")")
    if end < 0:
        return None
    try:
        pid = int(text[:text.find(" ")])
        fields = text[end + 2:].split()
        # Fields after comm start at state (field 3): ppid=1, utime=11, stime=12,
        # rss=21. This avoids spaces and parentheses in comm confusing split().
        return pid, int(fields[1]), int(fields[21]), int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def process_tree_snapshot(root_pid: int, proc_root: Path = Path("/proc")) -> dict[str, int] | None:
    """Sum current RSS pages/CPU ticks for root and discoverable descendants."""
    records: dict[int, tuple[int, int, int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            parsed = parse_stat((entry / "stat").read_text())
        except OSError:
            continue
        if parsed:
            pid, ppid, rss_pages, ticks = parsed
            records[pid] = (ppid, rss_pages, ticks)
    if root_pid not in records:
        return None
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in records.items():
        children.setdefault(ppid, []).append(pid)
    pending, tree = [root_pid], set()
    while pending:
        pid = pending.pop()
        if pid in tree:
            continue
        tree.add(pid)
        pending.extend(children.get(pid, ()))
    page_size = os.sysconf("SC_PAGE_SIZE")
    return {
        "processes": len(tree),
        "rss_bytes_summed": sum(records[pid][1] * page_size for pid in tree),
        "cpu_ticks": sum(records[pid][2] for pid in tree),
    }


def meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, value, *_ = line.replace(":", " ").split()
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value) * 1024
    except (OSError, ValueError):
        pass
    return {"total_bytes": values.get("MemTotal", 0), "available_bytes": values.get("MemAvailable", 0)}


def directory_apparent_bytes(path: Path, timeout: float = 10) -> int | None:
    """One bounded apparent-size snapshot (`du -sb`)."""
    try:
        result = subprocess.run(["du", "-sb", "--", str(path)], capture_output=True, text=True, timeout=timeout, check=True)
        return int(result.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def directory_allocated_bytes(path: Path, timeout: float = 10) -> int | None:
    """One bounded allocated-block snapshot; unlike `-b`, sparse holes are not counted."""
    try:
        result = subprocess.run(["du", "-s", "-B1", "--", str(path)], capture_output=True, text=True, timeout=timeout, check=True)
        return int(result.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def sample(pid: int, path: Path, proc_root: Path = Path("/proc")) -> dict[str, Any] | None:
    tree = process_tree_snapshot(pid, proc_root)
    if tree is None:
        return None
    filesystem = shutil.disk_usage(path)
    return {"tree": tree, "memory": meminfo(proc_root / "meminfo"), "filesystem": {
        "total_bytes": filesystem.total, "used_bytes": filesystem.used, "free_bytes": filesystem.free,
    }}


def run(pid: int, interval: float, seconds: float, path: Path) -> dict[str, Any]:
    start = time.monotonic()
    first = sample(pid, path)
    if first is None:
        return {"schema_version": "resource-sample/v1", "status": "process_not_found", "pid": pid}
    apparent_start = directory_apparent_bytes(path)
    allocated_start = directory_allocated_bytes(path)
    peak_rss, peak_processes = first["tree"]["rss_bytes_summed"], first["tree"]["processes"]
    minimum_available = first["memory"]["available_bytes"]
    latest, stopped = first, False
    while time.monotonic() - start < seconds:
        time.sleep(min(interval, max(0, seconds - (time.monotonic() - start))))
        current = sample(pid, path)
        if current is None:
            stopped = True
            break
        latest = current
        peak_rss = max(peak_rss, current["tree"]["rss_bytes_summed"])
        peak_processes = max(peak_processes, current["tree"]["processes"])
        minimum_available = min(minimum_available, current["memory"]["available_bytes"])
    elapsed = time.monotonic() - start
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    cpu_seconds = max(0.0, (latest["tree"]["cpu_ticks"] - first["tree"]["cpu_ticks"]) / ticks_per_second)
    return {
        "schema_version": "resource-sample/v1", "status": "process_exited" if stopped else "completed",
        "pid": pid, "duration_seconds": round(elapsed, 3), "sample_interval_seconds": interval,
        "scope": "Linux-visible root process plus discoverable descendants; summed RSS is not VM physical memory and may double-count shared pages.",
        "host_memory": {"before": first["memory"], "minimum_available_bytes_during": minimum_available, "last": latest["memory"]},
        "process_tree": {"peak_rss_bytes_summed": peak_rss, "peak_process_count": peak_processes,
                         "cpu_seconds": round(cpu_seconds, 3), "cpu_core_percent": round(100 * cpu_seconds / elapsed, 2) if elapsed else 0},
        "filesystem": {"before": first["filesystem"], "last": latest["filesystem"],
                       "directory_bytes_before": apparent_start, "directory_bytes_last": directory_apparent_bytes(path),
                       "directory_bytes_kind": "apparent (du -sb)",
                       "directory_allocated_bytes_before": allocated_start,
                       "directory_allocated_bytes_last": directory_allocated_bytes(path),
                       "directory_allocated_bytes_kind": "allocated filesystem blocks (du -s -B1)"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample an existing Linux process tree; emits JSON only to --output.")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--path", type=Path, required=True, help="Existing workload directory; du is capped at 10 seconds per snapshot.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.pid <= 0 or not 0 < args.interval <= 60 or not 0 < args.seconds <= MAX_SECONDS or not args.path.is_dir():
        parser.error("--pid must be positive, --interval 0..60, --seconds 0..1800, and --path an existing directory")
    result = run(args.pid, args.interval, args.seconds, args.path)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
