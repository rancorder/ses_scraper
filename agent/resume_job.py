from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.input_resolver import resolve_drive_url
from agent.orchestrator import AgentLock
from agent.output_manager import OutputManager
from agent.drive_client import DriveClient
from agent.scraper_runner import ScraperRunner


def load_settings() -> dict:
    path = _REPO_ROOT / "config" / "agent.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume an interrupted SES Agent job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-folder-url", default=None)
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove stale agent lock after confirming the old process is no longer running",
    )
    args = parser.parse_args()

    settings = load_settings()
    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    workspace = work_root / args.job_id
    job_file = workspace / "job.json"
    if not job_file.exists():
        print(json.dumps({"error": f"job not found: {args.job_id}"}, ensure_ascii=False, indent=2))
        return 1

    job = json.loads(job_file.read_text(encoding="utf-8"))
    input_file = Path(job.get("input_file") or "")
    profile = job.get("profile")
    if not input_file.exists() or not profile:
        print(
            json.dumps(
                {"error": "job cannot be resumed: preserved input_file/profile missing"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    output_dir = workspace / "output"
    log_file = workspace / "logs" / "execution.log"
    runner = ScraperRunner(_REPO_ROOT)
    drive = DriveClient(settings.get("drive_remote", "gdrive"))
    output_manager = OutputManager(drive)

    try:
        with AgentLock(work_root / ".agent.lock", force=args.force):
            job["status"] = "RUNNING"
            job["error"] = None
            job["finished_at"] = None
            atomic_json(job_file, job)

            merged = runner.execute(
                input_file=input_file,
                profile=profile,
                output_dir=output_dir,
                log_file=log_file,
                batch_size=int(settings.get("batch_size", 200)),
                checkpoint_size=int(settings.get("checkpoint_size", 1)),
            )
            job["output_file"] = str(merged)

            output_url = args.output_folder_url
            if not args.no_upload:
                if not output_url:
                    raise RuntimeError("--output-folder-url is required unless --no-upload is used")
                resource = resolve_drive_url(output_url)
                if resource.resource_type != "folder":
                    raise RuntimeError("output-folder-url must be a Google Drive folder URL")
                job["status"] = "UPLOADING"
                atomic_json(job_file, job)
                job["output_drive_url"] = output_manager.upload(merged, resource.resource_id)

            job["status"] = "COMPLETED"
            job["error"] = None
    except Exception as exc:
        job["status"] = "FAILED"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_json(job_file, job)

    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0 if job.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
