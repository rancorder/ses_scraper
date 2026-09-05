from __future__ import annotations

from pathlib import Path

from .drive_client import DriveClient


class OutputManager:
    def __init__(self, drive: DriveClient):
        self.drive = drive

    def upload(self, output_file: Path, output_folder_id: str) -> str:
        if not output_file.exists() or output_file.stat().st_size == 0:
            raise RuntimeError(f"output file is missing or empty: {output_file}")
        return self.drive.upload_file(output_file, output_folder_id)
