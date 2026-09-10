# Project structure

Robot Log Workbench is a standalone, browser-only TypeScript application. It reuses open-source archive readers; robot-log recognition, bounded sampling, and the UI are project-specific. There is no backend, AI runtime, shell executor, or worker-computer integration.

## Source tree

```text
browser_parse/
├── index.html                 # Browser entry point
├── package.json               # Dependencies and development commands
├── package-lock.json          # Reproducible dependency resolutions
├── tsconfig.json              # Strict TypeScript configuration
├── .gitignore                 # Excludes generated output, logs, and credentials
├── README.md                  # Setup, supported formats, limits, verification
├── PROJECT_STRUCTURE.md       # This architecture and navigation guide
├── THIRD_PARTY_NOTICES.md     # Direct dependency licenses and attribution
├── docs/
│   └── open-source-options.md # Archive-library decision and format boundaries
├── src/
│   ├── main.ts                # File selection, worker lifecycle, results, downloads
│   ├── style.css              # Responsive UI styles
│   ├── preprocess.worker.ts   # Worker message dispatch: preprocess or context
│   ├── sources.ts             # Streaming inputs, archive safety, stable source IDs
│   ├── lines.ts               # UTF-8 line streaming, clipping and damage markers
│   ├── preprocess.ts          # Log recognition, coverage, evidence and patterns
│   ├── selection.ts           # Bounded archive/subsystem/file-balanced sampling
│   ├── brief.ts               # Compact, byte-budgeted analysis brief
│   ├── context.ts             # Bounded local source replay and pagination
│   └── types.ts               # Shared schemas, worker messages, default limits
└── tests/
    ├── sources.test.ts        # TAR/GZIP/ZIP, safety limits and source identities
    ├── lines.test.ts          # UTF-8, CRLF, oversized and damaged lines
    ├── preprocess.test.ts     # Format recognition, selection and coverage counts
    ├── brief.test.ts          # Serialized UTF-8 budget and brief excerpts
    └── context.test.ts        # Replay, byte limits and pagination
```

`node_modules/`, `dist/`, browser-test artifacts, uploaded logs, and downloaded evidence are not source files and are excluded from Git. Tests construct synthetic fixtures rather than committing private robot logs.

## Processing paths

```text
Selected local Files
  └─ main.ts → preprocess.worker.ts → sources.ts → lines.ts
       ├─ preprocess.ts + selection.ts → robot-log-evidence/v2
       │    └─ main.ts → brief.ts → robot-log-brief/v1 (≤16,000 bytes)
       └─ context.ts → local context page (UI: ≤20 lines / 12,000 bytes)
```

The browser retains access to the selected originals during the session. A context request reopens the relevant input instead of sending all logs to a model. ZIP reads the target member; TAR/GZIP streams through preceding data. Changing the selection or closing the page invalidates session access. Partial replay does not revalidate whole-archive integrity.

## Open-source and platform responsibilities

| Component | Responsibility |
| --- | --- |
| `it-tar` | Streaming TAR extraction using async iterables |
| `@zip.js/zip.js` | ZIP entry reading/decompression and CRC checking |
| Browser `DecompressionStream` | Gzip decompression; no additional gzip dependency |
| Browser File, Streams, TextDecoder and Worker APIs | Local input, UTF-8 decoding and background execution |
| TypeScript, Vite and Vitest | Type checking, bundling/development server and tests |

The app is not a fork of an entire log-analysis project. It composes these existing libraries with robot-specific preprocessing. `fflate`, MCAP/ROS decoders, model SDKs, and multi-agent frameworks are not installed. See [the dependency decision](docs/open-source-options.md) for licenses and unsupported formats.

## Developer commands

```sh
npm ci
npm run dev -- --port 5173 --strictPort
npm test
npm run build
```

Build output goes to `dist/`. No deployment or log upload happens automatically. Byte budgets are not exact token counts, and sampled evidence is not a diagnosis.
