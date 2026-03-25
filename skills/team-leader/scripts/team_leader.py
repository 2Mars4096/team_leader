#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import os.path
import re
import shlex
import shutil
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
DEFAULT_MAX_PARALLEL_SESSIONS = 8
DEFAULT_MAX_RELEASES_PER_CYCLE = 2
DEFAULT_RELEASE_WINDOW_SECONDS = 15
DEFAULT_LAST_MESSAGE_BYTES = 128 * 1024
DEFAULT_JSONL_SCAN_BYTES = 8 * 1024 * 1024
STDOUT_WARNING_BYTES = 8 * 1024 * 1024
STDERR_WARNING_BYTES = 4 * 1024 * 1024
CHILD_RUN_ENV = "TEAM_LEADER_CHILD_RUN"
MONITOR_RUN_ENV = "TEAM_LEADER_MONITOR_RUN"
QUESTION_SECTION_HINTS = ("question", "blocker", "human", "decision")
ANSWER_LINE_RE = re.compile(r"^\s*[-*+]\s*`?([a-z0-9][a-z0-9-]*)`?\s*:\s*(.+?)\s*$", re.IGNORECASE)
PRELAUNCH_STATUSES = {"prepared", "blocked"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "exited"}
PROJECT_BRIEF_FILE = "brief.json"
PROJECT_BRIEF_MD = "brief.md"
PROJECT_PLAN_FILE = "launch-plan.json"
PROJECT_PLAN_MD = "launch-plan.md"
PROJECT_VALIDATION_FILE = "validation.json"
PROJECT_VALIDATION_MD = "validation.md"
PLANNER_TASK_PREFIX = "manager-plan"
PLANNER_ROLE = "manager"
PLANNER_SOURCE = "team-leader-planner"
AUTONOMY_MODES = ("manual", "guided", "continuous")
CLARIFICATION_MODES = ("auto", "off")
INTEGRATION_READY_STATES = {"applied", "no-changes"}
INTEGRATION_BLOCKING_STATES = {"pending", "conflict", "scope-violation", "apply-failed", "commit-failed"}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def epoch_now() -> int:
    return int(time.time())


def parse_timestamp_epoch(value: str | None) -> int | None:
    text = normalize_optional_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def resolve_path(raw: str | Path) -> Path:
    return Path(raw).expanduser().resolve()


def resolve_executable(raw: str, *, cwd: Path | None = None) -> str:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        if candidate.exists():
            return str(candidate.resolve())
        raise RuntimeError(f"unable to resolve executable path: {raw}")
    if os.sep in raw:
        base = cwd or Path.cwd()
        resolved = (base / candidate).resolve()
        if resolved.exists():
            return str(resolved)
        raise RuntimeError(f"unable to resolve executable path: {raw}")
    found = shutil.which(raw)
    if found:
        return str(Path(found).resolve())
    raise RuntimeError(f"unable to locate executable on PATH: {raw}")


def path_looks_like_skill_dir(path: Path) -> bool:
    return (
        (path / "SKILL.md").exists()
        and (path / "scripts" / "team_leader.py").exists()
        and (path / "agents" / "openai.yaml").exists()
    )


def default_root() -> Path:
    cwd = Path.cwd()
    if path_looks_like_skill_dir(cwd):
        raise RuntimeError(
            "current working directory looks like the team-leader skill directory. "
            "Run this controller from the target project directory so the default "
            f"{DEFAULT_ROOT_NAME} root is created there, or pass --root and --cd explicitly."
        )
    default_path = cwd / DEFAULT_ROOT_NAME
    if default_path.exists():
        return default_path
    for legacy_name in LEGACY_ROOT_NAMES:
        legacy_path = cwd / legacy_name
        if legacy_path.exists():
            return legacy_path
    return default_path


def git_run(args: list[str], *, cwd: Path, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        input=input_text,
        capture_output=True,
        check=check,
    )


def git_toplevel(path: Path) -> Path | None:
    try:
        proc = git_run(["rev-parse", "--show-toplevel"], cwd=path)
    except subprocess.CalledProcessError:
        return None
    value = proc.stdout.strip()
    return resolve_path(value) if value else None


def git_head(path: Path) -> str | None:
    try:
        proc = git_run(["rev-parse", "HEAD"], cwd=path)
    except subprocess.CalledProcessError:
        return None
    value = proc.stdout.strip()
    return value or None


def git_has_tracked_changes(path: Path, base_ref: str) -> tuple[list[str], str]:
    try:
        changed = git_run(["diff", "--name-only", base_ref], cwd=path)
        patch = git_run(["diff", "--binary", base_ref], cwd=path)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"git diff failed in {path}") from exc
    changed_paths = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
    return changed_paths, patch.stdout


def path_within_owned_paths(path: str, owned_paths: list[str]) -> bool:
    normalized = path.strip().strip("/")
    if not normalized:
        return False
    for owned in owned_paths:
        candidate = owned.strip().strip("/")
        if not candidate:
            continue
        if path_overlaps(normalized, candidate):
            return True
    return False


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
        provider_bin = normalize_optional_text(run.get("provider_bin")) or self.resolved_bin()
        if os.path.basename(provider_bin) != "codex":
            return None
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
        run.setdefault("provider_bin", None)
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
        run.setdefault("planner_source", None)
        run.setdefault("planner_reason", None)
        run.setdefault("plan_applied_at", None)
        run.setdefault("plan_apply_error", None)
        run.setdefault("planned_run_ids", [])
        run.setdefault("source_cwd", run.get("cwd"))
        run.setdefault("source_repo_root", None)
        run.setdefault("source_repo_rel_cwd", None)
        run.setdefault("workspace_mode", "direct")
        run.setdefault("worktree_path", None)
        run.setdefault("workspace_base_ref", None)
        run.setdefault("workspace_prepared_at", None)
        run.setdefault("integration_state", None)
        run.setdefault("integration_note", None)
        run.setdefault("integration_updated_at", None)
        run.setdefault("changed_paths", [])
        run.setdefault("artifact_sizes", {})
        run.setdefault("output_warnings", [])
        run.setdefault("last_message_truncated", False)
        run.setdefault("last_message_original_bytes", None)
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


def child_prompt_guard(prompt_text: str) -> str:
    lines = [
        "You are a child Codex session launched by team-leader.",
        "Do not invoke team-leader, do not start nested team-leader-managed sessions, and do not recursively delegate back into this same manager.",
        "If you believe more delegation or replanning is needed, report that need in your response for the parent manager to handle.",
        "",
        prompt_text.strip(),
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_child_cli_guard(run_dir: Path, adapter: ProviderAdapter, real_bin: str) -> Path:
    guard_dir = run_dir / "child-bin"
    guard_dir.mkdir(parents=True, exist_ok=True)
    blocked_names = {
        adapter.default_bin,
        os.path.basename(adapter.resolved_bin()),
        os.path.basename(real_bin),
    }
    script = "\n".join(
        [
            "#!/usr/bin/env bash",
            'echo "nested provider launch is disabled inside a team-leader child session." >&2',
            'echo "Report replanning or delegation needs back to the parent manager instead." >&2',
            "exit 97",
            "",
        ]
    )
    for name in sorted(item for item in blocked_names if item):
        path = guard_dir / name
        write_text(path, script)
        path.chmod(0o755)
    return guard_dir


def max_parallel_sessions() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_MAX_PARALLEL_SESSIONS"))
    if raw is None:
        return DEFAULT_MAX_PARALLEL_SESSIONS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_PARALLEL_SESSIONS


def max_releases_per_cycle() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_MAX_RELEASES_PER_CYCLE"))
    if raw is None:
        return DEFAULT_MAX_RELEASES_PER_CYCLE
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_RELEASES_PER_CYCLE


def max_release_window_seconds() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_RELEASE_WINDOW_SECONDS"))
    if raw is None:
        return DEFAULT_RELEASE_WINDOW_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RELEASE_WINDOW_SECONDS


def max_last_message_bytes() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_MAX_LAST_MESSAGE_BYTES"))
    if raw is None:
        return DEFAULT_LAST_MESSAGE_BYTES
    try:
        return max(4096, int(raw))
    except ValueError:
        return DEFAULT_LAST_MESSAGE_BYTES


def max_jsonl_scan_bytes() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_MAX_JSONL_SCAN_BYTES"))
    if raw is None:
        return DEFAULT_JSONL_SCAN_BYTES
    try:
        return max(131072, int(raw))
    except ValueError:
        return DEFAULT_JSONL_SCAN_BYTES


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


def unique_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def project_workspace_dir(root: Path, project_name: str, slug: str | None = None) -> Path:
    return root / "projects" / (slug or project_slug(project_name))


def project_brief_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_BRIEF_FILE


def project_brief_md_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_BRIEF_MD


def project_launch_plan_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_PLAN_FILE


def project_launch_plan_md_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_PLAN_MD


def project_validation_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_VALIDATION_FILE


def project_validation_md_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_VALIDATION_MD


def default_project_brief(project_name: str) -> dict[str, Any]:
    return {
        "project": project_name,
        "goal": None,
        "repo_paths": [],
        "spec_paths": [],
        "notes": [],
        "constraints": [],
        "autonomy_mode": "manual",
        "clarification_mode": "auto",
        "validation_commands": [],
        "completion_sentinel": None,
        "max_planner_rounds": 3,
        "max_auto_fix_rounds": 2,
        "updated_at": utc_now(),
    }


def load_project_brief(project_dir: Path, project_name: str | None = None) -> dict[str, Any] | None:
    path = project_brief_path(project_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid project brief: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid project brief: {path}")
    brief = default_project_brief(project_name or str(payload.get("project") or project_dir.name))
    brief["project"] = str(payload.get("project") or brief["project"])
    brief["goal"] = normalize_optional_text(payload.get("goal"))
    brief["repo_paths"] = unique_preserve_order(
        [str(resolve_path(item)) for item in normalize_str_list(payload.get("repo_paths"), "repo_paths")]
    )
    brief["spec_paths"] = unique_preserve_order(
        [str(resolve_path(item)) for item in normalize_str_list(payload.get("spec_paths"), "spec_paths")]
    )
    brief["notes"] = unique_preserve_order(normalize_str_list(payload.get("notes"), "notes"))
    brief["constraints"] = unique_preserve_order(normalize_str_list(payload.get("constraints"), "constraints"))
    autonomy_mode = normalize_optional_text(payload.get("autonomy_mode")) or "manual"
    autonomy_mode = autonomy_mode.lower()
    if autonomy_mode not in AUTONOMY_MODES:
        autonomy_mode = "manual"
    brief["autonomy_mode"] = autonomy_mode
    clarification_mode = normalize_optional_text(payload.get("clarification_mode")) or "auto"
    clarification_mode = clarification_mode.lower()
    if clarification_mode not in CLARIFICATION_MODES:
        clarification_mode = "auto"
    brief["clarification_mode"] = clarification_mode
    brief["validation_commands"] = unique_preserve_order(
        normalize_str_list(payload.get("validation_commands"), "validation_commands")
    )
    brief["completion_sentinel"] = normalize_optional_text(payload.get("completion_sentinel"))
    max_rounds_raw = payload.get("max_planner_rounds")
    try:
        max_rounds = int(max_rounds_raw)
    except (TypeError, ValueError):
        max_rounds = 3
    brief["max_planner_rounds"] = max(1, max_rounds)
    max_auto_fix_rounds_raw = payload.get("max_auto_fix_rounds")
    try:
        max_auto_fix_rounds = int(max_auto_fix_rounds_raw)
    except (TypeError, ValueError):
        max_auto_fix_rounds = 2
    brief["max_auto_fix_rounds"] = max(0, max_auto_fix_rounds)
    brief["updated_at"] = normalize_optional_text(payload.get("updated_at")) or utc_now()
    return brief


def save_project_brief(project_dir: Path, brief: dict[str, Any]) -> None:
    brief = dict(brief)
    brief["updated_at"] = utc_now()
    write_text(
        project_brief_path(project_dir),
        json.dumps(brief, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )
    write_text(project_brief_md_path(project_dir), render_project_brief(brief))


def merge_project_brief(
    existing: dict[str, Any] | None,
    *,
    project_name: str,
    goal: str | None = None,
    repo_paths: list[str] | None = None,
    spec_paths: list[str] | None = None,
    notes: list[str] | None = None,
    constraints: list[str] | None = None,
    autonomy_mode: str | None = None,
    clarification_mode: str | None = None,
    validation_commands: list[str] | None = None,
    completion_sentinel: str | None = None,
    max_planner_rounds: int | None = None,
    max_auto_fix_rounds: int | None = None,
) -> dict[str, Any]:
    brief = dict(existing or default_project_brief(project_name))
    brief["project"] = project_name
    if goal is not None:
        brief["goal"] = goal
    brief["repo_paths"] = unique_preserve_order(
        normalize_str_list(brief.get("repo_paths"), "repo_paths")
        + [str(resolve_path(item)) for item in (repo_paths or [])]
    )
    brief["spec_paths"] = unique_preserve_order(
        normalize_str_list(brief.get("spec_paths"), "spec_paths")
        + [str(resolve_path(item)) for item in (spec_paths or [])]
    )
    brief["notes"] = unique_preserve_order(
        normalize_str_list(brief.get("notes"), "notes") + [item for item in (notes or []) if item]
    )
    brief["constraints"] = unique_preserve_order(
        normalize_str_list(brief.get("constraints"), "constraints")
        + [item for item in (constraints or []) if item]
    )
    if autonomy_mode is not None:
        mode = autonomy_mode.strip().lower()
        if mode not in AUTONOMY_MODES:
            raise RuntimeError(f"unsupported autonomy mode: {autonomy_mode}. supported values: {', '.join(AUTONOMY_MODES)}")
        brief["autonomy_mode"] = mode
    if clarification_mode is not None:
        mode = clarification_mode.strip().lower()
        if mode not in CLARIFICATION_MODES:
            raise RuntimeError(
                f"unsupported clarification mode: {clarification_mode}. "
                f"supported values: {', '.join(CLARIFICATION_MODES)}"
            )
        brief["clarification_mode"] = mode
    brief["validation_commands"] = unique_preserve_order(
        normalize_str_list(brief.get("validation_commands"), "validation_commands")
        + [item for item in (validation_commands or []) if item]
    )
    if completion_sentinel is not None:
        brief["completion_sentinel"] = completion_sentinel
    if max_planner_rounds is not None:
        brief["max_planner_rounds"] = max(1, int(max_planner_rounds))
    if max_auto_fix_rounds is not None:
        brief["max_auto_fix_rounds"] = max(0, int(max_auto_fix_rounds))
    brief["updated_at"] = utc_now()
    return brief


def render_project_brief(brief: dict[str, Any]) -> str:
    goal = normalize_optional_text(brief.get("goal"))
    repo_paths = normalize_str_list(brief.get("repo_paths"), "repo_paths")
    spec_paths = normalize_str_list(brief.get("spec_paths"), "spec_paths")
    notes = normalize_str_list(brief.get("notes"), "notes")
    constraints = normalize_str_list(brief.get("constraints"), "constraints")
    validation_commands = normalize_str_list(brief.get("validation_commands"), "validation_commands")
    autonomy_mode = normalize_optional_text(brief.get("autonomy_mode")) or "manual"
    clarification_mode = normalize_optional_text(brief.get("clarification_mode")) or "auto"
    completion_sentinel = normalize_optional_text(brief.get("completion_sentinel"))
    max_planner_rounds = brief.get("max_planner_rounds")
    max_auto_fix_rounds = brief.get("max_auto_fix_rounds")
    lines = [
        f"# {brief.get('project') or 'Project'} Brief",
        "",
        f"- Updated: `{normalize_optional_text(brief.get('updated_at')) or utc_now()}`",
        "",
        "## Goal",
        "",
        goal or "_No goal recorded yet._",
        "",
        "## Repo Paths",
        "",
    ]
    if not repo_paths:
        lines.append("_No repo paths recorded yet._")
    else:
        for item in repo_paths:
            lines.append(f"- `{item}`")
    lines.extend(["", "## Spec Paths", ""])
    if not spec_paths:
        lines.append("_No spec paths recorded yet._")
    else:
        for item in spec_paths:
            lines.append(f"- `{item}`")
    lines.extend(["", "## Constraints", ""])
    if not constraints:
        lines.append("_No constraints recorded yet._")
    else:
        for item in constraints:
            lines.append(f"- {item}")
    lines.extend(["", "## Delivery Policy", ""])
    lines.append(f"- Autonomy mode: `{autonomy_mode}`")
    lines.append(f"- Clarification mode: `{clarification_mode}`")
    lines.append(f"- Max parallel sessions: `{max_parallel_sessions()}`")
    lines.append(f"- Release throttle per cycle: `{max_releases_per_cycle()}`")
    lines.append(f"- Release throttle window: `{max_release_window_seconds()}` seconds")
    lines.append(f"- Max planner rounds: `{max_planner_rounds}`")
    lines.append(f"- Max auto-fix rounds: `{max_auto_fix_rounds}`")
    lines.append(f"- Completion sentinel: `{completion_sentinel or '-'}`")
    lines.append(f"- Last message cap: `{max_last_message_bytes()}` bytes")
    lines.append(f"- Session-id scan cap: `{max_jsonl_scan_bytes()}` bytes")
    lines.extend(["", "## Validation Commands", ""])
    if not validation_commands:
        lines.append("_No validation commands recorded yet._")
    else:
        for item in validation_commands:
            lines.append(f"- `{item}`")
    lines.extend(["", "## Notes", ""])
    if not notes:
        lines.append("_No notes recorded yet._")
    else:
        for item in notes:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def read_project_launch_plan(project_dir: Path) -> dict[str, Any] | None:
    path = project_launch_plan_path(project_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid launch plan file: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid launch plan file: {path}")
    return payload


def save_project_launch_plan(project_dir: Path, payload: dict[str, Any]) -> None:
    write_text(
        project_launch_plan_path(project_dir),
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )
    write_text(project_launch_plan_md_path(project_dir), render_project_launch_plan(payload))


def render_project_launch_plan(payload: dict[str, Any]) -> str:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        runs = []
    lines = [
        "# Launch Plan",
        "",
        f"- Updated: `{normalize_optional_text(payload.get('updated_at')) or utc_now()}`",
        f"- Source run: `{normalize_optional_text(payload.get('source_run_id')) or '-'}`",
        f"- Applied at: `{normalize_optional_text(payload.get('applied_at')) or '-'}`",
        "",
        "## Summary",
        "",
        normalize_optional_text(payload.get("plan_summary")) or "_No plan summary._",
        "",
        "## Planned Runs",
        "",
    ]
    if not runs:
        lines.append("_No launch plan captured yet._")
        lines.append("")
        return "\n".join(lines)
    rows: list[list[str]] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                str(item.get("task_id") or "-"),
                short_summary(normalize_optional_text(item.get("summary")), max_chars=48),
                str(item.get("role") or "-"),
                str(item.get("sandbox") or "-"),
                format_inline_list(normalize_str_list(item.get("depends_on"), "depends_on")),
                format_inline_list(normalize_str_list(item.get("owned_paths"), "owned_paths")),
            ]
        )
    lines.extend(
        [
            markdown_table(["task", "summary", "role", "sandbox", "depends_on", "owned_paths"], rows or [["-", "-", "-", "-", "-", "-"]]),
            "",
        ]
    )
    return "\n".join(lines)


def project_state_policy_markdown(project_name: str, project_dir: Path) -> list[str]:
    return [
        "## State Policy",
        "",
        f"- Project slug: `{project_slug(project_name)}`",
        f"- Reused folder: `{project_dir}`",
        "- Same project name reuses this folder and its tracked history.",
        "- Generated markdown files here are persistent manager state, not disposable temp files.",
        "- Normal continuation: keep the files and let the manager refresh them.",
        f"- Large child last messages are truncated to `{max_last_message_bytes()}` bytes with head/tail preservation.",
        f"- Launches are rate-limited to `{max_releases_per_cycle()}` new child sessions per `{max_release_window_seconds()}` seconds.",
        f"- Session-id log scans are capped at `{max_jsonl_scan_bytes()}` bytes per run refresh.",
        "- Human-edited file: `answers.md`.",
        "- Clean restart: use a new project name instead of deleting generated files by hand.",
        "",
    ]


def project_state_policy_cli(project_name: str, project_dir: Path) -> list[str]:
    return [
        "",
        "state_policy:",
        f"- project_slug={project_slug(project_name)}",
        f"- reused_folder={project_dir}",
        "- same project name reuses this folder and tracked history",
        "- generated markdown files are persistent manager state",
        f"- last messages are capped at {max_last_message_bytes()} bytes with head/tail preservation",
        f"- launches are capped at {max_releases_per_cycle()} per {max_release_window_seconds()}s window",
        f"- session-id scans are capped at {max_jsonl_scan_bytes()} bytes",
        "- normal continuation: keep the files; only answers.md is meant for human edits",
        "- clean restart: use a new project name instead of deleting generated files by hand",
    ]


def default_project_validation() -> dict[str, Any]:
    return {
        "updated_at": None,
        "status": "not-run",
        "basis": None,
        "validated_at": None,
        "completion_sentinel": None,
        "completion_satisfied": None,
        "completion_source": None,
        "commands": [],
    }


def load_project_validation(project_dir: Path) -> dict[str, Any] | None:
    path = project_validation_path(project_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid project validation file: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid project validation file: {path}")
    record = default_project_validation()
    record.update(payload)
    commands = payload.get("commands")
    if not isinstance(commands, list):
        record["commands"] = []
    return record


def save_project_validation(project_dir: Path, payload: dict[str, Any]) -> None:
    write_text(
        project_validation_path(project_dir),
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )
    write_text(project_validation_md_path(project_dir), render_project_validation(payload))


def render_project_validation(payload: dict[str, Any]) -> str:
    commands = payload.get("commands")
    if not isinstance(commands, list):
        commands = []
    lines = [
        "# Validation",
        "",
        f"- Updated: `{normalize_optional_text(payload.get('updated_at')) or utc_now()}`",
        f"- Status: `{normalize_optional_text(payload.get('status')) or 'not-run'}`",
        f"- Validated at: `{normalize_optional_text(payload.get('validated_at')) or '-'}`",
        f"- Basis: `{normalize_optional_text(payload.get('basis')) or '-'}`",
        f"- Completion sentinel: `{normalize_optional_text(payload.get('completion_sentinel')) or '-'}`",
        f"- Completion satisfied: `{normalize_optional_text(payload.get('completion_satisfied')) or '-'}`",
        f"- Completion source: `{normalize_optional_text(payload.get('completion_source')) or '-'}`",
        "",
        "## Commands",
        "",
    ]
    if not commands:
        lines.append("_No validation results recorded yet._")
        lines.append("")
        return "\n".join(lines)
    rows: list[list[str]] = []
    for item in commands:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                short_summary(normalize_optional_text(item.get("command")), max_chars=54),
                str(item.get("exit_code") if item.get("exit_code") is not None else "-"),
                short_summary(normalize_optional_text(item.get("status")), max_chars=18),
                short_summary(normalize_optional_text(item.get("stdout_preview")), max_chars=60),
                short_summary(normalize_optional_text(item.get("stderr_preview")), max_chars=60),
            ]
        )
    lines.extend(
        [
            markdown_table(["command", "exit", "status", "stdout", "stderr"], rows or [["-", "-", "-", "-", "-"]]),
            "",
        ]
    )
    return "\n".join(lines)


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


def output_warning_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        run for run in sorted(runs, key=run_sort_key)
        if normalize_str_list(run.get("output_warnings"), "output_warnings")
    ]


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


def project_git_root_for_run(run: dict[str, Any]) -> Path | None:
    raw = normalize_optional_text(run.get("source_repo_root"))
    if not raw:
        return None
    return Path(raw)


def run_project_dir(root: Path, run: dict[str, Any]) -> Path | None:
    project_name = normalize_optional_text(run.get("project"))
    if not project_name:
        return None
    return project_workspace_dir(root, project_name, normalize_optional_text(run.get("project_slug")) or project_slug(project_name))


def project_integration_dir(root: Path, run: dict[str, Any]) -> Path | None:
    project_dir = run_project_dir(root, run)
    if not project_dir:
        return None
    return project_dir / "integration"


def project_integration_branch(run: dict[str, Any]) -> str:
    slug = normalize_optional_text(run.get("project_slug")) or project_slug(normalize_optional_text(run.get("project")) or "project")
    return f"team-leader/{slug}/integration"


def run_worktree_branch(run: dict[str, Any]) -> str:
    slug = normalize_optional_text(run.get("project_slug")) or project_slug(normalize_optional_text(run.get("project")) or "project")
    return f"team-leader/{slug}/{run['run_id']}"


def run_requires_workspace_isolation(run: dict[str, Any]) -> bool:
    return str(run.get("workspace_mode") or "") == "worktree"


def ensure_integration_workspace(root: Path, run: dict[str, Any]) -> Path:
    repo_root = project_git_root_for_run(run)
    if not repo_root:
        raise RuntimeError("run has no source repo root for integration")
    integration_dir = project_integration_dir(root, run)
    if not integration_dir:
        raise RuntimeError("run has no project integration directory")
    if (integration_dir / ".git").exists():
        return integration_dir
    integration_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        git_run(
            ["worktree", "add", "-B", project_integration_branch(run), str(integration_dir), "HEAD"],
            cwd=repo_root,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"failed to create integration worktree for {run['run_id']}") from exc
    return integration_dir


def prepare_run_workspace(root: Path, run: dict[str, Any]) -> None:
    if not run_requires_workspace_isolation(run):
        return
    repo_root = project_git_root_for_run(run)
    if not repo_root:
        return
    integration_dir = ensure_integration_workspace(root, run)
    worktree_path_raw = normalize_optional_text(run.get("worktree_path"))
    if not worktree_path_raw:
        raise RuntimeError("run is missing worktree_path")
    worktree_path = Path(worktree_path_raw)
    if not (worktree_path / ".git").exists():
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            git_run(
                ["worktree", "add", "-b", run_worktree_branch(run), str(worktree_path), project_integration_branch(run)],
                cwd=repo_root,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr.strip() or f"failed to create worktree for {run['run_id']}") from exc
    repo_rel_cwd = normalize_optional_text(run.get("source_repo_rel_cwd")) or "."
    actual_cwd = worktree_path if repo_rel_cwd in {".", ""} else worktree_path / repo_rel_cwd
    run["cwd"] = str(actual_cwd)
    run["workspace_prepared_at"] = utc_now()
    run["workspace_base_ref"] = git_head(actual_cwd) or git_head(integration_dir)
    run["integration_state"] = run.get("integration_state") or "pending"
    run["integration_note"] = None
    run["integration_updated_at"] = utc_now()


def overlapping_writer_blockers(index: dict[str, Any], run: dict[str, Any]) -> list[str]:
    if not run_is_writer(run):
        return []
    my_paths = relative_owned_paths(run)
    if not my_paths:
        return []
    blockers: list[str] = []
    for candidate in sorted(dependency_pool(index, run), key=run_sort_key):
        if not run_is_writer(candidate):
            continue
        if run_sort_key(candidate) >= run_sort_key(run):
            continue
        other_paths = relative_owned_paths(candidate)
        if not other_paths:
            continue
        if not any(path_overlaps(left, right) for left in my_paths for right in other_paths):
            continue
        candidate_status = str(candidate.get("status") or "")
        integration_state = normalize_optional_text(candidate.get("integration_state"))
        if candidate_status not in TERMINAL_STATUSES:
            blockers.append(str(candidate.get("task_id") or candidate["run_id"]))
            continue
        if run_requires_workspace_isolation(candidate) and integration_state not in INTEGRATION_READY_STATES:
            blockers.append(str(candidate.get("task_id") or candidate["run_id"]))
    return blockers


def apply_run_to_integration(root: Path, run: dict[str, Any]) -> None:
    if not run_requires_workspace_isolation(run):
        return
    if str(run.get("status") or "") != "completed":
        return
    integration_state = normalize_optional_text(run.get("integration_state"))
    if integration_state in INTEGRATION_READY_STATES:
        return
    if integration_state in {"conflict", "scope-violation", "apply-failed", "commit-failed"}:
        return
    worktree_path_raw = normalize_optional_text(run.get("worktree_path"))
    base_ref = normalize_optional_text(run.get("workspace_base_ref"))
    if not worktree_path_raw or not base_ref:
        return
    worktree_path = Path(worktree_path_raw)
    if not worktree_path.exists():
        run["integration_state"] = "apply-failed"
        run["integration_note"] = "worktree path is missing"
        run["integration_updated_at"] = utc_now()
        return
    changed_paths, patch = git_has_tracked_changes(worktree_path, base_ref)
    run["changed_paths"] = changed_paths
    if not patch.strip():
        run["integration_state"] = "no-changes"
        run["integration_note"] = "writer completed without diff against the integration base"
        run["integration_updated_at"] = utc_now()
        return
    owned_paths = relative_owned_paths(run)
    outside_scope = [path for path in changed_paths if owned_paths and not path_within_owned_paths(path, owned_paths)]
    if outside_scope:
        run["integration_state"] = "scope-violation"
        run["integration_note"] = "changed paths outside declared ownership: " + ", ".join(outside_scope[:6])
        run["integration_updated_at"] = utc_now()
        return
    integration_dir = ensure_integration_workspace(root, run)
    try:
        git_run(["apply", "--3way", "--whitespace=nowarn", "-"], cwd=integration_dir, input_text=patch)
    except subprocess.CalledProcessError as exc:
        run["integration_state"] = "conflict"
        run["integration_note"] = short_summary(exc.stderr or exc.stdout or "git apply failed", max_chars=180)
        run["integration_updated_at"] = utc_now()
        return
    try:
        git_run(["add", "-A"], cwd=integration_dir)
        git_run(
            [
                "-c",
                "user.name=team-leader",
                "-c",
                "user.email=team-leader@local",
                "commit",
                "-m",
                f"team-leader integrate {run['run_id']}: {short_summary(normalize_optional_text(run.get('summary')), max_chars=72)}",
            ],
            cwd=integration_dir,
        )
    except subprocess.CalledProcessError as exc:
        run["integration_state"] = "commit-failed"
        run["integration_note"] = short_summary(exc.stderr or exc.stdout or "git commit failed", max_chars=180)
        run["integration_updated_at"] = utc_now()
        return
    run["integration_state"] = "applied"
    run["integration_note"] = f"applied into {integration_dir}"
    run["integration_updated_at"] = utc_now()


def maybe_integrate_completed_runs(root: Path, index: dict[str, Any]) -> None:
    for run in sorted(index["runs"], key=run_sort_key):
        apply_run_to_integration(root, run)


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


def run_is_planner(run: dict[str, Any]) -> bool:
    task_id = normalize_optional_text(run.get("task_id")) or ""
    if task_id.startswith(PLANNER_TASK_PREFIX):
        return True
    role = normalize_optional_text(run.get("role")) or ""
    return role.lower() == PLANNER_ROLE and normalize_optional_text(run.get("planner_source")) == PLANNER_SOURCE


def project_runs(index: dict[str, Any], project_name: str) -> list[dict[str, Any]]:
    project_filter = project_name.strip().lower()
    return [
        run
        for run in index["runs"]
        if project_filter
        in {
            str(run.get("project") or "").strip().lower(),
            str(run.get("project_slug") or "").strip().lower(),
        }
    ]


def latest_project_planner_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    planners = [run for run in runs if run_is_planner(run)]
    if not planners:
        return None
    return sorted(planners, key=run_sort_key)[-1]


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE):
        raw = match.group(1).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            candidates.append(payload)
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            candidates.append(payload)
    return candidates


def normalize_plan_item(item: dict[str, Any]) -> dict[str, Any]:
    prompt = item.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RuntimeError("launch plan item must include a non-empty prompt")
    normalized = {
        "task_id": normalize_optional_text(item.get("task_id")),
        "name": normalize_optional_text(item.get("name")),
        "role": normalize_optional_text(item.get("role")) or "research",
        "summary": normalize_optional_text(item.get("summary")) or derive_summary(prompt),
        "cwd": normalize_optional_text(item.get("cwd")),
        "sandbox": normalize_optional_text(item.get("sandbox")),
        "owned_paths": normalize_str_list(item.get("owned_paths"), "owned_paths"),
        "depends_on": normalize_str_list(item.get("depends_on"), "depends_on"),
        "prompt": prompt.strip(),
        "search": bool(item.get("search")),
        "skip_git_repo_check": bool(item.get("skip_git_repo_check")),
        "full_auto": bool(item.get("full_auto")),
        "dangerous": bool(item.get("dangerous")),
    }
    if not normalized["task_id"]:
        raise RuntimeError("launch plan item must include task_id")
    return normalized


def extract_launch_plan(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    for payload in extract_json_objects(text):
        runs = payload.get("runs")
        if not isinstance(runs, list):
            continue
        normalized_runs: list[dict[str, Any]] = []
        try:
            for item in runs:
                if not isinstance(item, dict):
                    raise RuntimeError("launch plan runs entries must be objects")
                normalized_runs.append(normalize_plan_item(item))
        except RuntimeError:
            continue
        return {
            "plan_summary": normalize_optional_text(payload.get("plan_summary")) or "Manager-generated launch plan",
            "runs": normalized_runs,
        }
    return None


def project_default_cwd(brief: dict[str, Any] | None) -> Path:
    if brief:
        repo_paths = normalize_str_list(brief.get("repo_paths"), "repo_paths")
        if repo_paths:
            return resolve_path(repo_paths[0])
    return Path.cwd()


def project_validation_cwd(project_dir: Path, brief: dict[str, Any], runs: list[dict[str, Any]]) -> Path:
    integration_dir = project_dir / "integration"
    if integration_dir.exists() and any(run_requires_workspace_isolation(run) for run in runs):
        return integration_dir
    return project_default_cwd(brief)


def planner_round_count(runs: list[dict[str, Any]]) -> int:
    return sum(1 for run in runs if run_is_planner(run))


def project_run_basis(runs: list[dict[str, Any]]) -> str:
    payload = [
        {
            "run_id": str(run.get("run_id") or ""),
            "status": str(run.get("status") or ""),
            "dispatch_state": str(run.get("dispatch_state") or ""),
            "finished_at": str(run.get("finished_at") or ""),
            "exit_code": run.get("exit_code"),
        }
        for run in sorted(runs, key=run_sort_key)
        if not run_is_planner(run)
    ]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def completion_signal_from_runs(runs: list[dict[str, Any]], sentinel: str | None) -> tuple[bool | None, str | None]:
    marker = normalize_optional_text(sentinel)
    if not marker:
        return None, None
    marker_l = marker.lower()
    for run in sorted(runs, key=run_sort_key, reverse=True):
        text = (last_message_for_run(run) or "").lower()
        if marker_l in text:
            return True, str(run.get("run_id") or "")
    return False, None


def execute_validation_commands(project_dir: Path, brief: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    commands = normalize_str_list(brief.get("validation_commands"), "validation_commands")
    basis = project_run_basis(runs)
    sentinel = normalize_optional_text(brief.get("completion_sentinel"))
    completion_satisfied, completion_source = completion_signal_from_runs(runs, sentinel)
    record = default_project_validation()
    record["updated_at"] = utc_now()
    record["basis"] = basis
    record["completion_sentinel"] = sentinel
    record["completion_satisfied"] = completion_satisfied
    record["completion_source"] = completion_source
    if not commands:
        record["status"] = "passed" if completion_satisfied is not False else "waiting-for-sentinel"
        record["validated_at"] = utc_now()
        record["commands"] = []
        return record
    cwd = project_validation_cwd(project_dir, brief, runs)
    results: list[dict[str, Any]] = []
    overall_ok = True
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                text=True,
                capture_output=True,
                timeout=600,
            )
            exit_code = proc.returncode
            stdout_preview = preview_text(proc.stdout, max_lines=4, max_chars=300)
            stderr_preview = preview_text(proc.stderr, max_lines=4, max_chars=300)
            status = "passed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout_preview = preview_text(exc.stdout, max_lines=4, max_chars=300) if exc.stdout else "_Timed out before stdout was captured._"
            stderr_preview = preview_text(exc.stderr, max_lines=4, max_chars=300) if exc.stderr else "_Timed out before stderr was captured._"
            status = "timeout"
        results.append(
            {
                "command": command,
                "exit_code": exit_code,
                "status": status,
                "stdout_preview": stdout_preview,
                "stderr_preview": stderr_preview,
            }
        )
        if status != "passed":
            overall_ok = False
    record["commands"] = results
    if not overall_ok:
        record["status"] = "failed"
    elif completion_satisfied is False:
        record["status"] = "waiting-for-sentinel"
    else:
        record["status"] = "passed"
    record["validated_at"] = utc_now()
    return record


def maybe_refresh_project_validation(project_dir: Path, brief: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    validation = load_project_validation(project_dir) or default_project_validation()
    commands = normalize_str_list(brief.get("validation_commands"), "validation_commands")
    sentinel = normalize_optional_text(brief.get("completion_sentinel"))
    if not commands and not sentinel:
        if project_validation_path(project_dir).exists() or project_validation_md_path(project_dir).exists():
            save_project_validation(project_dir, validation)
        return validation
    active = any(str(run.get("status") or "") == "running" for run in runs)
    blocked = any(str(run.get("dispatch_state") or "") == "blocked" for run in runs)
    failed = any(str(run.get("status") or "") == "failed" for run in runs)
    if active or blocked or failed:
        return validation
    if not runs:
        return validation
    basis = project_run_basis(runs)
    if validation.get("basis") == basis and validation.get("status") in {"passed", "failed", "waiting-for-sentinel"}:
        completion_satisfied, completion_source = completion_signal_from_runs(runs, sentinel)
        if validation.get("completion_satisfied") != completion_satisfied or validation.get("completion_source") != completion_source:
            validation["completion_satisfied"] = completion_satisfied
            validation["completion_source"] = completion_source
            validation["updated_at"] = utc_now()
            if validation.get("status") == "passed" and completion_satisfied is False:
                validation["status"] = "waiting-for-sentinel"
            elif validation.get("status") == "waiting-for-sentinel" and completion_satisfied is True:
                validation["status"] = "passed"
            save_project_validation(project_dir, validation)
        return validation
    validation = execute_validation_commands(project_dir, brief, runs)
    save_project_validation(project_dir, validation)
    return validation


def project_is_machine_complete(brief: dict[str, Any] | None, validation: dict[str, Any] | None) -> bool | None:
    if not brief:
        return None
    sentinel = normalize_optional_text(brief.get("completion_sentinel"))
    commands = normalize_str_list(brief.get("validation_commands"), "validation_commands")
    if not sentinel and not commands:
        return None
    if not validation:
        return False
    if validation.get("status") != "passed":
        return False
    if sentinel:
        return bool(validation.get("completion_satisfied"))
    return True


def brief_needs_clarification(brief: dict[str, Any] | None) -> bool:
    if not brief:
        return False
    if (normalize_optional_text(brief.get("clarification_mode")) or "auto") == "off":
        return False
    repo_paths = normalize_str_list(brief.get("repo_paths"), "repo_paths")
    spec_paths = normalize_str_list(brief.get("spec_paths"), "spec_paths")
    constraints = normalize_str_list(brief.get("constraints"), "constraints")
    notes = normalize_str_list(brief.get("notes"), "notes")
    if not repo_paths and not spec_paths:
        return True
    if not constraints and not notes:
        return True
    return False


def auto_fix_round_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        1
        for run in runs
        if run_is_planner(run)
        and normalize_optional_text(run.get("planner_reason"))
        in {"failed-runs", "validation-failed", "waiting-for-sentinel", "integration-conflict"}
    )


def planner_prompt_for_project(project_name: str, brief: dict[str, Any], project_dir: Path, existing_runs: list[dict[str, Any]]) -> str:
    goal = normalize_optional_text(brief.get("goal")) or "No goal recorded yet."
    repo_paths = normalize_str_list(brief.get("repo_paths"), "repo_paths")
    spec_paths = normalize_str_list(brief.get("spec_paths"), "spec_paths")
    notes = normalize_str_list(brief.get("notes"), "notes")
    constraints = normalize_str_list(brief.get("constraints"), "constraints")
    validation_commands = normalize_str_list(brief.get("validation_commands"), "validation_commands")
    autonomy_mode = normalize_optional_text(brief.get("autonomy_mode")) or "manual"
    clarification_mode = normalize_optional_text(brief.get("clarification_mode")) or "auto"
    completion_sentinel = normalize_optional_text(brief.get("completion_sentinel"))
    max_planner_rounds = brief.get("max_planner_rounds")
    max_auto_fix_rounds = brief.get("max_auto_fix_rounds")
    previous_workers = [run for run in existing_runs if not run_is_planner(run)]
    validation = load_project_validation(project_dir) or default_project_validation()
    validation_status = normalize_optional_text(validation.get("status")) or "not-run"
    answers = load_answers(project_dir)
    answered = answered_questions(collect_question_records(existing_runs), answers)
    lines = [
        f"You are the manager-planner for the project `{project_name}`.",
        "",
        "Your job is to inspect the project brief and produce the next safe child-session launch plan for team-leader.",
        "",
        f"Project brief path: `{project_brief_md_path(project_dir)}`",
        f"Project workspace path: `{project_dir}`",
        "",
        "Project goal:",
        goal,
        "",
        "Repo paths:",
    ]
    if repo_paths:
        for item in repo_paths:
            lines.append(f"- `{item}`")
    else:
        lines.append("- none recorded")
    lines.extend(["", "Spec paths:"])
    if spec_paths:
        for item in spec_paths:
            lines.append(f"- `{item}`")
    else:
        lines.append("- none recorded")
    lines.extend(["", "Constraints:"])
    if constraints:
        for item in constraints:
            lines.append(f"- {item}")
    else:
        lines.append("- none recorded")
    lines.extend(["", "Notes:"])
    if notes:
        for item in notes:
            lines.append(f"- {item}")
    else:
        lines.append("- none recorded")
    lines.extend(["", "Delivery policy:"])
    lines.append(f"- autonomy_mode={autonomy_mode}")
    lines.append(f"- clarification_mode={clarification_mode}")
    lines.append(f"- max_planner_rounds={max_planner_rounds}")
    lines.append(f"- max_auto_fix_rounds={max_auto_fix_rounds}")
    lines.append(f"- completion_sentinel={completion_sentinel or '-'}")
    lines.extend(["", "Validation commands:"])
    if validation_commands:
        for item in validation_commands:
            lines.append(f"- `{item}`")
    else:
        lines.append("- none recorded")
    lines.extend(["", "Validation state:"])
    lines.append(f"- status={validation_status}")
    lines.append(f"- validation_file={project_validation_md_path(project_dir)}")
    command_results = validation.get("commands")
    if isinstance(command_results, list) and command_results:
        for item in command_results[-3:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- "
                f"command={short_summary(normalize_optional_text(item.get('command')), max_chars=50)} "
                f"status={normalize_optional_text(item.get('status')) or '-'} "
                f"stderr={short_summary(normalize_optional_text(item.get('stderr_preview')), max_chars=64)}"
            )
    else:
        lines.append("- no recorded validation results yet")
    lines.extend(["", "Recent answered human questions:"])
    if answered:
        for question in answered[-5:]:
            lines.append(f"- {question['id']}: {answers[question['id']]}")
    else:
        lines.append("- none")
    lines.extend(["", "Existing tracked runs:"])
    if previous_workers:
        for run in previous_workers[-10:]:
            lines.append(
                f"- task={run.get('task_id') or run['run_id']} status={run.get('status') or '-'} summary={run.get('summary') or '-'}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Rules:",
            "- First decide whether the brief is clear enough to launch workers safely.",
            "- If the brief is missing key repo/spec context or important constraints, ask at most 3 concise questions under a `Questions For Humans` heading and do not emit a launch plan in the same response.",
            "- Do not invoke team-leader or create nested team-leader-managed sessions from inside this child session.",
            "- Use as few child sessions as necessary.",
            "- Split writers by disjoint file ownership whenever possible.",
            "- Prefer read-only research or review children if write boundaries are unclear.",
            "- Writers should own explicit disjoint paths whenever possible.",
            "- When validation failed, prefer a targeted fixer or reviewer wave over a broad restart.",
            "- Use the delivery policy to decide whether another wave is necessary.",
            "- If validation likely needs to run before more work, plan a reviewer or fixer wave only when the current outputs suggest more work is needed.",
            "- If a human decision is required, ask concise questions under a `Questions For Humans` heading.",
            "- If you have enough information, emit exactly one JSON code block with a launch plan.",
            "",
            "Launch plan JSON schema:",
            "```json",
            "{",
            '  "plan_summary": "one short summary",',
            '  "runs": [',
            "    {",
            '      "task_id": "stable-task-id",',
            '      "name": "short-run-name",',
            '      "role": "research|implementation|reviewer|manager",',
            '      "summary": "one-line summary",',
            '      "cwd": "/absolute/or/project-relative/path",',
            '      "sandbox": "read-only|workspace-write",',
            '      "owned_paths": ["relative/path"],',
            '      "depends_on": ["other-task-id"],',
            '      "search": false,',
            '      "skip_git_repo_check": false,',
            '      "full_auto": true,',
            '      "dangerous": false,',
            '      "prompt": "full child prompt text"',
            "    }",
            "  ]",
            "}",
            "```",
            "",
            "The child prompts must be ready to run as-is. Do not output shell commands. Do not leave TODO placeholders in the JSON.",
        ]
    )
    return "\n".join(lines)


def answers_updated_after(project_dir: Path, timestamp: str | None) -> bool:
    answers_path = project_dir / "answers.md"
    if not answers_path.exists():
        return False
    if not timestamp:
        return True
    try:
        cutoff = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return answers_path.stat().st_mtime > cutoff


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


def last_message_preview_path_for_run(run: dict[str, Any]) -> Path:
    return Path(run["run_dir"]) / "last_message.preview.md"


def last_message_display_for_run(run: dict[str, Any]) -> str | None:
    preview_path = last_message_preview_path_for_run(run)
    if preview_path.exists():
        return read_text_if_exists(preview_path)
    return last_message_for_run(run)


def latest_live_note(run: dict[str, Any]) -> str | None:
    path = Path(run["stdout_path"])
    if not path.exists():
        return None
    latest: str | None = None
    for line in read_tail_text(path).splitlines():
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
            left_task = str(left.get("task_id") or left["run_id"])
            right_task = str(right.get("task_id") or right["run_id"])
            right_blocked_on = normalize_str_list(right.get("blocked_on"), "blocked_on")
            left_blocked_on = normalize_str_list(left.get("blocked_on"), "blocked_on")
            if left_task in right_blocked_on or right_task in left_blocked_on:
                continue
            if (
                str(left.get("status") or "") in TERMINAL_STATUSES
                and (not run_requires_workspace_isolation(left) or normalize_optional_text(left.get("integration_state")) in INTEGRATION_READY_STATES)
            ):
                continue
            if (
                str(left.get("status") or "") in TERMINAL_STATUSES
                and str(right.get("status") or "") in TERMINAL_STATUSES
                and (not run_requires_workspace_isolation(left) or normalize_optional_text(left.get("integration_state")) in INTEGRATION_READY_STATES)
                and (not run_requires_workspace_isolation(right) or normalize_optional_text(right.get("integration_state")) in INTEGRATION_READY_STATES)
            ):
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
                    "left_task": left_task,
                    "right_task": right_task,
                    "paths": ", ".join(f"`{path}`" for path in unique_overlap),
                }
            )
    return conflicts


def integration_alerts(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for run in sorted(runs, key=run_sort_key):
        state = normalize_optional_text(run.get("integration_state"))
        if not state or state in INTEGRATION_READY_STATES:
            continue
        alerts.append(
            {
                "run_id": str(run["run_id"]),
                "task_id": str(run.get("task_id") or run["run_id"]),
                "state": state,
                "note": normalize_optional_text(run.get("integration_note")) or "-",
            }
        )
    return alerts


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
    blocked_on.extend(
        blocker
        for blocker in overlapping_writer_blockers(index, run)
        if blocker not in blocked_on
    )
    if blocked_on:
        return "blocked", blocked_on
    return "ready", []


def update_dispatch_metadata(index: dict[str, Any]) -> None:
    for run in index["runs"]:
        dispatch_state, blocked_on = compute_dispatch_state(index, run)
        run["dispatch_state"] = dispatch_state
        run["blocked_on"] = list(blocked_on)


def apply_parallel_limit_metadata(index: dict[str, Any]) -> None:
    active_count = sum(1 for run in index["runs"] if str(run.get("status") or "") == "running")
    parallel_limit = max_parallel_sessions()
    for run in sorted(index["runs"], key=run_sort_key):
        if str(run.get("status") or "") not in PRELAUNCH_STATUSES:
            continue
        if str(run.get("dispatch_state") or "") != "ready":
            continue
        if active_count >= parallel_limit:
            run["dispatch_state"] = "queued"
            run["blocked_on"] = [f"parallel-limit:{parallel_limit}"]
            continue
        active_count += 1


def apply_release_throttle_metadata(index: dict[str, Any]) -> None:
    release_budget = max_releases_per_cycle()
    release_window = max_release_window_seconds()
    cutoff_epoch = epoch_now() - release_window
    reserved = sum(
        1
        for run in index["runs"]
        if (
            (launched_epoch := parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))) is not None
            and launched_epoch >= cutoff_epoch
        )
    )
    for run in sorted(index["runs"], key=run_sort_key):
        if str(run.get("status") or "") not in PRELAUNCH_STATUSES:
            continue
        if str(run.get("dispatch_state") or "") != "ready":
            continue
        if reserved >= release_budget:
            run["dispatch_state"] = "queued"
            run["blocked_on"] = [f"release-throttle:{release_budget}/{release_window}s"]
            continue
        reserved += 1


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
    brief: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
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
    queued = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("dispatch_state") or "") == "queued"
    ]
    failed = [
        run for run in sorted(runs, key=run_sort_key)
        if str(run.get("status") or "") == "failed"
    ]
    open_questions = unanswered_questions(question_records, answers)
    machine_complete = project_is_machine_complete(brief, validation)
    validation_status = normalize_optional_text(validation.get("status")) if validation else None
    autonomy_mode = normalize_optional_text(brief.get("autonomy_mode")) if brief else None
    clarification_mode = normalize_optional_text(brief.get("clarification_mode")) if brief else None
    integration_conflicts = [
        run for run in sorted(runs, key=run_sort_key)
        if normalize_optional_text(run.get("integration_state")) in {"conflict", "scope-violation", "apply-failed", "commit-failed"}
    ]
    total = len(runs)
    progress = (
        f"{counts['completed']}/{total} completed, "
        f"{counts['running']} running, {len(blocked)} blocked, "
        f"{len(queued)} queued, "
        f"{len(open_questions)} open questions"
    )
    if integration_conflicts:
        current_stage = "resolve-integration"
        first_conflict = integration_conflicts[0]
        stage_reason = f"{len(integration_conflicts)} writer integration issue(s) need manager review"
        next_action = f"Inspect `{first_conflict['run_id']}` and resolve its integration note"
        focus = short_summary(normalize_optional_text(first_conflict.get("integration_note")), max_chars=96)
    elif conflicts:
        current_stage = "resolve-conflicts"
        stage_reason = f"{len(conflicts)} ownership overlap risk(s) detected"
        next_action = "Narrow write ownership or convert one child into a reviewer"
        focus = f"{conflicts[0]['left_task']} vs {conflicts[0]['right_task']}"
    elif open_questions:
        current_stage = "clarifying-brief" if clarification_mode == "auto" and not [run for run in runs if not run_is_planner(run)] else "waiting-for-human"
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
    elif queued:
        first_queued = queued[0]
        current_stage = "waiting-for-capacity"
        stage_reason = f"{len(queued)} task(s) waiting for the parallel session limit"
        next_action = f"Wait for a running child to finish so `{first_queued.get('task_id') or first_queued['run_id']}` can start"
        focus = str(first_queued.get("summary") or "-")
    elif failed:
        first_failed = failed[0]
        current_stage = "review-failures"
        stage_reason = f"{len(failed)} run(s) failed"
        next_action = f"Inspect `{first_failed['run_id']}` with `show` or `tail`"
        focus = str(first_failed.get("summary") or "-")
    elif validation_status == "failed":
        current_stage = "validation-failed"
        stage_reason = "Validation commands reported failures"
        if autonomy_mode == "continuous":
            next_action = "Manager will plan another delivery wave automatically"
        else:
            next_action = "Review `validation.md` and rerun `orchestrate` when ready"
        focus = "Validation gate did not pass"
    elif validation_status == "waiting-for-sentinel":
        current_stage = "awaiting-completion-signal"
        stage_reason = "Validation passed but the completion sentinel was not found"
        next_action = "Review results or run another planning round"
        focus = normalize_optional_text(brief.get("completion_sentinel")) or "Waiting for completion signal"
    elif machine_complete is True:
        current_stage = "delivered"
        stage_reason = "Machine-evaluable delivery criteria are satisfied"
        next_action = "Review final outputs and close the project"
        focus = "Delivery criteria satisfied"
    elif total > 0 and counts["completed"] == total:
        current_stage = "completed"
        stage_reason = "All tracked runs finished successfully"
        if validation_status and validation_status != "not-run":
            next_action = "Review `validation.md` and decide the next batch"
        else:
            next_action = "Review `manager-summary.md` and decide the next batch"
        focus = "All current tasks are complete"
    elif brief and normalize_optional_text(brief.get("goal")):
        current_stage = "ready-for-clarification" if brief_needs_clarification(brief) else "ready-for-planning"
        stage_reason = (
            "Project brief exists but the manager should ask a few clarification questions first"
            if brief_needs_clarification(brief)
            else "Project brief exists but no child runs are active yet"
        )
        next_action = "Run `orchestrate` to let the manager create the first planning round"
        focus = short_summary(normalize_optional_text(brief.get("goal")), max_chars=96)
    else:
        current_stage = "idle"
        stage_reason = "No active child sessions right now"
        next_action = "Record the project goal with `intake`, then run `orchestrate`"
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
    integration_issues = integration_alerts(runs)
    brief = load_project_brief(project_dir, project_name)
    validation = load_project_validation(project_dir)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
    goal = normalize_optional_text(brief.get("goal")) if brief else None
    autonomy_mode = normalize_optional_text(brief.get("autonomy_mode")) if brief else None
    clarification_mode = normalize_optional_text(brief.get("clarification_mode")) if brief else None
    max_auto_fix_rounds = brief.get("max_auto_fix_rounds") if brief else None
    validation_status = normalize_optional_text(validation.get("status")) if validation else None
    integration_issues = integration_alerts(runs)
    warning_runs = output_warning_runs(runs)
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
            f"- Brief file: `{project_brief_md_path(project_dir)}`",
            f"- Launch plan: `{project_launch_plan_md_path(project_dir)}`",
            f"- Validation: `{project_validation_md_path(project_dir)}`",
            f"- Goal: {goal or '_No goal recorded yet._'}",
            f"- Autonomy mode: `{autonomy_mode or 'manual'}`",
            f"- Clarification mode: `{clarification_mode or 'auto'}`",
            f"- Max parallel sessions: `{max_parallel_sessions()}`",
            f"- Release throttle per cycle: `{max_releases_per_cycle()}`",
            f"- Release throttle window: `{max_release_window_seconds()}` seconds",
            f"- Max auto-fix rounds: `{max_auto_fix_rounds if max_auto_fix_rounds is not None else 2}`",
            f"- Validation status: `{validation_status or 'not-run'}`",
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
            f"- Integration issues: `{len(integration_issues)}`",
            f"- Output warnings: `{len(warning_runs)}`",
            "",
            "## Files",
            "",
            "- `brief.md`: project goal, repo paths, spec paths, notes, and constraints",
            "- `launch-plan.md`: latest planner-produced child launch plan",
            "- `validation.md`: latest validation results and delivery status",
            "- `dashboard.md`: live run table, active notes, questions, and conflict alerts",
            "- `tasks.md`: task-oriented ledger with summaries and ownership",
            "- `manager-summary.md`: concise manager snapshot",
            "- `questions.md`: human-facing questions and blockers",
            "- `answers.md`: human-maintained answers keyed by question id",
            "- `answers-template.md`: copy-ready answer lines for open questions",
            "- `conflicts.md`: ownership overlap and conflict-risk notes",
            "- `reports/`: one markdown report per child run",
            "",
            *project_state_policy_markdown(project_name, project_dir),
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
                str(run.get("integration_state") or "-"),
                str(run.get("session_id") or "-"),
            ]
        )
    return "\n".join(
        [
            "# Task Ledger",
            "",
            markdown_table(
                ["task", "summary", "role", "status", "dispatch", "blocked_on", "run", "depends_on", "owned_paths", "integration", "session"],
                rows or [["-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"]],
            ),
            "",
        ]
    )


def render_dashboard(
    project_name: str,
    project_dir: Path | None,
    runs: list[dict[str, Any]],
    conflicts: list[dict[str, str]],
    question_records: list[dict[str, str]],
    answers: dict[str, str],
) -> str:
    counts = run_status_counts(runs)
    if project_dir is None and runs:
        project_dir = project_root(Path(runs[0]["run_dir"]).parent.parent, runs[0])
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
    brief = load_project_brief(project_dir, project_name) if project_dir else None
    validation = load_project_validation(project_dir) if project_dir else None
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
    goal = normalize_optional_text(brief.get("goal")) if brief else None
    launch_plan = read_project_launch_plan(project_dir) if project_dir else None
    validation_status = normalize_optional_text(validation.get("status")) if validation else None
    integration_issues = integration_alerts(runs)
    warning_runs = output_warning_runs(runs)
    for run in sorted(runs, key=run_sort_key):
        rows.append(
            [
                str(run["run_id"]),
                str(run.get("task_id") or "-"),
                short_summary(str(run.get("summary") or "-")),
                str(run.get("role") or "-"),
                str(run.get("status") or "-"),
                str(run.get("dispatch_state") or "-"),
                str(run.get("integration_state") or "-"),
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
        f"- Goal: {goal or '_No goal recorded yet._'}",
        f"- Validation status: `{validation_status or 'not-run'}`",
        f"- Current stage: `{stage['current_stage']}`",
        f"- Stage reason: {stage['stage_reason']}",
        f"- Next action: {stage['next_action']}",
        f"- Current focus: {stage['focus']}",
        f"- Progress: {stage['progress']}",
        f"- Manager watcher: `{watcher_state}`",
        f"- Release throttle per cycle: `{max_releases_per_cycle()}`",
        f"- Release throttle window: `{max_release_window_seconds()}` seconds",
        f"- Running: `{counts['running']}`",
        f"- Completed: `{counts['completed']}`",
        f"- Failed: `{counts['failed']}`",
        f"- Cancelled: `{counts['cancelled']}`",
        f"- Integration issues: `{len(integration_issues)}`",
        f"- Output warnings: `{len(warning_runs)}`",
        "",
        "## Planner Output",
        "",
    ]
    if launch_plan:
        lines.extend(
            [
                f"- Source run: `{normalize_optional_text(launch_plan.get('source_run_id')) or '-'}`",
                f"- Applied at: `{normalize_optional_text(launch_plan.get('applied_at')) or '-'}`",
                f"- Summary: {normalize_optional_text(launch_plan.get('plan_summary')) or '-'}",
                "",
            ]
        )
    else:
        lines.extend(["_No planner launch plan captured yet._", "",])
    lines.extend(
        [
        "## Validation",
        "",
        f"- Status: `{validation_status or 'not-run'}`",
        f"- File: `{project_validation_md_path(project_dir) if project_dir else '-'}`",
        "",
        *(project_state_policy_markdown(project_name, project_dir) if project_dir else []),
        "## Run Table",
        "",
        markdown_table(
            ["run", "task", "summary", "role", "status", "dispatch", "integration", "session", "owned_paths", "launched"],
            rows or [["-", "-", "-", "-", "-", "-", "-", "-", "-", "-"]],
        ),
        "",
        "## Integration",
        "",
        ]
    )
    if not integration_issues:
        lines.append("_No integration issues._")
    else:
        for item in integration_issues:
            lines.append(f"- `{item['task_id']}` / `{item['run_id']}`: `{item['state']}` {item['note']}")
    lines.extend(["", "## Active Runs", ""])
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
                    f"- Workspace mode: `{run.get('workspace_mode') or 'direct'}`",
                    f"- Integration: `{run.get('integration_state') or '-'}`",
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
                    f"- Integration: `{run.get('integration_state') or '-'}`",
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
                    preview_text(last_message_display_for_run(run)),
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
    lines.extend(["", "## Output Warnings", ""])
    if not warning_runs:
        lines.append("_No artifact-size warnings._")
    else:
        for run in warning_runs:
            warnings = format_inline_list(normalize_str_list(run.get("output_warnings"), "output_warnings"))
            lines.append(f"- `{run.get('task_id') or run['run_id']}`: {warnings}")
    lines.append("")
    return "\n".join(lines)


def render_manager_summary(
    project_name: str,
    project_dir: Path | None,
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
    if project_dir is None and runs:
        project_dir = project_root(Path(runs[0]["run_dir"]).parent.parent, runs[0])
    brief = load_project_brief(project_dir, project_name) if project_dir else None
    validation = load_project_validation(project_dir) if project_dir else None
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
    goal = normalize_optional_text(brief.get("goal")) if brief else None
    launch_plan = read_project_launch_plan(project_dir) if project_dir else None
    validation_status = normalize_optional_text(validation.get("status")) if validation else None
    warning_runs = output_warning_runs(runs)
    lines = [
        f"# {project_name} Manager Summary",
        "",
        f"- Updated: `{utc_now()}`",
        f"- Goal: {goal or '_No goal recorded yet._'}",
        f"- Validation status: `{validation_status or 'not-run'}`",
        f"- Current stage: `{stage['current_stage']}`",
        f"- Stage reason: {stage['stage_reason']}",
        f"- Next action: {stage['next_action']}",
        f"- Current focus: {stage['focus']}",
        f"- Progress: {stage['progress']}",
        f"- Release throttle per cycle: `{max_releases_per_cycle()}`",
        f"- Release throttle window: `{max_release_window_seconds()}` seconds",
        f"- Total runs: `{len(runs)}`",
        f"- Blocked: `{len(blocked)}`",
        f"- Running: `{counts['running']}`",
        f"- Completed: `{counts['completed']}`",
        f"- Failed: `{counts['failed']}`",
        f"- Cancelled: `{counts['cancelled']}`",
        f"- Output warnings: `{len(warning_runs)}`",
        "",
        "## Planner State",
        "",
    ]
    if not launch_plan:
        lines.append("_No planner launch plan captured yet._")
    else:
        lines.extend(
            [
                f"- Source run: `{normalize_optional_text(launch_plan.get('source_run_id')) or '-'}`",
                f"- Applied at: `{normalize_optional_text(launch_plan.get('applied_at')) or '-'}`",
                f"- Summary: {normalize_optional_text(launch_plan.get('plan_summary')) or '-'}",
            ]
        )
    lines.extend([""])
    if project_dir:
        lines.extend(project_state_policy_markdown(project_name, project_dir))
    lines.extend(["## Human Attention", ""])
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
    lines.extend(["", "## Output Warnings", ""])
    if not warning_runs:
        lines.append("_No artifact-size warnings._")
    else:
        for run in warning_runs:
            warnings = format_inline_list(normalize_str_list(run.get("output_warnings"), "output_warnings"))
            lines.append(f"- `{run.get('task_id') or run['run_id']}`: {warnings}")
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
                    preview_text(last_message_display_for_run(run), max_lines=5, max_chars=500),
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


def render_conflicts(conflicts: list[dict[str, str]], integration_issues: list[dict[str, str]]) -> str:
    lines = [
        "# Conflict Risks",
        "",
        f"_Updated: `{utc_now()}`_",
        "",
    ]
    if conflicts:
        rows = [
            [item["left_run"], item["right_run"], item["left_task"], item["right_task"], item["paths"]]
            for item in conflicts
        ]
        lines.extend(
            [
                markdown_table(["left_run", "right_run", "left_task", "right_task", "overlap"], rows),
                "",
                "These are conflict risks for the manager to resolve. Overlapping writers are serialized, and isolated writer worktrees are integrated through the manager branch.",
                "",
            ]
        )
    else:
        lines.extend(["_No owned-path overlap detected._", ""])
    lines.extend(["## Integration Issues", ""])
    if not integration_issues:
        lines.extend(["_No integration issues._", ""])
        return "\n".join(lines)
    for item in integration_issues:
        lines.append(f"- `{item['task_id']}` / `{item['run_id']}`: `{item['state']}` {item['note']}")
    lines.append("")
    return "\n".join(lines)


def render_project_cli_summary(root: Path, project_name: str, runs: list[dict[str, Any]]) -> str:
    slug = str(runs[0].get("project_slug") or project_slug(project_name)) if runs else project_slug(project_name)
    project_dir = project_workspace_dir(root, project_name, slug)
    watcher_state, watcher_heartbeat = monitor_state(root)
    counts = run_status_counts(runs)
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    conflicts = detect_conflict_risks(runs)
    integration_issues = integration_alerts(runs)
    warning_runs = output_warning_runs(runs)
    brief = load_project_brief(project_dir, project_name)
    validation = load_project_validation(project_dir)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
    launch_plan = read_project_launch_plan(project_dir)
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
        f"brief={project_brief_md_path(project_dir)}",
        f"launch_plan={project_launch_plan_md_path(project_dir)}",
        f"validation={project_validation_md_path(project_dir)}",
        f"goal={normalize_optional_text(brief.get('goal')) if brief else '-'}",
        f"autonomy_mode={normalize_optional_text(brief.get('autonomy_mode')) if brief else 'manual'}",
        f"clarification_mode={normalize_optional_text(brief.get('clarification_mode')) if brief else 'auto'}",
        f"parallel_limit={max_parallel_sessions()}",
        f"release_throttle={max_releases_per_cycle()}",
        f"release_window_seconds={max_release_window_seconds()}",
        f"max_auto_fix_rounds={brief.get('max_auto_fix_rounds') if brief else 2}",
        f"validation_status={normalize_optional_text(validation.get('status')) if validation else 'not-run'}",
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
        f"failed:{counts['failed']} cancelled:{counts['cancelled']} integration_issues:{len(integration_issues)} "
        f"output_warnings:{len(warning_runs)}"
    )
    lines.extend(project_state_policy_cli(project_name, project_dir))
    lines.extend(["", "planner:"])
    if not launch_plan:
        lines.append("- none")
    else:
        lines.append(
            f"- source_run={normalize_optional_text(launch_plan.get('source_run_id')) or '-'} "
            f"applied_at={normalize_optional_text(launch_plan.get('applied_at')) or '-'} "
            f"summary={short_summary(normalize_optional_text(launch_plan.get('plan_summary')), max_chars=80)}"
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
            lines.append(f"  integration: {run.get('integration_state') or '-'}")
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
    lines.extend(["", "integration:"])
    if not integration_issues:
        lines.append("- none")
    else:
        for item in integration_issues:
            lines.append(
                f"- {item['task_id']} [{item['state']}] {short_summary(item['note'], max_chars=90)}"
            )
    lines.extend(["", "output_warnings:"])
    if not warning_runs:
        lines.append("- none")
    else:
        for run in warning_runs:
            warnings = format_inline_list(normalize_str_list(run.get("output_warnings"), "output_warnings"))
            lines.append(f"- {run.get('task_id') or run['run_id']}: {warnings}")
    return "\n".join(lines)


def write_project_reports(project_dir: Path, runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    question_records = collect_question_records(runs)
    by_run: dict[str, list[dict[str, str]]] = {}
    for record in question_records:
        by_run.setdefault(record["run_id"], []).append(record)
    for run in runs:
        report_path = project_dir / "reports" / f"{run['run_id']}.md"
        last_message = last_message_display_for_run(run)
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
                f"- Workspace mode: `{run.get('workspace_mode') or 'direct'}`",
                f"- Worktree: `{run.get('worktree_path') or '-'}`",
                f"- Integration: `{run.get('integration_state') or '-'}`",
                f"- Depends on: {format_inline_list(normalize_str_list(run.get('depends_on'), 'depends_on'))}",
                f"- Output warnings: {format_inline_list(normalize_str_list(run.get('output_warnings'), 'output_warnings'))}",
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


def known_projects(root: Path, index: dict[str, Any]) -> dict[str, str]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in index["runs"]:
        project_name = normalize_optional_text(run.get("project"))
        if not project_name:
            continue
        slug = str(run.get("project_slug") or project_slug(project_name))
        grouped[slug] = {"name": project_name}
    projects_dir = root / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            slug = project_dir.name
            if slug in grouped:
                continue
            brief = load_project_brief(project_dir)
            if brief:
                grouped[slug] = {"name": str(brief.get("project") or slug)}
    return {slug: str(payload["name"]) for slug, payload in grouped.items()}


def sync_one_project(root: Path, project_name: str, slug: str, runs: list[dict[str, Any]]) -> None:
    project_dir = ensure_project_workspace(root, project_name, slug)
    brief = load_project_brief(project_dir, project_name)
    if brief:
        write_text(project_brief_md_path(project_dir), render_project_brief(brief))
    if not project_launch_plan_md_path(project_dir).exists():
        write_text(project_launch_plan_md_path(project_dir), render_project_launch_plan({}))
    validation = load_project_validation(project_dir)
    if validation:
        write_text(project_validation_md_path(project_dir), render_project_validation(validation))
    elif not project_validation_md_path(project_dir).exists():
        write_text(project_validation_md_path(project_dir), render_project_validation(default_project_validation()))
    question_records = write_project_reports(project_dir, runs)
    answers_path = project_dir / "answers.md"
    if not answers_path.exists():
        write_text(answers_path, render_answers_stub())
    answers = load_answers(project_dir)
    conflicts = detect_conflict_risks(runs)
    integration_issues = integration_alerts(runs)
    delete_if_exists(project_dir / "project.md")
    write_text(project_dir / "README.md", render_project_overview(project_name, project_dir, runs))
    write_text(project_dir / "tasks.md", render_task_ledger(runs))
    write_text(
        project_dir / "dashboard.md",
        render_dashboard(project_name, project_dir, runs, conflicts, question_records, answers),
    )
    write_text(
        project_dir / "manager-summary.md",
        render_manager_summary(project_name, project_dir, runs, conflicts, question_records, answers),
    )
    write_text(project_dir / "questions.md", render_questions(question_records, answers))
    write_text(
        project_dir / "answers-template.md",
        render_answers_template(question_records, answers),
    )
    write_text(project_dir / "conflicts.md", render_conflicts(conflicts, integration_issues))


def sync_projects(root: Path, index: dict[str, Any]) -> None:
    projects = known_projects(root, index)
    for slug, project_name in sorted(projects.items()):
        runs = sorted(
            [
                run
                for run in index["runs"]
                if normalize_optional_text(run.get("project_slug")) == slug
            ],
            key=run_sort_key,
        )
        sync_one_project(root, project_name, slug, runs)


def save_index_and_sync(root: Path, data: dict[str, Any]) -> None:
    update_dispatch_metadata(data)
    apply_parallel_limit_metadata(data)
    apply_release_throttle_metadata(data)
    save_index(root, data)
    sync_projects(root, data)


def next_planner_task_id(runs: list[dict[str, Any]]) -> str:
    count = sum(1 for run in runs if run_is_planner(run))
    return f"{PLANNER_TASK_PREFIX}-{count + 1}"


def infer_plan_sandbox(item: dict[str, Any]) -> str | None:
    sandbox = normalize_optional_text(item.get("sandbox"))
    if sandbox:
        return sandbox
    role = (normalize_optional_text(item.get("role")) or "").lower()
    if item.get("owned_paths") or role in {"implementation", "implementer", "writer", "owner", "manager"}:
        return "workspace-write"
    return "read-only"


def project_extra_add_dirs(default_cd: Path, brief: dict[str, Any] | None) -> list[Path]:
    if not brief:
        return []
    extras: list[Path] = []
    for item in normalize_str_list(brief.get("repo_paths"), "repo_paths"):
        path = resolve_path(item)
        if path == default_cd:
            continue
        extras.append(path)
    return extras


def dispatch_options_from_plan_item(
    item: dict[str, Any],
    *,
    project_name: str,
    brief: dict[str, Any] | None,
    planner_run: dict[str, Any],
) -> DispatchOptions:
    cwd_raw = normalize_optional_text(item.get("cwd"))
    if cwd_raw:
        candidate = Path(cwd_raw)
        cd = resolve_path(candidate) if candidate.is_absolute() else resolve_path(project_default_cwd(brief) / candidate)
    else:
        cd = project_default_cwd(brief)
    add_dirs = project_extra_add_dirs(cd, brief)
    add_dirs.extend(resolve_path(path) for path in normalize_str_list(planner_run.get("add_dirs"), "add_dirs"))
    add_dirs = [path for idx, path in enumerate(add_dirs) if path not in add_dirs[:idx]]
    sandbox = infer_plan_sandbox(item)
    return DispatchOptions(
        provider=str(planner_run.get("provider") or DEFAULT_PROVIDER),
        name=normalize_optional_text(item.get("name")) or normalize_optional_text(item.get("task_id")),
        project=project_name,
        task_id=normalize_optional_text(item.get("task_id")),
        role=normalize_optional_text(item.get("role")),
        summary=normalize_optional_text(item.get("summary")),
        prompt_text=str(item["prompt"]),
        cd=cd,
        sandbox=sandbox,
        model=normalize_optional_text(planner_run.get("model")),
        profile=normalize_optional_text(planner_run.get("profile")),
        add_dirs=add_dirs,
        configs=normalize_str_list(planner_run.get("configs"), "configs"),
        enables=normalize_str_list(planner_run.get("enables"), "enables"),
        disables=normalize_str_list(planner_run.get("disables"), "disables"),
        images=[resolve_path(path) for path in normalize_str_list(planner_run.get("images"), "images")],
        search=bool(item.get("search")) or bool(planner_run.get("search")),
        skip_git_repo_check=bool(item.get("skip_git_repo_check")) or bool(planner_run.get("skip_git_repo_check")),
        ephemeral=bool(planner_run.get("ephemeral")),
        full_auto=bool(item.get("full_auto")) or bool(planner_run.get("full_auto")),
        dangerous=bool(item.get("dangerous")) or bool(planner_run.get("dangerous")),
        dry_run=False,
        owned_paths=normalize_str_list(item.get("owned_paths"), "owned_paths"),
        depends_on=normalize_str_list(item.get("depends_on"), "depends_on"),
    )


def apply_planner_run(root: Path, index: dict[str, Any], run: dict[str, Any]) -> list[str]:
    if run.get("plan_applied_at") or run.get("plan_apply_error"):
        return normalize_str_list(run.get("planned_run_ids"), "planned_run_ids")
    project_name = normalize_optional_text(run.get("project"))
    if not project_name:
        run["plan_apply_error"] = "planner run has no project"
        return []
    project_dir = project_workspace_dir(root, project_name, str(run.get("project_slug") or project_slug(project_name)))
    brief = load_project_brief(project_dir, project_name)
    plan = extract_launch_plan(last_message_for_run(run))
    if not plan:
        run["plan_apply_error"] = "no-launch-plan-found"
        payload = {
            "source_run_id": run["run_id"],
            "plan_summary": "Planner finished without a parseable launch plan",
            "runs": [],
            "applied_at": None,
            "updated_at": utc_now(),
        }
        save_project_launch_plan(project_dir, payload)
        return []
    planned_ids: list[str] = []
    existing_task_ids = {normalize_optional_text(item.get("task_id")) for item in index["runs"]}
    normalized_runs: list[dict[str, Any]] = []
    for item in plan["runs"]:
        normalized_runs.append(dict(item))
        task_id = normalize_optional_text(item.get("task_id"))
        if task_id and task_id in existing_task_ids:
            continue
        options = dispatch_options_from_plan_item(item, project_name=project_name, brief=brief, planner_run=run)
        child = materialize_run(root, index, options, announce=False)
        planned_ids.append(str(child["run_id"]))
        existing_task_ids.add(normalize_optional_text(child.get("task_id")))
    run["plan_applied_at"] = utc_now()
    run["plan_apply_error"] = None
    run["planned_run_ids"] = planned_ids
    save_project_launch_plan(
        project_dir,
        {
            "source_run_id": run["run_id"],
            "plan_summary": plan["plan_summary"],
            "runs": normalized_runs,
            "applied_at": run["plan_applied_at"],
            "updated_at": utc_now(),
        },
    )
    return planned_ids


def apply_planner_outputs(root: Path, index: dict[str, Any]) -> None:
    for run in sorted(index["runs"], key=run_sort_key):
        if not run_is_planner(run):
            continue
        if str(run.get("status") or "") != "completed":
            continue
        apply_planner_run(root, index, run)


def should_spawn_planner_for_project(project_dir: Path, brief: dict[str, Any], runs: list[dict[str, Any]]) -> tuple[bool, str]:
    if not normalize_optional_text(brief.get("goal")):
        return False, "missing-goal"
    open_questions = unanswered_questions(collect_question_records(runs), load_answers(project_dir))
    if open_questions:
        return False, "waiting-for-human"
    conflicts = detect_conflict_risks(runs)
    if conflicts:
        return False, "resolve-conflicts"
    if any(str(run.get("status") or "") == "running" for run in runs):
        return False, "active-runs"
    if any(str(run.get("dispatch_state") or "") == "blocked" for run in runs):
        return False, "blocked-runs"
    latest_planner = latest_project_planner_run(runs)
    if latest_planner and str(latest_planner.get("status") or "") in {"running", "prepared", "blocked"}:
        return False, "planner-already-running"
    max_rounds = int(brief.get("max_planner_rounds") or 3)
    if planner_round_count(runs) >= max_rounds:
        return False, "max-rounds-reached"
    max_auto_fix_rounds = max(0, int(brief.get("max_auto_fix_rounds") or 0))
    validation = maybe_refresh_project_validation(project_dir, brief, runs) if runs else load_project_validation(project_dir)
    if project_is_machine_complete(brief, validation) is True:
        return False, "complete"
    if auto_fix_round_count(runs) >= max_auto_fix_rounds and max_auto_fix_rounds >= 0:
        if validation and validation.get("status") in {"failed", "waiting-for-sentinel"}:
            return False, "max-auto-fix-rounds-reached"
    if latest_planner and latest_planner.get("plan_applied_at") and not normalize_str_list(latest_planner.get("planned_run_ids"), "planned_run_ids"):
        if not answers_updated_after(project_dir, normalize_optional_text(latest_planner.get("finished_at"))):
            if not validation or validation.get("status") not in {"failed", "waiting-for-sentinel"}:
                return False, "planner-produced-no-work"
    if not runs:
        return True, "first-plan"
    if any(str(run.get("status") or "") == "failed" for run in runs):
        return True, "failed-runs"
    if any(
        normalize_optional_text(run.get("integration_state"))
        in {"conflict", "scope-violation", "apply-failed", "commit-failed"}
        for run in runs
    ):
        return True, "integration-conflict"
    if validation and validation.get("status") in {"failed", "waiting-for-sentinel"}:
        return True, str(validation.get("status"))
    if latest_planner is None:
        return True, "missing-planner"
    return False, "manual-review"


def spawn_planner_run(
    root: Path,
    index: dict[str, Any],
    project_name: str,
    brief: dict[str, Any],
    project_dir: Path,
    *,
    planner_reason: str,
) -> dict[str, Any]:
    runs = project_runs(index, project_name)
    project_cd = project_default_cwd(brief)
    planner_options = DispatchOptions(
        provider=DEFAULT_PROVIDER,
        name=next_planner_task_id(runs),
        project=project_name,
        task_id=next_planner_task_id(runs),
        role=PLANNER_ROLE,
        summary=f"Plan and assign the next child sessions for {project_name}",
        prompt_text=planner_prompt_for_project(project_name, brief, project_dir, runs),
        cd=project_cd,
        sandbox="read-only",
        model=None,
        profile=None,
        add_dirs=project_extra_add_dirs(project_cd, brief),
        configs=[],
        enables=[],
        disables=[],
        images=[],
        search=False,
        skip_git_repo_check=False,
        ephemeral=False,
        full_auto=True,
        dangerous=False,
        dry_run=False,
        owned_paths=[],
        depends_on=[],
    )
    return materialize_run(
        root,
        index,
        planner_options,
        announce=False,
        extra_fields={"planner_source": PLANNER_SOURCE, "planner_reason": planner_reason},
    )


def maybe_auto_drive_projects(root: Path, index: dict[str, Any]) -> None:
    for slug, project_name in sorted(known_projects(root, index).items()):
        project_dir = ensure_project_workspace(root, project_name, slug)
        brief = load_project_brief(project_dir, project_name)
        if not brief:
            continue
        maybe_refresh_project_validation(project_dir, brief, project_runs(index, project_name))
        autonomy_mode = normalize_optional_text(brief.get("autonomy_mode")) or "manual"
        if autonomy_mode != "continuous":
            continue
        runs = project_runs(index, project_name)
        should_spawn, reason = should_spawn_planner_for_project(project_dir, brief, runs)
        if should_spawn:
            spawn_planner_run(root, index, project_name, brief, project_dir, planner_reason=reason)


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_tail_text(path: Path, *, max_bytes: int = 131072) -> str:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as fh:
        if start:
            fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if start and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def read_tail_lines(path: Path, line_count: int, *, max_bytes: int = 1048576) -> list[str]:
    target = max(1, int(line_count))
    chunk = min(65536, max_bytes)
    lines = read_tail_text(path, max_bytes=chunk).splitlines()
    while len(lines) < target and chunk < max_bytes:
        chunk = min(max_bytes, chunk * 2)
        lines = read_tail_text(path, max_bytes=chunk).splitlines()
    return lines[-target:]


def read_head_text(path: Path, *, max_bytes: int = 131072) -> str:
    with path.open("rb") as fh:
        data = fh.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def read_jsonl_candidates(path: Path, *, max_scan_bytes: int | None = None) -> list[str]:
    candidates: list[str] = []
    if not path.exists():
        return candidates
    scan_budget = max_scan_bytes if max_scan_bytes is not None else max_jsonl_scan_bytes()
    scanned = 0
    with path.open("rb") as fh:
        while scanned < scan_budget:
            raw_line = fh.readline()
            if not raw_line:
                break
            scanned += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            collect_uuid_candidates(payload, candidates)
            if candidates:
                break
    return candidates


def build_truncated_text_preview(path: Path, *, max_bytes: int) -> tuple[str | None, bool, int | None]:
    if not path.exists():
        return None, False, None
    original_size = path.stat().st_size
    if original_size <= max_bytes:
        return read_text_if_exists(path), False, original_size
    head_budget = max(1024, (max_bytes // 2) - 256)
    tail_budget = max(1024, max_bytes - head_budget - 256)
    head = read_head_text(path, max_bytes=head_budget)
    tail = read_tail_text(path, max_bytes=tail_budget)
    marker = (
        "\n\n[truncated by team-leader: original file exceeded "
        f"{max_bytes} bytes; middle content omitted]\n\n"
    )
    payload = (head + marker + tail).encode("utf-8", errors="replace")
    if len(payload) > max_bytes:
        payload = payload[: max_bytes - 4] + b"...\n"
    return payload.decode("utf-8", errors="replace"), True, original_size


def refresh_run_artifacts(run: dict[str, Any]) -> None:
    artifact_sizes: dict[str, int] = {}
    warnings: list[str] = []
    stdout_path = Path(run["stdout_path"])
    stderr_path = Path(run["stderr_log"])
    last_message_path = Path(run["last_message_path"])
    preview_path = last_message_preview_path_for_run(run)
    for label, path in (
        ("stdout_jsonl", stdout_path),
        ("stderr_log", stderr_path),
        ("last_message", last_message_path),
    ):
        if path.exists():
            artifact_sizes[label] = path.stat().st_size
    if last_message_path.exists():
        preview_text_value, truncated, original_size = build_truncated_text_preview(
            last_message_path,
            max_bytes=max_last_message_bytes(),
        )
        if original_size is not None:
            run["last_message_original_bytes"] = original_size
        run["last_message_truncated"] = bool(truncated)
        if truncated and preview_text_value is not None:
            write_text(preview_path, preview_text_value)
            artifact_sizes["last_message_preview"] = preview_path.stat().st_size
        else:
            delete_if_exists(preview_path)
        if run.get("last_message_truncated"):
            warnings.append(
                f"last_message_truncated:{run.get('last_message_original_bytes') or artifact_sizes.get('last_message') or '-'}"
            )
    else:
        delete_if_exists(preview_path)
        run["last_message_truncated"] = False
        run["last_message_original_bytes"] = None
    if artifact_sizes.get("stdout_jsonl", 0) >= STDOUT_WARNING_BYTES:
        warnings.append(f"stdout_jsonl_large:{artifact_sizes['stdout_jsonl']}")
    if artifact_sizes.get("stderr_log", 0) >= STDERR_WARNING_BYTES:
        warnings.append(f"stderr_log_large:{artifact_sizes['stderr_log']}")
    run["artifact_sizes"] = artifact_sizes
    run["output_warnings"] = unique_preserve_order(warnings)


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
        status = str(run.get("status") or "")
        if status == "running":
            return True
        if status in TERMINAL_STATUSES:
            continue
        if pid_alive(run.get("pid")):
            return True
    return False


def ensure_monitor(root: Path, index: dict[str, Any]) -> None:
    if os.environ.get(MONITOR_RUN_ENV) == "1":
        return
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
        env={**os.environ, MONITOR_RUN_ENV: "1"},
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
    root = Path(run["run_dir"]).parent.parent
    prepare_run_workspace(root, run)
    refresh_runner_for_run(run)
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
    apply_parallel_limit_metadata(index)
    apply_release_throttle_metadata(index)
    for run in sorted(index["runs"], key=run_sort_key):
        if str(run.get("status") or "") not in PRELAUNCH_STATUSES:
            continue
        dispatch_state = str(run.get("dispatch_state") or "")
        if dispatch_state == "blocked":
            run["status"] = "blocked"
            continue
        if dispatch_state == "queued":
            continue
        start_run_process(run)
    update_dispatch_metadata(index)
    apply_parallel_limit_metadata(index)
    apply_release_throttle_metadata(index)


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
        if str(run.get("status") or "") in TERMINAL_STATUSES:
            run["pid"] = None
    elif pid_alive(run.get("pid")):
        run["status"] = "running"
    elif run.get("status") == "running":
        run["status"] = "exited"
        run["pid"] = None
    if exit_code and exit_code.strip().lstrip("-").isdigit():
        run["exit_code"] = int(exit_code.strip())
    if finished_at:
        run["finished_at"] = finished_at.strip()
    if str(run.get("status") or "") in TERMINAL_STATUSES:
        run["pid"] = None
    if run_has_provider_artifacts(run) and not get_session_id(run):
        session_id = provider_for_run(run).detect_session_id(run)
        if session_id:
            set_session_id(run, session_id)
    refresh_run_artifacts(run)


def refresh_index_state(root: Path, index: dict[str, Any]) -> None:
    for run in index["runs"]:
        refresh_run(run)
    maybe_integrate_completed_runs(root, index)
    apply_planner_outputs(root, index)
    launch_ready_runs(root, index)
    maybe_auto_drive_projects(root, index)
    save_index_and_sync(root, index)
    ensure_monitor(root, index)


def build_runner_script(
    *,
    command: list[str],
    prompt_path: Path,
    state_path: Path,
    exit_code_path: Path,
    started_path: Path,
    finished_path: Path,
    env_exports: dict[str, str] | None = None,
    path_prefix: Path | None = None,
) -> str:
    cmd = quote_command(command)
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        f"export {CHILD_RUN_ENV}=1",
    ]
    if path_prefix is not None:
        lines.append(f"export PATH={shlex.quote(str(path_prefix))}:$PATH")
    for key, value in sorted((env_exports or {}).items()):
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.extend(
        [
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
    return "\n".join(lines)


def dispatch_options_for_run(run: dict[str, Any]) -> DispatchOptions:
    prompt_text = read_text_if_exists(Path(run["prompt_path"])) or ""
    return DispatchOptions(
        provider=str(run.get("provider") or DEFAULT_PROVIDER),
        name=normalize_optional_text(run.get("name")),
        project=normalize_optional_text(run.get("project")),
        task_id=normalize_optional_text(run.get("task_id")),
        role=normalize_optional_text(run.get("role")),
        summary=normalize_optional_text(run.get("summary")),
        prompt_text=prompt_text,
        cd=Path(str(run["cwd"])),
        sandbox=normalize_optional_text(run.get("sandbox")),
        model=normalize_optional_text(run.get("model")),
        profile=normalize_optional_text(run.get("profile")),
        add_dirs=[Path(path) for path in normalize_str_list(run.get("add_dirs"), "add_dirs")],
        configs=normalize_str_list(run.get("configs"), "configs"),
        enables=normalize_str_list(run.get("enables"), "enables"),
        disables=normalize_str_list(run.get("disables"), "disables"),
        images=[Path(path) for path in normalize_str_list(run.get("images"), "images")],
        search=bool(run.get("search")),
        skip_git_repo_check=bool(run.get("skip_git_repo_check")),
        ephemeral=bool(run.get("ephemeral")),
        full_auto=bool(run.get("full_auto")),
        dangerous=bool(run.get("dangerous")),
        dry_run=False,
        owned_paths=normalize_str_list(run.get("owned_paths"), "owned_paths"),
        depends_on=normalize_str_list(run.get("depends_on"), "depends_on"),
    )


def refresh_runner_for_run(run: dict[str, Any]) -> None:
    adapter = get_provider(str(run.get("provider") or DEFAULT_PROVIDER))
    options = dispatch_options_for_run(run)
    command = adapter.build_exec_command(
        prompt_path=Path(run["prompt_path"]),
        last_message_path=Path(run["last_message_path"]),
        options=options,
    )
    provider_bin_raw = normalize_optional_text(run.get("provider_bin")) or adapter.resolved_bin()
    real_provider_bin = resolve_executable(provider_bin_raw, cwd=Path(str(run["cwd"])))
    command[0] = real_provider_bin
    run_dir = Path(run["run_dir"])
    runner_path = Path(run["runner_path"])
    guard_dir = write_child_cli_guard(run_dir, adapter, real_provider_bin)
    guard_target = str(guard_dir / adapter.default_bin)
    write_text(
        runner_path,
        build_runner_script(
            command=command,
            prompt_path=Path(run["prompt_path"]),
            state_path=run_dir / "state.txt",
            exit_code_path=run_dir / "exit_code.txt",
            started_path=run_dir / "started_at.txt",
            finished_path=run_dir / "finished_at.txt",
            env_exports={
                adapter.bin_env_var: guard_target,
            },
            path_prefix=guard_dir,
        ),
    )
    runner_path.chmod(0o755)
    write_text(run_dir / "command.txt", quote_command(command))


def print_run_summary(run: dict[str, Any]) -> None:
    print(run_summary_text(run))


def run_summary_text(run: dict[str, Any]) -> str:
    session_id = get_session_id(run) or "-"
    exit_code = run.get("exit_code")
    exit_text = str(exit_code) if exit_code is not None else "-"
    task_text = str(run.get("task_id") or "-")
    summary_text = short_summary(str(run.get("summary") or "-"), max_chars=42)
    dispatch_state = str(run.get("dispatch_state") or run.get("status") or "-")
    blocked_on = normalize_str_list(run.get("blocked_on"), "blocked_on")
    if blocked_on:
        dispatch_state = f"{dispatch_state}:{','.join(blocked_on[:2])}"
    integration_state = normalize_optional_text(run.get("integration_state")) or "-"
    return (
        f"{run['run_id']:<30} {run['status']:<10} provider={run.get('provider', DEFAULT_PROVIDER):<6} "
        f"pid={run.get('pid') or '-':<8} exit={exit_text:<4} task={task_text:<18} "
        f"dispatch={dispatch_state:<18} integration={integration_state:<14} "
        f"session={session_id} summary={summary_text}"
    )


def render_watch_view(root: Path, project_name: str, runs: list[dict[str, Any]]) -> str:
    lines = [render_project_cli_summary(root, project_name, runs), "", "runs:"]
    if not runs:
        lines.append("- none")
        return "\n".join(lines)
    for run in runs:
        lines.append(run_summary_text(run))
        live_note = latest_live_note(run)
        if live_note and str(run.get("status") or "") == "running":
            lines.append(f"  note: {short_summary(live_note, max_chars=140)}")
    return "\n".join(lines)


def watch_view_key(view: str) -> str:
    return re.sub(r"(?m)^(watcher=[^\n]+?) heartbeat=[^\n]+$", r"\1", view)


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


def cmd_intake(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name = args.project.strip()
    with root_lock(root):
        index = load_index(root)
        project_dir = ensure_project_workspace(root, project_name, project_slug(project_name))
        brief = merge_project_brief(
            load_project_brief(project_dir, project_name),
            project_name=project_name,
            goal=normalize_optional_text(args.goal),
            repo_paths=list(args.repo_path),
            spec_paths=list(args.spec_path),
            notes=list(args.note),
            constraints=list(args.constraint),
            autonomy_mode=normalize_optional_text(args.autonomy_mode),
            clarification_mode=normalize_optional_text(args.clarification_mode),
            validation_commands=list(args.validation_command),
            completion_sentinel=normalize_optional_text(args.completion_sentinel),
            max_planner_rounds=args.max_planner_rounds,
            max_auto_fix_rounds=args.max_auto_fix_rounds,
        )
        if not normalize_optional_text(brief.get("goal")):
            raise RuntimeError("project brief still has no goal; provide --goal")
        save_project_brief(project_dir, brief)
        save_index_and_sync(root, index)
    print(f"project={project_name}")
    print(f"workspace={project_dir}")
    print(f"brief={project_brief_md_path(project_dir)}")
    return 0


def cmd_orchestrate(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name = args.project.strip()
    with root_lock(root):
        index = load_index(root)
        refresh_index_state(root, index)
        project_dir = ensure_project_workspace(root, project_name, project_slug(project_name))
        brief = merge_project_brief(
            load_project_brief(project_dir, project_name),
            project_name=project_name,
            goal=normalize_optional_text(args.goal),
            repo_paths=list(args.repo_path),
            spec_paths=list(args.spec_path),
            notes=list(args.note),
            constraints=list(args.constraint),
            autonomy_mode=normalize_optional_text(args.autonomy_mode),
            clarification_mode=normalize_optional_text(args.clarification_mode),
            validation_commands=list(args.validation_command),
            completion_sentinel=normalize_optional_text(args.completion_sentinel),
            max_planner_rounds=args.max_planner_rounds,
            max_auto_fix_rounds=args.max_auto_fix_rounds,
        )
        if not normalize_optional_text(brief.get("goal")):
            raise RuntimeError("project brief still has no goal; provide --goal")
        save_project_brief(project_dir, brief)
        runs = project_runs(index, project_name)
        maybe_refresh_project_validation(project_dir, brief, runs)
        question_records = collect_question_records(runs)
        answers = load_answers(project_dir)
        open_questions = unanswered_questions(question_records, answers)
        latest_planner = latest_project_planner_run(runs)
        if latest_planner:
            refresh_run(latest_planner)
        if latest_planner and str(latest_planner.get("status") or "") in {"running", "prepared", "blocked"} and not args.replan:
            save_index_and_sync(root, index)
            ensure_monitor(root, index)
            print(render_project_cli_summary(root, project_name, project_runs(index, project_name)))
            return 0
        if open_questions and not args.replan:
            save_index_and_sync(root, index)
            ensure_monitor(root, index)
            print(render_project_cli_summary(root, project_name, project_runs(index, project_name)))
            return 0
        worker_runs = [run for run in runs if not run_is_planner(run)]
        if latest_planner and worker_runs and not args.replan:
            save_index_and_sync(root, index)
            ensure_monitor(root, index)
            print(render_project_cli_summary(root, project_name, project_runs(index, project_name)))
            return 0
        if latest_planner and not args.replan and not answers_updated_after(project_dir, normalize_optional_text(latest_planner.get("finished_at"))):
            if latest_planner.get("plan_applied_at") or latest_planner.get("plan_apply_error"):
                save_index_and_sync(root, index)
                ensure_monitor(root, index)
                print(render_project_cli_summary(root, project_name, project_runs(index, project_name)))
                return 0
        add_dirs = project_extra_add_dirs(project_default_cwd(brief), brief)
        add_dirs.extend(resolve_path(path) for path in args.add_dir)
        add_dirs = [path for idx, path in enumerate(add_dirs) if path not in add_dirs[:idx]]
        planner_task_id = next_planner_task_id(runs)
        planner_options = DispatchOptions(
            provider=args.provider,
            name=planner_task_id,
            project=project_name,
            task_id=planner_task_id,
            role=PLANNER_ROLE,
            summary=f"Plan and assign the next child sessions for {project_name}",
            prompt_text=planner_prompt_for_project(project_name, brief, project_dir, runs),
            cd=resolve_path(args.cd) if args.cd else project_default_cwd(brief),
            sandbox=args.sandbox or "read-only",
            model=args.model,
            profile=args.profile,
            add_dirs=add_dirs,
            configs=list(args.config),
            enables=list(args.enable),
            disables=list(args.disable),
            images=[resolve_path(path) for path in args.image],
            search=bool(args.search),
            skip_git_repo_check=bool(args.skip_git_repo_check),
            ephemeral=bool(args.ephemeral),
            full_auto=True,
            dangerous=bool(args.dangerous),
            dry_run=bool(args.dry_run),
            owned_paths=[],
            depends_on=[],
        )
        materialize_run(
            root,
            index,
            planner_options,
            extra_fields={"planner_source": PLANNER_SOURCE, "planner_reason": "manual-orchestrate"},
        )
    return 0


def materialize_run(
    root: Path,
    index: dict[str, Any],
    options: DispatchOptions,
    *,
    announce: bool = True,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_root(root)
    adapter = get_provider(options.provider)
    adapter.validate_options(options)
    repo_root = git_toplevel(options.cd)
    source_repo_rel_cwd: str | None = None
    workspace_mode = "direct"
    worktree_path: str | None = None
    if options.project and repo_root and (options.sandbox and options.sandbox != "read-only"):
        try:
            source_repo_rel_cwd = str(options.cd.relative_to(repo_root))
        except ValueError:
            source_repo_rel_cwd = "."
        workspace_mode = "worktree"
    run_id = make_run_id({run["run_id"] for run in index["runs"]}, options.name)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if workspace_mode == "worktree" and options.project:
        project_dir = ensure_project_workspace(root, options.project, project_slug(options.project))
        worktree_path = str(project_dir / "worktrees" / run_id)

    prompt_path = run_dir / "prompt.md"
    last_message_path = run_dir / "last_message.md"
    stdout_path = run_dir / "stdout.jsonl"
    stderr_path = run_dir / "stderr.log"
    state_path = run_dir / "state.txt"
    exit_code_path = run_dir / "exit_code.txt"
    started_path = run_dir / "started_at.txt"
    finished_path = run_dir / "finished_at.txt"
    runner_path = run_dir / "runner.sh"

    write_text(prompt_path, child_prompt_guard(options.prompt_text))

    command = adapter.build_exec_command(
        prompt_path=prompt_path,
        last_message_path=last_message_path,
        options=options,
    )
    provider_bin_raw = adapter.resolved_bin()
    real_provider_bin = resolve_executable(provider_bin_raw, cwd=options.cd)
    command[0] = real_provider_bin
    guard_dir = write_child_cli_guard(run_dir, adapter, real_provider_bin)
    guard_target = str(guard_dir / adapter.default_bin)

    write_text(
        runner_path,
        build_runner_script(
            command=command,
            prompt_path=prompt_path,
            state_path=state_path,
            exit_code_path=exit_code_path,
            started_path=started_path,
            finished_path=finished_path,
            env_exports={
                adapter.bin_env_var: guard_target,
            },
            path_prefix=guard_dir,
        ),
    )
    runner_path.chmod(0o755)
    write_text(run_dir / "command.txt", quote_command(command))

    run = {
        "run_id": run_id,
        "name": options.name or run_id,
        "provider": adapter.name,
        "provider_bin": provider_bin_raw,
        "project": options.project,
        "project_slug": project_slug(options.project) if options.project else None,
        "task_id": options.task_id,
        "role": options.role,
        "summary": options.summary or derive_summary(options.prompt_text),
        "status": "dry-run" if options.dry_run else "prepared",
        "run_dir": str(run_dir),
        "cwd": str(options.cd),
        "source_cwd": str(options.cd),
        "source_repo_root": str(repo_root) if repo_root else None,
        "source_repo_rel_cwd": source_repo_rel_cwd,
        "workspace_mode": workspace_mode,
        "worktree_path": worktree_path,
        "workspace_base_ref": None,
        "workspace_prepared_at": None,
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
        "planner_source": None,
        "planner_reason": None,
        "plan_applied_at": None,
        "plan_apply_error": None,
        "planned_run_ids": [],
        "integration_state": None,
        "integration_note": None,
        "integration_updated_at": None,
        "changed_paths": [],
    }
    if extra_fields:
        run.update(extra_fields)

    index["runs"].append(run)
    if not options.dry_run:
        launch_ready_runs(root, index)
    save_index_and_sync(root, index)
    ensure_monitor(root, index)
    if announce:
        print_run_summary(run)
    if announce and options.project:
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
        refresh_index_state(root, index)
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
            project_name = args.project
            if runs:
                project_name = str(runs[0].get("project") or runs[0].get("project_slug") or args.project)
    if args.json:
        print(json.dumps(runs, ensure_ascii=True, indent=2))
        return 0
    if not runs:
        if project_name:
            project_dir = project_workspace_dir(root, project_name, project_slug(project_name))
            if project_brief_path(project_dir).exists() or project_launch_plan_md_path(project_dir).exists():
                print(render_project_cli_summary(root, project_name, []))
                return 0
        print("no runs")
        return 0
    if project_name:
        print(render_project_cli_summary(root, project_name, runs))
        print()
        print("runs:")
    for run in runs:
        print_run_summary(run)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name = args.project.strip()
    is_tty = sys.stdout.isatty()
    if not is_tty and not args.once and not args.allow_non_tty_stream:
        args.once = True
    should_clear = is_tty and not args.no_clear
    use_alt_screen = should_clear and not args.no_alt_screen
    last_view: str | None = None
    if use_alt_screen:
        print("\033[?1049h\033[H", end="", flush=True)
    try:
        while True:
            with root_lock(root):
                index = load_index(root)
                refresh_index_state(root, index)
                runs = project_runs(index, project_name)
                if runs:
                    project_name = str(runs[0].get("project") or runs[0].get("project_slug") or project_name)
                view = render_watch_view(root, project_name, runs)
                view_key = watch_view_key(view)
                active = any(str(run.get("status") or "") == "running" for run in runs)
                blocked = any(str(run.get("dispatch_state") or "") == "blocked" for run in runs)
            if view_key != last_view or args.once:
                if should_clear:
                    print("\033[2J\033[H", end="")
                print(view)
                last_view = view_key
            if args.once:
                return 0
            if args.exit_when_settled and not active and not blocked:
                return 0
            time.sleep(max(1, int(args.interval)))
    finally:
        if use_alt_screen:
            print("\033[?1049l", end="", flush=True)


def cmd_show(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        refresh_index_state(root, index)
        run = resolve_run(index, args.run)
        last_message = last_message_for_run(run)
        display_message = last_message_display_for_run(run)
    print(json.dumps(run, ensure_ascii=True, indent=2, sort_keys=True))
    if display_message:
        print()
        if args.full_message:
            print((last_message or "").rstrip())
        else:
            print(preview_text(display_message, max_lines=args.message_lines, max_chars=args.message_chars))
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        refresh_index_state(root, index)
        run = resolve_run(index, args.run)
        path = Path(run["stderr_log"] if args.stderr else run["stdout_jsonl"])
    if not path.exists():
        print(f"missing log: {path}", file=sys.stderr)
        return 1
    for line in read_tail_lines(path, args.lines):
        print(line)
    return 0


def cmd_resume_cmd(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        refresh_index_state(root, index)
        run = resolve_run(index, args.run)
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
        refresh_index_state(root, index)
        targets = [resolve_run(index, args.run)] if args.run else index["runs"]
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
    os.environ[MONITOR_RUN_ENV] = "1"
    pid_path = monitor_pid_path(root)
    heartbeat_path = monitor_heartbeat_path(root)
    current_pid = os.getpid()
    write_text(pid_path, f"{current_pid}\n")
    try:
        while True:
            with root_lock(root):
                index = load_index(root)
                refresh_index_state(root, index)
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


def add_project_capture_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--goal", help="High-level project goal")
    parser.add_argument("--repo-path", action="append", default=[], help="Relevant repo or working directory path")
    parser.add_argument("--spec-path", action="append", default=[], help="Relevant spec, design, or reference path")
    parser.add_argument("--note", action="append", default=[], help="Additional project note or Q/A context")
    parser.add_argument("--constraint", action="append", default=[], help="Constraint the planner should respect")
    parser.add_argument("--autonomy-mode", choices=AUTONOMY_MODES, help="How self-driving the manager should be")
    parser.add_argument("--clarification-mode", choices=CLARIFICATION_MODES, help="Whether the planner should clarify the brief before launching workers")
    parser.add_argument("--validation-command", action="append", default=[], help="Validation command to run when a project wave settles")
    parser.add_argument("--completion-sentinel", help="Text marker that indicates delivery is complete when found in child output")
    parser.add_argument("--max-planner-rounds", type=int, help="Maximum number of manager planning rounds before stopping")
    parser.add_argument("--max-auto-fix-rounds", type=int, help="Maximum automatic validation-fix or recovery planning rounds")


def add_orchestrate_options(parser: argparse.ArgumentParser) -> None:
    add_project_capture_options(parser)
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help="CLI provider adapter to use for the manager planner child",
    )
    parser.add_argument("--cd", help="Working directory for the planner child")
    parser.add_argument("--sandbox", help="Sandbox mode for the planner child")
    parser.add_argument("--model", help="Provider model override")
    parser.add_argument("--profile", help="Provider profile override")
    parser.add_argument("--add-dir", action="append", default=[], help="Additional directory to expose to the planner child")
    parser.add_argument("--config", action="append", default=[], help="Pass through provider --config")
    parser.add_argument("--enable", action="append", default=[], help="Pass through provider --enable")
    parser.add_argument("--disable", action="append", default=[], help="Pass through provider --disable")
    parser.add_argument("--image", action="append", default=[], help="Image path to attach where supported")
    parser.add_argument("--search", action="store_true", help="Enable provider live web search where supported")
    parser.add_argument("--skip-git-repo-check", action="store_true", help="Allow planner child outside a Git repository where supported")
    parser.add_argument("--ephemeral", action="store_true", help="Run planner child without persisting provider session files")
    parser.add_argument("--dangerous", action="store_true", help="Run planner child with provider dangerous no-sandbox mode")
    parser.add_argument("--dry-run", action="store_true", help="Create the planner run directory but do not launch the provider CLI")
    parser.add_argument("--replan", action="store_true", help="Force a fresh manager-planner run even if a prior plan exists")


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

    intake_p = sub.add_parser("intake", help="Record or update a project brief from goal, paths, and notes")
    add_project_capture_options(intake_p)
    intake_p.set_defaults(func=cmd_intake)

    orchestrate_p = sub.add_parser("orchestrate", help="Use the manager to plan and launch child runs from a project brief")
    add_orchestrate_options(orchestrate_p)
    orchestrate_p.set_defaults(func=cmd_orchestrate)

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

    watch_p = sub.add_parser("watch", help="Watch one project with a live terminal summary")
    watch_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    watch_p.add_argument("--project", required=True, help="Project name or project slug")
    watch_p.add_argument("--interval", type=int, default=2, help="Seconds between refreshes")
    watch_p.add_argument("--once", action="store_true", help="Render one frame and exit")
    watch_p.add_argument("--exit-when-settled", action="store_true", help="Exit when the project has no running or blocked runs")
    watch_p.add_argument("--no-clear", action="store_true", help="Do not clear the terminal between frames")
    watch_p.add_argument("--no-alt-screen", action="store_true", help="Keep normal terminal scrollback instead of using an alternate screen")
    watch_p.add_argument("--allow-non-tty-stream", action="store_true", help="Permit continuous watch output even when stdout is captured instead of attached to a real terminal")
    watch_p.set_defaults(func=cmd_watch)

    show_p = sub.add_parser("show", help="Show one run and its last message")
    show_p.add_argument("run", help="Run id or unique prefix")
    show_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    show_p.add_argument("--full-message", action="store_true", help="Print the full child last_message instead of a bounded preview")
    show_p.add_argument("--message-lines", type=int, default=12, help="Maximum preview lines when --full-message is not used")
    show_p.add_argument("--message-chars", type=int, default=2000, help="Maximum preview characters when --full-message is not used")
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
        if os.environ.get(CHILD_RUN_ENV) == "1":
            raise RuntimeError(
                "nested team-leader invocation is disabled inside a team-leader child session. "
                "Report delegation or replanning needs back to the parent manager instead."
            )
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
