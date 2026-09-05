from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from datetime import datetime
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

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def execute(
        self,
        input_file: Path,
        profile: str,
        output_dir: Path,
        log_file: Path,
        batch_size: int = 200,
        checkpoint_size: int = 1,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = input_file.stem

        from batch_run import read_file

        companies, _, _ = read_file(str(input_file), "企業ホームページURL")
        if not companies:
            raise RuntimeError("input contains no valid company URLs")

        checkpoint_size = max(1, checkpoint_size)
        total = len(companies)
        expected_batches = math.ceil(total / checkpoint_size)
        checkpoint_file = output_dir / "checkpoint.json"

        completed_batches = 0
        for batch_num in range(1, expected_batches + 1):
            start = (batch_num - 1) * checkpoint_size
            count = min(checkpoint_size, total - start)
            destination = output_dir / f"{prefix}_batch{batch_num:03d}.xlsx"

            # A completed checkpoint is durable. After VPS/Python restart, skip it.
            if destination.exists() and destination.stat().st_size > 0:
                completed_batches += 1
                self._atomic_json(
                    checkpoint_file,
                    {
                        "status": "RUNNING",
                        "total_companies": total,
                        "checkpoint_size": checkpoint_size,
                        "completed_batches": completed_batches,
                        "completed_count": min(completed_batches * checkpoint_size, total),
                        "next_index": min(start + count, total),
                        "last_company": companies[start + count - 1].get("name", ""),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                continue

            legacy_target = self.legacy_output_dir / destination.name
            if legacy_target.exists():
                # Previous subprocess may have finished just before a crash.
                shutil.move(str(legacy_target), str(destination))
                completed_batches += 1
            else:
                self._run(
                    [
                        sys.executable,
                        "agent/checkpoint_batch.py",
                        "--file",
                        str(input_file),
                        "--profile",
                        profile,
                        "--start",
                        str(start),
                        "--count",
                        str(count),
                        "--output-prefix",
                        prefix,
                        "--batch-num",
                        str(batch_num),
                    ],
                    log_file,
                )

                produced = sorted(
                    self.legacy_output_dir.glob(f"*{prefix}_batch{batch_num:03d}.xlsx")
                )
                if len(produced) != 1:
                    raise RuntimeError(
                        f"checkpoint output missing/ambiguous for batch {batch_num}: "
                        f"found {len(produced)}"
                    )
                shutil.move(str(produced[0]), str(destination))
                completed_batches += 1

            self._atomic_json(
                checkpoint_file,
                {
                    "status": "RUNNING",
                    "total_companies": total,
                    "checkpoint_size": checkpoint_size,
                    "completed_batches": completed_batches,
                    "completed_count": min(start + count, total),
                    "next_index": min(start + count, total),
                    "last_company": companies[start + count - 1].get("name", ""),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )

        batch_files = sorted(output_dir.glob(f"{prefix}_batch*.xlsx"))
        if len(batch_files) != expected_batches:
            raise RuntimeError(
                f"incomplete checkpoint output: expected {expected_batches}, found {len(batch_files)}"
            )

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

        self._atomic_json(
            checkpoint_file,
            {
                "status": "COMPLETED",
                "total_companies": total,
                "checkpoint_size": checkpoint_size,
                "completed_batches": expected_batches,
                "completed_count": total,
                "next_index": total,
                "last_company": companies[-1].get("name", ""),
                "merged_output": str(merged),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        return merged
