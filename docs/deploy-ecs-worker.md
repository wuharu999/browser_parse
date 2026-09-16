# ECS API and two independent Docker workers

The existing ECS endpoint is `47.239.12.206:8000`. Each worker initiates HTTP requests to ECS; neither machine needs a connection to the other, and ECS does not connect back to workers. Keep the database and uploaded files on ECS. Build/start the local Docker job runtime on each machine using [Docker worker deployment](docker-worker.md).

Before upgrading ECS, stop/drain the old workers and back up the SQLite database plus uploads. Install dependencies with `uv sync --frozen`, build the frontend with `npm ci && npm run build`, then restart the API. The additive worker-owner migration happens at API startup. Deploy this API before the new workers: claims now require worker identity and capacity. Preserve populated environment files and ignored data during updates.

## 1. ECS API configuration and service

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

Replace the domain with your own. `JOB_POOL_*` now sets the maximum size of one
submitted job. Each worker advertises its own available capacity when claiming;
these values do not sum resources from both machines or describe ECS RAM.

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

## 2. Publish the ECS website over HTTPS

Point the domain's DNS A record at ECS. Install Nginx and your chosen ACME/TLS
client. Configure the following server block, replacing `logs.example.com` and
both documentation-only worker outbound addresses `203.0.113.10` and `203.0.113.11`. On Ubuntu, this can be
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
        allow 203.0.113.11;
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

## 3. Shared Q&A for completed analyses

Deploy the API/frontend before updating workers. The API adds `jobs.analysis_context`
and a `questions` table automatically; existing reports immediately support report-only
Q&A. Back up the SQLite database with SQLite's backup command before restarting, and
preserve the populated environment, uploads, and database during the code update.

Add these settings to `/etc/robot-workbench/api.env` using an editor; the key stays on
ECS and is never sent to the browser or workers:

```dotenv
ROBOT_CHAT_API_KEY=REPLACE_WITH_CHAT_PROVIDER_KEY
ROBOT_CHAT_BASE_URL=https://api.deepseek.com
ROBOT_CHAT_MODEL=deepseek-flash
```

The chat key is independent of `ROBOT_CODEX_*`. A blank chat key disables new questions
without affecting reports or existing conversation history. The implementation uses
[DeepSeek streaming Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
with thinking disabled, no tools, a 1,200-token output limit, and a 60-second request
deadline. There is no daily question count limit; Q&A never consumes analysis slots.

On ECS, after updating the checkout:

```sh
cd /opt/robot-workbench/app
uv sync --frozen
npm ci
npm run build
sudo systemctl restart robot-api
curl --fail http://127.0.0.1:8000/api/budget
sudo journalctl -u robot-api -n 30 --no-pager
```

Keep one API process and proxy buffering disabled as configured above. In a completed
analysis, submit a question and confirm the answer appears progressively. Open the same
analysis in another browser and confirm shared history; refresh to verify persistence.
Without a chat key, the panel must show its unavailable state. With no worker online,
report-only Q&A must still work. A dropped stream, timeout, or restart preserves a
partial answer labeled interrupted; retry creates a new attempt. Same-request network
retries reuse the original record and never start a second model request.

Then, **on the worker machine**, update the checkout and rebuild its Docker job image
using [the worker upgrade instructions](worker-one-machine.md). The image now includes
`sandbox/analysis_context.py` alongside the runner; future analyses save validated,
credential-redacted `analysis-notes.json` (at most 16 KiB) in their normal run. The
worker saves those notes through the authenticated
`POST /api/worker/jobs/{id}/analysis-context` endpoint and waits for ECS acknowledgment
before deleting the container, then includes them in the completion request. The
completion request also accepts optional `analysis_context`; already saved notes are
retained and cannot be replaced. Missing/invalid notes fall back to report-only Q&A and do not fail
the analysis. No historical container needs to be restarted. No worker SSH from the
development machine is required.

Public interfaces: `GET /api/jobs/{id}/questions?limit=50&before=<id>` returns shared
history oldest-to-newest within the page, `next_before`, active generation, availability,
and context mode. `POST /api/jobs/{id}/questions` accepts `{question, request_id}` and
streams NDJSON `started`, `answer`, `done`/`interrupted` events; `answer` contains the
current cumulative text. An already-seen request ID returns JSON `{item, replayed:true}`.
Requests with a conflicting ID or another active answer return 409; unconfigured Q&A
returns 503. Questions are at most 4,000 characters and successful history persists in
full; each model request receives only the last 12 completed exchanges plus fixed
analysis context and the latest labeled human review. Q&A never edits the report/notes.
