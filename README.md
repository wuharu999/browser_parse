# Robot Log Workbench

A browser tool for preprocessing compressed robot logs, with an optional shared Python analysis queue. Select archives or a folder, click **Preprocess logs**, then **Download analysis brief**. Reading, decompression, parsing, and local context retrieval happen in Web Workers. **Preprocessing needs no Codex, API key, model calls, or LLM tokens.** Only explicitly submitting a shared analysis uploads originals, evidence and attachments to the server.

The **EN / 中文** interface switch remembers your selection. The analysis output-language selector is separate: it follows the interface until you choose it explicitly.

The default brief is at most **16,000 UTF-8 bytes** of serialized JSON; this is a byte budget, not an exact token count. Keep the larger **Export JSON** package local and use **Read original context** for bounded, on-demand excerpts. The brief is a starting point, not exhaustive evidence or a diagnosis.

## Run

Use Node.js 22.12+ in the 22.x line (tested on 22.22.1). The tooling also supports Node.js 20.19+ in the 20.x line and Node.js 24+; the exact range is declared in `package.json`.

```sh
npm ci
npm run dev -- --port 5173 --strictPort
```

Open http://127.0.0.1:5173. Keep the terminal running. Chrome/Chromium is the tested browser; folder picking requires browser support for `webkitdirectory`, and gzip requires `DecompressionStream`. File selection is available independently of folder selection.

```sh
npm test
npm run build
```

The production static application is written to `dist/`. Serve that directory with an HTTP server; opening `index.html` directly as a `file://` URL does not support the module worker.

See [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for the module map and runtime flow, and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for open-source dependencies and attribution.

## Supported inputs

- `.tar.gz`, `.tgz`, `.tar`: streamed with native gzip decompression and [`it-tar`](https://github.com/alanshaw/it-tar), without extracting files to disk. Archive paths remain in every source reference.
- `.zip`: [`zip.js`](https://gildas-lormeau.github.io/zip.js/) reads entries as streams with CRC checks. Encrypted archives are not supported.
- Plain or gzip-compressed `.log`, `.txt`, `.jsonl`, `.ndjson`, `.json`, `.yaml`, `.yml`, `.ini`, `.conf`, `.cfg`, rotated `.log.*` files, glog `.INFO`/`.WARNING`/`.ERROR`/`.FATAL` names, and conventional syslog/messages/dmesg names.
- Text recognition covers the supplied XOS bracketed timestamps/levels, Walker severity-letter timestamps, g3log, glog, syslog, JSON log records, and explicit generic level prefixes. UTF-8 damage and NUL padding are marked, counted, and make coverage partial, but readable text is retained. Oversized lines retain both ends with an explicit omission marker. JSON/configuration is read line by line; arbitrary multiline documents are not structurally parsed. No uploaded code or configuration is executed.

ROS1 `.bag`, ROS2 `.db3`, MCAP, nested archives, and unrecognized file types are inventoried as skipped. Symbolic/hard links and unsafe paths are never followed. TAR directories are traversed but do not become file reports. Invalid gzip data, truncated TAR bodies/end markers, CRC failures, and limits are surfaced as coverage problems. Earlier evidence may remain in an incomplete package; it must not be treated as a fully covered source.

## Evidence contract

The optional larger `robot-log-evidence/v2` JSON contains:

- Selected-source catalog and all emitted entry reports: stable source ID, archive/node/subsystem, path, read bytes, line counts, explicit severity counts, encountered timestamp bounds, parse quality, status, and reason. Per-file candidate/retained/omitted counts reconcile after replacements.
- Bounded warning/error/fatal excerpts, lifecycle/recovery events, metadata, and normal time-coverage anchors. Retained events include up to five preceding/following lines and source path plus 1-based line number. Clipping/damage markers distinguish excerpts from exact original lines.
- Unknown-severity diagnostic keyword candidates, explicitly marked `selectionReason: diagnostic-keyword`. These are clues, not inferred failures or diagnoses.
- Repeated candidate signatures scoped by archive, node/subsystem, and severity. Numeric motor/channel/error codes and values are **not normalized away**. Whitespace is normalized. Counts marked `countComplete: false` are lower bounds, including after bounded pattern eviction or partial archive coverage.
- Resource limits and omitted-evidence/pattern-event counts. Timestamps retain their source spelling; missing years/timezones are not invented, and first/last timestamps refer to encounter order rather than normalized chronological minima/maxima.

Defaults: 10,000 files/archive entries (directories count toward the safety limit), 16,384 retained characters per line before clipping markers/NUL escaping, 500 evidence items, 200 candidate patterns, 2 GiB expanded text per file, and 8 GiB aggregate expanded input. TAR expansion includes skipped bodies and framing for the byte guard; exported `totals.expandedBytes` counts bytes delivered to text processing and therefore differs from total archive expansion. ZIP skipped entry bodies are not expanded.

The evidence budget reserves 75% for fault candidates, 15% for lifecycle/recovery, and 10% for metadata/normal anchors (first, every 1,000th, and last eligible line). Each stratum balances archives, then node/subsystems, then files, prioritizing severity inside a share. Up to three exact repeats per file/severity keep the first two and latest selected occurrences. Unused strata need not fill the overall cap. Selection is deterministic but encounter-order-dependent; it is not exhaustive, statistically representative, or a guarantee that every rare fault/recovery is retained. Omission counts count candidate events, not all normal lines excluded by the candidate rules.

## Token-efficient handoff and local context

- `robot-log-brief/v1`: bounded summary, source names, coverage/omission warnings, and archive/subsystem-diverse excerpts without duplicated raw/context blocks. It can omit an important event present in the larger package. Do not diagnose from the brief alone.
- Full evidence export: optional local inspection artifact, deliberately larger because it preserves context. It is not the default LLM prompt.
- Context reader: choose **any readable file**, including one with no retained evidence, and a starting line; or click **Read wider context**. Each UI page contains at most 20 lines and 12,000 serialized UTF-8 bytes. Clipped lines are marked. Next-page/download/cancel controls are provided.
- Source IDs include selection order and archive-member ordinal, so duplicate member names remain distinct. Keep the original selection and browser session open. Closing/changing it invalidates access; an exported JSON alone cannot fetch local originals.
- ZIP accesses the target member; TAR/GZIP must stream through preceding data. A late-line query can be expensive and is cancellable. Early-stop context replay does **not** revalidate the entire archive checksum (`integrity: not-revalidated`). Raw files are never modified.

The UI shows at most 100 reports, candidates, and patterns per section; the export contains the complete bounded package. Exports include original log excerpts and may contain identifying or sensitive information. No automated redaction is promised. Cancelling terminates the worker and discards that run's partial package.

These bounds constrain retained text/evidence and streamed payloads. Archive-library metadata overhead and browser file-list overhead still depend on the input; the demo is intended for trusted robot log exports, not as a hardened service for arbitrary hostile archives.

## Local shared analysis demo

Use Python through [uv](https://docs.astral.sh/uv/). From this checkout:

```sh
uv sync
npm run build
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. This serves the built UI and persistent SQLite API together. For frontend development, keep the API running and use `npm run dev -- --port 5173 --strictPort`; Vite proxies `/api`. Preprocess logs first, add an optional scene description and image/PDF attachments, select the report language, then explicitly submit. Image/PDF-only jobs are allowed. The queue works without a model, but such jobs remain queued; no analysis is fabricated.

All visitors see job descriptions, activity summaries, finished reports and immutable review versions. Anyone can request a hard stop. A running job remains **stopping** until the worker confirms sandbox termination or its lease expires; the API cannot itself kill a disconnected remote VM. The worker also configures a maximum 30-minute sandbox lifetime. Reviews require a name, success/failure, an explanation and edited debugging procedure. Claims expire after 30 minutes; saving releases the claim and creates a new version. Names are not verified identities. Version-list and append endpoints provide the extension point for future version management; existing versions are not overwritten.

Data stays in ignored `data/` until the operator removes it. Keep the database and upload folder together in backups. No automatic public URL, TLS, cleanup, user accounts or production abuse protection is configured. This is a local integration demo, **not a hardened internet deployment**. Public reports may contain sensitive excerpts; best-effort credential redaction is not a privacy guarantee. Do not publish confidential logs/wiki content without permission.

### Configure the sandbox worker

Copy `.env.example` to an ignored `.env`, replace the worker secret, and supply your Cube API URL/key/template and model key. Restart the API using `uv run --env-file .env uvicorn backend.app:app --host 127.0.0.1 --port 8000`, then in another terminal:

```sh
uv sync --extra worker
uv run --env-file .env python -m backend.worker
```

`sandbox/Dockerfile` is a candidate Cube template based on the upstream envd image, with pinned Codex and Poppler for PDF text/page inspection. It has **not** been built, registered or boot-tested here. Follow [CubeSandbox's deployment/template instructions](https://github.com/TencentCloud/CubeSandbox) on a suitable host; do not treat an ordinary Docker container as a verified Cube template. This checkout does not install privileged Cube services or change storage. Missing Cube configuration fails closed: Codex is never launched unrestricted on the host.

Each job receives its own Cube VM. Native Codex subagents share that VM, with full permissions inside it, not separate VMs per child. The main prompt and two bounded roles live in `sandbox/runtime/`; raw logs and wiki pages are data, not instructions. The worker streams and hashes original uploads and supplies bounded local evidence tools instead of putting whole archives/wiki into the prompt. The complete local wiki is in ignored `knowledge/wiki/`: 564 Markdown pages were indexed, including pages absent from its navigation index. Raw evidence and page context remain retrievable by source/line. Activity exposes coarse lifecycle events, not private reasoning or raw shell output.

The model key enters the sandbox and is accessible to a full-permission agent. Use a dedicated, restricted-budget provider key and limit sandbox network access externally. Provider replacement uses `ROBOT_CODEX_PROVIDER_URL`, `ROBOT_CODEX_API_KEY_ENV` and `ROBOT_CODEX_MODEL`; the endpoint must be compatible with Codex's Responses protocol. Arbitrary providers are not automatically supported.

Defaults admit at most two concurrent jobs, with 20 pending/uploading jobs and a USD 5 reservation per job against a **USD 10 Asia/Shanghai-day admission cap**. Active reservations count across midnight. Unknown actual cost is conservatively settled at the reservation. This is **not verified billing or a hard USD 10 final-charge ceiling**: in-flight jobs finish and can exceed estimates. No paid benchmark has run; the requested Luna pilot (10 minutes, below USD 5) requires a separately enforced provider spending guard before execution. CPU/RAM/disk allocation is selected in the Cube template; current metrics are not a benchmark or a sizing recommendation.

```sh
uv run python -m unittest discover -s tests_backend -v
npm test
npm run build
```

See [PROGRESS.md](PROGRESS.md) for implemented/tested versus externally blocked work.

## DeepSeek / Qwen and estimated cost

`.env.example` now selects DeepSeek through the existing Codex Responses runner. No runtime replacement is required for DeepSeek; Qwen through OpenCode is a documented alternative, not yet wired in. See [provider setup and limitations](docs/model-providers.md).

Run the offline estimator with `uv run python scripts/estimate_cost.py --model deepseek-v4-flash` or `--model qwen3-coder-flash-cn`. It accounts for agents, repeated requests, growing context, cached input, reasoning output and optional sandbox charges. It is a planning estimate, not a measured bill or enforced spend cap.

Library selection and format boundaries are recorded in [docs/open-source-options.md](docs/open-source-options.md). The small custom parser is limited to robot-specific normalization, source provenance, and evidence selection; archive and compression formats use existing implementations.

## Verification (2026-09-09)

Production build and 41 automated tests pass, covering format recognition, archive/source fairness, lifecycle/metadata retention, numeric identity, UTF-8/long-line preservation, reconciled omissions, brief byte budgets, context pagination, duplicate archive paths, corrupt archives, and resource caps.

Browser checks used the local samples without copying raw inputs into the application:

| Input | Browser result |
| --- | --- |
| September XOS + sys/app/kernel TAR.GZ (4 archives), v2 | 212 reports; 231,525 lines; 177 processed, 28 partial, 7 skipped. All four archives represented in the 500-event package and 58-event brief. Full package: 1,118,242 bytes; brief: 15,996 bytes (~98.6% smaller in bytes, not a measured token saving). The ~1.2 GB MCAP entry is inventoried and skipped, not decoded. |
| Walker S2 ZIP, v2 | 474 reports; 681,548 lines; 421 processed, 53 partial. Recognized 3 fatal, 14,601 error, and 43,665 warning records, including readable damaged lines previously omitted. Brief: 15,934 bytes; approximately 8.6 seconds. These counts are log levels, not confirmed robot diagnoses. |
| August ROS2 bag TAR.GZ, previous baseline | Five SQLite recordings explicitly skipped; trailing gzip garbage surfaced as an archive error. |

The v2 browser check downloaded/parsed both exports, retrieved a 20-line context page and previously unselected extraction metadata, and checked no horizontal page overflow at 390 pixels. Observed preprocessing requests were only local application assets; no log/model upload request occurred. Four-archive v2 runs took roughly 3.6–4.7 seconds on this machine, not a cross-machine performance guarantee. The extra readable lines/configuration and wider context intentionally make the local full package larger than v1; only the compact brief is intended as the small initial handoff.
