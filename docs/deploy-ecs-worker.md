# Deploy on ECS and a separate worker computer

This guide deploys the existing application, not a replacement service. Commands
are an operator runbook: they have **not** been executed against an ECS instance.
Local validation covers CubeSandbox 0.7.0, small/standard templates and a tiny
DeepSeek pilot. Large jobs, cloud concurrency and internet abuse resistance are
not production-certified.

## 1. Topology and prerequisites

```text
Visitors ── HTTPS ── ECS: Nginx → FastAPI + built UI + SQLite/uploads
                             ↑
                   outbound HTTPS polling
                             │
Worker computer: Python worker → private Cube API/proxy → per-job VM
                                                       ├─ main Codex
                                                       └─ two bounded subagents
```

The worker initiates requests to ECS; ECS does not SSH into the worker. Visitors
need only the public website, not access to the worker IP. A public GitHub repo
does not automatically create this website.

| Machine | Install / store | Planning allocation |
| --- | --- | --- |
| ECS web server | Python via uv, Node 22 for frontend build, Nginx/TLS, app DB and uploads | Start with 2 CPU/4 GiB; size persistent disk for retained uploads |
| Worker | Python via uv, CubeSandbox, prepared images, private wiki, provider key | Requested 8 CPU/16 GiB **system RAM**; no GPU needed for API inference |

Worker storage must include image/template caches, writable layers, temporary
transfers and wiki, not just compressed upload size. Reserve at least 200 GB for
a multi-template trial and monitor free space; this is planning headroom, not a
measured upper bound. Uploads persist indefinitely unless an operator manages
retention. The current application accepts up to 2 GiB per file / 4 GiB per job.

Use a dedicated x86-64 Linux worker with usable `/dev/kvm`, Docker and XFS reflink
storage for Cube. On a cloud worker, verify the **exact instance** exposes KVM;
8 CPU/16 GiB alone does not establish nested virtualization support. The ECS
web server does not need KVM. Cube's alternative PVM kernel path is a separate
operator decision involving kernel/boot changes, not an automatic fallback here.

Before installing Cube, read its [bare-metal guide](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/bare-metal-deploy.md),
[XFS storage explanation](https://github.com/TencentCloud/CubeSandbox/issues/311)
and [network hardening guide](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/network-hardening.md).
The installer changes host services/storage configuration. Use a dedicated
worker or isolated development guest; do not run it on the ECS web server or
reformat an existing data disk. Alibaba documents nested virtualization for
[ECS Bare Metal Instances](https://www.alibabacloud.com/help/en/ecs/user-guide/elastic-bare-metal-server-overview);
verify availability before purchasing a worker instance.

## 2. Network and credential boundaries

- ECS security group: allow HTTPS 443 publicly, HTTP 80 for redirect/certificate
  validation, SSH 22 only from administrator addresses. Do not expose port 8000.
- Worker: no public Cube API, proxy, dashboard, registry, database or management
  ports. Restrict them with cloud security groups **and** host/Docker firewall
  rules. The worker process reaches Cube on loopback/private networking.
- Restrict ECS `/api/worker/` to the worker's fixed outbound IP or routed private
  address, and require the existing `ROBOT_WORKER_TOKEN` bearer authentication.
  No VPN is required when using authenticated HTTPS with a fixed outbound IP.
  Update the allowlist if that IP changes; never disable authentication to fix it.
- The same random worker token belongs on ECS and the worker. Cube credentials
  and the model key belong only on the worker. GitHub tokens belong on neither.
- A full-permission agent can read the model key injected into its job VM. Use a
  dedicated provider key with an enforced spending limit and appropriate egress
  restrictions. Do not inject cloud administrator or GitHub credentials.
- Sandbox network policy should deny cloud metadata, the ECS worker API and
  unrelated private services while permitting the selected provider. Enforce
  this through Cube/firewall controls, not only agent instructions.

The public app intentionally has no user accounts: everyone can see incident
descriptions, reports and review versions, and everyone can request a hard stop.
Names on reviews are unverified. Only authorized-to-share evidence should enter
a public instance. Add edge rate limits/quotas and review the threat model before
accepting arbitrary internet uploads; the daily allowance is not abuse protection.

## 3. Install the application on both machines

Example paths below use a dedicated `robotworkbench` user, with the checkout at
`/opt/robot-workbench/app`. Provision that user and a writable parent directory
using your OS tools. Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/)
and, on the frontend build machine, Node.js 22.12+ in the 22.x line. Run checkout
and dependency commands as the application user, not root:

```sh
git clone https://github.com/wuharu999/browser_parse.git /opt/robot-workbench/app
cd /opt/robot-workbench/app
uv python install 3.12
uv sync --frozen --python 3.12
```

On ECS (or a trusted build machine, then transfer the resulting `dist/`):

```sh
npm ci
npm test
npm run build
uv run --frozen python -m unittest discover -s tests_backend
```

On the worker:

```sh
uv sync --frozen --extra worker --python 3.12
```

Node/npm is needed to **build** the frontend and the prepared Codex image, not
to serve the built website or run the host Python worker. Codex runs inside the
image, never unrestricted on either host. Runtime analysis uses preinstalled
packages; initial builds still require registry/npm/Python repository access.
GitHub connectivity alone does not prove those endpoints or the model provider
are reachable. Build elsewhere and transfer the image/artifacts if necessary.

## 4. ECS API configuration and service

Create `/etc/robot-workbench/api.env`, owned/readable only by `robotworkbench`
(mode `0600`). Use an editor, not secrets in shell command arguments. Generate
one strong random worker token in a password manager and copy it securely to
the worker's config. Example **placeholders**, not usable credentials:

```dotenv
ROBOT_WORKER_TOKEN=REPLACE_WITH_THE_SHARED_RANDOM_WORKER_TOKEN
JOB_DB=/var/lib/robot-workbench/jobs.sqlite3
JOB_UPLOAD_DIR=/var/lib/robot-workbench/uploads
JOB_DIST=/opt/robot-workbench/app/dist
JOB_DAILY_LIMIT_USD=10
JOB_ESTIMATE_USD=5
JOB_MAX_RUNNING=2
JOB_MAX_PENDING=20
JOB_MAX_RUNTIME_SECONDS=1800
JOB_CLAIM_TTL_SECONDS=1800
JOB_POOL_CPU_MILLI=4000
JOB_POOL_MEMORY_MB=8192
JOB_POOL_DISK_MB=32768
JOB_ALLOWED_ORIGINS=https://logs.example.com
```

Replace the domain with your own. For the smaller local development guest, use
`JOB_POOL_MEMORY_MB=4096`; it cannot fit the large profile. The pool must describe
the worker's actual available job resources, **not** the ECS web server's RAM.

Create `/etc/systemd/system/robot-api.service`:

```ini
[Unit]
Description=Robot Log Workbench API
After=network-online.target
Wants=network-online.target

[Service]
User=robotworkbench
Group=robotworkbench
WorkingDirectory=/opt/robot-workbench/app
EnvironmentFile=/etc/robot-workbench/api.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/robot-workbench/app/.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5
StateDirectory=robot-workbench
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Keep **one Uvicorn process**, one local SQLite database and one ECS instance for
this deployment. Do not place the SQLite database on NFS or run independent API
replicas with separate stores; admission/history would no longer be shared.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now robot-api
curl --fail http://127.0.0.1:8000/api/budget
sudo journalctl -u robot-api -n 30 --no-pager
```

`/api/budget` is the application health/config check; there is no application
`/health` endpoint. Do not print the environment file when debugging.

## 5. Publish the ECS website over HTTPS

Point the domain's DNS A record at ECS. Install Nginx and your chosen ACME/TLS
client. Configure the following server block, replacing `logs.example.com` and
the documentation-only worker address `203.0.113.10`. On Ubuntu, this can be
`/etc/nginx/sites-available/robot-workbench`, enabled with the normal sites-enabled
symlink. Do not replace unrelated virtual hosts.

```nginx
server {
    listen 80;
    server_name logs.example.com;
    client_max_body_size 2g;
    client_body_timeout 300s;

    location /api/worker/ {
        allow 203.0.113.10;
        deny all;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

Validate with `sudo nginx -t`, reload Nginx, then obtain a certificate and enable
HTTP→HTTPS redirects with your ACME client (for example, Certbot's Nginx plugin).
Verify HTTPS and renewal before uploading evidence or starting the worker.
Keep both location rules in the TLS server. No Vite dev server should be public.
If another load balancer/CDN fronts Nginx, configure trusted client-IP handling
before relying on the worker IP allowlist; do not trust arbitrary forwarded IPs.

The proxy deliberately streams uploads instead of staging a second full copy;
see the [Nginx proxy documentation](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
Set edge limits conservatively: the UI polls every three seconds, and file
uploads may take minutes. Test your actual WAF/CDN upload caps as well.

## 6. Prepare Cube and analysis templates on the worker

Use CubeSandbox **0.7.0** initially to match the locked SDK and local validation.
Download/review the matching release from the [official releases](https://github.com/TencentCloud/CubeSandbox/releases)
and follow its installation guide on the dedicated worker. Do not blindly run
a latest-version installer against an existing computer. Confirm KVM access,
glibc/OS requirements, XFS reflink storage and sufficient free disk first.

Enable the deployed version's [Cube authentication](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/authentication.md)
and network restrictions. The local demo placeholder key is not production
authentication. Cube may use an external verification endpoint; configure and
test that dependency using its documentation. Never expose unauthenticated
Cube management services to the internet.

`scripts/cube_local.sh` only manages an **already prepared local development
VM**; it is not a fresh-machine installer. Keep that smaller guest for local
evaluation, or follow the dedicated-worker procedure above for cloud capacity.

Build the same image for all workload types on a Docker-capable build machine:

```sh
cd /opt/robot-workbench/app
docker build -f sandbox/Dockerfile -t registry.example.com/robot-analysis:RELEASE .
docker run --rm --network none --entrypoint python3 \
  registry.example.com/robot-analysis:RELEASE /opt/sandbox/check_environment.py
docker push registry.example.com/robot-analysis:RELEASE
```

Replace registry/tag, configure its credentials through the registry/Cube
credential mechanism, and make it reachable from Cube. Record the pushed image
digest. Do not copy `.env`, uploads or wiki into the build context/image.
The Docker allowlist and worker runtime transfer keep them separate.

Run these commands where `cubemastercli` manages your installed Cube cluster:

```sh
cubemastercli tpl create-from-image \
  --image https://registry.example.com/robot-analysis:RELEASE \
  --writable-layer-size 8Gi --cpu 1000 --memory 2048 \
  --expose-port 49983 --probe 49983 --probe-path /health
cubemastercli tpl create-from-image \
  --image https://registry.example.com/robot-analysis:RELEASE \
  --writable-layer-size 16Gi --cpu 2000 --memory 4096 \
  --expose-port 49983 --probe 49983 --probe-path /health
cubemastercli tpl create-from-image \
  --image https://registry.example.com/robot-analysis:RELEASE \
  --writable-layer-size 24Gi --cpu 4000 --memory 8192 \
  --expose-port 49983 --probe 49983 --probe-path /health
```

Create sequentially, wait for each to be `READY`, and save **this cluster's**
three template IDs. Use binary `Gi`, not decimal `G`. Provision large only when
Cube and its host have enough headroom. Usable filesystem space is smaller than
the provisioned layer due to filesystem overhead. Templates must match
`backend/resources.py`; the worker checks actual CPU/RAM before a model call.

## 7. Worker config, private wiki and service

Create `/etc/robot-workbench/worker.env`, mode `0600`, owned by the application
user. Match the worker token to ECS; fill provider and Cube credentials locally:

```dotenv
ROBOT_API_URL=https://logs.example.com
ROBOT_WORKER_TOKEN=REPLACE_WITH_THE_SHARED_RANDOM_WORKER_TOKEN
CUBE_API_URL=http://127.0.0.1:3000
CUBE_API_KEY=REPLACE_WITH_YOUR_CONFIGURED_CUBE_CREDENTIAL
CUBE_PROXY_NODE_IP=127.0.0.1
CUBE_PROXY_PORT_HTTP=80
CUBE_TEMPLATE_ID=YOUR_STANDARD_TEMPLATE_ID
CUBE_TEMPLATES_JSON='{"small":"YOUR_SMALL_ID","standard":"YOUR_STANDARD_ID","large":"YOUR_LARGE_ID"}'
ROBOT_WORKER_PARALLEL=1
ROBOT_JOB_TIMEOUT_SECONDS=1800
ROBOT_CODEX_MODEL=deepseek-v4-flash-vision-exp
ROBOT_CODEX_PROVIDER_URL=https://api.deepseek.com
ROBOT_CODEX_API_KEY_ENV=DEEPSEEK_API_KEY
DEEPSEEK_API_KEY=REPLACE_WITH_A_RESTRICTED_PROVIDER_KEY
ROBOT_WIKI_DIR=/var/lib/robot-workbench/wiki
```

Cube URLs above assume same-machine direct deployment with reachable proxy
ports. For the local QEMU setup only, use API port 13000 and proxy port 11080
instead; see [local sandbox networking](local-sandbox.md). Use HTTPS/private
routing and the appropriate CA if Cube is remote; do not disable verification.

Copy only your approved wiki to the configured folder using a secure file
transfer. It is deliberately absent from this public repository. Keep the tree
readable by `robotworkbench` and out of public static directories. Relevant wiki
excerpts may appear in public reports, so a private filesystem does not make
those excerpts confidential. Never mount the entire worker home or Docker socket
into a job. Without a wiki the worker can still analyze uploads, but must report
the missing knowledge context.

The model ID above preserves the requested DeepSeek compatibility alias. Check
provider availability before launch; if unavailable, select a supported model
explicitly and repeat a bounded test. Responses API compatibility is required;
arbitrary Qwen endpoints are not drop-in replacements. See [model providers](model-providers.md).

Create `/etc/systemd/system/robot-worker.service`:

```ini
[Unit]
Description=Robot Log Workbench Cube worker
After=network-online.target
Wants=network-online.target

[Service]
User=robotworkbench
Group=robotworkbench
WorkingDirectory=/opt/robot-workbench/app
EnvironmentFile=/etc/robot-workbench/worker.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/robot-workbench/app/.venv/bin/python -m backend.worker
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Keep the worker stopped until Cube checks pass. The runtime skill and roles are
copied from `sandbox/runtime/` into each job; no personal Codex skill installation
is needed. As the application user, run:

```sh
cd /opt/robot-workbench/app
uv run --frozen --extra worker --env-file /etc/robot-workbench/worker.env \
  python scripts/check_sandbox_runtime.py small
uv run --frozen --extra worker --env-file /etc/robot-workbench/worker.env \
  python scripts/check_sandbox_runtime.py standard
```

These boot/delete real VMs with synthetic fixtures and **no model credentials
sent into the VM**. They do not spend the application admission budget. Test
`large` separately on the cloud worker before admitting that size. Start with
one worker slot; use `ROBOT_WORKER_PARALLEL=2` only after a two-job benchmark.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now robot-worker
sudo journalctl -u robot-worker -n 30 --no-pager
```

## 8. Acceptance, limits and routine operations

1. From a non-administrator browser, verify HTTPS, EN/中文 switching, history,
   allowance status and Back to upload. A public worker route should reject an
   unauthorized request, and Cube ports should be inaccessible externally.
2. From the worker, verify ECS HTTPS connectivity and Cube health. A queued job
   with available allowance must be claimed; no job is fabricated if Cube fails.
3. Run one clearly labelled synthetic log/image analysis with the restricted
   provider key. This **is a paid test**; use normal admission and retain the
   report, events, elapsed time and provider usage. Do not bypass exhausted limits.
4. Verify report evidence citations, wiki applicability, human review version
   persistence, and cross-browser visibility. Safe activity is not a complete
   private reasoning transcript. A human decides whether debugging succeeded.
5. Test public hard stop during a dedicated test job, confirming the entire job
   VM disappears, then run a bounded timeout test. No unrelated job should die.
6. Measure representative large logs/PDFs and concurrent jobs before expanding
   capacity. `scripts/benchmark_resources.py` only sees local process trees;
   parent RSS is not aggregate Cube VM memory. Use Cube/host measurements too.

The default job pool is 4 CPU/8 GiB RAM/32 GiB writable disk. It fits two standard
jobs or one large job. Other host resources stay available for Cube, OS and
caches; this is a reservation plan, not a measured throughput guarantee. One
global API pool is implemented, not multi-host capacity discovery/autoscaling.

The USD 10/day limit resets at Asia/Shanghai midnight. Running reservations
carry over; in-flight work may finish above its estimate. This is **admission
accounting, not a hard provider bill ceiling**. Configure provider-side spending
controls separately. Every job also has a maximum 30-minute sandbox lifetime.

### Updates and backups

- Before updating either checkout, drain running work. Stop the idle worker,
  briefly stop the API, then back up SQLite **and uploads together**. A cold
  backup includes any SQLite WAL/SHM files present; restore them as a consistent
  set. Store backups encrypted and outside the public repo/web root.
- Preserve `/etc/robot-workbench/*.env`, the job data directory, private wiki,
  Cube storage and registry. Never use `git clean -fdx` on a deployment checkout.
- Update to the same reviewed Git revision on both machines, run
  `uv sync --frozen` (worker: add `--extra worker`), rebuild/copy `dist/`, run
  tests, then restart API and worker. Rebuild templates only when image/runtime
  compatibility requires it; runtime skill edits transfer on the next job.
- Verify health, `/api/budget`, journal logs, the live page and Git revision
  after restart. Keep the previous artifact/revision and matching database
  backup; schema rollback is not automatically supported.
- There is no automated retention/garbage collector. Review disk usage and
  back up before any deliberate deletion. Deleting uploads invalidates future
  original-evidence retrieval; preserve finished reports and human versions.
- Rotate any token pasted into chat, including the publication GitHub token.
  Public repository visibility grants no license to redistribute private data.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| Job stays queued | Daily allowance, pending/running counts, pool headroom, worker service and outbound HTTPS |
| Worker gets 401/403 | Shared token, Nginx worker IP allowlist, trusted proxy/origin settings |
| Browser upload gets 413 | Nginx/CDN/WAF body caps, 2 GiB file and 4 GiB job limits |
| Cube will not boot | KVM access, XFS reflink, free storage, template READY state and actual resources |
| Template mismatch / missing profile | Correct cluster IDs and JSON quotes; restart worker after config edits |
| No wiki matches | File permissions, actual full index, bilingual/canonical query, correct robot model |
| Report missing or model unavailable | Safe job events, provider endpoint/model compatibility and key quota |
| Stopping persists | Worker connectivity and Cube termination; reservations remain until confirmation/lease expiry |

See [resource profiles](resource-profiles.md), [wiki review](wiki-review.md) and
[progress](../PROGRESS.md) for precisely what has and has not been tested.
