# Analysis service progress

## ECS/worker runbook and public-publication attempt — 2026-09-10

- Added `docs/deploy-ecs-worker.md`: separate ECS/worker responsibilities,
  HTTPS reverse proxy, secret separation, systemd services, Cube prerequisites,
  template creation, private wiki transfer, resource/admission controls, smoke
  tests, backups, upgrades and failure checks. These are deployment instructions,
  not evidence of an actual ECS installation.
- Updated README/navigation and publication boundaries. Private wiki, uploads,
  credentials, VM data and local editor settings are excluded. Public visibility
  does not automatically grant an open-source license or deploy a website.
- Scanned all 141 historical Git blobs: no supported credential-pattern matches
  or private-data path candidates. This is a bounded publication check, not a
  guarantee that arbitrary sensitive text can never exist. No supplied token
  was written to a file or Git configuration.
- Retested: 70 Python and 48 TypeScript tests, production build and diff checks
  pass. No new model request or cloud resource was created.
- The newly supplied token authenticated as `wuharu999`, but private and public
  repository creation both returned HTTP 403: `Resource not accessible by
  personal access token`, requiring `administration=write`. The existing CLI
  login was also denied creation. No browser session was available as an
  alternative. The requested public `wuharu999/browser_parse` repository and
  push remain blocked; local changes are ready for publication after permission
  correction. Contents write permission is also needed to push.

## Sandbox context, automatic sizing and clearer navigation — 2026-09-10

- Added `robot-analysis-context` **for the agents inside each job sandbox**.
  It is transferred to `/workspace/.agents/skills/`, referenced by the main
  prompt and both native roles, and is not installed in personal Codex skills.
  A bounded `evidence.py context` command supplies upload mappings, allocation
  and wiki index status. Guidance routes to installed tools and original wiki
  lines, checks robot applicability, keeps subagents from recursively spawning,
  and addresses the prior UTC/synthetic-input provenance mistakes.
- Built a prepared image with uv-managed Python 3.12.13, hashed dependencies,
  PDF/OCR (English/Chinese), images, MCAP/ROS readers, HDF5 and data-analysis tools.
  Environment checks passed with Docker networking disabled and in real small
  and standard Cube sandboxes; skill assets, context and wiki indexing were
  available there. Test VMs were destroyed. These checks passed no model key,
  made no paid call and did not change admission accounting.
- Submission now estimates small/standard/large CPU/RAM/disk profiles without
  an LLM, using actual upload sizes and bounded browser hints. Atomic FIFO
  admission reserves all three resources; cancellation retains reservations
  until termination/expiry. Tests cover competing claims and mismatched Cube
  templates. This is pre-run selection, not mid-run resizing or a performance
  guarantee. See `docs/resource-profiles.md` for exact values and limits.
- Registered matching small (1 CPU/2 GiB/8Gi disk) and standard (2 CPU/4 GiB/
  16Gi disk) templates. The existing local outer guest is only 8 GiB, so the
  local job pool is deliberately 4 GiB with one worker slot. Large jobs are
  rejected locally; an 8-GiB large-profile/cloud-concurrency test is outstanding.
  Cube's decimal `G` and binary `Gi` differ; final templates use `Gi`.
- Added report-local Back to upload controls at top/bottom, preserving draft
  description/selection. Shared EN/中文 allowance bar shows accounted estimates,
  reservations, remaining allowance, reset date and pending slots. Capacity is
  labelled as reserved, not live utilization. Existing sidebar navigation also
  worked; the new report controls make the return path explicit.
- Browser checks passed report return/draft preservation, Chinese 390px layout
  without overflow, and UI-only mocked allowance states (available, exhausted,
  full queue, occupied capacity, unavailable API). Mocks did not submit jobs or
  alter the real budget. The real day's USD 10 allowance remains exhausted.
- Read-only full-wiki index: 564 pages, zero skipped. Six literal searches found
  canonical robot/component pages but English/Chinese and model-applicability
  gaps. The prior AI pilot used only a synthetic one-page wiki, so it does not
  establish full-wiki analysis quality. See `docs/wiki-review.md`.
- Verification: **70 Python + 48 TypeScript = 118 tests pass**, production build
  and diff checks pass; skill validator passes inside the prepared image. The
  updated skill has not yet been evaluated in a new paid diagnosis. API/UI and
  worker were restarted locally; original reports, wiki, credentials and job
  database were preserved. No GitHub push or public cloud deployment claimed.

## Local Cube and exact DeepSeek vision ID — 2026-09-10

- Configured exactly `deepseek-v4-flash-vision-exp` in the ignored, mode-0600 local environment and example configuration. DeepSeek's direct image test accepted this ID and read the screenshot heading correctly (214 input + 6 output tokens); its response reported serving model `deepseek-flash`, consistent with the documented retired-alias routing. The application does not substitute the requested ID.
- Installed CubeSandbox v0.7.0 inside its official OpenCloudOS development VM, using nested KVM in an unprivileged Docker/QEMU wrapper. Health checks passed. No Ubuntu host filesystem, DNS or privileged Cube service was reconfigured. Upstream source revision: `adbb358dc7c184fb7ef979739e0962debc885c62`.
- Built the reusable Codex 0.153.4 / Python 3.10.12 / Poppler 22.02 / ripgrep image. Refreshed local template `tpl-000995539963488d9fbd5ca8` is READY with 2000m CPU, 4096 MiB RAM and 12G writable layer. A real SDK smoke test passed file transfer, command execution and sandbox destruction. The local-only registry and VM manager are documented in `docs/local-sandbox.md`.
- First browser-submitted TAR.GZ + screenshot pilot reached a real sandbox, then failed before model execution because Cube rejects creating an existing `/workspace`. Added a realistic regression and SDK-aware directory handling. The failed job is retained in history; its USD 5 settlement is a conservative unknown-cost reservation, not a provider charge measurement.
- Added forwarded proxy-port support without host DNS changes, resource sampling, provisional 8-vCPU/16-GiB cloud profiles, and Vitest discovery restricted to this project's tests (not the ignored Cube source tree).
- Fixed the custom model catalog's required baseline instructions after a no-paid Codex startup test caught the missing field; rebuilt and registered the image. Startup then emitted real thread/turn events against a deliberately unreachable local dummy provider.
- **Real UI → preprocessing → upload → queue → Cube → Codex/DeepSeek → report completed**, job `d864a5e8797a6da75ad753fa847fd412`, in **93.287 seconds** (runner 88.286 seconds). Inputs were a seven-line synthetic TAR.GZ and a UI PNG (~126 KB combined), with a tiny synthetic wiki fixture, not the full wiki. The browser displayed summary, nine evidence items, uncertainties, eight workflow steps, review entry and persisted session activity without horizontal overflow.
- **Two actual native child sessions verified** from safe session metadata: `01a08a1d-1d07-7621-9495-ab0e574f3bc4` and `01a08a1d-24d5-7cb1-8579-7b822ca7190c`, both marked `source.subagent`, alongside the parent session. Private reasoning/transcripts were not exported. They shared job VM `331f53479de542f497a13b9322545de6`; Cube reported zero live sandboxes after completion. Automatic UI child-event attribution is still incomplete; this proof came from a separate metadata-only diagnostic.
- The run exposed a Python 3.10 incompatibility (`hashlib.file_digest`, added in 3.11). The model used a bounded direct archive-reading fallback and completed. The helper now uses streaming SHA-256 with regression coverage for the image's Python version.
- Model report is preserved verbatim, **not human-verified**: it correctly avoids inventing a physical cause, but incorrectly says timezone is missing despite `Z` timestamps, and repeats the fixture's “no model output” wording even though this report was model-generated. The ISO-prefixed fixture's severity metadata was unknown and the agent flagged it. These remain quality/recognition follow-ups, not evidence of a solved real incident.
- Cost remains **unknown** across all child requests; reported parent/session counters were 129115 input, 112512 cached input, 7435 output and 2016 reasoning-output tokens. Do not add reasoning to output without checking provider semantics. The two pilot admissions consumed the day's USD 10 conservative reservation allowance (including the pre-execution failure); this is not a measured USD 10 bill and the cap was not bypassed.
- A representative PDF workload, full-wiki transfer benchmark, two-job cloud load test, VPN-free connectivity and public production deployment remain unverified. See `docs/resource-profiles.md` for measured tiny-case versus provisional sizing.
- Final verification: **56 Python + 43 TypeScript = 99 passing tests**, production build/typecheck and diff checks pass. Evidence tests also pass offline in the actual image's Python 3.10.12. API/UI remains at `http://127.0.0.1:8000`; the single worker is running with the normal wiki path restored. Today's reservation allowance is exhausted, so further submitted jobs queue until the next Asia/Shanghai day rather than bypassing the cap.

## Simplified frontend and session view — 2026-09-10

- Replaced the separate preprocessing/dashboard controls with one upload-files/folder + incident-description form. Browser preprocessing is automatic and token-free; no intermediate exports/settings are exposed. Existing streaming archive libraries and preprocessing modules remain intact.
- Added left-hand shared history, all-active job visibility independent of history pagination, EN/中文 UI, structured summary/evidence chain/uncertainties, and editable resolution workflows with named, immutable review versions.
- Added a scrollable session view for public progress/output and safe tool/subagent status. It polls every three seconds, keeps a reader's scroll position, pages earlier persisted events in batches of 200, and retains session history on completed jobs. Hidden reasoning, commands, and raw tool outputs are excluded. Native child visibility is limited to emitted Codex events; a complete independent transcript for every child is not proven.
- Runner activity is bounded to 800 characters per message and a 64 KiB complete-record JSONL ring. Final reports use Codex's verified `--output-last-message` file independently, preserving structured output instead of using a truncated activity excerpt. Structured report redaction preserves JSON and source references.
- Added three clearly marked, completed synthetic examples and an uploadable synthetic log. Seeding is idempotent and never enters the worker queue. The normal local database contains the three examples; no AI was used to produce them.
- Verification: **43 TypeScript tests + 47 Python tests = 90 passing**, production build and diff checks pass. Browser checks covered compressed-log automatic handoff (`robot-log-evidence/v2` plus original upload), report sections, independent language controls, two-visitor lock conflict, version persistence, claim restoration/release, navigation during claim acquisition, 200-event pagination, scroll preservation and public stop/worker acknowledgement.
- Browser mutation tests used a separate SQLite/upload directory at `/tmp/robot-ui-redesign.9XevUG`. No paid model, real Cube VM, robot repair, public deployment, or GitHub push is claimed by these tests.

## Agreed scope

- Local API and worker now; separate server/worker machines later.
- Existing browser archive preprocessing retained; original archives, PDF/image attachments, description and output language become one analysis job.
- Shared read-only visibility of all jobs, sanitized activity, reports and review versions; queued admission when capacity is occupied.
- Named anonymous reviewer claims an edit, saves success/failure plus a required explanation and final debugging procedure, creating a new immutable version. No account system or version-management UI.
- Daily USD 10 admission budget; already-running work finishes. This cannot guarantee a USD 10 final bill when in-flight work costs more than reserved.
- Full command permissions only inside a CubeSandbox job VM. No unrestricted host Codex fallback. Native Codex subagents share that job VM.
- Any visitor may hard-stop any analysis job; a job also hard-stops after 30 minutes. Stopping destroys the job sandbox and child processes, not unrelated host processes.
- Original benchmark request was Luna, at most 10 minutes and below USD 5; the user subsequently selected DeepSeek and explicitly reconfirmed `deepseek-v4-flash-vision-exp`. Local pilot timeout is 600 seconds with one worker; provider-enforced spending limits remain separate from the application's reservation accounting.

## Checklist

- [x] Browser preprocessing: 41 tests, build and archive browser checks passed before backend work.
- [x] Repository cleanup and project structure committed as `feaee9f`.
- [x] Read-only CubeSandbox, Codex and wiki feasibility checks.
- [x] Persistent job/upload/queue/budget API and review versioning.
- [x] Shared dashboard, attachments, description and persistent 中文 / English toggle; independent report language.
- [x] CubeSandbox worker, cancellation and bounded process telemetry implemented and mock-tested, not VM-validated.
- [x] Runtime roles, PDF/evidence skills and indexed wiki tools. Local wiki copy is ignored by Git.
- [x] Local API/UI integration and focused input/concurrency checks; not a production penetration test.
- [x] Real local CubeSandbox template boot, file transfer, tool execution and destruction.
- [x] Completed Codex/DeepSeek synthetic log + image pilot and metadata-confirmed native subagents.
- [ ] Representative large-log/PDF/full-wiki resource and concurrency benchmark.
- [ ] Public GitHub repository and push (latest user request supersedes private).

## Current external blockers

CubeSandbox is now installed in the disposable local VM and its template has booted successfully; see the latest section above. Production cloud deployment still requires nested KVM support and an operationally secured worker. No host storage has been reformatted or reconfigured.

GitHub repository creation still fails with HTTP 403 on the latest attempt;
see the public-publication section above. Commits remain local; no successful
push or visibility change is claimed. Never put credentials in this file.

## Resource estimates versus measurements

Memory, CPU, writable disk and model pricing will be configurable. Proposed defaults are not benchmark results. Record actual sandbox/template IDs, image size, peak memory, elapsed time, tokens, estimated cost and observed child agents only after a real run.

Current telemetry labels parent-Codex RSS explicitly; it does not measure the whole VM or all child processes. Actual aggregate model cost and child usage remain unknown. A strict sub-USD-5 paid pilot needs a provider-enforced spending guard; no paid run is authorized by passing mock tests alone.

## DeepSeek / Qwen follow-up

- Added an offline stdlib cost estimator with dated DeepSeek/Qwen pricing, per-agent/request accounting, cache, context growth, optional infrastructure and a planning buffer. No paid request is made by this tool.
- DeepSeek officially supports Codex Responses. Added sandbox-local model metadata and example DeepSeek credentials configuration while preserving the existing runtime and native roles. Text-only versus experimental vision modalities are explicit.
- Qwen through OpenCode is documented as the alternative; it has not been installed or connected to the worker. Do not claim a live provider/subagent proof without credentials and Cube.
- Current Python suite: 30 passing tests. Frontend unchanged from the 41-test baseline.
- Retried GitHub after the reported permission update: authenticated owner is `wuharu999`; GET target returned 404 and POST `/user/repos` still returned 403 with required permission `administration=write`. Repository creation and push remain blocked; no credential was saved.

## Local verification — 2026-09-10

- 41 browser-preprocessing unit tests and production build pass; npm audit reports zero vulnerabilities.
- 26 Python tests pass: bounded chunked JSON, ordered/hash-checked uploads, concurrent admission, cancellation reservations, midnight accounting, claim exclusion, immutable reviews, mocked sandbox cleanup, large wiki images, bounded evidence and runner helpers.
- Browser: synthetic TAR.GZ → local preprocessing → explicit shared upload → queued job verified. A private test-worker API call supplied a clearly labeled synthetic report, not a model result. Report rendering, reviewer claim conflict (HTTP 409), review save/release, version/reviewer persistence after reload, and public queued-job cancellation checked.
- UI language persists on reload; explicitly selected report language survives interface toggles. Chinese mobile layout at 390 pixels has no horizontal overflow.
- Wiki index scans all 564 Markdown pages, not just navigation links; zero pages skipped. Canonical Walker C1 page is retrieved. Wiki originals remain unchanged and the local copy is excluded from Git.
- Cube SDK import/signatures inspected against locked `cubesandbox==0.7.0`; no Cube VM, real Codex fan-out, PDF/image model interpretation, aggregate resource benchmark or public deployment is claimed.
- SQLite/upload test state is isolated under `/tmp/robot-workbench-e2e.dBgDWJ`, not committed or mixed into the normal demo database.
