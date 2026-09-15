"""Bounded public child-thread metadata shared by worker and API."""
import re

_IDENT = re.compile(r"[A-Za-z0-9._:/-]{1,120}\Z")
_STATUSES = {"pending_init", "running", "interrupted", "completed", "errored", "shutdown", "not_found", "unknown"}
_TOOLS = {"spawn_agent", "send_input", "wait", "close_agent"}
_ROLES = {"log_investigator", "telemetry_investigator", "evidence_reviewer"}


def validate_subagent(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"thread_id", "parent_thread_id", "status", "tool", "role"}:
        raise ValueError("invalid subagent lifecycle")
    child, parent = value.get("thread_id"), value.get("parent_thread_id")
    if not isinstance(child, str) or not _IDENT.fullmatch(child):
        raise ValueError("invalid subagent thread ID")
    if parent is not None and (not isinstance(parent, str) or not _IDENT.fullmatch(parent)):
        raise ValueError("invalid subagent parent ID")
    for field, allowed in (("status", _STATUSES), ("tool", _TOOLS)):
        if not isinstance(value.get(field), str) or value[field] not in allowed:
            raise ValueError(f"invalid subagent {field}")
    role = value.get("role")
    if role is not None and (not isinstance(role, str) or role not in _ROLES):
        raise ValueError("invalid subagent role")
    return {"thread_id": child, "parent_thread_id": parent, "status": value["status"], "tool": value["tool"],
            **({"role": role} if role is not None else {})}
