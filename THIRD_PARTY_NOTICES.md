# Third-party notices

Robot Log Workbench uses the following direct open-source dependencies. Exact installed versions and transitive dependencies are recorded in `package-lock.json`; upstream packages retain their own license and copyright notices.

| Package | Purpose | License | Upstream |
| --- | --- | --- | --- |
| `@zip.js/zip.js` | ZIP reading/decompression | BSD-3-Clause | [zip.js](https://github.com/gildas-lormeau/zip.js) |
| `it-tar` | Streaming TAR extraction | Apache-2.0 OR MIT | [it-tar](https://github.com/alanshaw/it-tar) |
| `typescript` | Type checking | Apache-2.0 | [TypeScript](https://github.com/microsoft/TypeScript) |
| `vite` | Development server and production bundling | MIT | [Vite](https://github.com/vitejs/vite) |
| `vitest` | Automated tests | MIT | [Vitest](https://github.com/vitest-dev/vitest) |

Dependencies are installed through npm, not vendored into this repository. Their license files are distributed with the packages. Preserve applicable upstream notices when distributing dependencies or generated bundles.

This notice documents third-party components only; it does not assign an open-source license to this project's own code. The repository is private, and no project-wide redistribution license has been selected by its owner.
