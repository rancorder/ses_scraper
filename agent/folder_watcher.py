from __future__ import annotations

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
from agent.queue_submit import enqueue_source


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


def version_key(source: dict[str, str]) -> str:
    # Drive file ID identifies the logical file. ModTime identifies the version.
    return f"{source.get('id', '')}|{source.get('mod_time', '')}"


def existing_queue_versions(queue_root: Path) -> set[str]:
    seen: set[str] = set()
    for state in ("pending", "running", "completed", "failed"):
        directory = queue_root / state
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            file_id = str(item.get("source_file_id") or "")
            if not file_id:
                continue
            mod_time = str(item.get("source_mod_time") or "")
            # Legacy queue items did not persist source_mod_time. Keep the ID so
            # bootstrap can treat the current version as already seen once.
            seen.add(f"{file_id}|{mod_time}")
            if not mod_time:
                seen.add(f"{file_id}|*")
    return seen


def main() -> int:
    settings = load_settings()
    watch = settings.get("folder_watch") or {}
    if not watch.get("enabled", False):
        print(json.dumps({"status": "DISABLED"}, ensure_ascii=False))
        return 0

    folder_url = str(watch.get("folder_url") or "").strip()
    profile = str(watch.get("profile") or "").strip()
    output_folder_url = str(watch.get("output_folder_url") or folder_url).strip()
    priority = str(watch.get("priority") or "normal")
    if not folder_url or not profile:
        raise RuntimeError("folder_watch.folder_url and folder_watch.profile are required")

    resource = resolve_drive_url(folder_url)
    if resource.resource_type != "folder":
        raise RuntimeError("folder_watch.folder_url must be a Google Drive folder URL")

    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    queue_root = work_root / "queue"
    pending_dir = queue_root / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_root / "folder_watch_state.json"

    drive = DriveClient(settings.get("drive_remote", "gdrive"))
    sources = drive.list_folder_sources(resource.resource_id)
    queued_versions = existing_queue_versions(queue_root)

    previous: dict[str, dict] = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8")).get("files", {})
        except Exception:
            previous = {}

    submitted = []
    files_state: dict[str, dict] = dict(previous)
    for source in sources:
        file_id = source["id"]
        key = version_key(source)
        old = previous.get(file_id) or {}
        old_mod = str(old.get("mod_time") or "")

        already_queued = key in queued_versions
        legacy_seen = f"{file_id}|*" in queued_versions and not old_mod
        unchanged = old_mod == source.get("mod_time", "") and bool(old)

        if not (already_queued or legacy_seen or unchanged):
            submitted.append(
                enqueue_source(
                    pending_dir=pending_dir,
                    source=source,
                    profile=profile,
                    output_folder_url=output_folder_url,
                    priority=priority,
                )
            )

        files_state[file_id] = {
            "name": source.get("name", ""),
            "mod_time": source.get("mod_time", ""),
            "mime_type": source.get("mime_type", ""),
            "last_seen_at": datetime.now().isoformat(timespec="seconds"),
        }

    atomic_json(
        state_file,
        {
            "folder_url": folder_url,
            "profile": profile,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "files": files_state,
        },
    )

    print(
        json.dumps(
            {
                "status": "OK",
                "scanned": len(sources),
                "submitted": len(submitted),
                "items": submitted,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
