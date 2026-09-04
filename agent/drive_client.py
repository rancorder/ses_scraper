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
            self._run("copy", f"{self.remote}:{{{resource.resource_id}}}", str(target), "--drive-export-formats", "xlsx,csv")
            return target

        # rclone supports Drive's root-folder-id/file-id syntax through backend commands inconsistently
        # across versions. We therefore resolve the object path by ID first, then copy/export it.
        metadata_raw = self._run("backend", "copyid", f"{self.remote}:", resource.resource_id, str(destination), "--json")
        try:
            metadata = json.loads(metadata_raw) if metadata_raw else {}
        except json.JSONDecodeError:
            metadata = {}

        files = [p for p in destination.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError(f"Drive resource {resource.resource_id} was not downloaded")
        if len(files) > 1:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0]

    def upload_file(self, local_file: Path, folder_id: str) -> None:
        self._run("copyto", str(local_file), f"{self.remote}:{{{folder_id}}}/{local_file.name}")

    def file_url(self, folder_id: str, file_name: str) -> str:
        # Public/shareable URL generation depends on Drive permissions; return a stable folder pointer.
        return f"https://drive.google.com/drive/folders/{folder_id}"
