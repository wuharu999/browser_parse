# Worker does not claim jobs: Docker disk capacity

Run these steps on the **dedicated remote worker machine**. They do not change
ECS or a development workstation. The screenshot from 2026-09-15 showed Docker
on the 40-GB root filesystem, with approximately 10 GiB free, while `/home` had
approximately 82 GB free. Those are reported values, not a live inspection.

The worker calculates:

```text
claimable disk MiB = min(ROBOT_WORKER_DISK_MB,
                        free MiB on DockerRootDir - ROBOT_HOST_DISK_RESERVE_MB)
10169 - 8192 = 1977 MiB
```

That cannot fit even a small job. With the default 8-GiB reserve, the minimum
free space **after the image build, storage copy and proxy setup** is:

| Job profile | Job disk budget | Free space needed on Docker's filesystem |
| --- | ---: | ---: |
| small | 8 GiB | 16 GiB |
| standard | 16 GiB | 24 GiB |
| large | 24 GiB | 32 GiB |

These are admission thresholds, not recommended total disk sizes. Image layers,
build cache, copied data and unrelated host activity require additional space.
Increasing `ROBOT_WORKER_DISK_MB` does not create disk space. Reducing the
reserve cannot make the screenshot's 10 GiB fit a 16-GiB standard job.

## Inspect the actual worker storage

```sh
sudo docker info --format 'DockerRootDir={{.DockerRootDir}} Driver={{.Driver}} DriverStatus={{json .DriverStatus}}'
findmnt -T /var/lib/docker
findmnt -T /home
df -h /var/lib/docker /home
sudo docker system df
sudo systemctl cat docker.service containerd.service
```

Use the reported `DockerRootDir` if it differs from `/var/lib/docker`. Check
that `/home` is a persistent local filesystem suitable for Docker storage.
Keep each worker's Docker data local, never on a shared worker filesystem.
Inspect the service definitions for custom `--data-root`, `--config-file`, or
containerd `--root` arguments before editing the default configuration paths.

Docker installations using the containerd image store keep images/snapshots
separately, normally in `/var/lib/containerd`. Moving Docker's `data-root` only
moves its own data, including workspace volumes. It does **not** relocate that
containerd image store. See [Docker data-directory configuration](https://docs.docker.com/engine/daemon/#daemon-data-directory)
and [containerd storage](https://docs.docker.com/engine/storage/containerd/#disk-space-usage).

## Move storage to /home during worker maintenance

The commands below assume the default rootful Docker/system containerd paths,
the service names from [the one-worker guide](worker-one-machine.md), and a
fresh destination. Adapt them to the inspection results; preserve existing
configuration keys. Drain active jobs before stopping the worker. Docker must
have no running job or unrelated containers when its storage is copied.

1. Stop the worker and its proxy, then confirm no containers are running. Do not
   proceed with a live job, an unrelated workload or live-restore containers.

   ```sh
   sudo systemctl stop robot-worker-one
   cd /opt/robot-workbench/app
   sudo docker compose -f deploy/docker-compose.worker.yml stop
   sudo docker ps
   ```

2. Back up the existing daemon configurations to a dated, root-only maintenance
   directory. Install `rsync` if needed. Stop Docker and its socket before copying.
   When relocating the system containerd store too, stop `containerd.service`;
   first ensure no other service uses it.

   ```sh
   sudo systemctl stop docker.service docker.socket
   sudo install -d -m 710 /home/robot-docker
   sudo rsync -aHAXx --numeric-ids /var/lib/docker/ /home/robot-docker/
   ```

   If the inspected daemon uses the system containerd store at
   `/var/lib/containerd`, relocate it while both services are stopped:

   ```sh
   sudo systemctl stop containerd.service
   sudo install -d -m 710 /home/robot-containerd
   sudo rsync -aHAXx --numeric-ids /var/lib/containerd/ /home/robot-containerd/
   ```

   These commands retain the original directories for rollback. Do not prune
   volumes, change storage drivers, or delete the originals during this step.

3. Edit `/etc/docker/daemon.json` with `sudoedit`, merging this property into
   the existing JSON object:

   ```json
   "data-root": "/home/robot-docker"
   ```

   If no file exists, create an object containing that property. Do not also
   set `--data-root` in the daemon's service command. Validate the result:

   ```sh
   sudo dockerd --validate --config-file=/etc/docker/daemon.json
   ```

   If you moved the system containerd store, edit its actual configuration
   (normally `/etc/containerd/config.toml`) and set the **top-level** property
   below, before any `[section]`. Preserve its version, plugins and other settings:

   ```toml
   root = "/home/robot-containerd"
   ```

   Keep containerd's volatile `state` under `/run`; do not move it to persistent
   storage. Do not overwrite a customized containerd configuration with defaults.

4. Ensure storage is mounted before services start. Using
   `sudo systemctl edit docker.service`, add:

   ```ini
   [Unit]
   RequiresMountsFor=/home/robot-docker
   ```

   If containerd was relocated, add its corresponding drop-in via
   `sudo systemctl edit containerd.service`:

   ```ini
   [Unit]
   RequiresMountsFor=/home/robot-containerd
   ```

5. Set the worker's existing protected environment file to match Docker:

   ```dotenv
   ROBOT_DOCKER_DATA_DIR=/home/robot-docker
   ROBOT_HOST_DISK_RESERVE_MB=8192
   ROBOT_WORKER_DISK_MB=24576
   ```

   Retain the worker ID, token, provider key and immutable image ID. If host
   upload/wiki staging also needs more space, create a private directory owned
   by the service user on `/home` and set `TMPDIR` in the worker environment.
   Stop all copies of that worker first: the single-worker lock also uses
   `TMPDIR`, so every launch of the same worker must use the same value.

6. Restart only the infrastructure, then check storage and the existing image:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl start containerd.service
   sudo systemctl start docker.service
   sudo docker info --format 'DockerRootDir={{.DockerRootDir}} DriverStatus={{json .DriverStatus}}'
   df -h /home/robot-docker
   sudo docker image ls --no-trunc
   sudo docker compose -f deploy/docker-compose.worker.yml up -d
   ```

   If the image is missing, investigate the copied store and configuration;
   avoid rebuilding into an unexpectedly empty or incorrect storage path.
   Confirm the image ID still matches the protected worker configuration.
   If using containerd's separate image store, also check that its effective
   root points at `/home/robot-containerd` and that filesystem has free space.

7. Run the credential-free `standard` runtime check from the one-worker guide.
   It creates/removes a synthetic container, without a model request. Then start
   the worker and check its capacity log:

   ```sh
   sudo systemctl start robot-worker-one
   sudo journalctl -u robot-worker-one -n 60 --no-pager
   ```

   The capacity line must include `standard` in `fits=` for the queued standard
   job, and `disk_mb` must be at least `16384`. CPU and available memory must
   also meet the profile. An empty queue or exhausted ECS budget can still leave
   a capable worker idle; capacity alone does not prove a completed analysis.

Keep the original data and configuration backups until the image, runtime
check and a real job have been verified. A rollback requires stopping the
worker, proxy and daemons again, restoring the original storage settings and
using the original data. Do not merge two stores that have independently run.

## Capacity logging in the worker

The worker logs available CPU/memory/disk, the checked Docker path, disk reserve
and fitting profiles on the first poll and when the set of fitting profiles
changes. `fits=none` means it does not contact ECS to claim a job; `fits=small`
means a standard job cannot be claimed. The admission limits remain enforced.
Logs omit API/provider credentials and are not repeated on every idle poll.
