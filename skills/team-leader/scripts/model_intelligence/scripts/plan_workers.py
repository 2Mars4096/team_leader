#!/usr/bin/env python3
"""Assign bounded worker tasks to capable, cost-efficient OpenRouter models."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from common import (
    DEFAULT_OPENROUTER_MODEL,
    atomic_write,
    build_recommendation,
    data_dir,
    json_dumps,
    latest_snapshot_ids,
    load_profiles,
    open_database,
)


SCHEMA = "model-intelligence-worker-plan/v1"
DIFFICULTY_FLOORS = {"simple": 0.80, "standard": 0.90, "complex": 0.96, "critical": 1.00}
STRATEGY_ADJUSTMENTS = {"economy": -0.08, "balanced": 0.0, "quality": 0.10}


def model_override(task: Mapping[str, Any], manifest: Mapping[str, Any]) -> str | None:
    selected = (task.get("model") or manifest.get("default_model")
                or os.environ.get("MODEL_INTELLIGENCE_OPENROUTER_MODEL")
                or DEFAULT_OPENROUTER_MODEL)
    return None if selected == "auto" else str(selected)
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def estimated_cost(candidate: Mapping[str, Any], input_tokens: int, output_tokens: int) -> float | None:
    operational = candidate.get("operational") or {}
    input_price = operational.get("price_input_per_million")
    output_price = operational.get("price_output_per_million")
    if input_price is None or output_price is None:
        return None
    return round((float(input_price) * input_tokens + float(output_price) * output_tokens) / 1_000_000, 8)


def quality_floor(difficulty: str, strategy: str, top_quality: float) -> float:
    ratio = DIFFICULTY_FLOORS[difficulty] + STRATEGY_ADJUSTMENTS[strategy]
    return top_quality * min(1.0, max(0.70, ratio))


def choose_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    difficulty: str,
    strategy: str,
    input_tokens: int,
    output_tokens: int,
    max_cost: float | None,
) -> tuple[Mapping[str, Any], float | None, float]:
    if not candidates:
        raise ValueError("no OpenRouter candidates have relevant benchmark evidence")
    top_quality = max(float(item["quality_score"]) for item in candidates)
    floor = quality_floor(difficulty, strategy, top_quality)
    eligible: list[tuple[Mapping[str, Any], float | None]] = []
    for item in candidates:
        if float(item["quality_score"]) + 1e-12 < floor:
            continue
        cost = estimated_cost(item, input_tokens, output_tokens)
        if max_cost is not None and (cost is None or cost > max_cost):
            continue
        eligible.append((item, cost))
    if not eligible:
        raise ValueError("no candidate satisfies the capability and cost constraints")
    if strategy == "quality":
        chosen, cost = min(
            eligible,
            key=lambda pair: (-float(pair[0]["quality_score"]), pair[1] if pair[1] is not None else float("inf"), pair[0]["model_family"]),
        )
    else:
        priced = [pair for pair in eligible if pair[1] is not None]
        pool = priced or eligible
        chosen, cost = min(
            pool,
            key=lambda pair: (pair[1] if pair[1] is not None else float("inf"), -float(pair[0]["quality_score"]), pair[0]["model_family"]),
        )
    return chosen, cost, round(floor, 4)


def normalize_task(task: Mapping[str, Any], base: Path) -> dict[str, Any]:
    required = ("id", "title", "profile", "prompt_file")
    missing = [key for key in required if not task.get(key)]
    if missing:
        raise ValueError(f"task is missing: {', '.join(missing)}")
    identifier = str(task["id"])
    if not identifier.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"task id must contain only letters, digits, '-' or '_': {identifier}")
    prompt_path = Path(str(task["prompt_file"])).expanduser()
    if not prompt_path.is_absolute():
        prompt_path = base / prompt_path
    normalized = dict(task)
    normalized["id"] = identifier
    normalized["title"] = str(task["title"])
    normalized["profile"] = str(task["profile"])
    normalized["prompt_file"] = str(prompt_path.resolve())
    if not Path(normalized["prompt_file"]).is_file():
        raise ValueError(f"task {identifier}: prompt file does not exist: {normalized['prompt_file']}")
    if task.get("system_file"):
        system_path = Path(str(task["system_file"])).expanduser()
        if not system_path.is_absolute():
            system_path = base / system_path
        normalized["system_file"] = str(system_path.resolve())
        if not Path(normalized["system_file"]).is_file():
            raise ValueError(f"task {identifier}: system file does not exist: {normalized['system_file']}")
    normalized["pdfs"] = [
        value if str(value).startswith(("http://", "https://"))
        else str(((base / Path(str(value))) if not Path(str(value)).expanduser().is_absolute() else Path(str(value)).expanduser()).resolve())
        for value in task.get("pdfs", [])
    ]
    normalized["requirements"] = sorted({str(value) for value in task.get("requirements", [])})
    normalized["estimated_input_tokens"] = int(task.get("estimated_input_tokens", 4000))
    normalized["max_output_tokens"] = int(task.get("max_output_tokens", 4096))
    if task.get("reasoning_effort") not in {None, *REASONING_EFFORTS}:
        raise ValueError(f"task {identifier}: unsupported reasoning_effort")
    if normalized["estimated_input_tokens"] < 1 or normalized["max_output_tokens"] < 1:
        raise ValueError(f"task {identifier}: token estimates must be positive")
    return normalized


def override_candidate(connection, slug: str, recommendations: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for item in recommendations:
        if (item.get("operational") or {}).get("openrouter_slug") == slug:
            return item
    snapshot_id = latest_snapshot_ids(connection).get("openrouter")
    row = None if snapshot_id is None else connection.execute(
        """SELECT * FROM observations
           WHERE snapshot_id=? AND source='openrouter' AND benchmark='catalog' AND model_key=?""",
        (snapshot_id, slug),
    ).fetchone()
    if row is None:
        raise ValueError(f"model override is absent from the latest OpenRouter catalog: {slug}")
    return {
        "model": row["model_name"],
        "model_family": row["model_family"],
        "quality_score": None,
        "coverage": 0.0,
        "evidence": [],
        "operational": {
            "openrouter_slug": row["model_key"],
            "supports_tools": None if row["supports_tools"] is None else bool(row["supports_tools"]),
            "supports_files": None if row["supports_files"] is None else bool(row["supports_files"]),
            "context_length": row["context_length"],
            "price_input_per_million": row["price_input"],
            "price_output_per_million": row["price_output"],
        },
    }


def assign_manifest(connection, manifest: Mapping[str, Any], *, base: Path) -> dict[str, Any]:
    strategy = str(manifest.get("strategy", "balanced"))
    if strategy not in STRATEGY_ADJUSTMENTS:
        raise ValueError("strategy must be economy, balanced, or quality")
    tasks = [normalize_task(task, base) for task in manifest.get("tasks", [])]
    if not tasks:
        raise ValueError("manifest must contain at least one task")
    identifiers = [task["id"] for task in tasks]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("task ids must be unique")
    profiles = load_profiles()
    assignments: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for task in tasks:
        if task["profile"] not in profiles:
            raise ValueError(f"task {task['id']}: unknown profile {task['profile']!r}")
        difficulty = str(task.get("difficulty", "standard"))
        task_strategy = str(task.get("strategy", strategy))
        if difficulty not in DIFFICULTY_FLOORS:
            raise ValueError(f"task {task['id']}: difficulty must be simple, standard, complex, or critical")
        if task_strategy not in STRATEGY_ADJUSTMENTS:
            raise ValueError(f"task {task['id']}: strategy must be economy, balanced, or quality")
        recommendation = build_recommendation(
            connection,
            task=task["profile"],
            limit=100,
            requirements=task["requirements"],
            openrouter_only=True,
            use_default_requirements=False,
        )
        fingerprints.add(str(recommendation["snapshot_fingerprint"]))
        candidates = recommendation["recommendations"]
        override = model_override(task, manifest)
        if override:
            chosen = override_candidate(connection, str(override), candidates)
            cost = estimated_cost(chosen, task["estimated_input_tokens"], task["max_output_tokens"])
            floor = None
            max_cost = float(task["max_estimated_cost_usd"]) if task.get("max_estimated_cost_usd") is not None else None
            if max_cost is not None and (cost is None or cost > max_cost):
                raise ValueError(f"task {task['id']}: model override exceeds or lacks the task cost limit")
        else:
            chosen, cost, floor = choose_candidate(
                candidates,
                difficulty=difficulty,
                strategy=task_strategy,
                input_tokens=task["estimated_input_tokens"],
                output_tokens=task["max_output_tokens"],
                max_cost=float(task["max_estimated_cost_usd"]) if task.get("max_estimated_cost_usd") is not None else None,
            )
        operational = chosen["operational"]
        assignments.append({
            **task,
            "difficulty": difficulty,
            "strategy": task_strategy,
            "model": operational["openrouter_slug"],
            "quality_score": chosen["quality_score"],
            "quality_floor": floor,
            "coverage": chosen["coverage"],
            "estimated_max_cost_usd": cost,
            "pricing_per_million": {
                "input": operational["price_input_per_million"],
                "output": operational["price_output_per_million"],
            },
            "evidence": chosen["evidence"][:2],
        })
    known_costs = [task["estimated_max_cost_usd"] for task in assignments if task["estimated_max_cost_usd"] is not None]
    unknown_costs = sum(task["estimated_max_cost_usd"] is None for task in assignments)
    plan_core = {
        "schema": SCHEMA,
        "objective": str(manifest.get("objective", "")),
        "strategy": strategy,
        "max_parallel": max(1, int(manifest.get("max_parallel", min(4, len(assignments))))),
        "tasks": assignments,
        "snapshot_fingerprints": sorted(fingerprints),
        "estimated_max_cost_usd": round(sum(known_costs), 8) if not unknown_costs else None,
        "unknown_cost_tasks": unknown_costs,
    }
    plan_core["plan_fingerprint"] = hashlib.sha256(json_dumps(plan_core).encode("utf-8")).hexdigest()
    return plan_core


def compact_plan(plan: Mapping[str, Any]) -> str:
    cost = plan["estimated_max_cost_usd"]
    lines = [
        f"WORKER PLAN | tasks={len(plan['tasks'])} parallel={plan['max_parallel']} strategy={plan['strategy']}",
        f"Estimated maximum model cost: {'unknown' if cost is None else f'${cost:.4f}'}",
    ]
    for task in plan["tasks"]:
        task_cost = task["estimated_max_cost_usd"]
        quality_label = "user-fixed" if task["quality_score"] is None else f"{task['quality_score']:.3f}"
        floor_label = "n/a" if task["quality_floor"] is None else f"{task['quality_floor']:.3f}"
        lines.append(
            f"- {task['id']} [{task['profile']}/{task['difficulty']}] -> {task['model']} "
            f"quality={quality_label} floor={floor_label} "
            f"cost<={'unknown' if task_cost is None else f'${task_cost:.4f}'}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="JSON task manifest")
    parser.add_argument("--data-dir")
    parser.add_argument("--output", help="write assigned JSON plan atomically")
    parser.add_argument("--format", choices=("context", "json"), default="context")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    connection = open_database(data_dir(args.data_dir), readonly=True)
    try:
        plan = assign_manifest(connection, manifest, base=manifest_path.parent)
    finally:
        connection.close()
    if args.output:
        atomic_write(Path(args.output).expanduser().resolve(), (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) if args.format == "json" else compact_plan(plan))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"worker-planner: {exc}", file=sys.stderr)
        raise SystemExit(2)
