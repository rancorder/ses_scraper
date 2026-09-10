from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.orchestrator import Orchestrator


def load_settings() -> dict:
    path = _REPO_ROOT / "config" / "agent.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_job(work_root: Path, job_id: str) -> dict:
    path = work_root / job_id / "job.json"
    if not path.exists():
        raise FileNotFoundError(f"job not found: {job_id}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run a prior SES Agent job as a new local/no-upload job"
    )
    parser.add_argument("--job-id", required=True, help="source job ID")
    parser.add_argument(
        "--replacement-file",
        help="optional replacement local CSV/XLS/XLSX instead of the preserved job input",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=None,
        help="retry count for evaluation/merge",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove an existing agent lock; only after confirming no job is running",
    )
    args = parser.parse_args()

    settings = load_settings()
    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))

    try:
        source_job = load_job(work_root, args.job_id)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    profile = source_job.get("profile")
    if not profile:
        print(json.dumps({"error": "source job has no profile"}, ensure_ascii=False, indent=2))
        return 1

    local_file = args.replacement_file or source_job.get("input_file")
    if not local_file:
        print(
            json.dumps(
                {
                    "error": (
                        "source job has no preserved input_file; "
                        "use --replacement-file to provide a new input"
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    retries = args.retries if args.retries is not None else int(settings.get("retries", 1))
    if retries < 0:
        parser.error("--retries must be 0 or greater")

    orchestrator = Orchestrator(
        repo_root=_REPO_ROOT,
        work_root=work_root,
        drive_remote=settings.get("drive_remote", "gdrive"),
        batch_size=int(settings.get("batch_size", 200)),
        retries=retries,
        retry_delay_seconds=int(settings.get("retry_delay_seconds", 5)),
    )

    result = orchestrator.run(
        local_file=local_file,
        profile=profile,
        upload=False,
        force=args.force,
    )

    payload = result.__dict__.copy()
    payload["rerun_of"] = args.job_id
    payload["source_file"] = str(local_file)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
