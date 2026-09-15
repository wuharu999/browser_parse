"""Deterministic, bounded upload sizing. Estimates are not workload guarantees."""
from __future__ import annotations

from typing import Any

MIB = 1024**2
GIB = 1024**3
PROFILES = {
    "small": {"cpu_milli": 1000, "memory_mb": 2048, "disk_mb": 8192},
    "standard": {"cpu_milli": 2000, "memory_mb": 4096, "disk_mb": 16384},
    # 7168 MiB (7 GiB) is the container limit on an 8 GiB host node, reserving
    # ~1 GiB for the Linux kernel, Docker daemon, and host processes.
    "large": {"cpu_milli": 4000, "memory_mb": 7168, "disk_mb": 24576},
}


def _count(value: Any, ceiling: int) -> int:
    return min(value, ceiling) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def estimate_resources(files: list[dict], evidence: Any) -> dict:
    """Use server-verified upload sizes; browser metadata can only raise the tier."""
    uploaded = sum(_count(item.get("size"), 4 * GIB) for item in files)
    names = [str(item.get("name", "")).lower() for item in files]
    media = sum(name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif")) for name in names)
    pdfs = sum(name.endswith(".pdf") for name in names)
    archives = any(name.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".gz")) for name in names)
    valid_evidence = isinstance(evidence, dict) and evidence.get("schemaVersion") == "robot-log-evidence/v2"
    totals = evidence.get("totals", {}) if valid_evidence else {}
    totals = totals if isinstance(totals, dict) else {}
    expanded = _count(totals.get("expandedBytes"), 8 * GIB)
    entries = max(len(files), _count(totals.get("files"), 10000))
    reports = evidence.get("files", []) if valid_evidence else []
    reports = reports[:10000] if isinstance(reports, list) else []
    binary = any(name.endswith((".mcap", ".bag", ".db3", ".h5", ".hdf5")) for name in names + [str(row.get("path", "")).lower() for row in reports if isinstance(row, dict)])
    incomplete = any(isinstance(row, dict) and row.get("status") in {"partial", "error"} for row in reports)
    reasons = []
    if uploaded >= 512 * MIB or expanded >= 2 * GIB or entries >= 2000 or media >= 20 or pdfs >= 10:
        profile = "large"
        reasons.append("High upload, expanded-data, entry-count or media-count estimate")
    elif uploaded >= 32 * MIB or expanded >= 128 * MIB or entries >= 200 or media or binary or (archives and not valid_evidence) or incomplete:
        profile = "standard"
        reasons.append("Media/binary processing, medium data volume or uncertain archive coverage")
    else:
        profile = "small"
        reasons.append("Small text-focused input with bounded streaming tools")
    return {"schema_version": "robot-resource-plan/v1", "profile": profile, **PROFILES[profile],
            "uploaded_bytes": uploaded, "expanded_bytes_hint": expanded, "entry_count_hint": entries,
            "media_count": media, "reasons": reasons,
            "limits": "Heuristic only. Compressed size does not bound expanded data or peak memory; preserve streaming, bounded rendering and runtime limits."}


def allocation(plan: dict | None) -> dict[str, int]:
    # Old running jobs used the fixed 2-CPU/4-GiB template; reserve standard
    # capacity during migration rather than counting them as free resources.
    return PROFILES[(plan or {}).get("profile", "standard")]
