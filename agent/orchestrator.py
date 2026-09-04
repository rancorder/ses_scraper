from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from profile_loader import load_profile

from .drive_client import DriveClient
from .input_resolver import resolve_drive_url
from .output_manager import OutputManager
from .scraper_runner import ScraperRunner

T = TypeVar("T")


@dataclass
class JobResult:
    job_id: str
    status: str
    profile: str
    input_file: str | None = None
    output_file: str | None = None
    output_drive_url: str | None = None
    error: str | None = None
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None


class AgentLock:
    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.acquired = False

    def __enter__(self) -> "AgentLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force and self.path.exists():
            self.path.unlink()

        payload = json.dumps(
            {
                "pid": os.getpid(),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            try:
                existing = self.path.read_text(encoding="utf-8")
            except Exception:
                existing = "(unreadable lock)"
            raise RuntimeError(
                f"agent is already locked: {self.path}\n{existing}\n"
                "Use --force only after confirming no other job is running."
            ) from exc

        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class Orchestrator:
    def __init__(
        self,
        repo_root: Path,
        work_root: Path,
        drive_remote: str = "gdrive",
        batch_size: int = 200,
        retries: int = 1,
        retry_delay_seconds: int = 5,
    ):
        self.repo_root = repo_root
        self.work_root = work_root
        self.drive = DriveClient(drive_remote)
        self.runner = ScraperRunner(repo_root)
        self.output_manager = OutputManager(self.drive)
        self.batch_size = batch_size
        self.retries = max(0, retries)
        self.retry_delay_seconds = max(0, retry_delay_seconds)

    def _with_retry(self, action: Callable[[], T], result: JobResult) -> T:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 2):
            result.attempts = attempt
            try:
                return action()
            except Exception as exc:
                last_exc = exc
                if attempt > self.retries:
                    raise
                time.sleep(self.retry_delay_seconds)
        assert last_exc is not None
        raise last_exc

    def _append_history(self, result: JobResult) -> None:
        history_file = self.work_root / "jobs.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")

    def run(
        self,
        *,
        profile: str,
        drive_url: str | None = None,
        local_file: str | None = None,
        output_folder_url: str | None = None,
        upload: bool = True,
        force: bool = False,
    ) -> JobResult:
        if bool(drive_url) == bool(local_file):
            raise ValueError("exactly one of drive_url or local_file must be provided")
        if upload and not output_folder_url:
            raise ValueError("output_folder_url is required when upload is enabled")

        now = datetime.now()
        job_id = f"JOB-{now:%Y%m%d-%H%M%S-%f}"
        workspace = self.work_root / job_id
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        log_file = workspace / "logs" / "execution.log"
        for directory in (input_dir, output_dir, log_file.parent):
            directory.mkdir(parents=True, exist_ok=True)

        result = JobResult(
            job_id=job_id,
            status="CREATED",
            profile=profile,
            started_at=now.isoformat(timespec="seconds"),
        )
        job_file = workspace / "job.json"

        def persist() -> None:
            job_file.write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        persist()
        try:
            with AgentLock(self.work_root / ".agent.lock", force=force):
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
                    downloaded = self._with_retry(
                        lambda: self.drive.download(source, input_dir), result
                    )
                else:
                    source_path = Path(local_file or "").expanduser().resolve()
                    if not source_path.exists() or not source_path.is_file():
                        raise FileNotFoundError(f"local input file not found: {source_path}")
                    downloaded = input_dir / source_path.name
                    shutil.copy2(source_path, downloaded)
                    result.attempts = 1

                if downloaded.is_dir():
                    candidates = sorted(
                        [
                            p
                            for p in downloaded.rglob("*")
                            if p.suffix.lower() in {".xlsx", ".xls", ".csv"}
                        ],
                        key=lambda p: p.name,
                    )
                    if len(candidates) != 1:
                        raise RuntimeError(
                            "folder input must contain exactly one supported spreadsheet; "
                            f"found {len(candidates)}"
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

                merged = self._with_retry(
                    lambda: self.runner.execute(
                        input_file=job_input,
                        profile=profile,
                        output_dir=output_dir,
                        log_file=log_file,
                        batch_size=self.batch_size,
                    ),
                    result,
                )
                result.output_file = str(merged)

                if upload:
                    result.status = "UPLOADING"
                    persist()
                    result.output_drive_url = self._with_retry(
                        lambda: self.output_manager.upload(
                            merged,
                            output_resource.resource_id,
                        ),
                        result,
                    )

                result.status = "COMPLETED"
                result.error = None
        except Exception as exc:
            result.status = "FAILED"
            result.error = str(exc)
        finally:
            result.finished_at = datetime.now().isoformat(timespec="seconds")
            persist()
            self._append_history(result)

        return result
