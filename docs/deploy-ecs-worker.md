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
