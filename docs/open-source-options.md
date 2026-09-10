# Open-source dependency decision

The current implementation composes two open-source archive libraries with native browser APIs. It does not fork an entire log-analysis application or implement compression formats from scratch.

## Implemented stack

| Input / task | Implementation | Boundary |
| --- | --- | --- |
| TAR | [`it-tar`](https://github.com/alanshaw/it-tar) | Sequential async-iterable extraction; regular text/configuration entries only |
| TAR.GZ / TGZ | Browser `DecompressionStream('gzip')` followed by `it-tar` | Gzip-capable browser required; no JavaScript fallback installed |
| ZIP | [`@zip.js/zip.js`](https://gildas-lormeau.github.io/zip.js/) | Stream accepted entries with CRC checking; encrypted entries unsupported |
| Plain / gzip text | Browser File, Streams, TextDecoder and gzip APIs | Incremental UTF-8 decoding with bounded head/tail line excerpts |
| Background processing | Browser module Web Workers | Fixed application code, not AI agents or uploaded code execution |
| Build / tests | TypeScript, Vite, Vitest | Development tooling; not model or server dependencies |

The production imports are in [`src/sources.ts`](../src/sources.ts). Direct dependency ranges are in [`package.json`](../package.json); [`package-lock.json`](../package-lock.json) records exact resolutions.

## Why these libraries remain

- `it-tar` handles TAR headers and body iteration. Replacing it with a custom TAR parser would duplicate format logic and increase maintenance risk.
- `zip.js` provides ZIP entry streams and integrity checking. Its API is used directly, including in archive tests.
- Gzip already has a browser-native implementation, so a separate `fflate` dependency is unnecessary for the supported browsers.
- Robot-specific timestamp/severity recognition, evidence selection, source identifiers, and UI behavior remain project-owned modules. These are not generic compression replacements.

## Streaming and safety contract

The application does not extract uploaded archives to disk or load every file into one buffer. TAR bodies are consumed sequentially; skipped bodies must still be drained. ZIP skips unsupported payloads without expanding them. Source identities include archive-member ordinals so duplicate names remain distinguishable.

Limits cover entry counts, expanded bytes, retained line characters, evidence, patterns, brief bytes, and context-response bytes. Unsafe paths and links are not followed. Damaged/incomplete input and skipped binary data remain visible in coverage reports. Context replay may stop early and therefore does not revalidate a whole archive's integrity. This is a local tool for trusted robot-log exports, not a hardened service for arbitrary hostile archives.

## Evaluated but not installed

`fflate`, MCAP readers, and ROS bag/SQLite adapters were considered during initial research but are not dependencies or hidden fallback paths. `.mcap`, ROS1 `.bag`, ROS2 `.db3`, nested archives, and unknown formats are inventoried as unsupported; parsing their containers/payload schemas requires a separate, explicitly tested adapter.

No model SDK, agent framework, backend service, or remote worker transport is installed. The evidence/brief exports are the current handoff.

## Attribution and further detail

`it-tar` is dual-licensed Apache-2.0 OR MIT; `zip.js` is BSD-3-Clause. Development-tool licenses and upstream references are recorded in [third-party notices](../THIRD_PARTY_NOTICES.md). Native APIs are documented by MDN: [Compression Streams](https://developer.mozilla.org/en-US/docs/Web/API/Compression_Streams_API), [Streams](https://developer.mozilla.org/en-US/docs/Web/API/Streams_API), and [Web Workers](https://developer.mozilla.org/en-US/docs/Web/API/Web_Workers_API/Using_web_workers).

See [README.md](../README.md) for supported inputs, limits, operation, and verification; [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md) maps the source modules.
