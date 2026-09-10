# Analysis service progress

## Agreed scope

- Local API and worker now; separate server/worker machines later.
- Existing browser archive preprocessing retained; original archives, PDF/image attachments, description and output language become one analysis job.
- Shared read-only visibility of all jobs, sanitized activity, reports and review versions; queued admission when capacity is occupied.
- Named anonymous reviewer claims an edit, saves success/failure plus a required explanation and final debugging procedure, creating a new immutable version. No account system or version-management UI.
- Daily USD 10 admission budget; already-running work finishes. This cannot guarantee a USD 10 final bill when in-flight work costs more than reserved.
- Full command permissions only inside a CubeSandbox job VM. No unrestricted host Codex fallback. Native Codex subagents share that job VM.
- Any visitor may hard-stop any analysis job; a job also hard-stops after 30 minutes. Stopping destroys the job sandbox and child processes, not unrelated host processes.
- First paid benchmark: Luna only, at most 10 minutes and below USD 5. No paid run has been performed yet.

## Checklist

- [x] Browser preprocessing: 41 tests, build and archive browser checks passed before backend work.
- [x] Repository cleanup and project structure committed as `feaee9f`.
- [x] Read-only CubeSandbox, Codex and wiki feasibility checks.
- [x] Persistent job/upload/queue/budget API and review versioning.
- [x] Shared dashboard, attachments, description and persistent 中文 / English toggle; independent report language.
- [x] CubeSandbox worker, cancellation and bounded process telemetry implemented and mock-tested, not VM-validated.
- [x] Runtime roles, PDF/evidence skills and indexed wiki tools. Local wiki copy is ignored by Git.
- [x] Local API/UI integration and focused input/concurrency checks; not a production penetration test.
- [ ] Real CubeSandbox boot, Codex native-subagent proof and Luna resource benchmark.
- [ ] Private GitHub push.

## Current external blockers

CubeSandbox is not installed. Real sandbox validation needs a configured API endpoint and template; installing its privileged host services/storage is not part of ordinary local app setup. No host storage has been reformatted or reconfigured.

GitHub repository creation failed with HTTP 403 using both available credentials. The cleanup commit is local; no successful push is claimed. Never put credentials in this file.

## Resource estimates versus measurements

Memory, CPU, writable disk and model pricing will be configurable. Proposed defaults are not benchmark results. Record actual sandbox/template IDs, image size, peak memory, elapsed time, tokens, estimated cost and observed child agents only after a real run.

Current telemetry labels parent-Codex RSS explicitly; it does not measure the whole VM or all child processes. Actual aggregate model cost and child usage remain unknown. A strict sub-USD-5 paid pilot needs a provider-enforced spending guard; no paid run is authorized by passing mock tests alone.

## Local verification — 2026-09-10

- 41 browser-preprocessing unit tests and production build pass; npm audit reports zero vulnerabilities.
- 26 Python tests pass: bounded chunked JSON, ordered/hash-checked uploads, concurrent admission, cancellation reservations, midnight accounting, claim exclusion, immutable reviews, mocked sandbox cleanup, large wiki images, bounded evidence and runner helpers.
- Browser: synthetic TAR.GZ → local preprocessing → explicit shared upload → queued job verified. A private test-worker API call supplied a clearly labeled synthetic report, not a model result. Report rendering, reviewer claim conflict (HTTP 409), review save/release, version/reviewer persistence after reload, and public queued-job cancellation checked.
- UI language persists on reload; explicitly selected report language survives interface toggles. Chinese mobile layout at 390 pixels has no horizontal overflow.
- Wiki index scans all 564 Markdown pages, not just navigation links; zero pages skipped. Canonical Walker C1 page is retrieved. Wiki originals remain unchanged and the local copy is excluded from Git.
- Cube SDK import/signatures inspected against locked `cubesandbox==0.7.0`; no Cube VM, real Codex fan-out, PDF/image model interpretation, aggregate resource benchmark or public deployment is claimed.
- SQLite/upload test state is isolated under `/tmp/robot-workbench-e2e.dBgDWJ`, not committed or mixed into the normal demo database.
