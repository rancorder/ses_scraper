from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.orchestrator import Orchestrator

_STOP = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    global _STOP
    _STOP = True


def load_settings() -> dict:
    path = _REPO_ROOT / "config" / "agent.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def clear_stale_agent_lock(work_root: Path) -> None:
    lock = work_root / ".agent.lock"
    if not lock.exists():
        return
    try:
        payload = load_json(lock)
        pid = int(payload.get("pid") or 0)
    except Exception:
        pid = 0
    if not pid_alive(pid):
        lock.unlink(missing_ok=True)


def write_heartbeat(work_root: Path, *, status: str, current_job_id: str | None) -> None:
    atomic_json(
        work_root / "worker_heartbeat.json",
        {
            "pid": os.getpid(),
            "status": status,
            "current_job_id": current_job_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def move_item(path: Path, target_dir: Path, item: dict) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / path.name
    atomic_json(path, item)
    path.replace(destination)
    return destination


def resume_existing(item: dict, settings: dict, work_root: Path) -> dict:
    clear_stale_agent_lock(work_root)
    cmd = [
        sys.executable,
        "agent/resume_job.py",
        "--job-id",
        item["job_id"],
        "--output-folder-url",
        item["output_folder_url"],
    ]
    proc = subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout.strip():
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
    return {
        "job_id": item["job_id"],
        "status": "FAILED",
        "error": proc.stderr.strip() or proc.stdout.strip() or f"resume exited {proc.returncode}",
    }


def run_new(item: dict, settings: dict, work_root: Path) -> dict:
    orchestrator = Orchestrator(
        repo_root=_REPO_ROOT,
        work_root=work_root,
        drive_remote=settings.get("drive_remote", "gdrive"),
        batch_size=int(settings.get("batch_size", 200)),
        checkpoint_size=int(settings.get("checkpoint_size", 1)),
        retries=int(settings.get("retries", 1)),
        retry_delay_seconds=int(settings.get("retry_delay_seconds", 5)),
    )
    result = orchestrator.run(
        drive_url=item["drive_url"],
        profile=item["profile"],
        output_folder_url=item["output_folder_url"],
        upload=True,
        job_id=item["job_id"],
    )
    return result.__dict__


def process_item(path: Path, dirs: dict[str, Path], settings: dict, work_root: Path) -> None:
    item = load_json(path)
    item["status"] = "RUNNING"
    item["started_at"] = item.get("started_at") or datetime.now().isoformat(timespec="seconds")
    item["error"] = None
    atomic_json(path, item)
    write_heartbeat(work_root, status="RUNNING", current_job_id=item["job_id"])

    workspace = work_root / item["job_id"]
    job_file = workspace / "job.json"
    try:
        if job_file.exists():
            job = load_json(job_file)
            if job.get("status") == "COMPLETED":
                result = job
            else:
                result = resume_existing(item, settings, work_root)
        else:
            result = run_new(item, settings, work_root)

        item["result"] = result
        item["finished_at"] = datetime.now().isoformat(timespec="seconds")
        result_status = result.get("status")
        if result_status == "COMPLETED":
            item["status"] = "COMPLETED"
            item["error"] = None
            move_item(path, dirs["completed"], item)
        else:
            item["status"] = "FAILED"
            item["error"] = result.get("error") or f"job ended with {result_status}"
            move_item(path, dirs["failed"], item)
    except Exception as exc:
        item["status"] = "FAILED"
        item["error"] = str(exc)
        item["finished_at"] = datetime.now().isoformat(timespec="seconds")
        move_item(path, dirs["failed"], item)
    finally:
        write_heartbeat(work_root, status="IDLE", current_job_id=None)


def recover_running(dirs: dict[str, Path]) -> None:
    """Keep interrupted queue items in running; they are resumed before new pending work."""
    dirs["running"].mkdir(parents=True, exist_ok=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = load_settings()
    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    queue_root = work_root / "queue"
    dirs = {name: queue_root / name for name in ("pending", "running", "completed", "failed")}
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)

    poll_seconds = max(1, int(settings.get("worker_poll_seconds", 5)))
    clear_stale_agent_lock(work_root)
    recover_running(dirs)
    write_heartbeat(work_root, status="IDLE", current_job_id=None)

    while not _STOP:
        running = sorted(dirs["running"].glob("*.json"))
        if running:
            path = running[0]
            process_item(path, dirs, settings, work_root)
            continue

        pending = sorted(dirs["pending"].glob("*.json"))
        if not pending:
            write_heartbeat(work_root, status="IDLE", current_job_id=None)
            time.sleep(poll_seconds)
            continue

        source = pending[0]
        destination = dirs["running"] / source.name
        source.replace(destination)
        process_item(destination, dirs, settings, work_root)

    write_heartbeat(work_root, status="STOPPED", current_job_id=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
