"""Bounded local evidence tools. Archives are read, never extracted. No model calls."""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tarfile
import zipfile

MAX_SCAN = 2 * 1024**3


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Requested file is unavailable or outside its source root")
    return path


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def bounded(value: dict, limit: int = 12000) -> str:
    limit = max(2048, min(65536, limit))
    key = "lines" if "lines" in value else "results"
    while len(encode(value).encode()) > limit and value.get(key):
        if key == "lines" and len(value[key]) == 1:
            item = value[key][0]
            original = item["text"]
            item["clipped"] = True
            low, high = 0, len(original)
            while low < high:
                size = (low + high + 1) // 2
                half = size // 2
                item["text"] = original[:half] + " [... clipped ...] " + (original[-half:] if half else "")
                if len(encode(value).encode()) <= limit:
                    low = size
                else:
                    high = size - 1
            half = low // 2
            item["text"] = original[:half] + " [... clipped ...] " + (original[-half:] if half else "")
            break
        removed = value[key].pop()
        value["truncated"] = True
        if key == "lines":
            value["next_line"] = removed["line"]
    result = encode(value)
    if len(result.encode()) > limit:
        return encode({"error": "Response metadata exceeds byte budget", "truncated": True})
    return result


def line_window(stream, start: int, count: int = 20) -> dict:
    start, count = max(1, start), max(1, min(200, count))
    lines, number, scanned = [], 0, 0
    while True:
        # Bounded readline avoids allocating an entire malformed/giant line.
        part = stream.readline(16385)
        if not part:
            return {"lines": lines, "truncated": False}
        scanned += len(part)
        if scanned > MAX_SCAN:
            return {"lines": lines, "truncated": True, "warning": "2 GiB scan limit reached"}
        initial = part
        head, tail, total = part[:8192], part[-4096:], len(part)
        while not part.endswith(b"\n") and part:
            part = stream.readline(16385)
            scanned += len(part)
            total += len(part)
            tail = (tail + part)[-4096:]
            if scanned > MAX_SCAN:
                return {"lines": lines, "truncated": True, "warning": "2 GiB scan limit reached"}
        number += 1
        if number < start:
            continue
        if len(lines) >= count:
            return {"lines": lines, "truncated": True, "next_line": number}
        clipped = total > 16384
        raw = head + b"\n[... line middle omitted ...]\n" + tail if clipped else initial
        text = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
        lines.append({"line": number, "text": text.replace("\0", "\\0"), "clipped": clipped,
                      "damaged": "\0" in text or "\ufffd" in text})


def index_wiki(root: Path, database: Path) -> dict:
    database.parent.mkdir(parents=True, exist_ok=True)
    skipped = []
    with sqlite3.connect(database) as db:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(path UNINDEXED, sha256 UNINDEXED, text)")
        db.execute("DELETE FROM pages")
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            if path.stat().st_size > 4 * 1024**2:
                skipped.append(name)
                continue
            raw = path.read_bytes()
            db.execute("INSERT INTO pages VALUES (?,?,?)", (name, hashlib.sha256(raw).hexdigest(), raw.decode("utf-8", errors="replace")))
        count = db.execute("SELECT count(*) FROM pages").fetchone()[0]
    return {"indexed_pages": count, "skipped": skipped, "scope": "all Markdown files, not index.md links"}


def search_wiki(database: Path, query: str, count: int = 8) -> dict:
    terms = re.findall(r"[\w-]+", query[:300], flags=re.UNICODE)[:8]
    if not terms:
        return {"results": [], "truncated": False}
    count = max(1, min(20, count))
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as db:
        # Exact canonical page names outrank short query pages that merely mention them.
        basename = query.strip().removesuffix(".md") + ".md"
        exact = db.execute("SELECT path,sha256,text FROM pages WHERE path=? OR path=? OR path=? LIMIT ?", (basename, "entities/" + basename, "concepts/" + basename, count)).fetchall()
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        rows = db.execute("SELECT path,sha256,text FROM pages WHERE pages MATCH ? ORDER BY rank LIMIT ?", (expression, count)).fetchall()
        if not rows:
            # ponytail: LIKE fallback handles unsegmented Chinese; replace only if vault-scale scans become slow.
            clauses = " OR ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
            escaped = ["%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for term in terms]
            rows = db.execute(f"SELECT path,sha256,text FROM pages WHERE {clauses} LIMIT ?", [*escaped, count]).fetchall()
        seen = {row[0] for row in exact}
        rows = (exact + [row for row in rows if row[0] not in seen])[:count]
    results = []
    for path, digest, body in rows:
        offsets = [body.casefold().find(term.casefold()) for term in terms]
        offset = min((position for position in offsets if position >= 0), default=0)
        start = max(0, body.rfind("\n", 0, offset) + 1)
        results.append({"path": path, "sha256": digest, "line": body.count("\n", 0, start) + 1, "excerpt": body[start:start + 700]})
    return {"results": results, "truncated": len(rows) == count}


def wiki_context(root: Path, name: str, start: int, count: int) -> dict:
    try:
        path = inside(root, name)
    except ValueError:
        # Obsidian links may name a unique basename rather than a relative path.
        if "/" in name or "\\" in name:
            raise
        matches = [p for p in root.rglob(name if name.endswith(".md") else name + ".md") if p.is_file() and not p.is_symlink()]
        if len(matches) != 1:
            raise ValueError("Wiki basename is absent or ambiguous; use its root-relative path")
        path = inside(root, matches[0].relative_to(root).as_posix())
    with path.open("rb") as stream:
        result = line_window(stream, start, count)
    return {"path": path.relative_to(root).as_posix(), **result}


def log_context(workspace: Path, source_id: str, start: int, count: int) -> dict:
    job = json.loads(inside(workspace, "job.json").read_text())
    evidence = job.get("evidence") or {}
    report = next((item for item in evidence.get("files", []) if item.get("sourceId") == source_id), None)
    if not report or report.get("retrieval") == "unsupported":
        raise ValueError("Source is absent from the immutable job manifest or unsupported")
    index = report["sourceIndex"]
    catalog = evidence["source"]["catalog"][index]
    files = job.get("files", [])
    # Upload order is part of the manifest; never silently fall back to a same-named file.
    if index >= len(files) or files[index]["name"] != catalog["name"]:
        raise ValueError("Upload order/name differs from browser source catalog")
    artifact = files[index]
    path = inside(workspace, artifact["local_path"])
    with path.open("rb") as original:
        digest = hashlib.file_digest(original, "sha256").hexdigest()
    if digest != artifact.get("sha256"):
        raise ValueError("Original upload digest does not match its immutable job manifest")
    member_name = report["path"].split("!/", 1)[1] if "!/" in report["path"] else None
    name = catalog["name"].lower()
    with contextlib.ExitStack() as stack:
        if member_name is not None:
            ordinal = int(source_id.rsplit("entry-", 1)[1])
            if name.endswith(".zip"):
                archive = stack.enter_context(zipfile.ZipFile(path))
                members = archive.infolist()
                if ordinal >= len(members) or members[ordinal].filename != member_name or members[ordinal].is_dir():
                    raise ValueError("ZIP member ordinal/path does not match browser evidence")
                stream = stack.enter_context(archive.open(members[ordinal]))
            elif name.endswith((".tar", ".tar.gz", ".tgz")):
                archive = stack.enter_context(tarfile.open(path, mode="r|*"))
                member = next((item for n, item in enumerate(archive) if n == ordinal), None)
                if member is None or member.name != member_name or not member.isfile():
                    raise ValueError("TAR member ordinal/path does not match browser evidence")
                if member.size > MAX_SCAN:
                    raise ValueError("Declared member exceeds scan limit")
                stream = stack.enter_context(archive.extractfile(member))
            else:
                raise ValueError("Unsupported archive type")
        else:
            stream = stack.enter_context(gzip.open(path, "rb") if name.endswith(".gz") else path.open("rb"))
        result = line_window(stream, start, count)
    return {"source_id": source_id, "path": report["path"], "archive_sha256": artifact.get("sha256"),
            "integrity": "original upload SHA-256 verified; partial archive replay does not revalidate CRC", **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--wiki", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--max-bytes", type=int, default=12000)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index")
    search = commands.add_parser("search")
    search.add_argument("query")
    for command in ("wiki-context", "log-context"):
        sub = commands.add_parser(command)
        sub.add_argument("source")
        sub.add_argument("--start", type=int, default=1)
        sub.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    root = args.wiki or args.workspace / "wiki"
    database = args.database or args.workspace / "wiki.sqlite3"
    try:
        if args.command == "index":
            if not root.is_dir():
                raise ValueError("Wiki root does not exist")
            result = index_wiki(root, database)
        elif args.command == "search":
            result = search_wiki(database, args.query)
        elif args.command == "wiki-context":
            result = wiki_context(root, args.source, args.start, args.count)
        else:
            result = log_context(args.workspace, args.source, args.start, args.count)
    except (OSError, ValueError, KeyError, IndexError, sqlite3.Error, tarfile.TarError, zipfile.BadZipFile) as exc:
        result = {"error": str(exc)[:500]}
    print(bounded(result, args.max_bytes))


if __name__ == "__main__":
    main()
