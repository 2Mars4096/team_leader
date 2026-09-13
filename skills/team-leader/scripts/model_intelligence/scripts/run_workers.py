#!/usr/bin/env python3
"""Dry-run or concurrently execute an approved model-intelligence worker plan."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

from common import atomic_write, sanitize_error, utc_now
from plan_workers import SCHEMA, compact_plan


SCRIPT = Path(__file__).with_name("delegate_openrouter.py")


def validate_execution(plan: Mapping[str, Any], limit: float) -> None:
    if not math.isfinite(limit) or limit < 0:
        raise ValueError("cost guard must be finite and nonnegative")
    tasks = plan.get("tasks", [])
    if not tasks:
        raise ValueError("plan must contain tasks")
    identifiers = set()
    total = 0.0
    for task in tasks:
        identifier = str(task.get("id", ""))
        if not identifier or not identifier.replace("-", "").replace("_", "").isalnum() or identifier in identifiers:
            raise ValueError("task ids must be unique safe filenames")
        identifiers.add(identifier)
        cost = task.get("estimated_max_cost_usd")
        if cost is None or not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("every task requires a finite nonnegative cost estimate")
        if not task.get("model") or int(task.get("max_output_tokens", 0)) < 1:
            raise ValueError("every task requires a model and positive output limit")
        total += float(cost)
    estimate = plan.get("estimated_max_cost_usd")
    if estimate is None or not math.isfinite(float(estimate)) or float(estimate) < 0:
        raise ValueError("plan requires a finite nonnegative cost estimate")
    if abs(float(estimate) - total) > 0.000001:
        raise ValueError("plan estimate does not match task estimates")
    if total > limit:
        raise ValueError("plan estimate exceeds the cost guard")


def command_for(task: Mapping[str, Any]) -> list[str]:
    command = [
        sys.executable, str(SCRIPT), "--prompt-file", str(task["prompt_file"]),
        "--model", str(task["model"]), "--max-tokens", str(task["max_output_tokens"]), "--json",
    ]
    if task.get("system_file"):
        command.extend(["--system-file", str(task["system_file"])])
    for pdf in task.get("pdfs", []):
        command.extend(["--pdf", str(pdf)])
    if task.get("pdf_engine"):
        command.extend(["--pdf-engine", str(task["pdf_engine"])])
    if task.get("reasoning_effort"):
        command.extend(["--reasoning-effort", str(task["reasoning_effort"])])
    return command


def run_one(task: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command_for(task), capture_output=True, text=True, timeout=timeout,
            env=os.environ.copy(), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"id": task["id"], "model": task["model"], "status": "failed", "error": "worker timeout"}
    if completed.returncode != 0:
        return {
            "id": task["id"], "model": task["model"], "status": "failed",
            "error": sanitize_error(completed.stderr.strip()[-2000:] or f"exit {completed.returncode}"),
        }
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"id": task["id"], "model": task["model"], "status": "failed", "error": f"invalid worker JSON: {exc}"}
    return {"id": task["id"], "model": task["model"], "status": "success", "response": response}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="assigned JSON plan from plan_workers.py")
    parser.add_argument("--execute", action="store_true", help="make paid external API calls; otherwise dry-run")
    parser.add_argument("--output-dir", help="required with --execute")
    parser.add_argument("--max-estimated-cost-usd", type=float, help="required spending guard with --execute")
    parser.add_argument("--max-parallel", type=int)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = Path(args.plan).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != SCHEMA:
        raise ValueError(f"unsupported plan schema: {plan.get('schema')!r}")
    if not args.execute:
        print(compact_plan(plan))
        print("DRY RUN: no external model calls were made")
        return 0
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("missing required OpenRouter credential in the environment")
    if not args.output_dir:
        raise ValueError("--output-dir is required with --execute")
    if args.max_estimated_cost_usd is None:
        raise ValueError("--max-estimated-cost-usd is required with --execute")
    validate_execution(plan, args.max_estimated_cost_usd)
    if args.timeout <= 0 or (args.max_parallel is not None and args.max_parallel <= 0):
        raise ValueError("timeout and concurrency must be positive")
    estimate = plan.get("estimated_max_cost_usd")
    if estimate is None:
        raise ValueError("plan has unknown-cost tasks; assign priced models before execution")
    if float(estimate) > args.max_estimated_cost_usd:
        raise ValueError(f"estimated maximum ${estimate:.6f} exceeds guard ${args.max_estimated_cost_usd:.6f}")
    tasks = list(plan.get("tasks", []))
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(args.max_parallel or int(plan.get("max_parallel", 1)), len(tasks)))
    by_id: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_one, task, args.timeout): task["id"] for task in tasks}
        for future in as_completed(futures):
            by_id[futures[future]] = future.result()
    ordered = [by_id[task["id"]] for task in tasks]
    for result in ordered:
        atomic_write(
            output_dir / f"{result['id']}.json",
            (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
    summary = {
        "schema": "model-intelligence-worker-run/v1",
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "completed_at": utc_now(),
        "results": ordered,
    }
    atomic_write(output_dir / "run.json", (json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    successes = sum(result["status"] == "success" for result in ordered)
    print(f"workers={len(ordered)} success={successes} failed={len(ordered) - successes} output={output_dir}")
    return 0 if successes == len(ordered) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"worker-runner: {sanitize_error(exc)}", file=sys.stderr)
        raise SystemExit(2)
