# Provisional resource profiles

The requested cloud target is 8 vCPU and 16 GiB RAM. It can run one reusable analysis image containing Codex, Python and Poppler for log, image and PDF cases; do not create three separate images just because the inputs differ.

These are planning allocations, not benchmark results:

| Workload | Provisional allocation |
| --- | ---: |
| Log-focused job | 1 vCPU, 2 GiB RAM |
| Multimodal image/PDF job | 2 vCPU, 4 GiB RAM |
| Two parallel multimodal jobs | 4 vCPU, 8 GiB RAM total |

That leaves 4 vCPU and 8 GiB for the worker, API, operating system, cache and measurement headroom on the 8 vCPU/16 GiB machine. RAM is system memory; no GPU/VRAM is required by this CPU-oriented image. If a future model/image stack uses a GPU, VRAM must be specified separately and cannot be inferred from this plan.

Allocate at least **12 GiB** to the writable layer for a 4 GiB maximum job: originals and staging are currently duplicated, and wiki/index/temp material also needs headroom. This is a planning floor, not a measurement.

## Collect an actual sample

Start a bounded job first, then, from the Linux host that can see its PID:

```sh
uv run python scripts/benchmark_resources.py \
  --pid 12345 --interval 1 --seconds 60 --path /bounded/job-workdir \
  --output /bounded/job-workdir/resource-sample.json
```

The script does not start Codex, install software, call a provider, read environment variables, or mutate host configuration. It only writes the explicitly named JSON output file. `--pid` must be positive and `--seconds` is limited to 1800. Monitoring stops when that process exits.

Choose a dedicated job directory for `--path`, not `/` or a whole wiki/repository. Each directory scan is limited to ten seconds but can still traverse a large tree during that time. Invalid output paths may appear in standard Python error messages; do not put credentials in paths or command arguments.

The result includes Linux-visible root-plus-descendant CPU ticks and summed RSS, `/proc/meminfo` availability before/during, filesystem occupancy, and timeout-bounded directory snapshots at start/end. `directory_bytes_*` is explicitly apparent length from `du -sb`; `directory_allocated_bytes_*` is allocated filesystem blocks from `du -s -B1`, so sparse qcow2-like files are not mistaken for their logical size. Summed RSS is explicitly **not** VM physical memory: shared pages can be counted more than once. A remote Cube process that is not visible in the host `/proc` cannot be measured by this tool; use the VM/cgroup metrics supplied by that environment for VM-level truth.

The reusable image’s reported Docker size (currently 380537899 bytes) is Docker-reported metadata, not proof of physical disk consumption: image layers may be compressed and unpacked differently. Verify the unpacked root filesystem and allocated writable-layer blocks in the target VM before making capacity claims.

No resource claim should be promoted beyond provisional sizing until samples are collected for representative log, image and PDF inputs under the intended concurrency.

## Measured tiny pilot — 2026-09-10

Real browser upload of a seven-line TAR.GZ plus a workbench PNG (~126 KB combined), a one-page synthetic wiki, Codex 0.153.4, and the exact requested `deepseek-v4-flash-vision-exp` ID. Two native child sessions were confirmed from metadata. This is not a production-data or full-wiki benchmark.

| Observation | Measured value / scope |
| --- | --- |
| End-to-end worker time | 93.287 s; runner 88.286 s |
| Template allocation | 2 vCPU, 4096 MiB RAM, 12G writable layer |
| Job workspace logical file sizes | 26,665,810 bytes; not allocated disk blocks |
| Codex launcher peak RSS | 7,892,992 bytes; does not measure all children/native processes |
| In-job memory spot check | MemTotal 4,020,692 KiB; MemAvailable 3,728,072 KiB; one sample, not peak |
| Outer development QEMU process | 8,290,582,528-byte peak RSS over 120 s; includes guest OS, Cube services, page cache and sandbox |
| Outer VM/source directory | 12,627,124,224 allocated bytes after pilot; includes two built templates and development artifacts |

The 4-GiB job allocation had substantial headroom for this tiny case; it does not establish sufficient memory for maximum-size archives, many-page PDFs, the full wiki or concurrent jobs. No OOM occurred and the job VM was removed after completion. The SDK reported `disk_size_mb=0`, so that field is not a trustworthy capacity measurement; the 12G figure comes from template provisioning.

Current image manifest: `sha256:c80e003a8d85841ffbd5a2c95bfdb1eebbe147b01970c50f72e0434a9d5f91d4`; Docker index: `sha256:dddcd0be6bd119f62201cd20225a98775884397ed62d4cb35749de3fe6042f4d`. Reuse the same image for log/image/PDF scenarios and tune template resources after representative measurements. Keep the local runtime role/skill files alongside the worker; they are transferred into each job, not baked into the image.
