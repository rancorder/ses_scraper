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


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive URL driven SES screening job")
    parser.add_argument("--drive-url", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-folder-url", default=None)
    args = parser.parse_args()

    settings = load_settings()
    output_folder_url = args.output_folder_url or settings.get("default_output_folder_url")
    if not output_folder_url:
        parser.error("--output-folder-url is required until default_output_folder_url is configured")

    work_root = Path(settings.get("work_dir") or (_REPO_ROOT / "work"))
    orchestrator = Orchestrator(
        repo_root=_REPO_ROOT,
        work_root=work_root,
        drive_remote=settings.get("drive_remote", "gdrive"),
        batch_size=int(settings.get("batch_size", 200)),
    )
    result = orchestrator.run(
        drive_url=args.drive_url,
        profile=args.profile,
        output_folder_url=output_folder_url,
    )

    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
