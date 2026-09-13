#!/usr/bin/env python3
"""Fetch public benchmark sources concurrently and update the historical store."""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from common import (
    Observation,
    add_observations,
    add_snapshot,
    as_float,
    as_int,
    atomic_write,
    build_recommendation,
    data_dir,
    exact_model_key,
    http_get,
    json_dumps,
    load_profiles,
    model_family,
    open_database,
    sanitize_error,
    sha256_bytes,
    snapshot_path,
    utc_now,
)


SOURCE_ORDER = ("arena", "artificial_analysis", "livebench", "swe_bench", "aider", "openrouter")


@dataclass
class FetchResult:
    source: str
    source_url: str
    payload: bytes | None
    suffix: str
    observations: list[Observation]
    source_date: str | None = None
    status: str = "success"
    error: str | None = None


def get_json(url: str, *, headers: Mapping[str, str] | None = None) -> tuple[Any, bytes]:
    payload, _ = http_get(url, headers=headers)
    return json.loads(payload.decode("utf-8")), payload


def first(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    lower = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        if name.lower() in lower:
            value = lower[name.lower()]
            if value is not None and value != "":
                return value
    return None


def fetch_arena() -> FetchResult:
    dataset = "lmarena-ai/leaderboard-dataset"
    split_url = "https://datasets-server.huggingface.co/splits?" + urllib.parse.urlencode({"dataset": dataset})
    split_doc, _ = get_json(split_url)
    split_rows = split_doc.get("splits", [])
    configs = sorted({row["config"] for row in split_rows if row.get("split") == "latest"})
    all_rows: list[dict[str, Any]] = []
    for config in configs:
        url = "https://datasets-server.huggingface.co/first-rows?" + urllib.parse.urlencode(
            {"dataset": dataset, "config": config, "split": "latest"}
        )
        document, _ = get_json(url)
        wrappers = document.get("rows", [])
        if document.get("truncated"):
            try:
                import pyarrow as pa  # type: ignore[import-not-found]
                import pyarrow.parquet as pq  # type: ignore[import-not-found]
            except ImportError:
                wrappers = []
                offset = 0
                while True:
                    page_url = "https://datasets-server.huggingface.co/rows?" + urllib.parse.urlencode(
                        {"dataset": dataset, "config": config, "split": "latest", "offset": offset, "length": 100}
                    )
                    page, _ = get_json(page_url)
                    page_rows = page.get("rows", [])
                    wrappers.extend(page_rows)
                    offset += len(page_rows)
                    if not page_rows or offset >= int(page.get("num_rows_total", offset)):
                        break
                    time.sleep(0.2)
            else:
                parquet_url = (
                    f"https://huggingface.co/datasets/{dataset}/resolve/main/"
                    f"{urllib.parse.quote(config, safe='')}/latest-00000-of-00001.parquet?download=true"
                )
                parquet_payload, _ = http_get(parquet_url, timeout=60.0)
                wrappers = [{"row": row} for row in pq.read_table(pa.BufferReader(parquet_payload)).to_pylist()]
        for wrapper in wrappers:
            row = dict(wrapper.get("row", wrapper))
            row["_arena_config"] = config
            all_rows.append(row)
        # Be polite to the public dataset service and avoid burst rate limits.
        time.sleep(0.1)
    raw = json_dumps({"dataset": dataset, "split": "latest", "rows": all_rows}).encode("utf-8")
    provisional: list[Observation] = []
    dates: list[str] = []
    for row in all_rows:
        model = first(row, ("model", "model_name", "name"))
        if not model:
            continue
        config = str(row.get("_arena_config", "unknown"))
        benchmark = config.replace("_style_control", "").replace("-style-control", "")
        category = str(first(row, ("category", "task", "domain", "subset")) or "overall")
        score = as_float(first(row, ("arena_score", "score", "rating", "elo")))
        rank = as_int(first(row, ("rank", "ranking")))
        observed = first(row, ("date", "publish_date", "leaderboard_date", "updated_at"))
        if observed:
            dates.append(str(observed)[:10])
        provider = first(row, ("organization", "provider", "creator", "model_organization"))
        metadata = {
            key: row[key]
            for key in ("votes", "rank_ub", "rank_lb", "license", "style_control")
            if key in row
        }
        provisional.append(
            Observation(
                source="arena", benchmark=benchmark, category=category, model_name=str(model),
                provider=str(provider) if provider else None, score=score, rank=rank,
                price_input=as_float(first(row, ("input_price", "price_input"))),
                price_output=as_float(first(row, ("output_price", "price_output"))),
                context_length=as_int(first(row, ("context", "context_length", "context_window"))),
                observed_at=str(observed) if observed else None, metadata=metadata,
            )
        )
    totals: dict[tuple[str, str], int] = {}
    for item in provisional:
        totals[(item.benchmark, item.category)] = totals.get((item.benchmark, item.category), 0) + 1
    observations = [replace(item, rank_total=totals[(item.benchmark, item.category)]) for item in provisional]
    return FetchResult(
        "arena", "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset",
        raw, "json", observations, max(dates) if dates else None,
    )


def flatten_numeric(mapping: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, float]]:
    for key in sorted(mapping):
        value = mapping[key]
        path = f"{prefix}_{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            yield from flatten_numeric(value, path)
        else:
            number = as_float(value)
            if number is not None:
                yield path, number


def fetch_artificial_analysis() -> FetchResult:
    key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
    source_url = "https://artificialanalysis.ai/api/v2/language/models"
    if not key:
        return FetchResult(
            "artificial_analysis", source_url, None, "json", [], status="skipped",
            error="ARTIFICIAL_ANALYSIS_API_KEY is not set",
        )
    headers = {"x-api-key": key}
    try:
        document, payload = get_json(source_url, headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code not in {404, 403}:
            raise
        source_url = "https://artificialanalysis.ai/api/v2/data/llms/models"
        document, payload = get_json(source_url, headers=headers)
    items = document.get("data", document if isinstance(document, list) else [])
    observations: list[Observation] = []
    dates: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        model = first(item, ("name", "model_name", "slug"))
        if not model:
            continue
        creator = item.get("model_creator") or item.get("creator") or {}
        provider = creator.get("name") if isinstance(creator, Mapping) else creator
        slug = str(item.get("slug") or model)
        pricing = item.get("pricing") if isinstance(item.get("pricing"), Mapping) else {}
        observed = first(item, ("updated_at", "release_date", "date"))
        if observed:
            dates.append(str(observed)[:10])
        observations.append(
            Observation(
                source="artificial_analysis", benchmark="catalog", category="operational",
                model_name=str(model), model_key=exact_model_key(slug), provider=str(provider) if provider else None,
                price_input=as_float(first(pricing, ("price_1m_input_tokens", "input", "input_price"))),
                price_output=as_float(first(pricing, ("price_1m_output_tokens", "output", "output_price"))),
                latency=as_float(first(item, ("median_time_to_first_token_seconds", "latency"))),
                throughput=as_float(first(item, ("median_output_tokens_per_second", "throughput"))),
                context_length=as_int(first(item, ("context_window", "context_length"))),
                observed_at=str(observed) if observed else None,
            )
        )
        evaluations = item.get("evaluations")
        if isinstance(evaluations, Mapping):
            for metric, value in flatten_numeric(evaluations):
                normalized = metric.lower().replace("artificial_analysis_", "")
                observations.append(
                    Observation(
                        source="artificial_analysis", benchmark=normalized, category="overall",
                        model_name=str(model), model_key=exact_model_key(slug), provider=str(provider) if provider else None,
                        score=value, observed_at=str(observed) if observed else None,
                    )
                )
    return FetchResult("artificial_analysis", source_url, payload, "json", observations, max(dates) if dates else None)


def fetch_livebench() -> FetchResult:
    api = "https://api.github.com/repos/LiveBench/new-livebench/contents/public"
    listing, listing_payload = get_json(api, headers={"Accept": "application/vnd.github+json"})
    names = [item.get("name", "") for item in listing if isinstance(item, Mapping)]
    table_names = sorted(name for name in names if re.fullmatch(r"table_\d{4}_\d{2}_\d{2}\.csv", name))
    if not table_names:
        raise RuntimeError("LiveBench public directory has no dated table CSV")
    table_name = table_names[-1]
    date_token = table_name[len("table_"):-len(".csv")]
    date = date_token.replace("_", "-")
    by_name = {item["name"]: item for item in listing if isinstance(item, Mapping) and item.get("name")}
    table_url = by_name[table_name]["download_url"]
    table_payload, _ = http_get(table_url)
    category_name = f"categories_{date_token}.json"
    categories: Mapping[str, Any] = {}
    category_payload = b"{}"
    if category_name in by_name:
        category_payload, _ = http_get(by_name[category_name]["download_url"])
        categories = json.loads(category_payload.decode("utf-8"))
    raw = json_dumps(
        {
            "release": date, "table_name": table_name,
            "table_csv": table_payload.decode("utf-8"), "categories": categories,
            "listing_sha256": sha256_bytes(listing_payload),
        }
    ).encode("utf-8")
    category_by_column: dict[str, str] = {}
    for category, columns in categories.items():
        if isinstance(columns, list):
            for column in columns:
                category_by_column[str(column)] = str(category)
    reader = csv.DictReader(io.StringIO(table_payload.decode("utf-8-sig")))
    observations: list[Observation] = []
    for row in reader:
        model_column = next((name for name in row if name.lower() in {"model", "model name", "model_name"}), None)
        if model_column is None and row:
            model_column = next(iter(row))
        model = row.get(model_column or "", "").strip()
        if not model:
            continue
        for column, raw_value in row.items():
            if column == model_column:
                continue
            value = as_float(raw_value)
            if value is None:
                continue
            category = category_by_column.get(column) or column.split("_", 1)[0]
            benchmark = str(category).lower().replace(" ", "_")
            observations.append(
                Observation(
                    source="livebench", benchmark=benchmark, category=column,
                    model_name=model, score=value, observed_at=date,
                )
            )
    return FetchResult("livebench", "https://github.com/LiveBench/new-livebench", raw, "json", observations, date)


def fetch_swe_bench() -> FetchResult:
    url = "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json"
    document, payload = get_json(url)
    leaderboards = document.get("leaderboards", [])
    observations: list[Observation] = []
    dates: list[str] = []
    for leaderboard in leaderboards:
        name = str(leaderboard.get("name", "unknown"))
        results = [result for result in leaderboard.get("results", []) if isinstance(result, Mapping)]
        scored = [(as_float(result.get("resolved")), result) for result in results]
        scored = [(score, result) for score, result in scored if score is not None]
        scored.sort(key=lambda item: (-item[0], str(item[1].get("name", ""))))
        for rank, (score, result) in enumerate(scored, 1):
            model = result.get("name") or result.get("model")
            if not model:
                continue
            observed = result.get("date")
            if observed:
                dates.append(str(observed)[:10])
            metadata = {
                key: result.get(key)
                for key in ("verified", "oss", "logs", "trajs", "warning", "folder")
                if key in result
            }
            observations.append(
                Observation(
                    source="swe_bench", benchmark=name, category="issue_resolution",
                    model_name=str(model), score=score, rank=rank, rank_total=len(scored),
                    harness=str(result.get("folder")) if result.get("folder") else None,
                    observed_at=str(observed) if observed else None, metadata=metadata,
                )
            )
    return FetchResult("swe_bench", url, payload, "json", observations, max(dates) if dates else None)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0:1] in {"\"", "'"}:
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value.strip("\"'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none", "~"}:
        return None
    number = as_float(value)
    return number if number is not None else value


def parse_flat_yaml_sequence(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            if current is not None:
                records.append(current)
            current = {}
            content = line[2:]
        elif line.startswith("  ") and current is not None:
            content = line.strip()
        else:
            continue
        if ":" not in content:
            continue
        key, value = content.split(":", 1)
        current[key.strip()] = parse_scalar(value)
    if current is not None:
        records.append(current)
    return records


def command_model(command: str) -> str | None:
    match = re.search(r"(?:^|\s)--model\s+([^\s#]+)", command)
    return match.group(1).strip("\"'") if match else None


def fetch_aider() -> FetchResult:
    url = "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml"
    payload, _ = http_get(url)
    records = parse_flat_yaml_sequence(payload.decode("utf-8"))
    observations: list[Observation] = []
    dates: list[str] = []
    for record in records:
        model = record.get("model")
        if not model:
            continue
        command = str(record.get("command") or "")
        key = command_model(command) or str(model)
        observed = record.get("date")
        if observed:
            dates.append(str(observed)[:10])
        score = as_float(record.get("pass_rate_2"))
        if score is None:
            score = as_float(record.get("pass_rate_1"))
        observations.append(
            Observation(
                source="aider", benchmark="aider_polyglot", category="coding_editing",
                model_name=str(model), model_key=exact_model_key(key), score=score,
                latency=as_float(record.get("seconds_per_case")), harness="aider",
                observed_at=str(observed) if observed else None,
                metadata={
                    "edit_format": record.get("edit_format"),
                    "pass_rate_1": record.get("pass_rate_1"),
                    "pass_rate_2": record.get("pass_rate_2"),
                    "well_formed_percent": record.get("percent_cases_well_formed"),
                    "total_cost": record.get("total_cost"),
                    "test_cases": record.get("test_cases"),
                },
            )
        )
    ranked = sorted(
        [item for item in observations if item.score is not None],
        key=lambda item: (-float(item.score), item.model_name),
    )
    rank_lookup = {id(item): rank for rank, item in enumerate(ranked, 1)}
    observations = [
        replace(item, rank=rank_lookup.get(id(item)), rank_total=len(ranked)) for item in observations
    ]
    return FetchResult("aider", url, payload, "yml", observations, max(dates) if dates else None)


def price_per_million(value: Any) -> float | None:
    number = as_float(value)
    if number is None:
        return None
    converted = number * 1_000_000 if abs(number) < 0.01 else number
    return round(converted, 8)


def fetch_openrouter() -> FetchResult:
    url = "https://openrouter.ai/api/v1/models"
    document, catalog_payload = get_json(url)
    items = document.get("data", [])
    observations: list[Observation] = []
    for item in items:
        model_id = str(item.get("id") or "")
        if not model_id:
            continue
        supported = {str(value) for value in item.get("supported_parameters", [])}
        architecture = item.get("architecture") if isinstance(item.get("architecture"), Mapping) else {}
        modalities = {str(value).lower() for value in architecture.get("input_modalities", [])}
        pricing = item.get("pricing") if isinstance(item.get("pricing"), Mapping) else {}
        observations.append(
            Observation(
                source="openrouter", benchmark="catalog", category="availability",
                model_name=str(item.get("name") or model_id), model_key=exact_model_key(model_id),
                model_family=model_family(model_id), provider=model_id.split("/", 1)[0] if "/" in model_id else None,
                price_input=price_per_million(pricing.get("prompt")),
                price_output=price_per_million(pricing.get("completion")),
                context_length=as_int(item.get("context_length")),
                supports_tools="tools" in supported,
                supports_files="file" in modalities,
                observed_at=utc_now()[:10],
                metadata={
                    "supported_parameters": sorted(supported),
                    "input_modalities": sorted(modalities),
                    "canonical_slug": item.get("canonical_slug"),
                    "created": item.get("created"),
                },
            )
        )
    raw_document: dict[str, Any] = {"models": document}
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        ranking_url = "https://openrouter.ai/api/v1/datasets/rankings-daily"
        rankings, _ = get_json(ranking_url, headers={"Authorization": f"Bearer {key}"})
        raw_document["rankings_daily"] = rankings
        rank_rows = rankings.get("data", [])
        real_rows = [row for row in rank_rows if row.get("model_permaslug") not in {None, "other"}]
        latest_date = max((str(row.get("date")) for row in real_rows), default=None)
        latest_rows = [row for row in real_rows if str(row.get("date")) == latest_date]
        latest_rows.sort(key=lambda row: (-int(row.get("total_tokens", 0)), str(row.get("model_permaslug"))))
        for rank, row in enumerate(latest_rows, 1):
            slug = str(row["model_permaslug"])
            observations.append(
                Observation(
                    source="openrouter", benchmark="usage", category="overall_adoption",
                    model_name=slug, model_key=exact_model_key(slug), model_family=model_family(slug),
                    score=as_float(row.get("total_tokens")), rank=rank, rank_total=len(latest_rows),
                    observed_at=latest_date, metadata={"metric": "total_tokens"},
                )
            )
    raw = json_dumps(raw_document).encode("utf-8")
    return FetchResult("openrouter", url, raw, "json", observations, utc_now()[:10])


FETCHERS: Mapping[str, Callable[[], FetchResult]] = {
    "arena": fetch_arena,
    "artificial_analysis": fetch_artificial_analysis,
    "livebench": fetch_livebench,
    "swe_bench": fetch_swe_bench,
    "aider": fetch_aider,
    "openrouter": fetch_openrouter,
}


def store_result(connection, root: Path, result: FetchResult, fetched_at: str) -> tuple[int, int]:
    digest = None
    relative = None
    if result.payload is not None:
        digest = sha256_bytes(result.payload)
        path = snapshot_path(root, result.source, fetched_at, digest, result.suffix)
        if not path.exists():
            atomic_write(path, result.payload)
        relative = str(path.relative_to(root))
    snapshot_id = add_snapshot(
        connection,
        source=result.source,
        fetched_at=fetched_at,
        source_url=result.source_url,
        status=result.status,
        source_date=result.source_date,
        sha256=digest,
        raw_path=relative,
        error=result.error,
    )
    count = add_observations(connection, snapshot_id, result.observations) if result.status == "success" else 0
    return snapshot_id, count


def rebuild_summaries(connection, root: Path) -> None:
    for task in sorted(load_profiles()):
        try:
            payload = build_recommendation(connection, task=task, limit=8)
        except RuntimeError:
            continue
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        atomic_write(root / "summaries" / f"{task}.json", encoded)
        connection.execute(
            """INSERT OR REPLACE INTO summaries(task,generated_at,snapshot_fingerprint,payload_json)
               VALUES(?,?,?,?)""",
            (task, payload["generated_at"], payload["snapshot_fingerprint"], json_dumps(payload)),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="refresh all configured sources")
    selection.add_argument("--source", action="append", choices=SOURCE_ORDER, help="refresh one source; repeatable")
    selection.add_argument("--rebuild-only", action="store_true", help="rebuild compact summaries without network calls")
    parser.add_argument("--data-dir", help="override the historical store directory")
    parser.add_argument("--workers", type=int, default=6, help="concurrent source fetches (default: 6)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = [] if args.rebuild_only else list(SOURCE_ORDER if args.all or not args.source else dict.fromkeys(args.source))
    root = data_dir(args.data_dir)
    fetched_at = utc_now()
    results: dict[str, FetchResult] = {}
    if selected:
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(selected)))) as executor:
            futures = {executor.submit(FETCHERS[source]): source for source in selected}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results[source] = future.result()
                except Exception as exc:  # source isolation is intentional
                    results[source] = FetchResult(
                        source, "unknown", None, "txt", [], status="failed", error=sanitize_error(exc)
                    )
    connection = open_database(root)
    successes = 0
    try:
        with connection:
            for source in SOURCE_ORDER:
                if source not in results:
                    continue
                result = results[source]
                _, count = store_result(connection, root, result, fetched_at)
                if result.status == "success":
                    successes += 1
                detail = f" rows={count}" if result.status == "success" else f" reason={result.error}"
                print(f"{source}: {result.status}{detail}")
            rebuild_summaries(connection, root)
    finally:
        connection.close()
    print(f"store={root / 'model_intelligence.sqlite3'}")
    return 0 if args.rebuild_only or successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
