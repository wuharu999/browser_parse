# Project structure

Robot Log Workbench presents one integrated shared analysis UI. Browser preprocessing runs automatically during submission, while FastAPI owns persistent history, uploads, status, hard stops, reports, and versioned human reviews. It reuses open-source archive readers and isolated Docker job containers. Real model execution requires a Docker-capable worker host and its restricted egress proxy; agents never execute directly on the host.

## Source tree

```text
browser_parse/
├── index.html                 # Browser entry point
├── package.json               # Dependencies and development commands
├── package-lock.json          # Reproducible dependency resolutions
├── tsconfig.json              # Strict TypeScript configuration
├── vite.config.ts             # Development API proxy
├── pyproject.toml / uv.lock    # Python API and Docker worker dependencies
├── .env.example               # Model/provider and worker configuration placeholders
├── .gitignore                 # Excludes generated output, logs, and credentials
├── README.md                  # Setup, supported formats, limits, verification
├── PROJECT_STRUCTURE.md       # This architecture and navigation guide
├── PROGRESS.md                # Implementation, validation and external blockers
├── THIRD_PARTY_NOTICES.md     # Direct dependency licenses and attribution
├── docs/
│   ├── deploy-ecs-worker.md   # ECS API service, TLS and two-worker access rules
│   ├── docker-worker.md       # ECS/API, two Docker workers, TLS and operations
│   ├── open-source-options.md # Archive-library decision and format boundaries
│   ├── model-providers.md     # DeepSeek setup, Qwen/OpenCode option and cost assumptions
│   ├── wiki-review.md         # Prior pilot limitations and full-index search probes
│   └── resource-profiles.md   # Automatic sizing, prepared tools and measurement limits
├── deploy/                    # Host-local Docker network/proxy Compose and Squid ACL
├── scripts/
│   ├── check_sandbox_runtime.py # Credential-free real Docker image/runtime check
│   ├── estimate_cost.py       # Dependency-free offline multi-agent cost estimator
│   ├── seed_demo.py           # Idempotent completed synthetic UI examples
│   └── benchmark_resources.py # Bounded, credential-free Linux resource sampling
├── public/examples/           # Downloadable synthetic log for upload testing
├── src/
│   ├── main.ts                # Integrated submission, history, status, reports and reviews
│   ├── report.ts              # Structured robot-analysis/v1 parser and safe fallback
│   ├── budget.ts              # Allowance, queue eligibility and bar calculations
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
│   ├── resources.py           # Deterministic upload-driven CPU/RAM/disk profiles
│   ├── guard.py               # Pre-execution LLM security guard (OWASP injection detection & prompt sanitization)
│   ├── docker_runtime.py      # Local Docker isolation, transfer, limits and cleanup
│   └── worker.py              # Docker dispatch, security guard, cost, cancellation and cleanup
├── sandbox/
│   ├── Dockerfile             # Reusable Docker image; Codex + Poppler + Python analysis venv
│   ├── requirements.in / .lock # Hashed Python analysis dependencies, built with uv
│   ├── check_environment.py   # Offline file-format/OCR/import fixtures
│   ├── run_codex.py           # In-container headless runner, virtualenv auto-sourcing, .db3 pre-scan, and subagent supervisor
│   └── runtime/
│       ├── MAIN_PROMPT.md     # Main analysis prompt with 3-subagent delegation guidance
│       ├── ENVIRONMENT.md     # File-type tools and prebuilt image constraints
│       ├── evidence.py        # Bounded wiki/log retrieval and SQLite FTS index
│       ├── .codex/agents/     # Subagent definitions (log_investigator, telemetry_investigator, evidence_reviewer)
│       └── .agents/skills/    # Sandbox context, robot evidence and PDF-reading skills
├── knowledge/
│   ├── README.md              # Wiki location and indexing guidance
│   └── wiki/                  # Local wiki copy, intentionally Git-ignored
├── tests_backend/             # API, guard pipeline, mock Docker worker, runner, telemetry scanner and evidence checks
│   ├── test_runner.py         # Unit tests for runner, concurrency, virtualenv, evidence compaction, and db3 scan
└── tests/
    ├── sources.test.ts        # TAR/GZIP/ZIP, safety limits and source identities
    ├── lines.test.ts          # UTF-8, CRLF, oversized and damaged lines
    ├── preprocess.test.ts     # Format recognition, selection and coverage counts
    ├── brief.test.ts          # Serialized UTF-8 budget and brief excerpts
    └── context.test.ts        # Replay, byte limits and pagination
```

`node_modules/`, `dist/`, browser-test artifacts, uploaded logs, and downloaded evidence are not source files and are excluded from Git. Tests construct synthetic fixtures rather than committing private robot logs.

Submission follows `main.ts → preprocess.worker.ts → backend/app.py → store.py` (uploads plus persistent queue), then `worker.py (pre-execution security guard) → Docker job container → run_codex.py → Codex/native subagents`. `main.ts` also renders the shared history and active status; `report.ts` validates structured results before rendering their summary, evidence chain, workflow, and uncertainties. Only private worker routes can retrieve originals or finish jobs. Public visitors can read shared results, request a hard stop, and claim/append human review versions.

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

The app is not a fork of an entire log-analysis project. It composes existing libraries with robot-specific preprocessing. The worker uses the local Docker CLI and native Codex subagents. Each worker polls ECS independently; no worker-to-worker connectivity is needed. `fflate` is not installed. MCAP/ROS readers are prepared in the sandbox image, not the browser; browser preprocessing still marks unsupported binary contents as coverage gaps. See [the dependency decision](docs/open-source-options.md) for archive licenses and browser format boundaries.

## Developer commands

```sh
npm ci
uv sync
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000
npm run dev -- --port 5175 --strictPort
npm test
npm run build
```

During development, open port 5175 and keep the port-8000 API running for Vite's `/api` proxy. For the integrated production-style local path, run `npm run build`, start the API, and open port 8000; FastAPI serves `dist/`. `uv run python scripts/seed_demo.py` adds exactly three completed, synthetic examples without AI calls or queue work and is safe to rerun. See `docs/docker-worker.md` for deployment and `PROGRESS.md` for validation status. Byte budgets are not exact token counts, and sampled evidence is not a diagnosis.
