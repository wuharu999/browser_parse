#!/usr/bin/env python3
"""Offline, synthetic capability check for the prebuilt analysis image."""
from __future__ import annotations

import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def check() -> dict:
    commands = ("python3", "uv", "codex", "rg", "jq", "sqlite3", "tar", "gzip", "unzip", "zstd", "lz4", "pdfinfo", "pdftotext", "pdftoppm", "tesseract")
    missing = [name for name in commands if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Missing commands: {', '.join(missing)}")
    assert sys.version_info[:2] == (3, 12), "Image Python must match the 3.12 runtime"
    with tempfile.TemporaryDirectory(prefix="robot-env-check-") as folder:
        root = Path(folder)
        os.environ["MPLCONFIGDIR"] = str(root / "mpl")
        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        import yaml
        import h5py
        import lz4.frame
        import zstandard
        from PIL import Image, ImageDraw, ImageFont
        from pypdf import PdfReader
        from mcap.writer import Writer
        from mcap.reader import make_reader
        from mcap_ros2.reader import read_ros2_messages  # noqa: F401
        from rosbags.typesys import Stores, get_typestore

        assert pd.read_csv(io.StringIO("motor,current\n2,3\n2,5\n"))["current"].mean() == 4
        assert yaml.safe_load("motor: 2")["motor"] == 2
        with h5py.File(root / "sample.h5", "w") as handle:
            handle["current"] = np.array([3, 5])
        with h5py.File(root / "sample.h5") as handle:
            assert handle["current"][:].mean() == 4
        plt.plot([0, 1], [3, 5]); plt.savefig(root / "plot.png"); plt.close()
        assert Image.open(root / "plot.png").width > 0
        blob = b"synthetic robot log"
        assert lz4.frame.decompress(lz4.frame.compress(blob)) == blob
        assert zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(blob)) == blob
        with (root / "sample.mcap").open("wb") as output:
            writer = Writer(output); writer.start()
            channel = writer.register_channel("motor", "json", 0)
            writer.add_message(channel, 1, b'{"motor":2}', 1); writer.finish()
        with (root / "sample.mcap").open("rb") as source:
            assert next(make_reader(source).iter_messages())[2].data == b'{"motor":2}'
        assert "std_msgs/msg/String" in get_typestore(Stores.ROS2_HUMBLE).types
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE pages USING fts5(text)")
            db.execute("INSERT INTO pages VALUES ('motor encoder')")
            assert db.execute("SELECT count(*) FROM pages WHERE pages MATCH 'motor'").fetchone()[0] == 1
        picture = Image.new("RGB", (600, 140), "white")
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
        ImageDraw.Draw(picture).text((20, 30), "ROBOT 42", fill="black", font=font)
        picture.save(root / "page.png"); picture.save(root / "page.pdf", "PDF")
        assert len(PdfReader(root / "page.pdf").pages) == 1
        for command in (["pdfinfo", str(root / "page.pdf")], ["pdftotext", str(root / "page.pdf"), str(root / "page.txt")], ["pdftoppm", "-f", "1", "-l", "1", "-scale-to", "600", "-singlefile", "-png", str(root / "page.pdf"), str(root / "render")]):
            subprocess.run(command, check=True, capture_output=True, timeout=15)
        ocr = subprocess.run(["tesseract", str(root / "render.png"), "stdout", "-l", "eng", "--psm", "6"], check=True, capture_output=True, text=True, timeout=15).stdout
        assert "ROBOT" in ocr and "42" in ocr, "Rendered PDF OCR failed"
        languages = subprocess.run(["tesseract", "--list-langs"], check=True, capture_output=True, text=True, timeout=10).stdout
        assert "chi_sim" in languages, "Simplified Chinese OCR data is absent"
    packages = ("numpy", "pandas", "matplotlib", "Pillow", "pypdf", "PyYAML", "h5py", "mcap", "mcap-ros2-support", "rosbags", "lz4", "zstandard")
    return {"status": "ok", "python": sys.version.split()[0], "packages": {name: importlib.metadata.version(name) for name in packages}, "checks": ["CSV/numeric", "YAML", "HDF5", "plot/image", "LZ4/Zstandard", "MCAP", "ROS2-type-store", "wiki-FTS5", "PDF-render/OCR", "English/Chinese-OCR-data"], "network_required": False}


if __name__ == "__main__":
    print(json.dumps(check(), separators=(",", ":")))
