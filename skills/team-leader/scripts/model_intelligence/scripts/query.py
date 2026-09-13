#!/usr/bin/env python3
"""Emit a compact model-selection context packet from historical evidence."""

from __future__ import annotations

import argparse
import json
import sys

from common import build_recommendation, compact_context, data_dir, load_profiles, open_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=sorted(load_profiles()))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--as-of", help="latest snapshots on or before YYYY-MM-DD")
    parser.add_argument("--require", action="append", choices=("tools", "files"), default=[])
    parser.add_argument("--openrouter-only", action="store_true")
    parser.add_argument("--format", choices=("context", "json"), default="context")
    parser.add_argument("--data-dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 20:
        raise SystemExit("--limit must be between 1 and 20")
    connection = open_database(data_dir(args.data_dir), readonly=True)
    try:
        payload = build_recommendation(
            connection,
            task=args.task,
            limit=args.limit,
            as_of=args.as_of,
            requirements=args.require,
            openrouter_only=args.openrouter_only,
        )
    finally:
        connection.close()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(compact_context(payload))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, KeyError) as exc:
        print(f"model-intelligence: {exc}", file=sys.stderr)
        raise SystemExit(2)
