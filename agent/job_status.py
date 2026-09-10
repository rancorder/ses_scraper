from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def load_settings() -> dict:
    path = _REPO_ROOT / "config" / "agent.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_history(history_file: Path) -> list[dict]:
    if not history_file.exists():
        return []

    rows: list[dict] = []
    with history_file.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid JSON in history file {history_file} line {line_no}: {exc}"
                ) from exc
    return rows


def load_job_file(work_root: Path, job_id: str) -> dict | None:
    path = work_root / job_id / "job.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect SES Agent job status/history")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--latest", action="store_true", help="show the latest terminal job")
    mode.add_argument("--job-id", help="show one job by ID")
    mode.add_argument("--failed", action="store_true", help="show recent failed jobs")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum rows for --failed (default: 10)",
    )
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be 1 or greater")

    settings = load_settings()
    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    history_file = work_root / "jobs.jsonl"
    rows = load_history(history_file)

    if args.job_id:
        live = load_job_file(work_root, args.job_id)
        if live is not None:
            print_json(live)
            return 0

        for row in reversed(rows):
            if row.get("job_id") == args.job_id:
                print_json(row)
                return 0

        print_json({"error": f"job not found: {args.job_id}"})
        return 1

    if args.latest:
        if not rows:
            print_json({"error": "no job history found"})
            return 1
        print_json(rows[-1])
        return 0

    failed = [row for row in rows if row.get("status") == "FAILED"]
    if not failed:
        print_json([])
        return 0

    print_json(failed[-args.limit :][::-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
