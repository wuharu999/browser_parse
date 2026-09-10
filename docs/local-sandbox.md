# Local CubeSandbox development VM

This checkout uses CubeSandbox's official disposable `dev-env` inside a QEMU/KVM virtual machine. It is an evaluation environment, not a production deployment. Cube services, XFS/reflink storage, SELinux changes, MySQL, Redis, CubeProxy, and nested microVMs stay inside the OpenCloudOS guest. The Ubuntu host is not reformatted, no host service is installed, and no host DNS or privileged port is changed.

The host still needs Docker access and usable nested KVM. On a cloud machine, `/dev/kvm` must exist and nested virtualization must be enabled. The current layout assigns four host CPUs and a 10 GiB container limit to an 8 GiB, four-vCPU guest; consult [resource profiles](resource-profiles.md) before increasing concurrency or sandbox sizes.

## Local layout and isolation

All persistent VM files are below the ignored `data/cube-local/` directory:

- `source/` is a shallow clone of `TencentCloud/CubeSandbox` at the reviewed revision.
- `OpenCloudOS-GenericCloud-9.6-20260514.2.x86_64.qcow2` is the official image, provisioned by `dev-env/prepare_image.sh` and resized to a sparse 100 GiB virtual disk.
- The tracked `sandbox/HostVM.Dockerfile` builds the local `robot-cube-vm-host:local` QEMU-tools image.
- `known_hosts` pins SSH host keys seen through the localhost forward. `.ssh-askpass.sh` is executable only by the owner (mode `0700`) and contains only the guest bootstrap password unless the operator explicitly overrides it.

The `robot-cube-vm` container runs as UID/GID 1000, dynamically receives the host `/dev/kvm` group, drops all capabilities, enables `no-new-privileges`, receives only `/dev/kvm`, uses host networking, and bind-mounts only the absolute `data/cube-local` path at `/cube-data`. It does not mount the repository, Docker socket, host root, or `.env`.

The official `run_vm.sh` binds QEMU forwards to localhost:

| Host | Guest | Purpose |
| --- | --- | --- |
| `127.0.0.1:10022` | `22` | Guest SSH |
| `127.0.0.1:13000` | `3000` | Cube API |
| `127.0.0.1:11080` | `80` | CubeProxy HTTP |
| `127.0.0.1:11443` | `443` | CubeProxy HTTPS |
| `127.0.0.1:12088` | `12088` | Cube Web UI |

The local OCI registry is `robot-cube-registry`, bound only at `127.0.0.1:15000`. The application image is tagged both `robot-log-analysis:local` and `127.0.0.1:15000/robot-log-analysis:local`; the QEMU guest reaches the registry through QEMU's user-network gateway as `http://10.0.2.2:15000`. Treat this registry as local development infrastructure, not a secured production registry.

## Manage the prepared VM

The manager performs no installation, image preparation, filesystem formatting, registry creation, or forced shutdown:

```sh
bash scripts/cube_local.sh status
bash scripts/cube_local.sh start
bash scripts/cube_local.sh ssh
bash scripts/cube_local.sh stop
```

`start` reuses existing containers. If `robot-cube-vm` does not exist, it creates only that restricted container, and only after verifying `/dev/kvm`, the prepared qcow2, the official `dev-env`, and `robot-cube-vm-host:local`. If the existing registry container is stopped, it starts it; it does not create or replace a registry. `ssh` authenticates as the public `opencloudos` bootstrap user and uses passwordless guest sudo to enter root. It also accepts a command, for example `bash scripts/cube_local.sh ssh systemctl status cube-sandbox-oneclick.service`. `stop` requests `poweroff` inside the guest and deliberately leaves the VM running if graceful shutdown cannot be confirmed.

To change the bootstrap password handling, set `CUBE_VM_PASSWORD` for the one command. Keep real application/provider secrets only in the repository's ignored `.env`, mode `0600`; the manager never reads or prints that file. Do not copy provider keys into the VM image or local registry.

## Cube and worker configuration

The Cube one-click installer runs as root inside the guest. Installation used the official China mirror and CubeSandbox v0.7.0. Check it without reinstalling:

```sh
bash scripts/cube_local.sh ssh curl -fsS http://127.0.0.1:3000/health
```

The original paid-pilot template was created successfully with:

```sh
bash scripts/cube_local.sh ssh cubemastercli tpl create-from-image \
  --image http://10.0.2.2:15000/robot-log-analysis:local \
  --writable-layer-size 12G --expose-port 49983 \
  --probe 49983 --probe-path /health --cpu 2000 --memory 4096
```

It reported `READY`, then passed real SDK file transfer, Python/Codex/Poppler execution, and sandbox deletion. The refreshed template `tpl-000995539963488d9fbd5ca8` completed the real 93-second Codex pilot with two native subagents; this ID is specific to this development cluster. Configure the host worker with:

```dotenv
CUBE_API_URL=http://127.0.0.1:13000
CUBE_PROXY_NODE_IP=127.0.0.1
CUBE_PROXY_PORT_HTTP=11080
CUBE_TEMPLATE_ID=<ready-template-id>
```

Use the cluster's configured API key policy; a placeholder is suitable only while local Cube authentication is disabled. The native `cubesandbox` SDK keeps the virtual `*.cube.app` Host header while dialing the forwarded proxy directly, so no host DNS entry is needed.

Provider credentials and model selection remain separate from Cube. This checkout selects exactly `deepseek-v4-flash-vision-exp`, as requested. DeepSeek documents this as a retired compatibility alias served by `deepseek-flash`; the exact alias passed a tiny image-reading test but must not be assumed to remain available forever. Keep `ROBOT_CODEX_MODEL` consistent with [the provider guide](model-providers.md) and use a restricted-budget key. Local Cube health and a ready template do not by themselves prove native subagent execution.

The newer prepared image and automatic profiles supersede that fixed-template
configuration for new jobs. Current local IDs are small
`tpl-5dc519104a1f43628d0193cb` (1000m CPU, 2048 MiB, `8Gi`) and standard
`tpl-dd7fefce285841a48ad9236a` (2000m CPU, 4096 MiB, `16Gi`). These IDs are
cluster-specific. Configure the single-quoted `CUBE_TEMPLATES_JSON` map and
resource pool as described in [resource profiles](resource-profiles.md).
The local outer guest remains 8 GiB; the job RAM pool is capped at 4 GiB and
the worker remains single-slot. An 8-GiB large job needs a larger worker and
has not been boot/load-tested here. Old templates remain available; no VM disk
or historical job data was removed during this update.

## Boundaries and recovery

- Never run the bare-metal installer on the Ubuntu host. It requires root, XFS/reflink storage, and host services.
- Never bypass this design with a privileged container, Docker-socket mount, host-root mount, host port 80, or `/etc/hosts` change.
- Shut down with `cube_local.sh stop` or guest `poweroff`; abrupt QEMU termination can corrupt the guest image.
- The VM is disposable. After a confirmed shutdown, recovery may delete only `data/cube-local` artifacts the operator explicitly chooses to rebuild. Preserve the local registry or rebuild/push the application image before deleting it.

Official references: [development environment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/dev-environment.md), [`dev-env` README](https://github.com/TencentCloud/CubeSandbox/blob/master/dev-env/README.md), [quick start and template creation](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/quickstart.md), and [native Python SDK networking](https://github.com/TencentCloud/CubeSandbox/blob/master/sdk/python/README.md).
