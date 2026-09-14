# Docker job capacity

The API selects a resource plan from verified upload sizes and evidence hints. The plan is an admission estimate; the Docker worker applies hard CPU and memory limits and runs only one job per machine.

| Profile | CPU limit | Memory limit | Disk monitor threshold |
| --- | ---: | ---: | ---: |
| Small | 1 CPU | 2 GiB | 8 GiB |
| Standard | 2 CPU | 4 GiB | 16 GiB |
| Large | 4 CPU | 8 GiB | 24 GiB |

Upload volume, expanded-data hints, entry count and media choose a profile in `backend/resources.py`. Configure each machine's per-job maxima; it can claim any of the three profiles that fit its currently available capacity. The central limit is two jobs, one per worker; it is not a claim that both jobs fit any particular host.

CPU and memory are Docker cgroup limits. Disk is intentionally different: the worker monitors free Docker/workspace storage before and during work and stops a job at its configured threshold. Docker writable-layer and volume quotas are not used, so a stopped job can still leave image/volume overhead until automatic cleanup completes. Keep operating-system and Docker storage headroom, alert on the Docker data directory, and test cancellation and cleanup under load.

The analysis image includes pinned Codex, Python, Poppler and the packages in `sandbox/requirements.lock`. It is built from the official Python Debian image, runs as the non-root `analysis` user, and has no provider credential baked in. The Cube-free image has been built locally and passed the credential-free runtime check. This is not a representative load benchmark or a production capacity measurement.

After starting the local proxy/network, validate the image without API/model credentials:

```sh
uv run --env-file .env python scripts/check_sandbox_runtime.py standard
```

See [Docker deployment](docker-worker.md) for image IDs and required host settings.
