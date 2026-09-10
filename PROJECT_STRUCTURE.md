# Project structure

Robot Log Workbench presents one integrated shared analysis UI. Browser preprocessing runs automatically during submission, while FastAPI owns persistent history, uploads, status, hard stops, reports, and versioned human reviews. It reuses open-source archive readers and CubeSandbox. Real model execution requires a Cube endpoint/template; the local development VM provides one without silently executing agents on the host.

## Source tree

```text
browser_parse/
├── index.html                 # Browser entry point
├── package.json               # Dependencies and development commands
├── package-lock.json          # Reproducible dependency resolutions
├── tsconfig.json              # Strict TypeScript configuration
├── vite.config.ts             # Development API proxy
├── pyproject.toml / uv.lock    # Python API and optional Cube worker dependencies
├── .env.example               # Model/provider and worker configuration placeholders
├── .gitignore                 # Excludes generated output, logs, and credentials
├── README.md                  # Setup, supported formats, limits, verification
├── PROJECT_STRUCTURE.md       # This architecture and navigation guide
├── PROGRESS.md                # Implementation, validation and external blockers
├── THIRD_PARTY_NOTICES.md     # Direct dependency licenses and attribution
├── docs/
│   ├── open-source-options.md # Archive-library decision and format boundaries
│   ├── model-providers.md     # DeepSeek setup, Qwen/OpenCode option and cost assumptions
│   ├── local-sandbox.md       # Disposable local Cube VM and forwarded networking
│   └── resource-profiles.md   # Provisional cloud sizing and measurement boundaries
├── scripts/
│   ├── estimate_cost.py       # Dependency-free offline multi-agent cost estimator
│   ├── seed_demo.py           # Idempotent completed synthetic UI examples
│   ├── cube_local.sh          # Conservative local VM start/status/ssh/stop
│   └── benchmark_resources.py # Bounded, credential-free Linux resource sampling
├── public/examples/           # Downloadable synthetic log for upload testing
├── src/
│   ├── main.ts                # Integrated submission, history, status, reports and reviews
│   ├── report.ts              # Structured robot-analysis/v1 parser and safe fallback
│   ├── i18n.ts                # Persistent English/Chinese interface selection
│   ├── style.css              # Responsive UI styles
│   ├── preprocess.worker.ts   # Worker message dispatch: preprocess or context
│   ├── sources.ts             # Streaming inputs, archive safety, stable source IDs
│   ├── lines.ts               # UTF-8 line streaming, clipping and damage markers
│   ├── preprocess.ts          # Log recognition, coverage, evidence and patterns
│   ├── selection.ts           # Bounded archive/subsystem/file-balanced sampling
│   ├── brief.ts               # Compact, byte-budgeted analysis brief
│   ├── context.ts             # Bounded local source replay and pagination
│   └── types.ts               # Shared schemas, worker messages, default limits
├── backend/
│   ├── app.py                 # Public API, bounded uploads, private worker routes
│   ├── store.py               # SQLite queue, budget reservations, claims and versions
│   └── worker.py              # Cube-only transfers, concurrency, cancel and timeout
├── sandbox/
│   ├── Dockerfile             # Boot-tested reusable Cube image; Codex + Poppler
│   ├── HostVM.Dockerfile       # Unprivileged Docker wrapper for local QEMU tools
│   ├── run_codex.py           # In-VM headless runner and bounded public telemetry
│   └── runtime/
│       ├── MAIN_PROMPT.md     # Main analysis prompt shared across jobs
│       ├── evidence.py        # Bounded wiki/log retrieval and SQLite FTS index
│       ├── .codex/agents/     # Log investigator and evidence reviewer roles
│       └── .agents/skills/    # Robot evidence and PDF-reading instructions
├── knowledge/
│   ├── README.md              # Wiki location and indexing guidance
│   └── wiki/                  # Local wiki copy, intentionally Git-ignored
├── tests_backend/             # API, mocked Cube worker, runner and evidence checks
└── tests/
    ├── sources.test.ts        # TAR/GZIP/ZIP, safety limits and source identities
    ├── lines.test.ts          # UTF-8, CRLF, oversized and damaged lines
    ├── preprocess.test.ts     # Format recognition, selection and coverage counts
    ├── brief.test.ts          # Serialized UTF-8 budget and brief excerpts
    └── context.test.ts        # Replay, byte limits and pagination
```

`node_modules/`, `dist/`, browser-test artifacts, uploaded logs, and downloaded evidence are not source files and are excluded from Git. Tests construct synthetic fixtures rather than committing private robot logs.

Submission follows `main.ts → preprocess.worker.ts → backend/app.py → store.py` (uploads plus persistent queue), then `worker.py → Cube job VM → run_codex.py → Codex/native subagents`. `main.ts` also renders the shared history and active status; `report.ts` validates structured results before rendering their summary, evidence chain, workflow, and uncertainties. Only private worker routes can retrieve originals or finish jobs. Public visitors can read shared results, request a hard stop, and claim/append human review versions.

## Processing paths

```text
Selected local Files
  └─ main.ts → preprocess.worker.ts → sources.ts → lines.ts
       ├─ preprocess.ts + selection.ts → robot-log-evidence/v2
       │    ├─ main.ts → backend upload and queue submission
       │    └─ brief.ts → robot-log-brief/v1 (≤16,000 bytes; developer API)
       └─ context.ts → bounded replay (developer API: ≤20 lines / 12,000 bytes)
```

The simplified UI has no manual preprocessing, export, or context-replay controls. Internally, the browser still retains selected originals while preparing a submission. The tested context API can reopen a relevant input instead of loading all logs at once: ZIP reads the target member, while TAR/GZIP streams through preceding data. Changing the selection or closing the page invalidates that local access. Partial replay does not revalidate whole-archive integrity.

## Open-source and platform responsibilities

| Component | Responsibility |
| --- | --- |
| `it-tar` | Streaming TAR extraction using async iterables |
| `@zip.js/zip.js` | ZIP entry reading/decompression and CRC checking |
| Browser `DecompressionStream` | Gzip decompression; no additional gzip dependency |
| Browser File, Streams, TextDecoder and Worker APIs | Local input, UTF-8 decoding and background execution |
| TypeScript, Vite and Vitest | Type checking, bundling/development server and tests |

The app is not a fork of an entire log-analysis project. It composes existing libraries with robot-specific preprocessing. The optional worker uses the official `cubesandbox` SDK and native Codex subagents, not an additional agent framework. `fflate` and MCAP/ROS decoders are not installed. See [the dependency decision](docs/open-source-options.md) for archive licenses and unsupported formats.

## Developer commands

```sh
npm ci
uv sync
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000
npm run dev -- --port 5175 --strictPort
npm test
npm run build
```

During development, open port 5175 and keep the port-8000 API running for Vite's `/api` proxy. For the integrated production-style local path, run `npm run build`, start the API, and open port 8000; FastAPI serves `dist/`. `uv run python scripts/seed_demo.py` adds exactly three completed, synthetic examples without AI calls or queue work and is safe to rerun. See `docs/local-sandbox.md` for the boot-tested development template and `PROGRESS.md` for actual end-to-end validation status. Byte budgets are not exact token counts, and sampled evidence is not a diagnosis.
