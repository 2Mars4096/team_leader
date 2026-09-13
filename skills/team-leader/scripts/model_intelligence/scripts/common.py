#!/usr/bin/env python3
"""Shared deterministic storage, normalization, and ranking utilities."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = SKILL_ROOT / "data"
PROFILE_PATH = SKILL_ROOT / "references" / "task-profiles.json"
SCHEMA_VERSION = 1
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
USER_AGENT = "model-intelligence-skill/1.0 (+deterministic benchmark cache)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_dir(value: str | None = None) -> Path:
    path = value or os.environ.get("MODEL_INTELLIGENCE_DATA_DIR")
    return Path(path).expanduser().resolve() if path else DEFAULT_DATA_DIR.resolve()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def snapshot_path(root: Path, source: str, fetched_at: str, digest: str, suffix: str) -> Path:
    date_part = fetched_at[:10]
    safe_suffix = suffix.lstrip(".") or "bin"
    return root / "raw" / source / date_part / f"{digest[:16]}.{safe_suffix}"


def http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 2,
) -> tuple[bytes, Mapping[str, str]]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"}
    if headers:
        request_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404} or attempt == retries:
                raise
            last_error = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
            except ValueError:
                delay = 0.5 * (2**attempt)
            time.sleep(min(max(delay, 0.0), 10.0))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise
            last_error = exc
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"request failed: {last_error}")


def sanitize_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for key in ("OPENROUTER_API_KEY", "ARTIFICIAL_ANALYSIS_API_KEY"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(api[-_ ]?key|authorization|bearer)[=: ]+[^ ,;]+", r"\1=<redacted>", text)
    return text[:1000]


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    text = text.rstrip("%")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return int(number) if number is not None else None


def as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "supported"}:
        return True
    if text in {"false", "no", "0", "unsupported"}:
        return False
    return None


PROVIDER_PREFIXES = {
    "openai", "anthropic", "google", "deepseek", "z-ai", "zai", "mistralai",
    "mistral", "meta-llama", "qwen", "moonshotai", "x-ai", "cohere", "nvidia",
    "amazon", "alibaba", "minimax", "together", "vertex_ai", "azure",
}


def infer_provider(name: str, explicit: str | None = None) -> str | None:
    if explicit:
        return normalize_token(explicit)
    raw = name.strip().lower()
    if "/" in raw:
        prefix = raw.split("/", 1)[0]
        if prefix in PROVIDER_PREFIXES:
            return normalize_token(prefix)
    checks = (
        ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
        ("claude", "anthropic"), ("gemini", "google"), ("gemma", "google"),
        ("deepseek", "deepseek"), ("glm", "z-ai"), ("qwen", "alibaba"),
        ("kimi", "moonshotai"), ("grok", "x-ai"), ("llama", "meta"),
    )
    for needle, provider in checks:
        if needle in raw:
            return provider
    return None


def normalize_token(value: str) -> str:
    text = value.strip().lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9.+/-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-/")


def exact_model_key(name: str) -> str:
    text = name.strip()
    text = re.sub(r"^openrouter/", "", text, flags=re.I)
    return normalize_token(text)


def model_family(name: str) -> str:
    text = exact_model_key(name)
    parts = text.split("/")
    if len(parts) > 1 and parts[0] in PROVIDER_PREFIXES:
        text = "/".join(parts[1:])
    text = re.sub(r"-(19|20)\d{6}$", "", text)
    text = re.sub(r"-(19|20)\d{2}-\d{2}-\d{2}$", "", text)
    text = re.sub(r"-(preview|latest)-?(19|20)?\d{0,8}$", r"-\1", text)
    text = re.sub(r"-(low|medium|high|xhigh|max|thinking)$", "", text)
    return text.strip("-")


def reasoning_effort(name: str, explicit: str | None = None) -> str | None:
    if explicit:
        return normalize_token(explicit)
    match = re.search(r"(?:\(|-|\b)(low|medium|high|xhigh|max|thinking)(?:\)|-|\b)", name, re.I)
    return match.group(1).lower() if match else None


def open_database(root: Path, *, readonly: bool = False) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "model_intelligence.sqlite3"
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"historical store not found: {db_path}")
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    if not readonly:
        # DELETE journaling keeps the finished cache readable from read-only
        # Dropbox/project mounts without requiring SQLite sidecar files.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            source_date TEXT,
            status TEXT NOT NULL CHECK(status IN ('success','failed','skipped')),
            sha256 TEXT,
            raw_path TEXT,
            row_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            source_url TEXT NOT NULL,
            UNIQUE(source, fetched_at)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_time
            ON snapshots(source, fetched_at DESC);
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            benchmark TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'overall',
            model_name TEXT NOT NULL,
            model_key TEXT NOT NULL,
            model_family TEXT NOT NULL,
            provider TEXT,
            score REAL,
            rank INTEGER,
            rank_total INTEGER,
            price_input REAL,
            price_output REAL,
            latency REAL,
            throughput REAL,
            context_length INTEGER,
            supports_tools INTEGER,
            supports_files INTEGER,
            reasoning_effort TEXT,
            harness TEXT,
            observed_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_observations_lookup
            ON observations(source, benchmark, category, model_family);
        CREATE INDEX IF NOT EXISTS idx_observations_snapshot
            ON observations(snapshot_id);
        CREATE TABLE IF NOT EXISTS summaries (
            task TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            snapshot_fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(task, snapshot_fingerprint)
        );
        """
    )
    current = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if current is None:
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
        )
    elif int(current[0]) > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current[0]} is newer than supported {SCHEMA_VERSION}")
    elif int(current[0]) < SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current[0]} requires an explicit migration")
    connection.commit()


@dataclass(frozen=True)
class Observation:
    source: str
    benchmark: str
    category: str
    model_name: str
    model_key: str | None = None
    model_family: str | None = None
    provider: str | None = None
    score: float | None = None
    rank: int | None = None
    rank_total: int | None = None
    price_input: float | None = None
    price_output: float | None = None
    latency: float | None = None
    throughput: float | None = None
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_files: bool | None = None
    reasoning_effort: str | None = None
    harness: str | None = None
    observed_at: str | None = None
    metadata: Mapping[str, Any] | None = None

    def row(self, snapshot_id: int) -> tuple[Any, ...]:
        key = self.model_key or exact_model_key(self.model_name)
        family = self.model_family or model_family(key)
        provider = infer_provider(key, self.provider)
        effort = reasoning_effort(self.model_name, self.reasoning_effort)
        return (
            snapshot_id, self.source, normalize_token(self.benchmark), normalize_token(self.category or "overall"),
            self.model_name.strip(), key, family, provider, self.score, self.rank, self.rank_total,
            self.price_input, self.price_output, self.latency, self.throughput, self.context_length,
            None if self.supports_tools is None else int(self.supports_tools),
            None if self.supports_files is None else int(self.supports_files),
            effort, self.harness, self.observed_at, json_dumps(dict(self.metadata or {})),
        )


def add_snapshot(
    connection: sqlite3.Connection,
    *,
    source: str,
    fetched_at: str,
    source_url: str,
    status: str,
    source_date: str | None = None,
    sha256: str | None = None,
    raw_path: str | None = None,
    error: str | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO snapshots
           (source,fetched_at,source_date,status,sha256,raw_path,error,source_url)
           VALUES(?,?,?,?,?,?,?,?)""",
        (source, fetched_at, source_date, status, sha256, raw_path, error, source_url),
    )
    return int(cursor.lastrowid)


def add_observations(connection: sqlite3.Connection, snapshot_id: int, rows: Iterable[Observation]) -> int:
    payload = [row.row(snapshot_id) for row in rows]
    payload.sort(key=lambda item: (item[1], item[2], item[3], item[6], item[5]))
    connection.executemany(
        """INSERT INTO observations (
           snapshot_id,source,benchmark,category,model_name,model_key,model_family,provider,
           score,rank,rank_total,price_input,price_output,latency,throughput,context_length,
           supports_tools,supports_files,reasoning_effort,harness,observed_at,metadata_json
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        payload,
    )
    connection.execute("UPDATE snapshots SET row_count=? WHERE id=?", (len(payload), snapshot_id))
    return len(payload)


def load_profiles() -> Mapping[str, Any]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]


def latest_snapshot_ids(connection: sqlite3.Connection, as_of: str | None = None) -> dict[str, int]:
    parameters: list[Any] = []
    where = "status='success'"
    if as_of:
        where += " AND substr(fetched_at,1,10) <= ?"
        parameters.append(as_of)
    rows = connection.execute(
        f"""SELECT s.source, s.id
            FROM snapshots s
            JOIN (
              SELECT source, MAX(fetched_at) AS fetched_at
              FROM snapshots WHERE {where} GROUP BY source
            ) latest ON latest.source=s.source AND latest.fetched_at=s.fetched_at
            WHERE s.status='success'""",
        parameters,
    ).fetchall()
    return {str(row["source"]): int(row["id"]) for row in rows}


def snapshot_fingerprint(connection: sqlite3.Connection, ids: Mapping[str, int]) -> str:
    payload = []
    for source, snapshot_id in sorted(ids.items()):
        row = connection.execute(
            "SELECT source,fetched_at,sha256 FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        payload.append([row["source"], row["fetched_at"], row["sha256"]])
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def _percentile_scores(rows: Sequence[sqlite3.Row]) -> dict[int, float]:
    ranked = [row for row in rows if row["rank"] is not None and row["rank_total"] not in {None, 0, 1}]
    result: dict[int, float] = {}
    for row in ranked:
        result[int(row["id"])] = max(
            0.0, min(1.0, 1.0 - (float(row["rank"]) - 1.0) / (float(row["rank_total"]) - 1.0))
        )
    remaining = [row for row in rows if int(row["id"]) not in result and row["score"] is not None]
    ordered_values = sorted({float(row["score"]) for row in remaining})
    if len(ordered_values) == 1:
        for row in remaining:
            result[int(row["id"])] = 0.5
    elif ordered_values:
        positions = {value: index / (len(ordered_values) - 1) for index, value in enumerate(ordered_values)}
        for row in remaining:
            result[int(row["id"])] = positions[float(row["score"])]
    return result


def build_recommendation(
    connection: sqlite3.Connection,
    *,
    task: str,
    limit: int = 5,
    as_of: str | None = None,
    requirements: Sequence[str] = (),
    openrouter_only: bool = False,
    use_default_requirements: bool = True,
) -> dict[str, Any]:
    profiles = load_profiles()
    if task not in profiles:
        raise KeyError(f"unknown task profile {task!r}; choose from {', '.join(sorted(profiles))}")
    profile = profiles[task]
    ids = latest_snapshot_ids(connection, as_of)
    if not ids:
        raise RuntimeError("historical store has no successful snapshots; run refresh.py first")
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM observations WHERE snapshot_id IN ({placeholders})",
        tuple(ids.values()),
    ).fetchall()

    catalog: dict[str, sqlite3.Row] = {}
    for row in rows:
        if row["source"] == "openrouter" and row["benchmark"] == "catalog":
            current = catalog.get(row["model_family"])
            if current is None or str(row["observed_at"] or "") >= str(current["observed_at"] or ""):
                catalog[row["model_family"]] = row

    requested = {
        normalize_token(item) for item in profile.get("default_requirements", [])
    } if use_default_requirements else set()
    requested.update(normalize_token(item) for item in requirements)
    contributions: dict[str, list[dict[str, Any]]] = {}
    exact_variants: dict[str, sqlite3.Row] = {}
    total_weight = sum(float(signal["weight"]) for signal in profile["signals"])

    for signal_index, signal in enumerate(profile["signals"]):
        source_re = re.compile(f"^(?:{signal['source']})$", re.I)
        benchmark_re = re.compile(f"^(?:{signal['benchmark']})$", re.I)
        category_re = re.compile(f"^(?:{signal.get('category', '.*')})$", re.I)
        matched = [
            row for row in rows
            if source_re.search(row["source"])
            and benchmark_re.search(row["benchmark"])
            and category_re.search(row["category"])
            and (row["score"] is not None or row["rank"] is not None)
        ]
        groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in matched:
            groups.setdefault((row["source"], row["benchmark"], row["category"]), []).append(row)
        normalized: dict[int, float] = {}
        for group_rows in groups.values():
            normalized.update(_percentile_scores(group_rows))
        by_family: dict[str, list[tuple[float, sqlite3.Row]]] = {}
        for row in matched:
            value = normalized.get(int(row["id"]))
            if value is not None:
                by_family.setdefault(row["model_family"], []).append((value, row))
        weight = float(signal["weight"])
        for family, values in by_family.items():
            signal_value = statistics.fmean(value for value, _ in values)
            evidence_row = max(values, key=lambda item: (item[0], str(item[1]["observed_at"] or "")))[1]
            contributions.setdefault(family, []).append(
                {
                    "signal": signal_index,
                    "weight": weight,
                    "normalized": signal_value,
                    "source": evidence_row["source"],
                    "benchmark": evidence_row["benchmark"],
                    "category": evidence_row["category"],
                    "score": evidence_row["score"],
                    "rank": evidence_row["rank"],
                    "model_name": evidence_row["model_name"],
                    "observed_at": evidence_row["observed_at"],
                }
            )
            previous = exact_variants.get(family)
            if previous is None or str(evidence_row["observed_at"] or "") >= str(previous["observed_at"] or ""):
                exact_variants[family] = evidence_row

    recommendations: list[dict[str, Any]] = []
    for family, items in contributions.items():
        operational = catalog.get(family)
        if openrouter_only and operational is None:
            continue
        if "tools" in requested and (operational is None or operational["supports_tools"] != 1):
            continue
        if "files" in requested and (operational is None or operational["supports_files"] != 1):
            continue
        present_weight = sum(item["weight"] for item in items)
        if present_weight <= 0:
            continue
        raw_quality = sum(item["weight"] * item["normalized"] for item in items) / present_weight
        coverage = min(1.0, present_weight / total_weight) if total_weight else 0.0
        quality = raw_quality * (0.75 + 0.25 * coverage)
        variant = operational or exact_variants[family]
        evidence = sorted(items, key=lambda item: (-item["weight"], item["source"], item["benchmark"]))[:3]
        recommendations.append(
            {
                "model": variant["model_name"],
                "model_key": variant["model_key"],
                "model_family": family,
                "provider": variant["provider"],
                "quality_score": round(quality, 4),
                "coverage": round(coverage, 4),
                "evidence": [
                    {
                        "source": item["source"],
                        "benchmark": item["benchmark"],
                        "category": item["category"],
                        "score": item["score"],
                        "rank": item["rank"],
                        "observed_at": item["observed_at"],
                    }
                    for item in evidence
                ],
                "operational": None if operational is None else {
                    "openrouter_slug": operational["model_key"],
                    "supports_tools": None if operational["supports_tools"] is None else bool(operational["supports_tools"]),
                    "supports_files": None if operational["supports_files"] is None else bool(operational["supports_files"]),
                    "context_length": operational["context_length"],
                    "price_input_per_million": operational["price_input"],
                    "price_output_per_million": operational["price_output"],
                },
            }
        )
    recommendations.sort(key=lambda item: (-item["quality_score"], -item["coverage"], item["model_family"]))
    snapshots = []
    for source, snapshot_id in sorted(ids.items()):
        row = connection.execute(
            "SELECT fetched_at,source_date,row_count,source_url FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        snapshots.append(
            {
                "source": source,
                "fetched_at": row["fetched_at"],
                "source_date": row["source_date"],
                "rows": row["row_count"],
                "url": row["source_url"],
            }
        )
    return {
        "schema": "model-intelligence-context/v1",
        "task": task,
        "task_label": profile["label"],
        "as_of": as_of,
        "generated_at": utc_now(),
        "snapshot_fingerprint": snapshot_fingerprint(connection, ids),
        "requirements": sorted(requested),
        "recommendations": recommendations[:limit],
        "sources": snapshots,
        "limitations": [
            "Public benchmarks screen candidates; validate the exact provider, model, effort, prompt, tools, and harness on representative local tasks.",
            "Missing OpenRouter catalog metadata means compatibility is unknown, not unsupported.",
            "Quality scores are task-profile percentiles and are not comparable across different task profiles.",
        ],
    }


def compact_context(payload: Mapping[str, Any]) -> str:
    def short_number(value: Any) -> str:
        return "unknown" if value is None else f"{float(value):.8g}"

    lines = [
        f"MODEL INTELLIGENCE | task={payload['task']} | generated={payload['generated_at']}",
        f"Evidence snapshots: " + ", ".join(
            f"{item['source']}@{item['source_date'] or item['fetched_at'][:10]}"
            for item in payload["sources"]
        ),
    ]
    if payload.get("requirements"):
        lines.append("Requirements: " + ", ".join(payload["requirements"]))
    if not payload["recommendations"]:
        lines.append("No candidates met the evidence and compatibility filters.")
    for index, item in enumerate(payload["recommendations"], 1):
        op = item.get("operational")
        operational = ""
        if op:
            operational = (
                f" | OR={op['openrouter_slug']} tools={op['supports_tools']} files={op['supports_files']}"
                f" ctx={op['context_length']} price/M={short_number(op['price_input_per_million'])}/"
                f"{short_number(op['price_output_per_million'])}"
            )
        lines.append(
            f"{index}. {item['model']} | quality={item['quality_score']:.3f} coverage={item['coverage']:.2f}{operational}"
        )
        for evidence in item["evidence"]:
            value = f"rank={evidence['rank']}" if evidence["rank"] is not None else f"score={evidence['score']}"
            lines.append(
                f"   - {evidence['source']}:{evidence['benchmark']}/{evidence['category']} {value}"
            )
    lines.append("Use this shortlist as screening evidence; run a representative local eval before permanent routing.")
    return "\n".join(lines)
