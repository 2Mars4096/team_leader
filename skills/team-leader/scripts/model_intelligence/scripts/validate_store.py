#!/usr/bin/env python3
"""Validate structural and privacy invariants of the historical store."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from common import SCHEMA_VERSION, data_dir, load_profiles, open_database


SECRET_PATTERNS = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"),
    re.compile(r"(?i)(api[-_ ]?key)[=: ]+[A-Za-z0-9._-]{12,}"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = data_dir(args.data_dir)
    connection = open_database(root, readonly=True)
    errors: list[str] = []
    try:
        schema = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if schema is None or int(schema[0]) != SCHEMA_VERSION:
            errors.append(f"schema version mismatch: {schema[0] if schema else 'missing'}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"sqlite integrity: {integrity}")
        orphans = connection.execute(
            """SELECT COUNT(*) FROM observations o
               LEFT JOIN snapshots s ON s.id=o.snapshot_id WHERE s.id IS NULL"""
        ).fetchone()[0]
        if orphans:
            errors.append(f"orphan observations: {orphans}")
        bad_success = connection.execute(
            "SELECT COUNT(*) FROM snapshots WHERE status='success' AND (sha256 IS NULL OR raw_path IS NULL)"
        ).fetchone()[0]
        if bad_success:
            errors.append(f"successful snapshots without raw payload: {bad_success}")
        for row in connection.execute("SELECT raw_path,sha256 FROM snapshots WHERE status='success'"):
            path = root / row["raw_path"]
            if not path.exists():
                errors.append(f"missing raw snapshot: {row['raw_path']}")
        known_tasks = set(load_profiles())
        for row in connection.execute("SELECT task,payload_json FROM summaries"):
            if row["task"] not in known_tasks:
                errors.append(f"summary for unknown task: {row['task']}")
            payload = json.loads(row["payload_json"])
            if "raw" in payload or "observations" in payload:
                errors.append(f"summary leaks raw rows: {row['task']}")
        for row in connection.execute("SELECT error,metadata_json FROM snapshots LEFT JOIN observations ON snapshots.id=observations.snapshot_id"):
            text = f"{row['error'] or ''}\n{row['metadata_json'] or ''}"
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                errors.append("possible credential found in database")
                break
    finally:
        connection.close()
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"OK: {root / 'model_intelligence.sqlite3'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
