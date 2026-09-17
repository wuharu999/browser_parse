"""Docker CLI adapter for one disposable, restricted analysis container.

The trusted host worker controls Docker. Jobs receive only their own named
workspace volume and an internal network; never the Docker socket or host paths.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MIB = 1024**2
LABEL = "robot.workbench.managed=true"
PYTHON = "/opt/analysis-venv/bin/python"


class DockerError(RuntimeError):
    pass


class DockerCleanupError(DockerError):
    """A failed create may have left a container alive; retain the API lease."""


@dataclass(frozen=True)
class DockerJob:
    container: str
    volume: str


class DockerRuntime:
    def __init__(self, *, image: str, network: str, proxy_url: str, worker_id: str,
                 data_dir: Path | None, disk_reserve_mb: int, pids_limit: int = 512) -> None:
        if not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", image):
            raise DockerError("ROBOT_DOCKER_IMAGE must be a registry digest or local sha256 image ID")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", network):
            raise DockerError("ROBOT_DOCKER_NETWORK has an invalid name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", worker_id):
            raise DockerError("ROBOT_WORKER_ID has an invalid name")
        try:
            proxy = urlsplit(proxy_url)
            valid_proxy = (proxy.scheme == "http" and bool(proxy.hostname) and
                           proxy.port is not None and proxy.username is None and
                           proxy.password is None and proxy.path in {"", "/"} and
                           not proxy.query and not proxy.fragment)
        except ValueError:
            valid_proxy = False
        if not valid_proxy:
            raise DockerError("ROBOT_EGRESS_PROXY_URL must be http://internal-proxy:port without credentials")
        if disk_reserve_mb < 1 or not 16 <= pids_limit <= 4096:
            raise DockerError("invalid Docker disk reserve or PID limit")
        self.image, self.network, self.proxy_url = image, network, proxy_url
        self.worker_id = worker_id
        self.expected_data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self.data_dir: Path | None = None
        self.disk_reserve_mb, self.pids_limit = disk_reserve_mb, pids_limit
        self._info: dict = {}

    @staticmethod
    def _run(args: list[str], *, timeout: float = 30, check: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(["docker", *args], text=True, errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Never include argv or Docker output: create's environment is private.
            raise DockerError("Docker operation unavailable or timed out") from exc
        if check and result.returncode:
            raise DockerError(f"Docker {args[0]} operation failed")
        return result

    def preflight(self) -> None:
        context = os.environ.get("DOCKER_CONTEXT")
        endpoint = os.environ.get("DOCKER_HOST") if not context else None
        if not endpoint:
            args = ["context", "inspect"] + ([context] if context else [])
            endpoint = self._run([*args, "--format", "{{.Endpoints.docker.Host}}"]).stdout.strip()
        if not endpoint.startswith("unix://"):
            raise DockerError("worker requires a local Unix-socket Docker daemon")
        try:
            info = json.loads(self._run(["info", "--format", "{{json .}}"]).stdout)
            image = json.loads(self._run(["image", "inspect", self.image]).stdout)[0]
            network = json.loads(self._run(["network", "inspect", self.network]).stdout)[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise DockerError("Docker inspection returned invalid data") from exc
        if info.get("OSType") != "linux" or any(
            not info.get(key) for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
        ):
            raise DockerError("Docker daemon cannot enforce the required Linux resource limits")
        security = info.get("SecurityOptions") or []
        if not any("name=seccomp" in option for option in security):
            raise DockerError("Docker daemon must support its default seccomp profile")
        root_dir = info.get("DockerRootDir")
        if not isinstance(root_dir, str) or not root_dir.strip():
            raise DockerError("Docker daemon did not report a usable DockerRootDir")
        detected_data_dir = Path(root_dir)
        if not detected_data_dir.is_absolute() or not detected_data_dir.is_dir():
            raise DockerError("Docker daemon reported an unavailable DockerRootDir")
        detected_data_dir = detected_data_dir.resolve()
        if self.expected_data_dir is not None and self.expected_data_dir != detected_data_dir:
            raise DockerError("ROBOT_DOCKER_DATA_DIR does not match the local Docker daemon data directory")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")):
            raise DockerError("Docker image has no immutable local ID")
        self.image = image["Id"]
        if set((image.get("Config") or {}).get("Volumes") or {}) - {"/workspace"}:
            raise DockerError("job image declares unexpected writable volumes")
        options = network.get("Options") or {}
        if (network.get("Driver") != "bridge" or not network.get("Internal") or
            network.get("EnableIPv6") or
            options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"):
            raise DockerError("job network must be an internal bridge with isolated IPv4 gateway and IPv6 disabled")
        if shutil.disk_usage(detected_data_dir).free < self.disk_reserve_mb * MIB:
            raise DockerError("Docker filesystem is below the free-space reserve")
        # Do not cache partial state from a failed preflight. A caller that
        # retries must repeat every daemon/image/network validation.
        self._info = info
        self.data_dir = detected_data_dir

    def _data_dir(self) -> Path:
        if self.data_dir is None:
            raise DockerError("DockerRootDir has not been verified")
        return self.data_dir

    def available_capacity(self, maxima: dict[str, int], memory_reserve_mb: int = 1024) -> dict[str, int]:
        if not self._info:
            self.preflight()
        available = 0
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) // 1024
                break
        return {
            "cpu_milli": max(0, min(maxima["cpu_milli"], max(0, int(self._info["NCPU"]) - 1) * 1000)),
            "memory_mb": max(0, min(maxima["memory_mb"], available - memory_reserve_mb,
                                    int(self._info["MemTotal"]) // MIB - memory_reserve_mb)),
            "disk_mb": max(0, min(maxima["disk_mb"], shutil.disk_usage(self._data_dir()).free // MIB - self.disk_reserve_mb)),
        }

    def _owned(self, kind: str) -> list[str]:
        command = ["ps", "-aq"] if kind == "container" else ["volume", "ls", "-q"]
        return self._run([*command, "--filter", f"label={LABEL}",
                          "--filter", f"label=robot.workbench.worker_id={self.worker_id}"]).stdout.split()

    def cleanup_orphans(self) -> None:
        for container in self._owned("container"):
            self._run(["rm", "-f", container])
        for volume in self._owned("volume"):
            self._run(["volume", "rm", volume])
        if self._owned("container") or self._owned("volume"):
            raise DockerError("owned Docker resources remain after startup cleanup")

    def create(self, job_id: str, plan: dict[str, int], env: dict[str, str]) -> DockerJob:
        suffix = hashlib.sha256(f"{self.worker_id}:{job_id}".encode()).hexdigest()[:20] + "-" + uuid.uuid4().hex[:12]
        job = DockerJob(f"robot-job-{suffix}", f"robot-workspace-{suffix}")
        labels = ["--label", LABEL, "--label", f"robot.workbench.worker_id={self.worker_id}",
                  "--label", f"robot.workbench.job_id={job_id}"]
        try:
            self._run(["volume", "create", *labels, job.volume])
            args = ["create", "--name", job.container, *labels, "--network", self.network,
                    "--read-only", "--log-driver", "none", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--user", "10001:10001",
                    "--pids-limit", str(self.pids_limit), "--cpus", str(plan["cpu_milli"] / 1000),
                    "--memory", f"{plan['memory_mb']}m", "--memory-swap", f"{plan['memory_mb']}m",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--shm-size", "64m",
                    "--mount", f"type=volume,source={job.volume},target=/workspace",
                    "--workdir", "/workspace", "--entrypoint", "sleep"]
            values = {**env, "HOME": "/workspace", "TMPDIR": "/tmp",
                      "HTTP_PROXY": self.proxy_url, "HTTPS_PROXY": self.proxy_url,
                      "http_proxy": self.proxy_url, "https_proxy": self.proxy_url,
                      "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
            for name in values:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    raise DockerError("invalid container environment variable name")
                args += ["--env", name]
            self._run([*args, self.image, "infinity"], env={**os.environ, **values})
            self._verify(job, plan)
            return job
        except Exception as exc:
            if not self.remove(job):
                raise DockerCleanupError("Docker creation failed and cleanup was not confirmed; stop this worker") from exc
            raise

    def _verify(self, job: DockerJob, plan: dict[str, int]) -> None:
        item = json.loads(self._run(["inspect", job.container]).stdout)[0]
        host, config = item["HostConfig"], item["Config"]
        expected = {"ReadonlyRootfs": True, "Privileged": False, "Memory": plan["memory_mb"] * MIB,
                    "MemorySwap": plan["memory_mb"] * MIB, "NanoCpus": plan["cpu_milli"] * 1_000_000,
                    "PidsLimit": self.pids_limit, "NetworkMode": self.network}
        if any(host.get(key) != value for key, value in expected.items()):
            raise DockerError("Docker did not apply the requested execution limits")
        if (config.get("User") != "10001:10001" or {c.upper() for c in host.get("CapDrop", [])} != {"ALL"} or
            "no-new-privileges" not in host.get("SecurityOpt", []) or host.get("CapAdd") or
            host.get("Binds") or host.get("Devices") or host.get("PortBindings") or
            host.get("PidMode") == "host" or host.get("IpcMode") == "host"):
            raise DockerError("Docker container has unexpected privileges or host access")
        mounts = item.get("Mounts") or []
        volumes = [m for m in mounts if m["Type"] != "tmpfs"]
        if len(volumes) != 1 or volumes[0].get("Type") != "volume" or volumes[0].get("Name") != job.volume or volumes[0].get("Destination") != "/workspace":
            raise DockerError("Docker container has unexpected filesystem mounts")

    def copy_in(self, job: DockerJob, source: Path, destination: str = "/workspace") -> None:
        if not source.is_dir() or destination != "/workspace":
            raise DockerError("invalid Docker staging directory")
        # docker cp -a honors archive ownership without a root/chown process in
        # the job. Stream the archive; do not duplicate multi-GiB inputs on disk.
        process = subprocess.Popen(["docker", "cp", "-a", "-", f"{job.container}:{destination}"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        timer = threading.Timer(300, process.kill)
        timer.start()
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for path in [source, *sorted(source.rglob("*"))]:
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue
                    name = path.relative_to(source).as_posix()
                    info = archive.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = 10001
                    info.uname = info.gname = ""
                    info.mode = 0o755 if path.is_dir() else 0o644
                    if path.is_file():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)
            process.stdin.close()
            if process.wait(timeout=30):
                raise DockerError("Docker evidence transfer failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError("Docker evidence transfer failed or timed out") from exc
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()

    def snapshot_workspace(self, job: DockerJob, target_path: Path) -> Path:
        """Capture /workspace contents (including hidden files) as a .tar.gz archive."""
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        process = None
        try:
            process = subprocess.Popen(
                ["docker", "cp", f"{job.container}:/workspace/.", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            with gzip.open(tmp_target, "wb") as gz_out:
                while chunk := process.stdout.read(65536):
                    gz_out.write(chunk)
            _, stderr = process.communicate(timeout=60)
            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise DockerError(f"Docker snapshot copy failed: {err_msg}")
            tmp_target.replace(target)
            return target
        except Exception as exc:
            if tmp_target.exists():
                tmp_target.unlink(missing_ok=True)
            if isinstance(exc, DockerError):
                raise
            raise DockerError(f"Failed to create workspace snapshot: {exc}") from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def restore_workspace(self, job: DockerJob, source_tarball: Path) -> None:
        """Extract a snapshot tarball into the container's /workspace directory."""
        source = Path(source_tarball).resolve()
        if not source.is_file():
            raise DockerError(f"Snapshot archive not found: {source}")

        # Validate archive integrity and guard against path traversal (zip-slip)
        try:
            with tarfile.open(source, mode="r:*") as in_tar:
                for member in in_tar.getmembers():
                    name = member.name.replace("\\", "/")
                    parts = PurePosixPath(name).parts
                    if member.name.startswith("/") or ".." in parts:
                        raise DockerError(f"Unsafe path in snapshot archive: {member.name}")
        except Exception as exc:
            if isinstance(exc, DockerError):
                raise
            raise DockerError(f"Corrupted snapshot archive: {exc}") from exc

        process = subprocess.Popen(
            ["docker", "cp", "-a", "-", f"{job.container}:/workspace"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        timer = threading.Timer(300, process.kill)
        timer.start()
        try:
            with tarfile.open(source, mode="r:*") as in_tar:
                with tarfile.open(fileobj=process.stdin, mode="w|") as out_tar:
                    for member in in_tar.getmembers():
                        name = member.name.lstrip("./")
                        if not name:
                            continue
                        info = tarfile.TarInfo(name=name)
                        info.size = member.size
                        info.mtime = member.mtime
                        info.mode = member.mode
                        info.type = member.type
                        info.linkname = member.linkname
                        info.uid = 10001
                        info.gid = 10001
                        info.uname = ""
                        info.gname = ""
                        if member.isreg():
                            stream = in_tar.extractfile(member)
                            if stream is not None:
                                out_tar.addfile(info, stream)
                            else:
                                out_tar.addfile(info)
                        else:
                            out_tar.addfile(info)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            _, stderr = process.communicate(timeout=60)
            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise DockerError(f"Docker workspace restore failed: {err_msg}")
        except Exception as exc:
            if isinstance(exc, DockerError):
                raise
            raise DockerError(f"Docker workspace restore failed or timed out: {exc}") from exc
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()

    def start(self, job: DockerJob) -> None:
        self._run(["start", job.container])

    def exec_runner(self, job: DockerJob) -> subprocess.Popen:
        return subprocess.Popen(["docker", "exec", job.container, PYTHON,
                                 "/opt/sandbox/run_codex.py", "/workspace/job.json"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def copy_out_text(self, job: DockerJob, path: str, maximum: int = 256 * 1024) -> str | None:
        if path not in {"/workspace/result.json", "/workspace/activity.jsonl"} or not 0 < maximum <= 1024 * 1024:
            raise DockerError("invalid job output path or read limit")
        program = ("import os,stat,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); "
                   "s=os.fstat(f); assert stat.S_ISREG(s.st_mode); "
                   "assert s.st_size<=int(sys.argv[2]); sys.stdout.buffer.write(os.read(f,int(sys.argv[2])))")
        result = self._run(["exec", job.container, PYTHON, "-I", "-c", program, path, str(maximum)], check=False)
        return result.stdout if result.returncode == 0 else None

    def read_activity(self, job: DockerJob, offset: int, maximum: int = 128 * 1024) -> tuple[str, int] | None:
        """Read complete debug JSONL records after a byte cursor, without copying history."""
        if not isinstance(offset, int) or offset < 0 or not 16384 <= maximum <= 1024 * 1024:
            raise DockerError("invalid activity cursor or limit")
        program = ("import os,stat,sys; f=os.open('/workspace/codex-debug.jsonl',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); "
                   "assert stat.S_ISREG(os.fstat(f).st_mode); os.lseek(f,int(sys.argv[1]),0); "
                   "data=os.read(f,int(sys.argv[2])); end=data.rfind(b'\\n')+1; sys.stdout.buffer.write(data[:end])")
        result = self._run(["exec", job.container, PYTHON, "-I", "-c", program, str(offset), str(maximum)], check=False)
        if result.returncode:
            return None
        return result.stdout, offset + len(result.stdout.encode("utf-8"))

    def workspace_bytes(self, job: DockerJob) -> int:
        result = self._run(["exec", job.container, "/usr/bin/du", "-sb", "/workspace", "/tmp", "/dev/shm"])
        try:
            counts = [int(line.split()[0]) for line in result.stdout.splitlines()]
        except (ValueError, IndexError) as exc:
            raise DockerError("invalid Docker disk measurement") from exc
        if len(counts) != 3 or any(count < 0 for count in counts):
            raise DockerError("incomplete Docker disk measurement")
        return sum(counts)

    def disk_healthy(self, job: DockerJob, limit_mb: int) -> bool:
        return (shutil.disk_usage(self._data_dir()).free >= self.disk_reserve_mb * MIB and
                self.workspace_bytes(job) <= limit_mb * MIB)

    def _exists(self, kind: str, name: str) -> bool:
        result = self._run([kind, "inspect", name], check=False)
        if result.returncode == 0:
            return True
        if any(message in result.stderr.lower() for message in ("no such", "not found")):
            return False
        raise DockerError("could not confirm Docker resource removal")

    def remove(self, job: DockerJob) -> bool:
        try:
            if self._exists("container", job.container):
                self._run(["rm", "-f", job.container], check=False)
            if self._exists("container", job.container):
                return False
            if self._exists("volume", job.volume):
                self._run(["volume", "rm", job.volume], check=False)
            return not self._exists("volume", job.volume)
        except DockerError:
            return False
