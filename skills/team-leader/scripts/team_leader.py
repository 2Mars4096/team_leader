#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
INDEX_VERSION = 3
DEFAULT_PROVIDER = "codex"
DEFAULT_ROOT_NAME = ".team-leader"
LEGACY_ROOT_NAMES = (".agent-subsessions", ".codex-subsessions")
LEGACY_ROOTS_LABEL = ", ".join(f"./{name}" for name in LEGACY_ROOT_NAMES)
QUESTION_SECTION_HINTS = ("question", "blocker", "human", "decision")
ANSWER_LINE_RE = re.compile(r"^\s*[-*+]\s*`?([a-z0-9][a-z0-9-]*)`?\s*:\s*(.+?)\s*$", re.IGNORECASE)
PRELAUNCH_STATUSES = {"prepared", "blocked"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "exited"}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def epoch_now() -> int:
    return int(time.time())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def resolve_path(raw: str | Path) -> Path:
    return Path(raw).expanduser().resolve()


def default_root() -> Path:
    cwd = Path.cwd()
    default_path = cwd / DEFAULT_ROOT_NAME
    if default_path.exists():
        return default_path
    for legacy_name in LEGACY_ROOT_NAMES:
        legacy_path = cwd / legacy_name
        if legacy_path.exists():
            return legacy_path
    return default_path


def index_path(root: Path) -> Path:
    return root / "index.json"


def ensure_root(root: Path) -> None:
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir(parents=True, exist_ok=True)


@contextmanager
def root_lock(root: Path) -> Any:
    ensure_root(root)
    lock_path = root / ".lock"
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class DispatchOptions:
    provider: str
    name: str | None
    project: str | None
    task_id: str | None
    role: str | None
    summary: str | None
    prompt_text: str
    cd: Path
    sandbox: str | None
    model: str | None
    profile: str | None
    add_dirs: list[Path]
    configs: list[str]
    enables: list[str]
    disables: list[str]
    images: list[Path]
    search: bool
    skip_git_repo_check: bool
    ephemeral: bool
    full_auto: bool
    dangerous: bool
    dry_run: bool
    owned_paths: list[str]
    depends_on: list[str]


@dataclass(frozen=True)
class ProviderCapabilities:
    sandbox_modes: tuple[str, ...] = ()
    supports_search: bool = False
    supports_skip_git_repo_check: bool = False
    supports_ephemeral: bool = False
    supports_full_auto: bool = False
    supports_dangerous: bool = False
    supports_model: bool = False
    supports_profile: bool = False
    supports_add_dir: bool = False
    supports_config: bool = False
    supports_enable_disable: bool = False
    supports_images: bool = False
    supports_exec_resume: bool = False


@dataclass(frozen=True)
class ProviderAdapter:
    name: str
    session_label: str
    bin_env_var: str
    default_bin: str
    capabilities: ProviderCapabilities

    def resolved_bin(self) -> str:
        return os.environ.get(self.bin_env_var, self.default_bin)

    def validate_options(self, options: DispatchOptions) -> None:
        capabilities = self.capabilities
        if options.sandbox:
            if not capabilities.sandbox_modes:
                raise RuntimeError(f"provider {self.name} does not support --sandbox")
            if options.sandbox not in capabilities.sandbox_modes:
                supported = ", ".join(capabilities.sandbox_modes)
                raise RuntimeError(
                    f"provider {self.name} does not support sandbox {options.sandbox!r}. "
                    f"supported values: {supported}"
                )
        if options.model and not capabilities.supports_model:
            raise RuntimeError(f"provider {self.name} does not support --model")
        if options.profile and not capabilities.supports_profile:
            raise RuntimeError(f"provider {self.name} does not support --profile")
        if options.add_dirs and not capabilities.supports_add_dir:
            raise RuntimeError(f"provider {self.name} does not support --add-dir")
        if options.configs and not capabilities.supports_config:
            raise RuntimeError(f"provider {self.name} does not support --config")
        if (options.enables or options.disables) and not capabilities.supports_enable_disable:
            raise RuntimeError(f"provider {self.name} does not support --enable/--disable")
        if options.images and not capabilities.supports_images:
            raise RuntimeError(f"provider {self.name} does not support --image")
        if options.search and not capabilities.supports_search:
            raise RuntimeError(f"provider {self.name} does not support --search")
        if options.skip_git_repo_check and not capabilities.supports_skip_git_repo_check:
            raise RuntimeError(f"provider {self.name} does not support --skip-git-repo-check")
        if options.ephemeral and not capabilities.supports_ephemeral:
            raise RuntimeError(f"provider {self.name} does not support --ephemeral")
        if options.full_auto and not capabilities.supports_full_auto:
            raise RuntimeError(f"provider {self.name} does not support --full-auto")
        if options.dangerous and not capabilities.supports_dangerous:
            raise RuntimeError(f"provider {self.name} does not support --dangerous")

    def describe(self) -> dict[str, Any]:
        capabilities = self.capabilities
        return {
            "name": self.name,
            "session_label": self.session_label,
            "bin_env_var": self.bin_env_var,
            "default_bin": self.default_bin,
            "supported_sandbox_modes": list(capabilities.sandbox_modes),
            "supports_search": capabilities.supports_search,
            "supports_skip_git_repo_check": capabilities.supports_skip_git_repo_check,
            "supports_ephemeral": capabilities.supports_ephemeral,
            "supports_full_auto": capabilities.supports_full_auto,
            "supports_dangerous": capabilities.supports_dangerous,
            "supports_model": capabilities.supports_model,
            "supports_profile": capabilities.supports_profile,
            "supports_add_dir": capabilities.supports_add_dir,
            "supports_config": capabilities.supports_config,
            "supports_enable_disable": capabilities.supports_enable_disable,
            "supports_images": capabilities.supports_images,
            "supports_exec_resume": capabilities.supports_exec_resume,
        }

    def build_exec_command(
        self,
        *,
        prompt_path: Path,
        last_message_path: Path,
        options: DispatchOptions,
    ) -> list[str]:
        raise NotImplementedError

    def detect_session_id(self, run: dict[str, Any]) -> str | None:
        raise NotImplementedError

    def build_resume_command(self, run: dict[str, Any], exec_mode: bool) -> str:
        raise NotImplementedError


class CodexProvider(ProviderAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="codex",
            session_label="thread",
            bin_env_var="CODEX_BIN",
            default_bin="codex",
            capabilities=ProviderCapabilities(
                sandbox_modes=("read-only", "workspace-write", "danger-full-access"),
                supports_search=True,
                supports_skip_git_repo_check=True,
                supports_ephemeral=True,
                supports_full_auto=True,
                supports_dangerous=True,
                supports_model=True,
                supports_profile=True,
                supports_add_dir=True,
                supports_config=True,
                supports_enable_disable=True,
                supports_images=True,
                supports_exec_resume=True,
            ),
        )

    def build_exec_command(
        self,
        *,
        prompt_path: Path,
        last_message_path: Path,
        options: DispatchOptions,
    ) -> list[str]:
        command = [
            self.resolved_bin(),
            "exec",
            "--json",
            "--output-last-message",
            str(last_message_path),
            "--cd",
            str(options.cd),
        ]
        if options.sandbox:
            command.extend(["--sandbox", options.sandbox])
        if options.model:
            command.extend(["--model", options.model])
        if options.profile:
            command.extend(["--profile", options.profile])
        for add_dir in options.add_dirs:
            command.extend(["--add-dir", str(add_dir)])
        for config in options.configs:
            command.extend(["--config", config])
        for feature in options.enables:
            command.extend(["--enable", feature])
        for feature in options.disables:
            command.extend(["--disable", feature])
        for image in options.images:
            command.extend(["--image", str(image)])
        if options.search:
            command.append("--search")
        if options.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if options.ephemeral:
            command.append("--ephemeral")
        if options.full_auto:
            command.append("--full-auto")
        if options.dangerous:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.append("-")
        return command

    def detect_session_id(self, run: dict[str, Any]) -> str | None:
        stdout_path = Path(run["stdout_path"])
        json_candidates = read_jsonl_candidates(stdout_path)
        if json_candidates:
            return json_candidates[0]
        known = known_thread_ids()
        for candidate in infer_thread_ids_from_logs(int(run["started_epoch"])):
            if candidate in known:
                return candidate
        candidates = infer_thread_ids_from_logs(int(run["started_epoch"]))
        if len(candidates) == 1:
            return candidates[0]
        return None

    def build_resume_command(self, run: dict[str, Any], exec_mode: bool) -> str:
        session_id = get_session_id(run)
        if not session_id:
            raise RuntimeError("run has no detected session_id; use reconcile or attach-session first")
        if exec_mode and not self.capabilities.supports_exec_resume:
            raise RuntimeError(f"provider {self.name} does not support non-interactive resume")
        cwd = shlex.quote(run["cwd"])
        provider_bin = shlex.quote(self.resolved_bin())
        if exec_mode:
            return f"cd {cwd} && {provider_bin} exec resume {shlex.quote(session_id)} -"
        return f"cd {cwd} && {provider_bin} resume {shlex.quote(session_id)}"


PROVIDERS: dict[str, ProviderAdapter] = {
    DEFAULT_PROVIDER: CodexProvider(),
}


def get_provider(name: str | None) -> ProviderAdapter:
    provider_name = (name or DEFAULT_PROVIDER).strip().lower()
    adapter = PROVIDERS.get(provider_name)
    if adapter is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise RuntimeError(
            f"unsupported provider: {provider_name}. supported providers: {supported}"
        )
    return adapter


def provider_for_run(run: dict[str, Any]) -> ProviderAdapter:
    return get_provider(str(run.get("provider") or DEFAULT_PROVIDER))


def get_session_id(run: dict[str, Any]) -> str | None:
    session_id = run.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    thread_id = run.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    return None


def set_session_id(run: dict[str, Any], session_id: str | None) -> None:
    run["session_id"] = session_id
    if str(run.get("provider") or DEFAULT_PROVIDER) == "codex":
        run["thread_id"] = session_id


def load_index(root: Path) -> dict[str, Any]:
    path = index_path(root)
    if not path.exists():
        return {"version": INDEX_VERSION, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid index file: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid index file: {path}")
    runs = data.get("runs")
    if not isinstance(runs, list):
        data["runs"] = []
    data.setdefault("version", INDEX_VERSION)
    for run in data["runs"]:
        if not isinstance(run, dict):
            continue
        run.setdefault("provider", DEFAULT_PROVIDER)
        run.setdefault("stdout_path", run.get("stdout_jsonl"))
        run.setdefault("project", None)
        run.setdefault("project_slug", None)
        run.setdefault("task_id", None)
        run.setdefault("role", None)
        run.setdefault("summary", None)
        run.setdefault("created_at", run.get("launched_at"))
        run.setdefault("owned_paths", [])
        run.setdefault("depends_on", [])
        run.setdefault("dispatch_state", None)
        run.setdefault("blocked_on", [])
        if get_session_id(run):
            set_session_id(run, get_session_id(run))
    return data


def save_index(root: Path, data: dict[str, Any]) -> None:
    ensure_root(root)
    data["version"] = INDEX_VERSION
    index_path(root).write_text(
        json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def make_run_id(existing: set[str], label: str | None) -> str:
    base = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(label or 'run')}"
    run_id = base
    counter = 2
    while run_id in existing:
        run_id = f"{base}-{counter}"
        counter += 1
    return run_id


def quote_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def delete_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def derive_summary(prompt_text: str) -> str:
    for raw_line in prompt_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        line = re.sub(r"^(goal|objective|summary|task)\s*:\s*", "", line, flags=re.IGNORECASE)
        line = line.strip(" -")
        if not line:
            continue
        if len(line) > 96:
            return line[:93].rstrip() + "..."
        return line
    return "Child session work"


def short_summary(value: str | None, max_chars: int = 52) -> str:
    if not value:
        return "-"
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def project_slug(project_name: str) -> str:
    return slugify(project_name)


def project_root(root: Path, run: dict[str, Any]) -> Path:
    slug = str(run.get("project_slug") or "")
    if not slug:
        raise RuntimeError("run has no project slug")
    return root / "projects" / slug


def format_inline_list(values: list[str]) -> str:
    if not values:
        return "-"
    return ", ".join(f"`{value}`" for value in values)


def format_short_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) >= 19:
        return value[:19] + "Z" if value.endswith("Z") else value[:19]
    return value


def run_status_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "other": 0,
    }
    for run in runs:
        status = str(run.get("status") or "")
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    return counts


def run_sort_key(run: dict[str, Any]) -> tuple[str, str]:
    created_at = str(run.get("created_at") or run.get("launched_at") or "")
    return created_at, str(run.get("run_id") or "")


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def preview_text(text: str | None, max_lines: int = 6, max_chars: int = 700) -> str:
    if not text:
        return "_No child report yet._"
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append("...")
    preview = "\n".join(lines).strip()
    if len(preview) > max_chars:
        preview = preview[: max_chars - 3].rstrip() + "..."
    return preview or "_No child report yet._"


def relative_owned_paths(run: dict[str, Any]) -> list[str]:
    cwd_raw = normalize_optional_text(run.get("cwd"))
    cwd = Path(cwd_raw) if cwd_raw else None
    result: list[str] = []
    for raw in normalize_str_list(run.get("owned_paths"), "owned_paths"):
        path = Path(raw)
        if path.is_absolute() and cwd:
            try:
                path = path.relative_to(cwd)
            except ValueError:
                pass
        normalized = str(path).replace("\\", "/").strip("/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def path_overlaps(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.startswith(right + "/"):
        return True
    if right.startswith(left + "/"):
        return True
    return False


def run_is_writer(run: dict[str, Any]) -> bool:
    sandbox = normalize_optional_text(run.get("sandbox"))
    if sandbox and sandbox != "read-only":
        return True
    role = (normalize_optional_text(run.get("role")) or "").lower()
    return role in {"implementation", "implementer", "writer", "owner", "manager"}


def extract_section_items(text: str, hints: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    active = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if active:
                break
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            active = any(hint in heading for hint in hints)
            continue
        if not active:
            continue
        if line.startswith(("-", "*", "+")):
            cleaned = line[1:].strip()
            if cleaned:
                items.append(cleaned)
            continue
        items.append(line)
    return items


def extract_questions(text: str | None) -> list[str]:
    if not text:
        return []
    questions: list[str] = []
    for item in extract_section_items(text, QUESTION_SECTION_HINTS):
        questions.append(item)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "?" not in line:
            continue
        cleaned = re.sub(r"^\s*[-*+0-9.()]+\s*", "", line).strip()
        if cleaned and cleaned not in questions:
            questions.append(cleaned)
    unique: list[str] = []
    seen: set[str] = set()
    for question in questions:
        if question in seen:
            continue
        seen.add(question)
        unique.append(question)
    return unique


def question_id_for(run: dict[str, Any], text: str) -> str:
    payload = f"{run['run_id']}\n{text}".encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:10]
    return f"q-{digest}"


def build_question_record(run: dict[str, Any], text: str) -> dict[str, str]:
    return {
        "id": question_id_for(run, text),
        "run_id": str(run["run_id"]),
        "task_id": str(run.get("task_id") or run["run_id"]),
        "summary": str(run.get("summary") or "-"),
        "text": text,
    }


def load_answers(project_dir: Path) -> dict[str, str]:
    text = read_text_if_exists(project_dir / "answers.md")
    if not text:
        return {}
    answers: dict[str, str] = {}
    for raw_line in text.splitlines():
        match = ANSWER_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        question_id = match.group(1).strip()
        answer = match.group(2).strip()
        if answer:
            answers[question_id] = answer
    return answers


def unanswered_questions(question_records: list[dict[str, str]], answers: dict[str, str]) -> list[dict[str, str]]:
    return [record for record in question_records if record["id"] not in answers]


def answered_questions(question_records: list[dict[str, str]], answers: dict[str, str]) -> list[dict[str, str]]:
    return [record for record in question_records if record["id"] in answers]


def last_message_for_run(run: dict[str, Any]) -> str | None:
    return read_text_if_exists(Path(run["last_message_path"]))


def latest_live_note(run: dict[str, Any]) -> str | None:
    path = Path(run["stdout_path"])
    if not path.exists():
        return None
    latest: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "agent_message" and isinstance(payload.get("text"), str):
                latest = payload["text"]
                continue
            if payload.get("type") != "item.completed":
                continue
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                latest = item["text"]
    return latest


def detect_conflict_risks(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    writers = [run for run in runs if run_is_writer(run)]
    for idx, left in enumerate(writers):
        left_paths = relative_owned_paths(left)
        if not left_paths:
            continue
        for right in writers[idx + 1 :]:
            right_paths = relative_owned_paths(right)
            if not right_paths:
                continue
            overlap = [
                path
                for path in left_paths
                for other in right_paths
                if path_overlaps(path, other)
            ]
            if not overlap:
                continue
            unique_overlap = sorted(set(overlap))
            conflicts.append(
                {
                    "left_run": str(left["run_id"]),
                    "right_run": str(right["run_id"]),
                    "left_task": str(left.get("task_id") or left["run_id"]),
                    "right_task": str(right.get("task_id") or right["run_id"]),
                    "paths": ", ".join(f"`{path}`" for path in unique_overlap),
                }
            )
    return conflicts


def project_workspace_dir(root: Path, project_name: str, slug: str | None = None) -> Path:
    return root / "projects" / (slug or project_slug(project_name))


def dependency_pool(index: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    project_slug_value = normalize_optional_text(run.get("project_slug"))
    pool = [candidate for candidate in index["runs"] if candidate is not run]
    if not project_slug_value:
        return pool
    return [
        candidate
        for candidate in pool
        if normalize_optional_text(candidate.get("project_slug")) == project_slug_value
    ]


def unresolved_dependencies(index: dict[str, Any], run: dict[str, Any]) -> list[str]:
    depends_on = normalize_str_list(run.get("depends_on"), "depends_on")
    if not depends_on:
        return []
    pool = dependency_pool(index, run)
    unresolved: list[str] = []
    for task_id in depends_on:
        candidates = [
            candidate
            for candidate in pool
            if normalize_optional_text(candidate.get("task_id")) == task_id
        ]
        if any(str(candidate.get("status") or "") == "completed" for candidate in candidates):
            continue
        unresolved.append(task_id)
    return unresolved


def compute_dispatch_state(index: dict[str, Any], run: dict[str, Any]) -> tuple[str, list[str]]:
    status = str(run.get("status") or "")
    if status == "running":
        return "running", []
    if status in TERMINAL_STATUSES:
        return status, []
    if status == "dry-run":
        return "dry-run", []
    blocked_on = unresolved_dependencies(index, run)
    if blocked_on:
        return "blocked", blocked_on
    return "ready", []


def update_dispatch_metadata(index: dict[str, Any]) -> None:
    for run in index["runs"]:
        dispatch_state, blocked_on = compute_dispatch_state(index, run)
        run["dispatch_state"] = dispatch_state
        run["blocked_on"] = list(blocked_on)


def ensure_project_workspace(root: Path, project_name: str, slug: str) -> Path:
    project_dir = project_workspace_dir(root, project_name, slug)
    (project_dir / "reports").mkdir(parents=True, exist_ok=True)
    return project_dir


def collect_question_records(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    question_records: list[dict[str, str]] = []
    seen_question_ids: set[str] = set()
    for run in runs:
        last_message = last_message_for_run(run)
        for question_text in extract_questions(last_message):
            record = build_question_record(run, question_text)
            if record["id"] in seen_question_ids:
                continue
            seen_question_ids.add(record["id"])
            question_records.append(record)
    return question_records


def project_stage_snapshot(
    runs: list[dict[str, Any]],
    question_records: list[dict[str, str]],
    answers: dict[str, str],
    conflicts: list[dict[str, str]],
) -> dict[str, str]:
    counts = run_status_counts(runs)
    active = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("status") or "") == "running"
    ]
    blocked = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("dispatch_state") or "") == "blocked"
    ]
    failed = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("status") or "") == "failed"
    ]
    open_questions = unanswered_questions(question_records, answers)
    total = len(runs)
    progress = (
        f"{counts['completed']}/{total} completed, "
        f"{counts['running']} running, {len(blocked)} blocked, "
        f"{len(open_questions)} open questions"
    )
    if conflicts:
        current_stage = "resolve-conflicts"
        stage_reason = f"{len(conflicts)} ownership overlap risk(s) detected"
        next_action = "Narrow write ownership or convert one child into a reviewer"
        focus = f"{conflicts[0]['left_task']} vs {conflicts[0]['right_task']}"
    elif open_questions:
        current_stage = "waiting-for-human"
        first_question = open_questions[0]
        stage_reason = f"{len(open_questions)} human decision(s) still open"
        next_action = f"Answer `{first_question['id']}` for `{first_question['task_id']}`"
        focus = short_summary(first_question["text"], max_chars=96)
    elif active:
        current_stage = "running-children"
        first_active = active[0]
        live_note = latest_live_note(first_active)
        stage_reason = f"{len(active)} child session(s) currently active"
        next_action = f"Monitor `{first_active.get('task_id') or first_active['run_id']}` until it finishes"
        focus = short_summary(
            live_note or str(first_active.get("summary") or "-"),
            max_chars=96,
        )
    elif blocked:
        first_blocked = blocked[0]
        current_stage = "waiting-on-dependencies"
        stage_reason = f"{len(blocked)} task(s) blocked on prerequisites"
        next_action = (
            f"Wait for {format_inline_list(normalize_str_list(first_blocked.get('blocked_on'), 'blocked_on'))}"
        )
        focus = str(first_blocked.get("summary") or "-")
    elif failed:
        first_failed = failed[0]
        current_stage = "review-failures"
        stage_reason = f"{len(failed)} run(s) failed"
        next_action = f"Inspect `{first_failed['run_id']}` with `show` or `tail`"
        focus = str(first_failed.get("summary") or "-")
    elif total > 0 and counts["completed"] == total:
        current_stage = "completed"
        stage_reason = "All tracked runs finished successfully"
        next_action = "Review `manager-summary.md` and decide the next batch"
        focus = "All current tasks are complete"
    else:
        current_stage = "idle"
        stage_reason = "No active child sessions right now"
        next_action = "Dispatch the next task batch"
        focus = "Waiting for manager input"
    return {
        "current_stage": current_stage,
        "stage_reason": stage_reason,
        "next_action": next_action,
        "focus": focus,
        "progress": progress,
    }


def render_project_overview(project_name: str, project_dir: Path, runs: list[dict[str, Any]]) -> str:
    counts = run_status_counts(runs)
    cwd_values = sorted({str(run.get("cwd") or "-") for run in runs})
    watcher_state, watcher_heartbeat = monitor_state(project_dir.parent.parent)
    blocked_count = sum(1 for run in runs if str(run.get("dispatch_state") or "") == "blocked")
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    conflicts = detect_conflict_risks(runs)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts)
    watcher_line = f"- Manager watcher: `{watcher_state}`"
    if watcher_heartbeat:
        watcher_line += f" (last heartbeat `{watcher_heartbeat}`)"
    return "\n".join(
        [
            f"# {project_name}",
            "",
            "Start here with `dashboard.md` for live progress. While children are active, the manager keeps these markdown files refreshed in the background. Use `manager-summary.md` for the latest manager synthesis and `questions.md` for anything the human needs to answer.",
            "",
            "## Metadata",
            "",
            f"- Updated: `{utc_now()}`",
            f"- Project folder: `{project_dir}`",
            f"- Current stage: `{stage['current_stage']}`",
            f"- Stage reason: {stage['stage_reason']}",
            f"- Next action: {stage['next_action']}",
            f"- Current focus: {stage['focus']}",
            f"- Progress: {stage['progress']}",
            watcher_line,
            f"- Working directories: {format_inline_list(cwd_values)}",
            f"- Total tracked runs: `{len(runs)}`",
            f"- Blocked by dependencies: `{blocked_count}`",
            f"- Running: `{counts['running']}`",
            f"- Completed: `{counts['completed']}`",
            f"- Failed: `{counts['failed']}`",
            f"- Cancelled: `{counts['cancelled']}`",
            "",
            "## Files",
            "",
            "- `dashboard.md`: live run table, active notes, questions, and conflict alerts",
            "- `tasks.md`: task-oriented ledger with summaries and ownership",
            "- `manager-summary.md`: concise manager snapshot",
            "- `questions.md`: human-facing questions and blockers",
            "- `answers.md`: human-maintained answers keyed by question id",
            "- `answers-template.md`: copy-ready answer lines for open questions",
            "- `conflicts.md`: ownership overlap and conflict-risk notes",
            "- `reports/`: one markdown report per child run",
            "",
        ]
    )


def render_task_ledger(runs: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for run in sorted(runs, key=run_sort_key):
        rows.append(
            [
                str(run.get("task_id") or run["run_id"]),
                str(run.get("summary") or "-"),
                str(run.get("role") or "-"),
                str(run.get("status") or "-"),
                str(run.get("dispatch_state") or "-"),
                format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on")),
                str(run["run_id"]),
                format_inline_list(normalize_str_list(run.get("depends_on"), "depends_on")),
                format_inline_list(relative_owned_paths(run)),
                str(run.get("session_id") or "-"),
            ]
        )
    return "\n".join(
        [
            "# Task Ledger",
            "",
            markdown_table(
                ["task", "summary", "role", "status", "dispatch", "blocked_on", "run", "depends_on", "owned_paths", "session"],
                rows or [["-", "-", "-", "-", "-", "-", "-", "-", "-", "-"]],
            ),
            "",
        ]
    )


def render_dashboard(
    project_name: str,
    runs: list[dict[str, Any]],
    conflicts: list[dict[str, str]],
    question_records: list[dict[str, str]],
    answers: dict[str, str],
) -> str:
    counts = run_status_counts(runs)
    project_dir = project_root(Path(runs[0]["run_dir"]).parent.parent, runs[0]) if runs else None
    watcher_state = "idle"
    watcher_heartbeat = None
    if project_dir is not None:
        watcher_state, watcher_heartbeat = monitor_state(project_dir.parent.parent)
    rows: list[list[str]] = []
    active: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts)
    for run in sorted(runs, key=run_sort_key):
        rows.append(
            [
                str(run["run_id"]),
                str(run.get("task_id") or "-"),
                short_summary(str(run.get("summary") or "-")),
                str(run.get("role") or "-"),
                str(run.get("status") or "-"),
                str(run.get("dispatch_state") or "-"),
                str(run.get("session_id") or "-"),
                format_inline_list(relative_owned_paths(run)),
                format_short_timestamp(normalize_optional_text(run.get("launched_at"))),
            ]
        )
        status = str(run.get("status") or "")
        if status == "running":
            active.append(run)
        elif str(run.get("dispatch_state") or "") == "blocked":
            blocked.append(run)
        elif status in {"completed", "failed", "cancelled"}:
            completed.append(run)
    lines = [
        f"# {project_name} Dashboard",
        "",
        f"- Updated: `{utc_now()}`",
        f"- Current stage: `{stage['current_stage']}`",
        f"- Stage reason: {stage['stage_reason']}",
        f"- Next action: {stage['next_action']}",
        f"- Current focus: {stage['focus']}",
        f"- Progress: {stage['progress']}",
        f"- Manager watcher: `{watcher_state}`",
        f"- Running: `{counts['running']}`",
        f"- Completed: `{counts['completed']}`",
        f"- Failed: `{counts['failed']}`",
        f"- Cancelled: `{counts['cancelled']}`",
        "",
        "## Run Table",
        "",
        markdown_table(
            ["run", "task", "summary", "role", "status", "dispatch", "session", "owned_paths", "launched"],
            rows or [["-", "-", "-", "-", "-", "-", "-", "-", "-"]],
        ),
        "",
        "## Active Runs",
        "",
    ]
    if watcher_heartbeat:
        lines.insert(3, f"- Last watcher heartbeat: `{watcher_heartbeat}`")
    if not active:
        lines.append("_No active runs._")
    else:
        for run in active:
            live_note = latest_live_note(run)
            lines.extend(
                [
                    f"### {run['run_id']}",
                    "",
                    f"- Task: `{run.get('task_id') or '-'}`",
                    f"- Summary: {run.get('summary') or '-'}",
                    f"- Role: `{run.get('role') or '-'}`",
                    f"- Session: `{run.get('session_id') or '-'}`",
                    f"- Owned paths: {format_inline_list(relative_owned_paths(run))}",
                    "",
                ]
            )
            if live_note:
                lines.extend([preview_text(live_note, max_lines=3, max_chars=320), ""])
    lines.extend(["## Blocked Runs", ""])
    if not blocked:
        lines.append("_No tasks are currently blocked on dependencies._")
    else:
        for run in blocked:
            lines.extend(
                [
                    f"### {run['run_id']}",
                    "",
                    f"- Task: `{run.get('task_id') or '-'}`",
                    f"- Summary: {run.get('summary') or '-'}",
                    f"- Waiting on: {format_inline_list(normalize_str_list(run.get('blocked_on'), 'blocked_on'))}",
                    "",
                ]
            )
    lines.extend(["## Recent Child Output", ""])
    if not completed:
        lines.append("_No finished child output yet._")
    else:
        for run in sorted(completed, key=run_sort_key)[-3:]:
            lines.extend(
                [
                    f"### {run['run_id']}",
                    "",
                    f"_Summary: {run.get('summary') or '-'}_",
                    "",
                    preview_text(last_message_for_run(run)),
                    "",
                ]
            )
    lines.extend(["## Questions For Humans", ""])
    if not open_questions:
        lines.append("_No human questions detected._")
    else:
        for question in open_questions:
            lines.append(
                f"- `{question['id']}` for `{question['task_id']}`: {question['text']}"
            )
    lines.extend(["", "## Human Answers", ""])
    if not answered:
        lines.append("_No answered questions recorded yet._")
    else:
        for question in answered[-5:]:
            lines.append(
                f"- `{question['id']}` for `{question['task_id']}`: {answers[question['id']]}"
            )
    lines.extend(["", "## Conflict Risks", ""])
    if not conflicts:
        lines.append("_No owned-path overlap detected._")
    else:
        for conflict in conflicts:
            lines.append(
                f"- `{conflict['left_run']}` vs `{conflict['right_run']}` on {conflict['paths']}"
            )
    lines.append("")
    return "\n".join(lines)


def render_manager_summary(
    project_name: str,
    runs: list[dict[str, Any]],
    conflicts: list[dict[str, str]],
    question_records: list[dict[str, str]],
    answers: dict[str, str],
) -> str:
    counts = run_status_counts(runs)
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    blocked = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("dispatch_state") or "") == "blocked"
    ]
    stage = project_stage_snapshot(runs, question_records, answers, conflicts)
    lines = [
        f"# {project_name} Manager Summary",
        "",
        f"- Updated: `{utc_now()}`",
        f"- Current stage: `{stage['current_stage']}`",
        f"- Stage reason: {stage['stage_reason']}",
        f"- Next action: {stage['next_action']}",
        f"- Current focus: {stage['focus']}",
        f"- Progress: {stage['progress']}",
        f"- Total runs: `{len(runs)}`",
        f"- Blocked: `{len(blocked)}`",
        f"- Running: `{counts['running']}`",
        f"- Completed: `{counts['completed']}`",
        f"- Failed: `{counts['failed']}`",
        f"- Cancelled: `{counts['cancelled']}`",
        "",
        "## Human Attention",
        "",
    ]
    if not open_questions and not conflicts:
        lines.append("_No human questions or conflict alerts detected._")
    else:
        for question in open_questions:
            lines.append(
                f"- Open question `{question['id']}` for `{question['task_id']}`: {question['text']}"
            )
        for conflict in conflicts:
            lines.append(
                f"- Conflict risk between `{conflict['left_run']}` and `{conflict['right_run']}` on {conflict['paths']}"
            )
    lines.extend(["", "## Human Answers", ""])
    if not answered:
        lines.append("_No human answers recorded yet._")
    else:
        for question in answered[-5:]:
            lines.append(
                f"- `{question['id']}` for `{question['task_id']}`: {answers[question['id']]}"
            )
    lines.extend(["", "## Blocked Tasks", ""])
    if not blocked:
        lines.append("_No blocked tasks._")
    else:
        for run in blocked:
            lines.append(
                f"- `{run.get('task_id') or run['run_id']}` waiting on {format_inline_list(normalize_str_list(run.get('blocked_on'), 'blocked_on'))}"
            )
    lines.extend(["", "## Finished Runs", ""])
    finished = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("status") or "") in {"completed", "failed", "cancelled"}
    ]
    if not finished:
        lines.append("_No finished runs yet._")
    else:
        for run in finished[-5:]:
            lines.extend(
                [
                    f"### {run['run_id']} ({run.get('status') or '-'})",
                    "",
                    f"_Summary: {run.get('summary') or '-'}_",
                    "",
                    preview_text(last_message_for_run(run), max_lines=5, max_chars=500),
                    "",
                ]
            )
    return "\n".join(lines)


def render_questions(question_records: list[dict[str, str]], answers: dict[str, str]) -> str:
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    lines = [
        "# Questions For Humans",
        "",
        f"_Updated: `{utc_now()}`_",
        "",
        "Copy any line from `answers-template.md` into `answers.md` and replace `TODO` with the human answer.",
        "",
        "## Open",
        "",
    ]
    if not open_questions:
        lines.append("_No open human questions detected._")
    else:
        for question in open_questions:
            lines.extend(
                [
                    f"### {question['id']}",
                    "",
                    f"- Task: `{question['task_id']}`",
                    f"- Run: `{question['run_id']}`",
                    f"- Summary: {question['summary']}",
                    f"- Question: {question['text']}",
                    "",
                ]
            )
    lines.extend(["## Answered", ""])
    if not answered:
        lines.append("_No answered questions recorded yet._")
    else:
        for question in answered:
            lines.extend(
                [
                    f"### {question['id']}",
                    "",
                    f"- Task: `{question['task_id']}`",
                    f"- Run: `{question['run_id']}`",
                    f"- Question: {question['text']}",
                    f"- Answer: {answers[question['id']]}",
                    "",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def render_answers_stub() -> str:
    return "\n".join(
        [
            "# Answers For Humans",
            "",
            "Add one bullet per answered question in this format:",
            "",
            "- `q-example1234`: your answer here",
            "",
            "The manager reads only bullet lines in that format and leaves the rest of this file alone.",
            "",
        ]
    )


def render_answers_template(question_records: list[dict[str, str]], answers: dict[str, str]) -> str:
    open_questions = unanswered_questions(question_records, answers)
    lines = [
        "# Answer Template",
        "",
        "Copy any line below into `answers.md` and replace `TODO` with the human answer.",
        "",
    ]
    if not open_questions:
        lines.append("_No open questions right now._")
        lines.append("")
        return "\n".join(lines)
    for question in open_questions:
        lines.extend(
            [
                f"- `{question['id']}`: TODO",
                f"  Source: `{question['task_id']}` / `{question['run_id']}`",
                f"  Question: {question['text']}",
                "",
            ]
        )
    return "\n".join(lines)


def render_conflicts(conflicts: list[dict[str, str]]) -> str:
    lines = [
        "# Conflict Risks",
        "",
        f"_Updated: `{utc_now()}`_",
        "",
    ]
    if not conflicts:
        lines.append("_No owned-path overlap detected._")
        lines.append("")
        return "\n".join(lines)
    rows = [
        [item["left_run"], item["right_run"], item["left_task"], item["right_task"], item["paths"]]
        for item in conflicts
    ]
    lines.extend(
        [
            markdown_table(["left_run", "right_run", "left_task", "right_task", "overlap"], rows),
            "",
            "These are conflict risks for the manager to resolve, not automatic merges. Resolve by narrowing ownership or turning one child into a reviewer.",
            "",
        ]
    )
    return "\n".join(lines)


def render_project_cli_summary(root: Path, project_name: str, runs: list[dict[str, Any]]) -> str:
    if not runs:
        return ""
    slug = str(runs[0].get("project_slug") or project_slug(project_name))
    project_dir = project_workspace_dir(root, project_name, slug)
    watcher_state, watcher_heartbeat = monitor_state(root)
    counts = run_status_counts(runs)
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    conflicts = detect_conflict_risks(runs)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts)
    active = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("status") or "") == "running"
    ]
    blocked = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("dispatch_state") or "") == "blocked"
    ]
    lines = [
        f"project={project_name}",
        f"workspace={project_dir}",
        f"landing_page={project_dir / 'README.md'}",
        f"dashboard={project_dir / 'dashboard.md'}",
        f"current_stage={stage['current_stage']}",
        f"stage_reason={stage['stage_reason']}",
        f"next_action={stage['next_action']}",
        f"current_focus={stage['focus']}",
        f"progress={stage['progress']}",
    ]
    watcher_line = f"watcher={watcher_state}"
    if watcher_heartbeat:
        watcher_line += f" heartbeat={watcher_heartbeat}"
    lines.append(watcher_line)
    lines.append(
        "counts="
        f"running:{counts['running']} blocked:{len(blocked)} completed:{counts['completed']} "
        f"failed:{counts['failed']} cancelled:{counts['cancelled']}"
    )
    lines.extend(["", "active_runs:"])
    if not active:
        lines.append("- none")
    else:
        for run in active:
            live_note = short_summary(latest_live_note(run), max_chars=80)
            lines.append(
                f"- {run.get('task_id') or run['run_id']}: {run.get('summary') or '-'}"
            )
            if live_note != "-":
                lines.append(f"  note: {live_note}")
    lines.extend(["", "blocked_runs:"])
    if not blocked:
        lines.append("- none")
    else:
        for run in blocked:
            waiting_on = format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on"))
            lines.append(
                f"- {run.get('task_id') or run['run_id']}: {run.get('summary') or '-'}"
            )
            lines.append(f"  waiting_on: {waiting_on}")
    lines.extend(["", "open_questions:"])
    if not open_questions:
        lines.append("- none")
    else:
        for question in open_questions[:5]:
            lines.append(
                f"- {question['id']} [{question['task_id']}] {short_summary(question['text'], max_chars=90)}"
            )
    lines.extend(["", "recent_answers:"])
    if not answered:
        lines.append("- none")
    else:
        for question in answered[-5:]:
            lines.append(
                f"- {question['id']} [{question['task_id']}] {short_summary(answers[question['id']], max_chars=90)}"
            )
    lines.extend(["", "conflicts:"])
    if not conflicts:
        lines.append("- none")
    else:
        for conflict in conflicts:
            lines.append(
                f"- {conflict['left_task']} vs {conflict['right_task']} on {conflict['paths']}"
            )
    return "\n".join(lines)


def write_project_reports(project_dir: Path, runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    question_records = collect_question_records(runs)
    by_run: dict[str, list[dict[str, str]]] = {}
    for record in question_records:
        by_run.setdefault(record["run_id"], []).append(record)
    for run in runs:
        report_path = project_dir / "reports" / f"{run['run_id']}.md"
        last_message = last_message_for_run(run)
        run_questions = by_run.get(str(run["run_id"]), [])
        content = "\n".join(
            [
                f"# {run['run_id']}",
                "",
                f"- Task: `{run.get('task_id') or run['run_id']}`",
                f"- Summary: {run.get('summary') or '-'}",
                f"- Role: `{run.get('role') or '-'}`",
                f"- Status: `{run.get('status') or '-'}`",
                f"- Session: `{run.get('session_id') or '-'}`",
                f"- Owned paths: {format_inline_list(relative_owned_paths(run))}",
                f"- Depends on: {format_inline_list(normalize_str_list(run.get('depends_on'), 'depends_on'))}",
                "",
                "## Child Output",
                "",
                last_message.strip() if last_message and last_message.strip() else "_No child report yet._",
                "",
                "## Questions Raised",
                "",
                *(
                    [f"- `{record['id']}`: {record['text']}" for record in run_questions]
                    if run_questions
                    else ["_No human questions detected from this child yet._"]
                ),
                "",
            ]
        )
        write_text(report_path, content)
    return question_records


def sync_projects(root: Path, index: dict[str, Any]) -> None:
    grouped: dict[str, dict[str, Any]] = {}
    for run in index["runs"]:
        project_name = normalize_optional_text(run.get("project"))
        if not project_name:
            continue
        slug = str(run.get("project_slug") or project_slug(project_name))
        payload = grouped.setdefault(slug, {"name": project_name, "runs": []})
        payload["runs"].append(run)
    for slug, payload in grouped.items():
        project_name = str(payload["name"])
        runs = sorted(payload["runs"], key=run_sort_key)
        project_dir = ensure_project_workspace(root, project_name, slug)
        question_records = write_project_reports(project_dir, runs)
        answers_path = project_dir / "answers.md"
        if not answers_path.exists():
            write_text(answers_path, render_answers_stub())
        answers = load_answers(project_dir)
        conflicts = detect_conflict_risks(runs)
        delete_if_exists(project_dir / "project.md")
        write_text(project_dir / "README.md", render_project_overview(project_name, project_dir, runs))
        write_text(project_dir / "tasks.md", render_task_ledger(runs))
        write_text(
            project_dir / "dashboard.md",
            render_dashboard(project_name, runs, conflicts, question_records, answers),
        )
        write_text(
            project_dir / "manager-summary.md",
            render_manager_summary(project_name, runs, conflicts, question_records, answers),
        )
        write_text(project_dir / "questions.md", render_questions(question_records, answers))
        write_text(
            project_dir / "answers-template.md",
            render_answers_template(question_records, answers),
        )
        write_text(project_dir / "conflicts.md", render_conflicts(conflicts))


def save_index_and_sync(root: Path, data: dict[str, Any]) -> None:
    update_dispatch_metadata(data)
    save_index(root, data)
    sync_projects(root, data)


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_jsonl_candidates(path: Path) -> list[str]:
    candidates: list[str] = []
    if not path.exists():
        return candidates
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            collect_uuid_candidates(payload, candidates)
    return candidates


def collect_uuid_candidates(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = key.lower()
            if isinstance(value, str) and (
                "thread" in key_l or "session" in key_l or "conversation" in key_l or key_l == "id"
            ):
                for match in UUID_RE.findall(value):
                    out.append(match)
            collect_uuid_candidates(value, out)
        return
    if isinstance(node, list):
        for item in node:
            collect_uuid_candidates(item, out)
        return
    if isinstance(node, str):
        for match in UUID_RE.findall(node):
            out.append(match)


def codex_log_db() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "logs_1.sqlite"


def known_thread_ids() -> set[str]:
    db = codex_log_db()
    if not db.exists():
        return set()
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "select distinct thread_id from logs where thread_id is not null"
        ).fetchall()
    return {row[0] for row in rows if row and row[0]}


def infer_thread_ids_from_logs(started_epoch: int) -> list[str]:
    db = codex_log_db()
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            """
            select thread_id, min(ts) as first_ts
            from logs
            where thread_id is not null and ts >= ?
            group by thread_id
            order by first_ts asc
            """,
            (max(0, started_epoch - 2),),
        ).fetchall()
    candidates: list[str] = []
    for thread_id, first_ts in rows:
        if not thread_id:
            continue
        if first_ts is None:
            continue
        if int(first_ts) <= started_epoch + 180:
            candidates.append(thread_id)
    return candidates


def resolve_run(index: dict[str, Any], run_ref: str) -> dict[str, Any]:
    exact = [run for run in index["runs"] if run["run_id"] == run_ref]
    if exact:
        return exact[0]
    prefix = [run for run in index["runs"] if run["run_id"].startswith(run_ref)]
    if len(prefix) == 1:
        return prefix[0]
    if not prefix:
        raise RuntimeError(f"unknown run: {run_ref}")
    raise RuntimeError(f"ambiguous run prefix: {run_ref}")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def monitor_pid_path(root: Path) -> Path:
    return root / "monitor.pid"


def monitor_heartbeat_path(root: Path) -> Path:
    return root / "monitor.heartbeat"


def read_pid_file(path: Path) -> int | None:
    value = read_text_if_exists(path)
    if not value:
        return None
    text = value.strip()
    if not text.isdigit():
        return None
    return int(text)


def index_has_active_runs(index: dict[str, Any]) -> bool:
    for run in index["runs"]:
        if str(run.get("status") or "") == "running":
            return True
        if pid_alive(run.get("pid")):
            return True
    return False


def ensure_monitor(root: Path, index: dict[str, Any]) -> None:
    if not index_has_active_runs(index):
        return
    pid_path = monitor_pid_path(root)
    current_pid = read_pid_file(pid_path)
    if current_pid and pid_alive(current_pid):
        return
    delete_if_exists(pid_path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "monitor",
            "--root",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(root),
        start_new_session=True,
    )
    write_text(pid_path, f"{process.pid}\n")


def monitor_state(root: Path) -> tuple[str, str | None]:
    pid = read_pid_file(monitor_pid_path(root))
    heartbeat = normalize_optional_text(read_text_if_exists(monitor_heartbeat_path(root)))
    if pid and pid_alive(pid):
        return "active", heartbeat
    return "idle", heartbeat


def start_run_process(run: dict[str, Any]) -> None:
    stdout_path = Path(run["stdout_path"])
    stderr_path = Path(run["stderr_log"])
    runner_path = Path(run["runner_path"])
    run_dir = Path(run["run_dir"])
    stdout_fh = stdout_path.open("w", encoding="utf-8")
    stderr_fh = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        ["/bin/bash", str(runner_path)],
        stdout=stdout_fh,
        stderr=stderr_fh,
        cwd=str(run["cwd"]),
        start_new_session=True,
    )
    stdout_fh.close()
    stderr_fh.close()
    run["pid"] = process.pid
    run["status"] = "running"
    run["dispatch_state"] = "running"
    run["blocked_on"] = []
    run["launched_at"] = utc_now()
    run["started_epoch"] = epoch_now()
    write_text(run_dir / "state.txt", "running\n")


def launch_ready_runs(root: Path, index: dict[str, Any]) -> None:
    update_dispatch_metadata(index)
    for run in sorted(index["runs"], key=run_sort_key):
        if str(run.get("status") or "") not in PRELAUNCH_STATUSES:
            continue
        dispatch_state = str(run.get("dispatch_state") or "")
        if dispatch_state == "blocked":
            run["status"] = "blocked"
            continue
        start_run_process(run)
    update_dispatch_metadata(index)


def run_has_provider_artifacts(run: dict[str, Any]) -> bool:
    status = str(run.get("status") or "")
    if status in {"dry-run", "prepared", "blocked"}:
        return False
    run_dir = Path(run["run_dir"])
    stdout_path = Path(run["stdout_path"])
    if stdout_path.exists():
        return True
    if (run_dir / "state.txt").exists():
        return True
    return bool(run.get("pid"))


def refresh_run(run: dict[str, Any]) -> None:
    run_dir = Path(run["run_dir"])
    state = read_text_if_exists(run_dir / "state.txt")
    exit_code = read_text_if_exists(run_dir / "exit_code.txt")
    finished_at = read_text_if_exists(run_dir / "finished_at.txt")
    if state:
        run["status"] = state.strip()
    elif pid_alive(run.get("pid")):
        run["status"] = "running"
    elif run.get("status") == "running":
        run["status"] = "exited"
    if exit_code and exit_code.strip().lstrip("-").isdigit():
        run["exit_code"] = int(exit_code.strip())
    if finished_at:
        run["finished_at"] = finished_at.strip()
    if run_has_provider_artifacts(run) and not get_session_id(run):
        session_id = provider_for_run(run).detect_session_id(run)
        if session_id:
            set_session_id(run, session_id)


def build_runner_script(
    *,
    command: list[str],
    prompt_path: Path,
    state_path: Path,
    exit_code_path: Path,
    started_path: Path,
    finished_path: Path,
) -> str:
    cmd = quote_command(command)
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -uo pipefail",
            f"printf '%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > {shlex.quote(str(started_path))}",
            f"printf '%s\\n' running > {shlex.quote(str(state_path))}",
            f"{cmd} < {shlex.quote(str(prompt_path))}",
            "status=$?",
            f"printf '%s\\n' \"$status\" > {shlex.quote(str(exit_code_path))}",
            "if [ \"$status\" -eq 0 ]; then",
            f"  printf '%s\\n' completed > {shlex.quote(str(state_path))}",
            "else",
            f"  printf '%s\\n' failed > {shlex.quote(str(state_path))}",
            "fi",
            f"printf '%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > {shlex.quote(str(finished_path))}",
            "exit \"$status\"",
            "",
        ]
    )


def print_run_summary(run: dict[str, Any]) -> None:
    session_id = get_session_id(run) or "-"
    exit_code = run.get("exit_code")
    exit_text = str(exit_code) if exit_code is not None else "-"
    task_text = str(run.get("task_id") or "-")
    summary_text = short_summary(str(run.get("summary") or "-"), max_chars=42)
    dispatch_state = str(run.get("dispatch_state") or run.get("status") or "-")
    blocked_on = normalize_str_list(run.get("blocked_on"), "blocked_on")
    if blocked_on:
        dispatch_state = f"{dispatch_state}:{','.join(blocked_on[:2])}"
    print(
        f"{run['run_id']:<30} {run['status']:<10} provider={run.get('provider', DEFAULT_PROVIDER):<6} "
        f"pid={run.get('pid') or '-':<8} exit={exit_text:<4} task={task_text:<18} "
        f"dispatch={dispatch_state:<18} "
        f"session={session_id} summary={summary_text}"
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        data = load_index(root)
        save_index_and_sync(root, data)
    print(root)
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    payload = []
    for name in sorted(PROVIDERS):
        adapter = PROVIDERS[name]
        record = adapter.describe()
        record["default"] = name == DEFAULT_PROVIDER
        payload.append(record)
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    for record in payload:
        suffix = " (default)" if record["default"] else ""
        sandboxes = ",".join(record["supported_sandbox_modes"]) or "-"
        print(
            f"{record['name']}{suffix}: session_label={record['session_label']} "
            f"bin_env={record['bin_env_var']} default_bin={record['default_bin']} "
            f"sandboxes={sandboxes} exec_resume={str(record['supports_exec_resume']).lower()}"
        )
    return 0


def materialize_run(
    root: Path,
    index: dict[str, Any],
    options: DispatchOptions,
) -> dict[str, Any]:
    ensure_root(root)
    adapter = get_provider(options.provider)
    adapter.validate_options(options)
    run_id = make_run_id({run["run_id"] for run in index["runs"]}, options.name)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    prompt_path = run_dir / "prompt.md"
    last_message_path = run_dir / "last_message.md"
    stdout_path = run_dir / "stdout.jsonl"
    stderr_path = run_dir / "stderr.log"
    state_path = run_dir / "state.txt"
    exit_code_path = run_dir / "exit_code.txt"
    started_path = run_dir / "started_at.txt"
    finished_path = run_dir / "finished_at.txt"
    runner_path = run_dir / "runner.sh"

    write_text(prompt_path, options.prompt_text)

    command = adapter.build_exec_command(
        prompt_path=prompt_path,
        last_message_path=last_message_path,
        options=options,
    )

    write_text(
        runner_path,
        build_runner_script(
            command=command,
            prompt_path=prompt_path,
            state_path=state_path,
            exit_code_path=exit_code_path,
            started_path=started_path,
            finished_path=finished_path,
        ),
    )
    runner_path.chmod(0o755)
    write_text(run_dir / "command.txt", quote_command(command))

    run = {
        "run_id": run_id,
        "name": options.name or run_id,
        "provider": adapter.name,
        "project": options.project,
        "project_slug": project_slug(options.project) if options.project else None,
        "task_id": options.task_id,
        "role": options.role,
        "summary": options.summary or derive_summary(options.prompt_text),
        "status": "dry-run" if options.dry_run else "prepared",
        "run_dir": str(run_dir),
        "cwd": str(options.cd),
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "stdout_jsonl": str(stdout_path),
        "stderr_log": str(stderr_path),
        "last_message_path": str(last_message_path),
        "runner_path": str(runner_path),
        "session_id": None,
        "thread_id": None,
        "pid": None,
        "exit_code": None,
        "created_at": utc_now(),
        "launched_at": None,
        "finished_at": None,
        "started_epoch": None,
        "sandbox": options.sandbox,
        "model": options.model,
        "profile": options.profile,
        "search": options.search,
        "skip_git_repo_check": options.skip_git_repo_check,
        "ephemeral": options.ephemeral,
        "full_auto": options.full_auto,
        "dangerous": options.dangerous,
        "add_dirs": [str(path) for path in options.add_dirs],
        "configs": list(options.configs),
        "enables": list(options.enables),
        "disables": list(options.disables),
        "images": [str(path) for path in options.images],
        "owned_paths": list(options.owned_paths),
        "depends_on": list(options.depends_on),
        "dispatch_state": "dry-run" if options.dry_run else "ready",
        "blocked_on": [],
    }

    index["runs"].append(run)
    if not options.dry_run:
        launch_ready_runs(root, index)
    save_index_and_sync(root, index)
    ensure_monitor(root, index)
    print_run_summary(run)
    if options.project:
        project_dir = project_workspace_dir(root, options.project, run.get("project_slug"))
        print(f"workspace={project_dir}")
        print(f"landing_page={project_dir / 'README.md'}")
        print(f"dashboard={project_dir / 'dashboard.md'}")
    return run


def parse_prompt(args: argparse.Namespace) -> str:
    if bool(args.prompt) == bool(args.prompt_file):
        raise RuntimeError("provide exactly one of --prompt or --prompt-file")
    if args.prompt:
        return args.prompt
    return resolve_path(args.prompt_file).read_text(encoding="utf-8")


def common_dispatch_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "options": DispatchOptions(
            provider=args.provider,
            name=args.name,
            project=normalize_optional_text(args.project),
            task_id=normalize_optional_text(args.task_id),
            role=normalize_optional_text(args.role),
            summary=normalize_optional_text(args.summary),
            prompt_text=parse_prompt(args),
            cd=resolve_path(args.cd) if args.cd else Path.cwd(),
            sandbox=args.sandbox,
            model=args.model,
            profile=args.profile,
            add_dirs=[resolve_path(path) for path in args.add_dir],
            configs=list(args.config),
            enables=list(args.enable),
            disables=list(args.disable),
            images=[resolve_path(path) for path in args.image],
            search=bool(args.search),
            skip_git_repo_check=bool(args.skip_git_repo_check),
            ephemeral=bool(args.ephemeral),
            full_auto=bool(args.full_auto),
            dangerous=bool(args.dangerous),
            dry_run=bool(args.dry_run),
            owned_paths=normalize_str_list(args.owned_path, "owned_paths"),
            depends_on=normalize_str_list(args.depends_on, "depends_on"),
        ),
    }


def cmd_dispatch(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        materialize_run(root, index, **common_dispatch_kwargs(args))
    return 0


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        runs = payload
    elif isinstance(payload, dict) and isinstance(payload.get("runs"), list):
        runs = payload["runs"]
    else:
        raise RuntimeError("manifest must be a list or an object with a runs array")
    specs: list[dict[str, Any]] = []
    for item in runs:
        if not isinstance(item, dict):
            raise RuntimeError("manifest entries must be objects")
        specs.append(item)
    return specs


def merged_prompt_spec(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    prompt = spec.get("prompt")
    prompt_file = spec.get("prompt_file")
    if bool(prompt) == bool(prompt_file):
        raise RuntimeError("each manifest run must set exactly one of prompt or prompt_file")
    if prompt is not None and not isinstance(prompt, str):
        raise RuntimeError("manifest prompt must be a string")
    if prompt_file is not None and not isinstance(prompt_file, str):
        raise RuntimeError("manifest prompt_file must be a string")
    return prompt, prompt_file


def cmd_batch(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    manifest_path = resolve_path(args.file)
    specs = load_manifest(manifest_path)

    with root_lock(root):
        index = load_index(root)
        for spec in specs:
            prompt, prompt_file = merged_prompt_spec(spec)
            temp_args = argparse.Namespace(**vars(args))
            temp_args.name = spec.get("name", args.name)
            temp_args.project = spec.get("project", args.project)
            temp_args.task_id = spec.get("task_id", args.task_id)
            temp_args.role = spec.get("role", args.role)
            temp_args.summary = spec.get("summary", args.summary)
            temp_args.prompt = prompt
            temp_args.prompt_file = prompt_file
            temp_args.provider = spec.get("provider", args.provider)
            temp_args.cd = spec.get("cd", args.cd)
            temp_args.sandbox = spec.get("sandbox", args.sandbox)
            temp_args.model = spec.get("model", args.model)
            temp_args.profile = spec.get("profile", args.profile)
            temp_args.search = spec.get("search", args.search)
            temp_args.skip_git_repo_check = spec.get(
                "skip_git_repo_check", args.skip_git_repo_check
            )
            temp_args.ephemeral = spec.get("ephemeral", args.ephemeral)
            temp_args.full_auto = spec.get("full_auto", args.full_auto)
            temp_args.dangerous = spec.get("dangerous", args.dangerous)
            temp_args.add_dir = spec.get("add_dirs", args.add_dir)
            temp_args.config = spec.get("configs", args.config)
            temp_args.enable = spec.get("enables", args.enable)
            temp_args.disable = spec.get("disables", args.disable)
            temp_args.image = spec.get("images", args.image)
            temp_args.owned_path = spec.get("owned_paths", args.owned_path)
            temp_args.depends_on = spec.get("depends_on", args.depends_on)
            materialize_run(root, index, **common_dispatch_kwargs(temp_args))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name: str | None = None
    with root_lock(root):
        index = load_index(root)
        for run in index["runs"]:
            refresh_run(run)
        launch_ready_runs(root, index)
        save_index_and_sync(root, index)
        ensure_monitor(root, index)
        runs = index["runs"]
        if args.project:
            project_filter = args.project.strip().lower()
            runs = [
                run
                for run in runs
                if project_filter
                in {
                    str(run.get("project") or "").strip().lower(),
                    str(run.get("project_slug") or "").strip().lower(),
                }
            ]
            if runs:
                project_name = str(runs[0].get("project") or runs[0].get("project_slug") or args.project)
    if args.json:
        print(json.dumps(runs, ensure_ascii=True, indent=2))
        return 0
    if not runs:
        print("no runs")
        return 0
    if project_name:
        print(render_project_cli_summary(root, project_name, runs))
        print()
        print("runs:")
    for run in runs:
        print_run_summary(run)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        refresh_run(run)
        launch_ready_runs(root, index)
        save_index_and_sync(root, index)
        ensure_monitor(root, index)
        last_message = read_text_if_exists(Path(run["last_message_path"]))
    print(json.dumps(run, ensure_ascii=True, indent=2, sort_keys=True))
    if last_message:
        print()
        print(last_message.rstrip())
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        refresh_run(run)
        launch_ready_runs(root, index)
        save_index_and_sync(root, index)
        ensure_monitor(root, index)
        path = Path(run["stderr_log"] if args.stderr else run["stdout_jsonl"])
    if not path.exists():
        print(f"missing log: {path}", file=sys.stderr)
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


def cmd_resume_cmd(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        refresh_run(run)
        launch_ready_runs(root, index)
        save_index_and_sync(root, index)
        ensure_monitor(root, index)
        command = provider_for_run(run).build_resume_command(run, args.exec)
    print(command)
    return 0


def cmd_attach_session(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        set_session_id(run, args.session_id)
        save_index_and_sync(root, index)
    print(f"{run['run_id']} -> {args.session_id}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        targets = [resolve_run(index, args.run)] if args.run else index["runs"]
        for run in targets:
            refresh_run(run)
        launch_ready_runs(root, index)
        save_index_and_sync(root, index)
        ensure_monitor(root, index)
    for run in targets:
        print_run_summary(run)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        refresh_run(run)
        pid = run.get("pid")
        if not pid or not pid_alive(pid):
            raise RuntimeError("run is not currently alive")
        sig = signal.SIGKILL if args.force else signal.SIGTERM
        os.killpg(pid, sig)
        run["status"] = "cancelled"
        run["finished_at"] = utc_now()
        run_dir = Path(run["run_dir"])
        write_text(run_dir / "state.txt", "cancelled\n")
        write_text(run_dir / "finished_at.txt", run["finished_at"] + "\n")
        save_index_and_sync(root, index)
    print_run_summary(run)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    pid_path = monitor_pid_path(root)
    heartbeat_path = monitor_heartbeat_path(root)
    current_pid = os.getpid()
    write_text(pid_path, f"{current_pid}\n")
    try:
        while True:
            with root_lock(root):
                index = load_index(root)
                for run in index["runs"]:
                    refresh_run(run)
                launch_ready_runs(root, index)
                save_index_and_sync(root, index)
                write_text(heartbeat_path, utc_now() + "\n")
                if not index_has_active_runs(index):
                    break
            time.sleep(max(1, int(args.interval)))
    finally:
        with root_lock(root):
            if read_pid_file(pid_path) == current_pid:
                delete_if_exists(pid_path)
                delete_if_exists(heartbeat_path)
    return 0


def add_common_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help="CLI provider adapter. Currently codex is implemented; the control plane is shaped for future providers.",
    )
    parser.add_argument("--name", help="Human-friendly run label")
    parser.add_argument("--project", help="Project name for automatic markdown aggregation and dashboards")
    parser.add_argument("--task-id", help="Stable task id inside the project workspace")
    parser.add_argument("--role", help="Worker role such as research, implementation, reviewer, or manager")
    parser.add_argument("--summary", help="Short human-readable summary of what this child session is working on")
    parser.add_argument("--prompt", help="Prompt text for the child session")
    parser.add_argument("--prompt-file", help="Path to a prompt file for the child session")
    parser.add_argument("--cd", help="Working directory for the child session")
    parser.add_argument(
        "--sandbox",
        help="Sandbox mode for the child session. Values are validated by the selected provider adapter.",
    )
    parser.add_argument("--model", help="Provider model override")
    parser.add_argument("--profile", help="Provider profile override")
    parser.add_argument("--add-dir", action="append", default=[], help="Additional writable directory where supported")
    parser.add_argument("--config", action="append", default=[], help="Pass through provider --config")
    parser.add_argument("--enable", action="append", default=[], help="Pass through provider --enable")
    parser.add_argument("--disable", action="append", default=[], help="Pass through provider --disable")
    parser.add_argument("--image", action="append", default=[], help="Image path to attach where supported")
    parser.add_argument("--owned-path", action="append", default=[], help="Declared owned file or directory path for conflict-risk tracking")
    parser.add_argument("--depends-on", action="append", default=[], help="Task ids that must complete before this task is ready")
    parser.add_argument("--search", action="store_true", help="Enable provider live web search where supported")
    parser.add_argument(
        "--skip-git-repo-check",
        action="store_true",
        help="Allow child sessions outside a Git repository where supported",
    )
    parser.add_argument("--ephemeral", action="store_true", help="Run child without persisting provider session files")
    parser.add_argument("--full-auto", action="store_true", help="Run child with provider full-auto mode")
    parser.add_argument(
        "--dangerous",
        action="store_true",
        help="Run child with provider dangerous no-sandbox mode",
    )
    parser.add_argument("--dry-run", action="store_true", help="Create the run directory but do not launch the provider CLI")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage real child sessions as subsessions, with a Codex provider implemented first.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize the controller directory")
    init_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    init_p.set_defaults(func=cmd_init)

    providers_p = sub.add_parser("providers", help="List supported provider adapters")
    providers_p.add_argument("--json", action="store_true", help="Print provider metadata as JSON")
    providers_p.set_defaults(func=cmd_providers)

    dispatch_p = sub.add_parser("dispatch", help="Dispatch one child session")
    add_common_run_options(dispatch_p)
    dispatch_p.set_defaults(func=cmd_dispatch)

    batch_p = sub.add_parser("batch", help="Dispatch multiple child sessions from a JSON manifest")
    add_common_run_options(batch_p)
    batch_p.add_argument("--file", required=True, help="Path to a JSON manifest")
    batch_p.set_defaults(func=cmd_batch)

    status_p = sub.add_parser("status", help="Show tracked runs")
    status_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    status_p.add_argument("--project", help="Filter to one project name or project slug")
    status_p.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    status_p.set_defaults(func=cmd_status)

    show_p = sub.add_parser("show", help="Show one run and its last message")
    show_p.add_argument("run", help="Run id or unique prefix")
    show_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    show_p.set_defaults(func=cmd_show)

    tail_p = sub.add_parser("tail", help="Tail stdout or stderr for one run")
    tail_p.add_argument("run", help="Run id or unique prefix")
    tail_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    tail_p.add_argument("-n", "--lines", type=int, default=20, help="Number of lines to print")
    tail_p.add_argument("--stderr", action="store_true", help="Show stderr instead of stdout JSONL")
    tail_p.set_defaults(func=cmd_tail)

    resume_p = sub.add_parser("resume-cmd", help="Print a shell-ready provider resume command")
    resume_p.add_argument("run", help="Run id or unique prefix")
    resume_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    resume_p.add_argument("--exec", action="store_true", help="Print a non-interactive provider resume command")
    resume_p.set_defaults(func=cmd_resume_cmd)

    attach_p = sub.add_parser("attach-session", help="Manually attach a provider session id to a run")
    attach_p.add_argument("run", help="Run id or unique prefix")
    attach_p.add_argument("session_id", help="Provider session id")
    attach_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    attach_p.set_defaults(func=cmd_attach_session)

    attach_thread_p = sub.add_parser("attach-thread", help="Backward-compatible alias for Codex thread ids")
    attach_thread_p.add_argument("run", help="Run id or unique prefix")
    attach_thread_p.add_argument("session_id", help="Codex thread id")
    attach_thread_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    attach_thread_p.set_defaults(func=cmd_attach_session)

    reconcile_p = sub.add_parser("reconcile", help="Refresh run statuses and infer missing session ids")
    reconcile_p.add_argument("run", nargs="?", help="Optional run id or unique prefix")
    reconcile_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    reconcile_p.set_defaults(func=cmd_reconcile)

    cancel_p = sub.add_parser("cancel", help="Cancel a running child session")
    cancel_p.add_argument("run", help="Run id or unique prefix")
    cancel_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    cancel_p.add_argument("--force", action="store_true", help="Use SIGKILL instead of SIGTERM")
    cancel_p.set_defaults(func=cmd_cancel)

    monitor_p = sub.add_parser("monitor", help=argparse.SUPPRESS)
    monitor_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    monitor_p.add_argument("--interval", type=int, default=2, help=argparse.SUPPRESS)
    monitor_p.set_defaults(func=cmd_monitor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
