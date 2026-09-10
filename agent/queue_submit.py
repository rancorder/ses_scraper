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

from agent.drive_client import DriveClient
from agent.input_resolver import resolve_drive_url


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


def queue_filename(priority: str, created_at: str, job_id: str) -> str:
    rank = {"high": "0", "normal": "1", "low": "2"}[priority]
    stamp = created_at.replace("-", "").replace(":", "").replace("T", "-").replace(".", "")
    return f"{rank}_{stamp}_{job_id}.json"


def enqueue_source(
    *,
    pending_dir: Path,
    source: dict[str, str],
    profile: str,
    output_folder_url: str,
    priority: str = "normal",
) -> dict:
    now = datetime.now()
    job_id = f"JOB-{now:%Y%m%d-%H%M%S-%f}"
    created_at = now.isoformat(timespec="microseconds")
    item = {
        "job_id": job_id,
        "status": "QUEUED",
        "priority": priority,
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "profile": profile,
        "source_name": source.get("name", ""),
        "source_file_id": source["id"],
        "source_mod_time": source.get("mod_time", ""),
        "source_mime_type": source.get("mime_type", ""),
        "drive_url": f"https://drive.google.com/file/d/{source['id']}/view",
        "output_folder_url": output_folder_url,
        "result": None,
        "error": None,
    }
    path = pending_dir / queue_filename(priority, created_at, job_id)
    atomic_json(path, item)
    return {"job_id": job_id, "source_name": item["source_name"], "queue_file": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit SES screening list(s) to the persistent queue")
    parser.add_argument("--drive-url", required=True, help="Drive file or folder URL")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-folder-url", default=None)
    parser.add_argument("--priority", choices=["high", "normal", "low"], default="normal")
    args = parser.parse_args()

    settings = load_settings()
    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    pending_dir = work_root / "queue" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    drive = DriveClient(settings.get("drive_remote", "gdrive"))
    resource = resolve_drive_url(args.drive_url)
    output_folder_url = args.output_folder_url or settings.get("default_output_folder_url")

    sources: list[dict[str, str]] = []
    if resource.resource_type == "folder":
        if not output_folder_url:
            output_folder_url = args.drive_url
        sources = drive.list_folder_sources(resource.resource_id)
        if not sources:
            raise RuntimeError("Drive folder contains no supported source CSV/XLS/XLSX/Google Sheets files")
    else:
        if not output_folder_url:
            raise RuntimeError("--output-folder-url is required when queueing a Drive file URL")
        sources = [{"id": resource.resource_id, "name": resource.resource_id, "mod_time": "", "mime_type": ""}]

    submitted = [
        enqueue_source(
            pending_dir=pending_dir,
            source=source,
            profile=args.profile,
            output_folder_url=output_folder_url,
            priority=args.priority,
        )
        for source in sources
    ]

    print(json.dumps({"submitted": len(submitted), "items": submitted}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
