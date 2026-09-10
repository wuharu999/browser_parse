import importlib.util
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("evidence", Path(__file__).parents[1] / "sandbox/runtime/evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class EvidenceTests(unittest.TestCase):
    def test_index_all_pages_and_bounded_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wiki = root / "wiki"
            wiki.mkdir()
            (wiki / "index.md").write_text("Only a table of contents")
            (wiki / "missing-from-index.md").write_text("# Motor\nencoder 33 code 16\n")
            result = evidence.index_wiki(wiki, root / "wiki.sqlite3")
            self.assertEqual(result["indexed_pages"], 2)
            self.assertEqual(evidence.search_wiki(root / "wiki.sqlite3", "encoder")["results"][0]["path"], "missing-from-index.md")
            self.assertEqual(evidence.search_wiki(root / "wiki.sqlite3", "missing-from-index")["results"][0]["path"], "missing-from-index.md")
            with self.assertRaises(ValueError):
                evidence.wiki_context(wiki, "../secret", 1, 20)
        original = b"x" * 14000 + b"\n"
        self.assertEqual(evidence.line_window(io.BytesIO(original), 1)["lines"][0]["text"], "x" * 14000)
        result = evidence.line_window(io.BytesIO(("head" + "中" * 10000 + "tail\n").encode()), 1)
        encoded = evidence.bounded(result, 2048)
        self.assertLessEqual(len(encoded.encode()), 2048)
        self.assertIn("head", encoded)
        self.assertIn("tail", encoded)

    def test_duplicate_tar_names_use_ordinal_not_first_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "archive.tar"
            with tarfile.open(path, "w") as archive:
                for text in (b"wrong\n", b"ERROR motor 33 code 16\n"):
                    info = tarfile.TarInfo("motor.log")
                    info.size = len(text)
                    archive.addfile(info, io.BytesIO(text))
            job = {"files": [{"name": "archive.tar", "local_path": "archive.tar", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}], "evidence": {
                "source": {"catalog": [{"name": "archive.tar"}]}, "files": [{"sourceId": "source-0/entry-1", "sourceIndex": 0,
                "path": "archive.tar!/motor.log", "retrieval": "rescan-original"}]}}
            (root / "job.json").write_text(json.dumps(job))
            result = evidence.log_context(root, "source-0/entry-1", 1, 20)
            self.assertEqual(result["lines"][0]["text"], "ERROR motor 33 code 16")

    def test_log_context_hashes_upload_without_python_311_file_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "robot.log"
            path.write_bytes(b"motor recovered\n")
            job = {"files": [{"name": "robot.log", "local_path": "robot.log", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}], "evidence": {
                "source": {"catalog": [{"name": "robot.log"}]}, "files": [{"sourceId": "source-0", "sourceIndex": 0,
                "path": "robot.log", "retrieval": "direct"}]}}
            (root / "job.json").write_text(json.dumps(job))

            with patch.object(evidence.hashlib, "file_digest", None, create=True):
                result = evidence.log_context(root, "source-0", 1, 20)

            self.assertEqual(result["lines"][0]["text"], "motor recovered")


if __name__ == "__main__":
    unittest.main()
