"""Boot a configured profile and verify runtime assets without a model/API job.

Run from the repo: uv run --env-file .env python scripts/check_sandbox_runtime.py small
The disposable VM receives synthetic input and no model credentials. It is
destroyed on completion or failure. This is not a workload performance benchmark.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.resources import PROFILES
from backend.worker import CubeWorker, WorkerConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    args = parser.parse_args()
    worker = CubeWorker(WorkerConfig.from_env())
    Sandbox, config = worker._cube(180, args.profile)
    sandbox = Sandbox.create(config=config, timeout=180)
    try:
        info = sandbox.get_info()
        expected = PROFILES[args.profile]
        for key in ("cpu_milli", "memory_mb"):
            if getattr(info, key) != expected[key]:
                raise RuntimeError(f"Template {key} differs from the reserved profile")
        worker._ensure_dir(sandbox, "/workspace")
        worker._write_tree(sandbox, worker.config.runtime_dir, "/workspace")
        worker._ensure_dir(sandbox, "/workspace/wiki")
        sandbox.files.write("/workspace/wiki/fixture.md", "# Synthetic test\nMotor diagnostic context only.\n")
        job = {"id": "offline-runtime-check", "description": "Synthetic context check; no model run.",
               "language": "en", "files": [], "resource_plan": {"profile": args.profile, **expected}}
        sandbox.files.write("/workspace/job.json", json.dumps(job))
        command = """python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
for name in ('DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ROBOT_WORKER_TOKEN'):
    assert not os.environ.get(name), 'Unexpected credential in offline check'
for name in ('robot-analysis-context', 'robot-evidence', 'pdf-evidence'):
    assert Path('/workspace/.agents/skills', name, 'SKILL.md').is_file(), name
subprocess.run(['python3', '/workspace/evidence.py', 'index'], check=True, capture_output=True)
context = json.loads(subprocess.check_output(['python3', '/workspace/evidence.py', 'context']))
assert context['wiki_indexed_pages'] == 1
assert context['job_id'] == 'offline-runtime-check'
environment = json.loads(subprocess.check_output(['python3', '/opt/sandbox/check_environment.py']))
print(json.dumps({'context': context, 'environment': environment}))
subprocess.run(['df', '-m', '/workspace'], check=True)
PY"""
        result = sandbox.commands.run(command, cwd="/workspace", timeout=90)
        if result.exit_code:
            raise RuntimeError(f"Sandbox check failed ({result.exit_code}): {result.stderr[:1000]}")
        print(result.stdout)
    finally:
        sandbox.kill()


if __name__ == "__main__":
    main()
