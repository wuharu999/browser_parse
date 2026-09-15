# First remote Docker worker (Ubuntu/Debian)

Run this guide **on the first dedicated remote worker machine**, not on the
ECS host and not on a development workstation. It creates one worker identity
and one local Docker job slot. The worker polls ECS at `http://47.239.12.206:8000` and accesses its configured
model provider; it does not need, expose, or discover a peer worker.

Complete the ECS API rollout before starting this worker: the current worker
protocol sends its identity and local capacity with each claim. The deployment
bundle supplied for this release contains the Docker migration. A GitHub
checkout can also be used if it contains this migration; run `npm ci && npm run
build` to create the frontend assets that are already included in the bundle.

## 1. Install prerequisites

Use a supported x86-64 (amd64) Ubuntu or Debian release. The pinned proxy
image is amd64. Install the current Docker Engine with isolated bridge gateway
support. Install Docker Engine and the
Compose plugin from Docker's official instructions for your exact OS:

- [Ubuntu Engine install](https://docs.docker.com/engine/install/ubuntu/)
- [Debian Engine install](https://docs.docker.com/engine/install/debian/)
- [Compose plugin install](https://docs.docker.com/compose/install/linux/)

After installation, confirm the daemon and Compose plugin work on this remote
host:

```sh
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

Install Python and an isolated system-wide `uv` executable using the
[documented PyPI installation method](https://docs.astral.sh/uv/getting-started/installation/#pypi):

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv curl ca-certificates
sudo python3 -m venv /opt/robot-uv
sudo /opt/robot-uv/bin/pip install uv==0.11.29
```
Create the service account and allow it to use the local Docker socket. Docker
socket access is privileged host administration; use a dedicated account.

```sh
sudo useradd --system --create-home --home-dir /opt/robot-workbench --shell /usr/sbin/nologin robotworkbench
sudo usermod -aG docker robotworkbench
sudo install -d -o robotworkbench -g robotworkbench /opt/robot-workbench /etc/robot-workbench
```

Log in again after changing group membership, or use a fresh service start.

## 2. Install the reviewed deployment bundle

Copy `robot-workbench-docker-one-worker-20260914.tgz` and its `.sha256` file
from this release to the worker, for example into `/tmp`. Verify the checksum
before extracting. The bundle includes the built frontend and source; it does
not include dependencies, images, credentials, live data, or the private wiki.

```sh
cd /tmp
sha256sum -c robot-workbench-docker-one-worker-20260914.tgz.sha256
sudo install -d -o robotworkbench -g robotworkbench /opt/robot-workbench/app
sudo tar -xzf robot-workbench-docker-one-worker-20260914.tgz -C /opt/robot-workbench/app --strip-components=1
sudo chown -R robotworkbench:robotworkbench /opt/robot-workbench/app
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && /opt/robot-uv/bin/uv sync --frozen --python 3.12'
```

Do not copy `.env`, ECS data, uploads, the SQLite database, or credentials into
the bundle. Copy an approved private wiki separately only if it is intended for
this worker.

## 3. Build the immutable job image and proxy network

Before building, check free space on Docker's actual filesystem. With the
default reserve, a standard job needs at least **24 GiB free after the build**
and a large job needs 32 GiB. A large `/home` partition does not help while Docker
still stores data on `/`. Follow [Docker storage checks and relocation](docker-storage.md)
if the root filesystem is too small.

The host-side worker runs Python directly. Each analysis runs in a newly
created Docker container, with no Docker socket, host bind mount, host network,
or published port.

As `robotworkbench`, build the job image and record its local immutable image
ID. The runtime accepts the resulting `sha256:...` value.

```sh
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && docker build -f sandbox/Dockerfile -t robot-log-analysis:worker-one .'
sudo -u robotworkbench docker image inspect robot-log-analysis:worker-one --format '{{.Id}}'
```

Before starting the proxy, edit `deploy/squid.conf` so its provider ACL contains
only the hostname(s) for the configured model provider. The checked-in value is
for DeepSeek. Do not allow private addresses, broad suffixes, metadata services,
or arbitrary ports.

Start the local proxy and internal job network from the bundle:

```sh
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && docker compose -f deploy/docker-compose.worker.yml up -d'
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && docker compose -f deploy/docker-compose.worker.yml ps'
```

The proxy has no published host port. Job containers join `robot-analysis-jobs`;
only Squid bridges that internal network to its provider-only egress network.

## 4. Create the protected worker environment

Create the protected file, then paste and fill the template below in the editor:

```sh
sudo install -m 600 -o robotworkbench -g robotworkbench /dev/null /etc/robot-workbench/worker-one.env
sudoedit /etc/robot-workbench/worker-one.env
```

For later updates, use `sudoedit` directly; the `install` command initializes an
empty file and must not be repeated over a populated configuration. Put the shared ECS worker token into it using your approved
secret channel; do not print it, paste it in this guide, or store it in Git.
`ROBOT_WORKER_ID` is trusted attribution and job ownership under that shared
token, not a second credential.

```dotenv
ROBOT_API_URL=http://47.239.12.206:8000
ROBOT_WORKER_TOKEN=SET_FROM_APPROVED_SECRET_CHANNEL
ROBOT_WORKER_ID=worker-one
ROBOT_WORKER_PARALLEL=1

ROBOT_DOCKER_IMAGE=sha256:PASTE_THE_LOCAL_IMAGE_ID_FROM_INSPECT
ROBOT_DOCKER_NETWORK=robot-analysis-jobs
ROBOT_EGRESS_PROXY_URL=http://robot-egress-proxy:3128
ROBOT_DOCKER_DATA_DIR=/var/lib/docker

# Per-job maxima. Keep headroom for Docker, the OS, image layers and the proxy.
ROBOT_WORKER_CPU_MILLI=4000
ROBOT_WORKER_MEMORY_MB=8192
ROBOT_WORKER_DISK_MB=24576
ROBOT_HOST_DISK_RESERVE_MB=8192
ROBOT_JOB_TIMEOUT_SECONDS=1800
ROBOT_POLL_SECONDS=3

ROBOT_CODEX_MODEL=deepseek-v4-flash-vision-exp
ROBOT_CODEX_PROVIDER_URL=https://api.deepseek.com
ROBOT_CODEX_API_KEY_ENV=DEEPSEEK_API_KEY
DEEPSEEK_API_KEY=SET_FROM_APPROVED_SECRET_CHANNEL
ROBOT_WIKI_DIR=/var/lib/robot-workbench/wiki
```

If the provider key uses another environment variable, set
`ROBOT_CODEX_API_KEY_ENV` to that name and use the same name for the protected
key. The Guard is enabled by default and uses the provider settings unless its
own `ROBOT_GUARD_*` values are explicitly supplied.

```sh
sudo chown robotworkbench:robotworkbench /etc/robot-workbench/worker-one.env
sudo chmod 600 /etc/robot-workbench/worker-one.env
sudo install -d -o robotworkbench -g robotworkbench /var/lib/robot-workbench/wiki
```

Set capacity no higher than this host can actually spare. CPU and memory become
Docker hard limits per job. Disk is monitored rather than quota-enforced: the
worker stops a job when workspace or Docker free space reaches its threshold.
For a 4-CPU/8-GiB large profile, leave at least one host CPU, 1 GiB RAM, and the
configured disk reserve available. One worker process claims at most one job;
set `JOB_MAX_RUNNING=1` in the ECS API environment and restart the API for
this first rollout. Increase it to `2` when adding the second worker.

## 5. Credential-free preflight

Before enabling the worker service, verify Docker, the image, internal network and
a synthetic analysis runtime without contacting the model provider or ECS worker
API. Replace the environment path only if you chose a different filename.

```sh
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && /opt/robot-uv/bin/uv run --frozen --env-file /etc/robot-workbench/worker-one.env python scripts/check_sandbox_runtime.py standard'
```

This command reads the protected environment file locally but does not echo its
values. It must finish with `status: ok`; it creates and removes a temporary container
and volume, without running a model. Use `small` if this host cannot fit the
standard 2-CPU/4-GiB profile.
If it fails, fix Docker limits, data-directory alignment, the internal network,
image ID, proxy URL, or host headroom before continuing.

## 6. Persist proxy and worker with systemd

Create `/etc/systemd/system/robot-worker-one.service`:

```ini
[Unit]
Description=Robot Log Workbench Docker worker one
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=robotworkbench
Group=robotworkbench
WorkingDirectory=/opt/robot-workbench/app
EnvironmentFile=/etc/robot-workbench/worker-one.env
ExecStartPre=/usr/bin/docker compose -f /opt/robot-workbench/app/deploy/docker-compose.worker.yml up -d
ExecStart=/opt/robot-workbench/app/.venv/bin/python -m backend.worker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable and inspect it on the remote worker:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now robot-worker-one
sudo systemctl status robot-worker-one --no-pager
sudo journalctl -u robot-worker-one -n 100 --no-pager
```

The worker reconciles only labelled containers and volumes carrying
`worker-one` during startup. If cleanup cannot be confirmed, it leaves the job unfinished and exits
rather than claiming another job; the ECS lease subsequently expires; investigate Docker and
restart only after cleanup succeeds.

## 7. Validate the ECS connection and operation

From this worker, first check the public ECS endpoint without credentials:

```sh
curl --fail --max-time 10 http://47.239.12.206:8000/api/budget
```

The response must contain `resource_envelope` and `max_running: 1`. If it still
contains `resource_pool`, ECS is running the old API: complete its upgrade
before starting this worker.

Then confirm the service remains active and is polling without exposing a local
listener:

```sh
sudo systemctl is-active robot-worker-one
sudo -u robotworkbench sh -c 'cd /opt/robot-workbench/app && docker compose -f deploy/docker-compose.worker.yml ps'
sudo docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
```

Do not send a hand-written worker claim with a token from a shell command. Use a
clearly labelled synthetic job through the ECS UI; this consumes model API
credit and the configured job allowance. Verify that ECS records `worker-one`, the job
container has CPU/memory limits, cancellation removes its labelled container
and volume, and the completed report appears at ECS. A disconnected worker or
expired lease fails the job without automatic retry.

For a future second host, set ECS `JOB_MAX_RUNNING=2` and repeat this guide with a distinct worker ID and its
own capacities. It still connects only to ECS; do not add peer routes, shared
Docker storage, a shared filesystem, or worker-to-worker credentials.

If provider requests are blocked, inspect the proxy logs and DNS. The provider
hostname must resolve to a real public address; VPN fake-IP ranges such as
`198.18.0.0/15` are intentionally blocked. Do not remove private-address ACLs
to work around DNS. See [proxy setup and troubleshooting](docker-worker.md).
