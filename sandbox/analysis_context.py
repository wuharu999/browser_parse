"""Bounded, factual notes shared by the runner and ECS (stdlib only)."""
import json
import re
from typing import Callable

MAX_CONTEXT_BYTES = 16 * 1024
FIELDS = ("observations", "hypotheses_checked", "evidence", "unresolved_questions")


def redact(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)\b(sk[-_][\w-]{8,}|AKIA[0-9A-Z]{12,})\b", "[redacted]", value)
    return re.sub(r"(?i)\b(password|api[_-]?key|token|secret)\s*([:=])\s*[^\s,;]+", r"\1\2[redacted]", value)


def validate_context(value: object, sanitizer: Callable[[str], str] = redact) -> dict | None:
    """Invalid/oversized optional notes are discarded, never truncate JSON."""
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_CONTEXT_BYTES:
            return None
        if not isinstance(value, dict) or set(value) != {"schemaVersion", *FIELDS}:
            return None
        if value["schemaVersion"] != "robot-analysis-notes/v1":
            return None
        result = {"schemaVersion": value["schemaVersion"]}
        for field in FIELDS:
            entries = value[field]
            if not isinstance(entries, list) or len(entries) > 40:
                return None
            if any(not isinstance(entry, str) or not entry.strip() or len(entry) > 2000 for entry in entries):
                return None
            result[field] = [sanitizer(redact(entry)) for entry in entries]
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
            return None
        return result
    except (ValueError, TypeError, RecursionError):
        return None
