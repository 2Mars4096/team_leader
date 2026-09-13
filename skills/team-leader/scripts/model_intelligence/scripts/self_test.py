#!/usr/bin/env python3
"""Offline behavioral checks for normalization, history, filters, and summaries."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from common import (
    Observation,
    add_observations,
    add_snapshot,
    atomic_write,
    build_recommendation,
    compact_context,
    model_family,
    open_database,
    sha256_bytes,
)
from delegate_openrouter import build_messages, extract_text
from plan_workers import choose_candidate
from run_workers import command_for


def add_fixture(connection, root: Path, source: str, date: str, rows: list[Observation]) -> None:
    payload = json.dumps([row.__dict__ for row in rows], default=str, sort_keys=True).encode()
    digest = sha256_bytes(payload)
    raw = root / "raw" / source / date / f"{digest[:16]}.json"
    atomic_write(raw, payload)
    snapshot_id = add_snapshot(
        connection,
        source=source,
        fetched_at=f"{date}T00:00:00+00:00",
        source_date=date,
        status="success",
        sha256=digest,
        raw_path=str(raw.relative_to(root)),
        source_url=f"https://example.invalid/{source}",
    )
    add_observations(connection, snapshot_id, rows)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="model-intelligence-test-") as temporary:
        root = Path(temporary)
        connection = open_database(root)
        try:
            add_fixture(
                connection, root, "arena", "2026-01-01",
                [
                    Observation("arena", "document", "overall", "Alpha Pro", score=1200, rank=1, rank_total=2),
                    Observation("arena", "document", "overall", "Beta Flash", score=1100, rank=2, rank_total=2),
                ],
            )
            add_fixture(
                connection, root, "artificial_analysis", "2026-01-01",
                [
                    Observation("artificial_analysis", "aa_lcr", "overall", "Alpha Pro", score=80),
                    Observation("artificial_analysis", "aa_lcr", "overall", "Beta Flash", score=70),
                    Observation("artificial_analysis", "coding_index", "overall", "Beta Flash", score=90),
                    Observation("artificial_analysis", "coding_index", "overall", "Alpha Pro", score=60),
                ],
            )
            add_fixture(
                connection, root, "aider", "2026-01-01",
                [
                    Observation("aider", "aider_polyglot", "coding_editing", "Beta Flash", score=88),
                    Observation("aider", "aider_polyglot", "coding_editing", "Alpha Pro", score=55),
                ],
            )
            add_fixture(
                connection, root, "openrouter", "2026-01-01",
                [
                    Observation(
                        "openrouter", "catalog", "availability", "Alpha Pro",
                        model_key="vendor/alpha-pro", model_family="alpha-pro", supports_tools=True,
                        supports_files=True, context_length=1000000, price_input=2, price_output=8,
                    ),
                    Observation(
                        "openrouter", "catalog", "availability", "Beta Flash",
                        model_key="vendor/beta-flash", model_family="beta-flash", supports_tools=True,
                        supports_files=False, context_length=200000, price_input=0.2, price_output=0.8,
                    ),
                ],
            )
            connection.commit()
            pdf = build_recommendation(
                connection, task="pdf-reading", requirements=["files"], openrouter_only=True
            )
            assert pdf["recommendations"][0]["model_family"] == "alpha-pro"
            assert all(item["model_family"] != "beta-flash" for item in pdf["recommendations"])
            code = build_recommendation(connection, task="code-generation")
            assert code["recommendations"][0]["model_family"] == "beta-flash"
            context = compact_context(pdf)
            assert "raw" not in context.lower()
            assert "MODEL INTELLIGENCE" in context
            assert model_family("deepseek/deepseek-v4-pro-20260423-high") == "deepseek-v4-pro-20260423"
            messages = build_messages("review this", system="be concise")
            assert messages == [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "review this"},
            ]
            assert extract_text({"choices": [{"message": {"content": "worker result"}}]}) == "worker result"
            candidates = [
                {
                    "model_family": "premium",
                    "quality_score": 0.95,
                    "operational": {
                        "openrouter_slug": "vendor/premium",
                        "price_input_per_million": 10,
                        "price_output_per_million": 30,
                    },
                },
                {
                    "model_family": "efficient",
                    "quality_score": 0.90,
                    "operational": {
                        "openrouter_slug": "vendor/efficient",
                        "price_input_per_million": 1,
                        "price_output_per_million": 3,
                    },
                },
            ]
            chosen, cost, floor = choose_candidate(
                candidates, difficulty="standard", strategy="balanced",
                input_tokens=10_000, output_tokens=2_000, max_cost=None,
            )
            assert chosen["model_family"] == "efficient"
            assert cost == 0.016 and floor == 0.855
            command = command_for({
                "prompt_file": "/tmp/task.txt", "model": "vendor/efficient",
                "max_output_tokens": 2000, "pdfs": [],
            })
            assert "vendor/efficient" in command and "--json" in command
        finally:
            connection.close()
    print("OK: offline behavioral checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
