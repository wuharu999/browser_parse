# Third-party notices

Robot Log Workbench uses the following direct open-source dependencies. Exact installed versions and transitive dependencies are recorded in `package-lock.json` and `uv.lock`; upstream packages retain their own license and copyright notices.

| Package | Purpose | License | Upstream |
| --- | --- | --- | --- |
| `@zip.js/zip.js` | ZIP reading/decompression | BSD-3-Clause | [zip.js](https://github.com/gildas-lormeau/zip.js) |
| `it-tar` | Streaming TAR extraction | Apache-2.0 OR MIT | [it-tar](https://github.com/alanshaw/it-tar) |
| `typescript` | Type checking | Apache-2.0 | [TypeScript](https://github.com/microsoft/TypeScript) |
| `vite` | Development server and production bundling | MIT | [Vite](https://github.com/vitejs/vite) |
| `vitest` | Automated tests | MIT | [Vitest](https://github.com/vitest-dev/vitest) |
| `fastapi` | Python HTTP API | MIT | [FastAPI](https://github.com/fastapi/fastapi) |
| `uvicorn` | ASGI server | BSD-3-Clause | [Uvicorn](https://github.com/encode/uvicorn) |
| `httpx` | API test transport | BSD-3-Clause | [HTTPX](https://github.com/encode/httpx) |
| `Squid` | Restricted outbound HTTPS proxy | GPL-2.0-or-later | [Squid](https://www.squid-cache.org/) |

The analysis container image also installs OpenAI Codex CLI (Apache-2.0), Node.js (MIT and bundled component notices), Python (PSF license), and Poppler utilities (GPL-family component licenses). Preserve image/package license notices when distributing an image. The supplied wiki and user uploads are private data, not licensed project dependencies, and are excluded from Git.

The prepared image additionally uses uv, Tesseract and English/Chinese language
data, jq, SQLite, compression utilities, and the Python packages listed in
`sandbox/requirements.in` (numpy, pandas, matplotlib, Pillow, pypdf, PyYAML,
h5py, mcap, mcap-ros2-support, rosbags, lz4 and zstandard). Exact Python versions,
transitive resolutions and wheel hashes are in `sandbox/requirements.lock`.
Their upstream license files remain in installed distributions; retain those
and the OS package notices when distributing an image. These are sandbox
dependencies, not packages added to the browser bundle or host Python runtime.

Dependencies are installed through npm, not vendored into this repository. Their license files are distributed with the packages. Preserve applicable upstream notices when distributing dependencies or generated bundles.

This notice documents third-party components only; it does not assign an open-source license to this project's own code. Public GitHub visibility does not itself grant a project-wide redistribution license; none has been selected by the owner.
