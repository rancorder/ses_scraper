from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from batch_run import read_file, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one resumable SES checkpoint batch")
    parser.add_argument("--file", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--batch-num", type=int, required=True)
    args = parser.parse_args()

    companies, _, _ = read_file(args.file, "企業ホームページURL")
    if args.start < 0 or args.start >= len(companies):
        raise RuntimeError(
            f"checkpoint start out of range: start={args.start}, total={len(companies)}"
        )
    batch = companies[args.start : args.start + max(1, args.count)]
    if not batch:
        raise RuntimeError("checkpoint contains no companies")

    results = asyncio.run(
        run_batch(batch, args.profile, args.output_prefix, args.batch_num)
    )
    if len(results) != len(batch):
        raise RuntimeError(
            f"checkpoint incomplete: expected {len(batch)} results, got {len(results)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
