from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from profile_loader import load_profile

from .drive_client import DriveClient
from .input_resolver import resolve_drive_url
from .output_manager import OutputManager
from .scraper_runner import ScraperRunner


@dataclass
class JobResult:
    job_id: str
    status: str
    profile: str
    input_file: str | None = None
    output_file: str | None = None
    output_drive_url: str | None = None
    error: str | None = None


class Orchestrator:
    def __init__(
        self,
        repo_root: Path,
        work_root: Path,
        drive_remote: str = "gdrive",
        batch_size: int = 200,
    ):
        self.repo_root = repo_root
        self.work_root = work_root
        self.drive = DriveClient(drive_remote)
        self.runner = ScraperRunner(repo_root)
        self.output_manager = OutputManager(self.drive)
        self.batch_size = batch_size

    def run(
        self,
        *,
        profile: str,
        drive_url: str | None = None,
        local_file: str | None = None,
        output_folder_url: str | None = None,
        upload: bool = True,
    ) -> JobResult:
        if bool(drive_url) == bool(local_file):
            raise ValueError("exactly one of drive_url or local_file must be provided")
        if upload and not output_folder_url:
            raise ValueError("output_folder_url is required when upload is enabled")

        now = datetime.now()
        job_id = f"JOB-{now:%Y%m%d-%H%M%S}"
        workspace = self.work_root / job_id
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        log_file = workspace / "logs" / "execution.log"
        for directory in (input_dir, output_dir, log_file.parent):
            directory.mkdir(parents=True, exist_ok=True)

        result = JobResult(job_id=job_id, status="CREATED", profile=profile)
        job_file = workspace / "job.json"

        def persist() -> None:
            job_file.write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        persist()
        try:
            load_profile(profile)

            output_resource = None
            if upload:
                output_resource = resolve_drive_url(output_folder_url or "")
                if output_resource.resource_type != "folder":
                    raise ValueError("output-folder-url must be a Google Drive folder URL")

            result.status = "DOWNLOADING" if drive_url else "READY"
            persist()

            if drive_url:
                source = resolve_drive_url(drive_url)
                downloaded = self.drive.download(source, input_dir)
            else:
                source_path = Path(local_file or "").expanduser().resolve()
                if not source_path.exists() or not source_path.is_file():
                    raise FileNotFoundError(f"local input file not found: {source_path}")
                downloaded = input_dir / source_path.name
                shutil.copy2(source_path, downloaded)

            if downloaded.is_dir():
                candidates = sorted(
                    [p for p in downloaded.rglob("*") if p.suffix.lower() in {".xlsx", ".xls", ".csv"}],
                    key=lambda p: p.name,
                )
                if len(candidates) != 1:
                    raise RuntimeError(
                        f"folder input must contain exactly one supported spreadsheet; found {len(candidates)}"
                    )
                downloaded = candidates[0]

            if downloaded.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                raise RuntimeError(f"unsupported input format: {downloaded.suffix}")

            job_input = input_dir / f"{job_id}_{downloaded.name}"
            if downloaded.resolve() != job_input.resolve():
                shutil.move(str(downloaded), str(job_input))
            result.input_file = str(job_input)
            result.status = "RUNNING"
            persist()

            merged = self.runner.execute(
                input_file=job_input,
                profile=profile,
                output_dir=output_dir,
                log_file=log_file,
                batch_size=self.batch_size,
            )
            result.output_file = str(merged)

            if upload:
                result.status = "UPLOADING"
                persist()
                result.output_drive_url = self.output_manager.upload(
                    merged,
                    output_resource.resource_id,
                )

            result.status = "COMPLETED"
            persist()
            return result
        except Exception as exc:
            result.status = "FAILED"
            result.error = str(exc)
            persist()
            return result
