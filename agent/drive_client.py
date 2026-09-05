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

    def list_folder_sources(self, folder_id: str) -> list[dict[str, str]]:
        """Return supported source spreadsheets directly under a Drive folder.

        Generated merged result files are deliberately excluded so the same Drive
        folder can be reused as both input and output without re-enqueueing results.
        """
        raw = self._run(
            "lsjson",
            f"{self.remote}:",
            "--drive-root-folder-id",
            folder_id,
            "--files-only",
        )
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("failed to parse rclone lsjson output for Drive folder") from exc

        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return []

        supported = {".csv", ".xlsx", ".xls"}
        sources: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name") or item.get("Path") or "").strip()
            file_id = str(item.get("ID") or "").strip()
            if not name or not file_id:
                continue
            if Path(name).suffix.lower() not in supported:
                continue
            if "_統合結果" in Path(name).stem:
                continue
            sources.append(
                {
                    "id": file_id,
                    "name": name,
                    "mod_time": str(item.get("ModTime") or ""),
                }
            )

        # Oldest first is deterministic and matches the default FIFO expectation.
        sources.sort(key=lambda item: (item["mod_time"], item["name"]))
        return sources

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

    def _find_uploaded_file_id(self, file_name: str, folder_id: str) -> str | None:
        """Resolve an uploaded file ID by listing the target folder.

        Listing the folder is more stable across rclone versions than calling
        lsjson against a single file path, whose output shape can vary.
        """
        raw = self._run(
            "lsjson",
            f"{self.remote}:",
            "--drive-root-folder-id",
            folder_id,
            "--files-only",
        )
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("failed to parse rclone lsjson output after upload") from exc

        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return None

        matches = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("Name") or item.get("Path")
            if name == file_name:
                matches.append(item)

        if not matches:
            return None

        # Prefer the newest matching item if duplicate names somehow exist.
        matches.sort(key=lambda item: str(item.get("ModTime") or ""), reverse=True)
        file_id = matches[0].get("ID")
        return str(file_id) if file_id else None

    def upload_file(self, local_file: Path, folder_id: str) -> str:
        self._run(
            "copyto",
            str(local_file),
            f"{self.remote}:{local_file.name}",
            "--drive-root-folder-id",
            folder_id,
        )

        file_id = self._find_uploaded_file_id(local_file.name, folder_id)
        if not file_id:
            raise RuntimeError(
                f"uploaded file was not found in Drive folder after copy: {local_file.name}"
            )

        return f"https://drive.google.com/file/d/{file_id}/view"
