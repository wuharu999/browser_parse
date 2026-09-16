"""Intake processing, security validation, and answer normalization for Grill Bot.

Restricts uploads to PDF, images, Markdown, and TXT files.
Performs multimodal inspection, OWASP prompt injection scanning,
and structures customer answers into sanitized state updates.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from backend.guard import GuardVerdict, SecurityGuard, GuardError

ALLOWED_GRILL_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".md",
    ".txt",
}

MAX_FILE_BYTES = 32 * 1024 * 1024  # 32 MiB max file upload
MAX_TEXT_BYTES = 64 * 1024         # 64 KiB max extracted text
INJECTION_OVERRIDE_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an|in|the)\b", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?rules", re.IGNORECASE),
    re.compile(r"bypass\s+(safety|security|verification)", re.IGNORECASE),
    re.compile(r"<script[\s>]", re.IGNORECASE),
]

KNOWN_ROBOTS = [
    "Unitree B2", "Unitree Go2", "Unitree H1", "Unitree G1", "Unitree B1", "Unitree AlienGo",
    "Boston Dynamics Spot", "Spot", "ANYbotics ANYmal", "ANYmal",
    "UR3", "UR5", "UR10", "UR16", "UR20", "UR30", "Universal Robots",
    "Franka Emika Panda", "Franka Panda", "Franka Research 3",
    "KUKA", "ABB", "Yaskawa", "Fanuc",
    "AgileX Scout", "AgileX Bunker", "AgileX Tracer", "AgileX Hunter", "AgileX Limbo",
    "Clearpath Husky", "Clearpath Jackal", "Clearpath Boxer",
    "Fetch", "TIAGo", "TurtleBot4", "TurtleBot3",
]


class SecurityError(ValueError):
    """Raised when an input or attachment fails security validation."""
    pass


@dataclass
class NormalizedIntake:
    task_intent: str
    explicit_requirements: list[str] = field(default_factory=list)
    explicit_constraints: list[str] = field(default_factory=list)
    referenced_robot: str | None = None
    security_flags: list[str] = field(default_factory=list)
    ambiguous_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_upload_filename(filename: str) -> str:
    """Verify that filename has an allowed extension and contains no path traversal."""
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("Filename cannot be empty")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"Invalid filename path characters: {filename}")
    ext = Path(cleaned).suffix.lower()
    if ext not in ALLOWED_GRILL_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{ext}'. Allowed extensions: "
            f"{', '.join(sorted(ALLOWED_GRILL_EXTENSIONS))}"
        )
    return cleaned


def extract_text_from_pdf(path: Path) -> str:
    """Extract text from PDF using pypdf or fallback to pdftotext."""
    # Try pypdf if available
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages_text = []
        for page in reader.pages[:10]:  # Cap at first 10 pages
            t = page.extract_text() or ""
            if t:
                pages_text.append(t)
        combined = "\n".join(pages_text)
        if combined.strip():
            return combined[:MAX_TEXT_BYTES]
    except Exception:
        pass

    # Fallback to system pdftotext
    try:
        proc = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "10", str(path), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
            errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout[:MAX_TEXT_BYTES]
    except Exception:
        pass

    return ""


def extract_file_content(path: Path, filename: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract textual content or image payload from supported file types."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        text = extract_text_from_pdf(path)
        return text, []
    elif ext in {".md", ".txt"}:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_BYTES]
            return content, []
        except Exception:
            return "", []
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        mime = f"image/{ext.lstrip('.').replace('jpg', 'jpeg')}"
        try:
            raw_bytes = path.read_bytes()
            b64 = base64.b64encode(raw_bytes[: 5 * 1024 * 1024]).decode("ascii")
            return f"[Image attachment: {filename}]", [{"mime_type": mime, "data": b64, "name": filename}]
        except Exception:
            return f"[Unreadable image attachment: {filename}]", []
    return "", []


def detect_injection_in_text(text: str) -> bool:
    """Check text against known prompt injection override patterns."""
    for pattern in INJECTION_OVERRIDE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def normalize_intake(
    task_intent: str,
    attachments_text: str = "",
    images: list[dict[str, Any]] | None = None,
    guard: SecurityGuard | None = None,
) -> NormalizedIntake:
    """Normalize user scenario description and attachments into typed NormalizedIntake.

    Performs prompt injection scan using Guard if provided, or local pattern matching.
    """
    clean_prompt = task_intent.strip()
    if not clean_prompt:
        raise ValueError("Task intent cannot be empty")

    # 1. Security scan
    combined_text = f"{clean_prompt}\n{attachments_text}"
    if detect_injection_in_text(combined_text):
        raise SecurityError("Prompt injection attempt detected in input prompt or documents.")

    if guard is not None:
        try:
            res = guard.inspect(
                description=clean_prompt,
                attachments_text=attachments_text,
                image_attachments=images or [],
            )
            if res.verdict == GuardVerdict.INJECTION:
                raise SecurityError(f"Security pre-check rejected input: {res.reason}")
            if res.sanitized_description:
                clean_prompt = res.sanitized_description
        except (GuardError, Exception) as exc:
            if isinstance(exc, SecurityError):
                raise
            # Fail-closed if security scanner errors
            raise SecurityError(f"Security validation failed: {exc}")

    # 2. Extract referenced robot
    referenced_robot: str | None = None
    lower_full = combined_text.lower()
    for robot in KNOWN_ROBOTS:
        if robot.lower() in lower_full:
            referenced_robot = robot
            break

    # 3. Extract explicit constraints and requirements via heuristic extraction
    requirements: list[str] = []
    constraints: list[str] = []
    ambiguous: list[str] = []

    # Check for payload/mass
    mass_match = re.search(r"(\d+(?:\.\d+)?)\s*(kg|千克|公斤|g|克)", combined_text, re.IGNORECASE)
    if mass_match:
        constraints.append(f"Payload/Mass: {mass_match.group(1)} {mass_match.group(2)}")
    else:
        ambiguous.append("Object mass / payload unknown")

    # Check for speed / time
    time_match = re.search(r"(\d+(?:\.\d+)?)\s*(s|秒|min|分钟|小时|h)", combined_text, re.IGNORECASE)
    if time_match:
        constraints.append(f"Duration limit: {time_match.group(1)} {time_match.group(2)}")

    # Check for dimension / distance
    dim_match = re.search(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|米|毫米|厘米)", combined_text, re.IGNORECASE)
    if dim_match:
        constraints.append(f"Dimension/Distance: {dim_match.group(1)} {dim_match.group(2)}")

    if not referenced_robot:
        ambiguous.append("Target robot model or hardware type not specified")

    requirements.append(clean_prompt)

    return NormalizedIntake(
        task_intent=clean_prompt,
        explicit_requirements=requirements,
        explicit_constraints=constraints,
        referenced_robot=referenced_robot,
        security_flags=[],
        ambiguous_items=ambiguous,
    )


def sanitize_free_text(text: str) -> str:
    """Sanitize free-text user response to prevent prompt injection and length bloat."""
    trimmed = text.strip()[:500]
    if detect_injection_in_text(trimmed):
        raise SecurityError("Unsafe instructions detected in free-text answer")
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", "", trimmed)
    return clean.strip()


def normalize_customer_answers(
    answers: list[dict[str, Any]],
    current_questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Structure customer turn answers into sanitized updates.

    Each answer matches a question in current_questions.
    Supports:
    - selected_option (A / B / C suggested label)
    - unknown (bool)
    - free_text (string, sanitized)
    """
    q_map = {q["id"]: q for q in current_questions if "id" in q}
    resolved_fields: list[dict[str, Any]] = []
    unresolved_fields: list[str] = []
    notes: list[str] = []

    for ans in answers:
        q_id = ans.get("question_id")
        q_obj = q_map.get(q_id)
        if not q_obj:
            continue

        target_ids = q_obj.get("target_ids", [])
        is_unknown = bool(ans.get("unknown", False))
        free_text = ans.get("free_text")
        selected_option = ans.get("selected_option")

        if is_unknown:
            for tid in target_ids:
                unresolved_fields.append(tid)
            notes.append(f"{q_obj.get('text')}: Customer marked as unknown/not sure.")
        elif free_text and str(free_text).strip():
            safe_val = sanitize_free_text(str(free_text))
            for tid in target_ids:
                resolved_fields.append({
                    "target_id": tid,
                    "value": safe_val,
                    "source": "free_text",
                    "interpretation": f"Customer specified: {safe_val}",
                })
            notes.append(f"{q_obj.get('text')}: {safe_val}")
        elif selected_option:
            # Find matching option interpretation
            opt_interp = ""
            for opt in q_obj.get("options", []):
                if opt.get("label") == selected_option:
                    opt_interp = opt.get("interpretation", "")
                    break
            for tid in target_ids:
                resolved_fields.append({
                    "target_id": tid,
                    "value": selected_option,
                    "source": "option",
                    "interpretation": opt_interp or selected_option,
                })
            notes.append(f"{q_obj.get('text')}: {selected_option} ({opt_interp})")

    return {
        "resolved_fields": resolved_fields,
        "unresolved_fields": unresolved_fields,
        "customer_notes": notes,
    }
