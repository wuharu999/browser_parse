# Docker job worker

This project runs the API on ECS at `http://47.239.12.206:8000` and runs two separate worker processes on two Docker-capable machines. Give them distinct `ROBOT_WORKER_ID` values. The topology is `worker A → ECS ← worker B`: workers have no inbound listener and never connect to each other. Each worker advertises one local slot; the central API enforces two jobs total and assigns queued work to a worker that can fit it.

Jobs are ordinary Docker containers, created by the host-side `backend.worker` process. The worker is deliberately not a Docker container: it needs the Docker CLI but no job ever receives the Docker socket, a host mount, host networking, or a published port.

## Build and prepare one worker machine

Install Docker Engine and the project's Python environment, then build the job image. Record its immutable digest and use that digest for `ROBOT_DOCKER_IMAGE`.

```sh
docker build -f sandbox/Dockerfile -t robot-log-analysis:local .
docker image inspect robot-log-analysis:local --format '{{.Id}}'
uv sync
```

Copy `.env.example` to a mode-0600 environment file owned by the worker service user, set a unique worker ID, the shared `ROBOT_WORKER_TOKEN`, the ECS URL, image digest and model credential, then start the proxy and worker:

```sh
docker compose -f deploy/docker-compose.worker.yml up -d
uv run --env-file /etc/robot-workbench/worker.env python -m backend.worker
```

Keep API secrets on ECS and provider credentials only in each worker's mode-0600 environment file. TLS belongs at the existing ECS reverse proxy: set the worker URL to its HTTPS public/private endpoint when TLS is enabled. The worker token authenticates the private worker routes; `ROBOT_WORKER_ID` is attribution, not a second credential.

## ECS API rollout

Deploy the API/store changes to ECS before starting these workers because claim
and capacity messages changed with this migration. Keep the existing API systemd
service and reverse proxy: serve the built `dist/`, terminate TLS there, set the
public origin/CORS configuration, and restrict worker-only claim, download and
completion routes to both workers' outbound IPs or a private route. The API is the only shared endpoint for both workers;
it does not need a route back to either host. Back up the SQLite database and
uploads before an API rollout, drain running jobs, run migrations/start the API,
check its health and logs, then start one worker on each host. No deployment is
performed by this repository change.

## Job isolation and capacity

The runtime creates a labelled container and a fresh named workspace volume per job. It runs as UID 10001 (`analysis`) with a read-only root filesystem, a 512-MiB writable `/tmp` tmpfs and the workspace volume mounted at `/workspace`. It drops all capabilities, enables `no-new-privileges`, limits PIDs, CPU, memory and swap, and removes the container and volume after completion, cancellation or startup recovery.

`ROBOT_WORKER_CPU_MILLI`, `ROBOT_WORKER_MEMORY_MB` and `ROBOT_WORKER_DISK_MB` set per-job maxima. The worker also checks local CPU count, currently available memory and Docker filesystem free space, leaving one CPU, 1 GiB RAM and `ROBOT_HOST_DISK_RESERVE_MB` disk headroom. Job limits follow the selected small/standard/large profile. A local build may use its immutable `sha256:<image ID>` from the command above. A separately pulled image should use its `repository@sha256:<manifest digest>`. Disk has no Docker hard quota: the worker checks workspace/daemon free space before dispatch and while running, requests a stop at its threshold, and records the reason. Image layers, volumes and Docker's own metadata still consume host storage, so retain host headroom and monitor the Docker data directory.

## Egress proxy

`deploy/docker-compose.worker.yml` creates an internal job network and a dual-homed Squid proxy. Job containers join only `robot-analysis-jobs`; Squid also joins `robot-analysis-egress` and publishes no host port. The internal network disables IPv6 and uses Docker bridge `gateway_mode_ipv4=isolated`, which is required because ordinary internal bridge networks still let containers reach services at the host gateway. Confirm the installed Docker version accepts this option before starting workers.

The runner sets `HTTP_PROXY` and `HTTPS_PROXY` to `ROBOT_EGRESS_PROXY_URL`. Squid accepts HTTPS CONNECT only on port 443, blocks private/link-local/loopback destinations, and defaults to deny. Edit `deploy/squid.conf` to contain the exact configured provider hostnames before deployment; the checked-in example allows only DeepSeek's API endpoint. Do not add a broad domain suffix, arbitrary ports, metadata IP, or a proxy bypass.

Validate after bringing up the stack: a job-network container can resolve and connect to `robot-egress-proxy:3128`; direct access to the bridge gateway, ECS, metadata/local/private addresses and an unlisted public hostname fails; proxy HTTPS access to each configured provider succeeds. Test this on every worker host after Docker or proxy changes.

## systemd and operations

Install the service user with access to the local Docker Unix socket (Docker administration is a trusted host privilege). Use one service per worker host, after the proxy Compose service is healthy:

```ini
[Unit]
Description=Robot Log Workbench Docker worker
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=robotworkbench
WorkingDirectory=/opt/robot-workbench/app
EnvironmentFile=/etc/robot-workbench/worker.env
ExecStart=/opt/robot-workbench/app/.venv/bin/python -m backend.worker
Restart=on-failure
UMask=0077
RestartSec=5

[Install]
WantedBy=multi-user.target
```

On restart the worker reconciles only containers and volumes carrying both its managed label and its own worker ID. A lost job is not automatically retried; its ECS lease expires and is settled as failed. Drain or stop the worker before replacing the image, restart the proxy if its ACL changes, then start the worker. Never use `git clean -fdx` on a deployment checkout and never delete Docker volumes without checking their labels and job state.

Local validation built the image, checked its tools and limits, and exercised synthetic HTTP jobs through two worker identities and real Docker containers. Proxy checks verified provider TLS and denial of non-allowed destinations. The local VPN returned synthetic `198.18.0.0/15` DNS addresses, so the provider-positive test used a temporary mapping to its public-DNS address. Do not weaken private-address ACLs for VPN fake DNS; ensure the deployed proxy resolves the provider to real public addresses. No ECS/two-machine deployment or paid model job is claimed.

For API service and two-address reverse-proxy setup, see [ECS deployment](deploy-ecs-worker.md).

For a worker that stays idle because its Docker filesystem is too small, see
[the disk-capacity diagnosis and storage relocation guide](docker-storage.md).

The worker detects `DockerRootDir` from the local Unix-socket Docker daemon at
preflight and uses its filesystem for capacity and runtime free-space checks.
Leave `ROBOT_DOCKER_DATA_DIR` empty for automatic detection. The legacy value
`/var/lib/docker` also means automatic detection; another explicit value asserts
that Docker uses that directory and fails if it does not match. Restart the
worker after moving daemon storage. Detection does not relocate data or add
free space, and containerd image storage can still occupy a separate filesystem.

### Subagent lifecycle updates

One fresh container is created per analysis job from the prepared image. Codex
subagents and their shell/Python subprocesses share its workspace and resource
limits. They do not require Docker access or additional containers.

The runner forwards native `collab_tool_call` child IDs and observed lifecycle
states through the Worker to ECS. A completed spawn tool does not imply a completed
child. Job details retain each child's latest recorded state beyond the activity
page limit, including after the job ends. Older jobs without recorded child IDs
cannot have that history reconstructed. Child token usage remains unknown when
the provider does not report it separately.

For this update, deploy the ECS API and frontend, then rebuild `sandbox/Dockerfile`
on the worker using the image-build procedure above, update `ROBOT_DOCKER_IMAGE`
to the new immutable image ID/digest, and restart `backend.worker` after active
jobs finish. A source pull alone does not update the runner baked into an existing
image. No Worker SSH from the development machine is needed.
