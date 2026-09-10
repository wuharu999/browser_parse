#!/usr/bin/env python3
"""Seed three immutable-looking synthetic examples without entering the work queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.store import Store, stamp  # noqa: E402


def _report(summary: str, evidence: list[dict[str, str]], workflow: list[str], uncertainties: list[str]) -> str:
    return json.dumps(
        {
            "schemaVersion": "robot-analysis/v1",
            "summary": summary,
            "evidenceChain": evidence,
            "workflow": workflow,
            "uncertainties": uncertainties,
            "demo": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


DEMOS = (
    {
        "id": "demo-motor",
        "description": "[DEMO] Synthetic motor timeout followed by controller recovery",
        "language": "en",
        "report": _report(
            "The synthetic trace shows a motor command timeout followed by a successful controller reset and resumed motion.",
            [
                {"id": "E1", "observation": "Motor 2 stopped acknowledging commands for 1.8 seconds.", "source": "synthetic_motor.log", "lines": "12-16", "excerpt": "motor=2 command timeout elapsed_ms=1800", "reasoning": "The timeout record establishes the failure interval and affected actuator."},
                {"id": "E2", "observation": "A controller reset completed before acknowledgements resumed.", "source": "synthetic_motor.log", "lines": "17-24", "excerpt": "reset complete; motor=2 ack restored", "reasoning": "The ordered reset and acknowledgement records support recovery, but not the timeout's root cause."},
            ],
            ["1. Verify the command and acknowledgement timestamps around the timeout [E1].", "2. Inspect controller reset telemetry and confirm acknowledgements remain stable after recovery [E2]."],
            ["The synthetic excerpt has no bus-voltage or thermal telemetry, so electrical and thermal causes cannot be separated."],
        ),
    },
    {
        "id": "demo-localization",
        "description": "[DEMO] Synthetic localization drift with unresolved sensor cause",
        "language": "zh",
        "report": _report(
            "合成日志显示定位残差持续增大，但现有证据无法区分激光雷达遮挡与轮速里程计打滑。",
            [
                {"id": "E1", "observation": "定位残差在八秒内从0.08米升至0.74米。", "source": "synthetic_localization.log", "lines": "30-38", "excerpt": "residual_m=0.08 ... residual_m=0.74", "reasoning": "连续增长支持定位质量恶化，而不是单帧离群。"},
                {"id": "E2", "observation": "同一时段轮速与激光匹配质量均出现异常标记。", "source": "synthetic_localization.log", "lines": "39-44", "excerpt": "wheel_slip=possible lidar_match=low", "reasoning": "两个候选原因同时存在，因此不能仅凭该日志确定根因。"},
            ],
            ["1. 对齐残差增长区间与传感器时间戳 [E1]。", "2. 分别复核轮速打滑指标和激光点云匹配质量，保留两个假设直到获得独立证据 [E2]。"],
            ["缺少原始点云和IMU数据。", "合成日志未记录地面材质与遮挡情况。"],
        ),
    },
    {
        "id": "demo-incomplete",
        "description": "[DEMO] Synthetic safety-stop analysis with incomplete log coverage",
        "language": "en",
        "report": _report(
            "A synthetic safety-stop event is visible, but the supplied log begins after the initiating condition and cannot establish causality.",
            [
                {"id": "E1", "observation": "The first available record already has the safety state latched.", "source": "synthetic_partial.log", "lines": "1-3", "excerpt": "capture_start safety_stop=latched", "reasoning": "This confirms the stopped state but shows that the trigger predates available coverage."},
                {"id": "E2", "observation": "The manifest marks the preceding 45 seconds as missing.", "source": "synthetic_manifest.json", "lines": "4-7", "excerpt": "missing_before_start_seconds: 45", "reasoning": "The missing interval contains the likely initiating event, preventing a supported root-cause conclusion."},
            ],
            ["1. Treat the current data as confirmation of a latched stop only [E1].", "2. Recover the preceding controller and safety-I/O logs before testing any causal hypothesis [E2]."],
            ["The initiating event is outside supplied coverage.", "No reset attempt or post-stop motion is present."],
        ),
    },
)


def seed(db_path: str | Path, uploads: str | Path) -> int:
    store = Store(str(db_path), str(uploads))
    inserted = 0
    created = stamp()
    finished_day = datetime.now(UTC).date().isoformat()
    metrics = json.dumps({"cost_source": "synthetic-demo", "demo": True}, separators=(",", ":"))
    try:
        with store.lock:
            store._tx()
            for demo in DEMOS:
                cursor = store.db.execute(
                    """INSERT OR IGNORE INTO jobs(
                       id,description,language,evidence,status,token_hash,created_at,updated_at,
                       submitted_at,report,metrics,cost_usd,reservation,finished_at,finished_day
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        demo["id"], demo["description"], demo["language"],
                        json.dumps({"demo": True, "synthetic": True}, separators=(",", ":")),
                        "completed", hashlib.sha256(f"synthetic:{demo['id']}".encode()).hexdigest(),
                        created, created, created, demo["report"], metrics, 0.0, 0.0, created, finished_day,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                    store._event(demo["id"], "system", "demo-seeder", "Synthetic demo seeded; no agent or model was run.")
            store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    finally:
        store.db.close()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed three completed synthetic demo analyses.")
    parser.add_argument("--db", default="data/jobs.sqlite3")
    parser.add_argument("--uploads", default="data/uploads")
    args = parser.parse_args()
    inserted = seed(args.db, args.uploads)
    print(f"Seeded {inserted} demo job(s); existing demo jobs were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
