from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path


class ScraperRunner:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.legacy_output_dir = repo_root / "company_analyzer" / "output"

    def _run(self, args: list[str], log_file: Path) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as log:
            proc = subprocess.run(
                args,
                cwd=self.repo_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}")

    def execute(
        self,
        input_file: Path,
        profile: str,
        output_dir: Path,
        log_file: Path,
        batch_size: int = 200,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = input_file.stem

        # batch_run.py catches per-batch exceptions and can still exit 0.
        # Calculate the expected batch count up front so partial output can never be
        # promoted to COMPLETED by the orchestration layer.
        from batch_run import read_file

        companies, _, _ = read_file(str(input_file), "企業ホームページURL")
        if not companies:
            raise RuntimeError("input contains no valid company URLs")
        expected_batches = math.ceil(len(companies) / batch_size)

        self._run(
            [
                sys.executable,
                "batch_run.py",
                "--file",
                str(input_file),
                "--profile",
                profile,
                "--batch",
                str(batch_size),
            ],
            log_file,
        )

        batch_files = sorted(self.legacy_output_dir.glob(f"*{prefix}*batch*.xlsx"))
        if len(batch_files) != expected_batches:
            raise RuntimeError(
                f"incomplete batch output: expected {expected_batches}, found {len(batch_files)}"
            )

        for source in batch_files:
            destination = output_dir / source.name
            shutil.move(str(source), str(destination))

        merged_name = f"{profile}_{prefix}_統合結果.xlsx"
        self._run(
            [
                sys.executable,
                "merge_results.py",
                "--prefix",
                prefix,
                "--output",
                merged_name,
                "--output-dir",
                str(output_dir),
            ],
            log_file,
        )

        merged = output_dir / merged_name
        if not merged.exists() or merged.stat().st_size == 0:
            raise RuntimeError(f"merged output was not created: {merged}")
        return merged
