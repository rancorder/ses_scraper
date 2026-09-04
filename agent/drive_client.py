from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .input_resolver import DriveResource


class DriveClient:
    def __init__(self, remote: str = "gdrive"):
        self.remote = remote.rstrip(":")
        if shutil.which("rclone") is None:
            raise RuntimeError("rclone command was not found on this host")

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            ["rclone", *args],
            check=False,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "rclone failed")
        return proc.stdout.strip()

    def download(self, resource: DriveResource, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)

        if resource.resource_type == "folder":
            target = destination / "source"
            target.mkdir(parents=True, exist_ok=True)
            self._run(
                "copy",
                f"{self.remote}:",
                str(target),
                "--drive-root-folder-id",
                resource.resource_id,
                "--drive-export-formats",
                "xlsx,csv",
            )
            return target

        before = {p.name for p in destination.iterdir() if p.is_file()}
        self._run(
            "backend",
            "copyid",
            f"{self.remote}:",
            resource.resource_id,
            f"{destination}/",
            "--drive-export-formats",
            "xlsx,csv",
        )
        files = [p for p in destination.iterdir() if p.is_file() and p.name not in before]
        if not files:
            files = [p for p in destination.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError(f"Drive resource {resource.resource_id} was not downloaded")
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0]

    def upload_file(self, local_file: Path, folder_id: str) -> str:
        self._run(
            "copyto",
            str(local_file),
            f"{self.remote}:{local_file.name}",
            "--drive-root-folder-id",
            folder_id,
        )
        raw = self._run(
            "lsjson",
            f"{self.remote}:{local_file.name}",
            "--drive-root-folder-id",
            folder_id,
        )
        try:
            items = json.loads(raw)
            item = items[0] if isinstance(items, list) and items else items
            file_id = item.get("ID") if isinstance(item, dict) else None
        except json.JSONDecodeError:
            file_id = None
        if file_id:
            return f"https://drive.google.com/file/d/{file_id}/view"
        return f"https://drive.google.com/drive/folders/{folder_id}"
