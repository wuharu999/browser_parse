# Prebuilt analysis environment

Use `python3` (uv-managed Python 3.12) or `uv run --active --no-project --no-sync`
for existing-environment scripts. `/opt/sandbox/environment.json` is the image's
offline build-check manifest; read it for installed versions. Prefer installed
tools to network-dependent installation during an incident.

| Input / operation | Available tools | Bounded approach |
| --- | --- | --- |
| Text, TAR/GZIP/ZIP | evidence.py, rg, tar, gzip, unzip, Python stdlib | Exact member and line windows; preserve originals |
| JSON / YAML / CSV | jq, PyYAML, pandas, numpy | Select fields; pandas chunksize for large tables |
| PDF text / scans | pdfinfo, pdftotext, pdftoppm, pypdf, Tesseract eng/chi_sim | Use pdf-evidence skill; OCR uncertainty is not a fact |
| Images / plots | Pillow, matplotlib with Agg | Inspect original; cap rendered pixels and batch size |
| MCAP / ROS bags | mcap, mcap-ros2-support, rosbags, lz4, zstandard | Inspect schema/topics/time bounds before decoding selected records |
| SQLite / HDF5 | sqlite3, h5py, numpy | Read-only SQLite; slice selected HDF5 datasets |
| Wiki | evidence.py search / wiki-context, SQLite FTS5 | Search then retrieve original lines, not the entire vault |

ROS message readers do not provide ROS runtime, robot drivers, GPU inference,
simulation, network access to hardware, or proprietary message definitions.
Custom schemas still require supplied definitions. A missing schema is an
evidence limitation, not permission to download or execute an arbitrary SDK.

The shared worker reserves resources before creating this VM. Small/standard/
large profiles are chosen from upload size, file types and bounded browser
expansion hints. Their CPU/RAM/disk limits are in `/workspace/job.json`.
This preparation does not guarantee that every possible package or workload is
supported. For an operator's offline image check, use
`python3 /opt/sandbox/check_environment.py`; routine analysis should read the
cached manifest instead of rerunning the full check.
