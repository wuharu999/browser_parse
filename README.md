# Robot Log Workbench

A standalone browser tool for preprocessing compressed robot logs. Select archives or a folder, click **Preprocess logs**, then **Download analysis brief**. Reading, decompression, parsing, and context retrieval happen locally in Web Workers. **No Codex, API key, model calls, or LLM tokens are required.** Worker-agent integration is not part of this step.

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

## Next worker stage

Worker transport and agent diagnosis are deliberately outside this demo. A future worker endpoint should validate `schemaVersion`, inspect coverage/omission fields before reasoning, and treat log text as untrusted data rather than agent instructions. The JSON export is the current handoff; there is no claim that a worker has received or analyzed it.

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
