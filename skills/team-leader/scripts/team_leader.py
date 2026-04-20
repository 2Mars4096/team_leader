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
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
INDEX_VERSION = 3
DEFAULT_PROVIDER = "codex"
PROVIDER_ALIASES: dict[str, str] = {
    "openai-codex": "codex",
    "codex-cli": "codex",
    "claude-code": "claude",
    "cc": "claude",
    "cursor-agent": "cursor",
    "kiro-cli": "kiro",
}
DEFAULT_ROOT_NAME = ".team-leader"
LEGACY_ROOT_NAMES = (".agent-subsessions", ".codex-subsessions")
LEGACY_ROOTS_LABEL = ", ".join(f"./{name}" for name in LEGACY_ROOT_NAMES)
DEFAULT_MAX_PARALLEL_SESSIONS = 8
DEFAULT_MAX_RELEASES_PER_CYCLE = 2
DEFAULT_RELEASE_WINDOW_SECONDS = 15
DEFAULT_MAX_PLAN_RUNS_PER_WAVE = 24
DEFAULT_PROJECT_MAX_PLANNER_ROUNDS = 12
DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS = 8
DEFAULT_LAST_MESSAGE_BYTES = 128 * 1024
DEFAULT_JSONL_SCAN_BYTES = 8 * 1024 * 1024
DEFAULT_RUN_HEARTBEAT_INTERVAL_SECONDS = 10
DEFAULT_RUN_HEARTBEAT_STALE_SECONDS = 45
STDOUT_WARNING_BYTES = 8 * 1024 * 1024
STDERR_WARNING_BYTES = 4 * 1024 * 1024
CODEX_BACKEND_HOST = "chatgpt.com"
CODEX_BACKEND_PORT = 443
DEFAULT_PROVIDER_PREFLIGHT_OK_SECONDS = 30
DEFAULT_PROVIDER_PREFLIGHT_FAIL_SECONDS = 5
CHILD_RUN_ENV = "TEAM_LEADER_CHILD_RUN"
MONITOR_RUN_ENV = "TEAM_LEADER_MONITOR_RUN"
QUESTION_SECTION_HINTS = ("question", "blocker", "human", "decision")
ANSWER_LINE_RE = re.compile(r"^\s*[-*+]\s*`?([a-z0-9][a-z0-9-]*)`?\s*:\s*(.+?)\s*$", re.IGNORECASE)
PRELAUNCH_STATUSES = {"prepared", "blocked"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "exited"}
DEFAULT_MANUAL_COMPACT_RUN_STATUSES = {"completed", "cancelled", "dry-run"}
MANUAL_COMPACT_EXTRA_STATUSES = {"failed", "exited"}
AUTO_COMPACT_RUN_STATUSES = DEFAULT_MANUAL_COMPACT_RUN_STATUSES | MANUAL_COMPACT_EXTRA_STATUSES
RUN_COMPACT_FILE_NAMES = (
    "command.txt",
    "exit_code.txt",
    "finished_at.txt",
    "heartbeat.txt",
    "last_message.md",
    "last_message.preview.md",
    "prompt.md",
    "runner.sh",
    "started_at.txt",
    "state.txt",
    "stderr.log",
    "stdout.jsonl",
)
PROJECT_COMPACT_DELETE_FILES = (
    "answers-template.md",
    "conflicts.md",
    "dashboard.md",
    "questions.md",
)
PROJECT_BRIEF_FILE = "brief.json"
PROJECT_BRIEF_MD = "brief.md"
PROJECT_PLAN_FILE = "launch-plan.json"
PROJECT_PLAN_MD = "launch-plan.md"
PROJECT_VALIDATION_FILE = "validation.json"
PROJECT_VALIDATION_MD = "validation.md"
PROJECT_METRICS_MD = "metrics.md"
PLANNER_TASK_PREFIX = "manager-plan"
PLANNER_ROLE = "manager"
PLANNER_SOURCE = "team-leader-planner"
AUTONOMY_MODES = ("manual", "guided", "continuous")
CLARIFICATION_MODES = ("auto", "off")
INTEGRATION_READY_STATES = {"applied", "applied-subset", "no-changes"}
INTEGRATION_ALERT_STATES = {"conflict", "scope-violation", "apply-failed", "commit-failed"}
INTEGRATION_BLOCKING_STATES = {"pending", *INTEGRATION_ALERT_STATES}
AUTO_FIX_PLANNER_REASONS = {"failed-runs", "validation-failed", "waiting-for-sentinel", "integration-conflict"}


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


def run_command_healthcheck(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> tuple[bool, str | None]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except OSError as exc:
        return False, short_summary(str(exc), max_chars=180)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout_seconds}s"
    detail = short_summary((proc.stderr.strip() or proc.stdout.strip() or "").strip(), max_chars=180)
    if proc.returncode != 0:
        if detail:
            return False, f"exit {proc.returncode}: {detail}"
        return False, f"exit {proc.returncode}"
    return True, detail


def extract_result_text_from_json_stream(path: Path) -> str | None:
    if not path.exists():
        return None
    result: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                candidate = payload.get("result")
                if isinstance(candidate, str) and candidate.strip():
                    result = candidate
    except OSError:
        return None
    return result


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


def git_common_dir(path: Path) -> Path | None:
    try:
        proc = git_run(["rev-parse", "--git-common-dir"], cwd=path)
    except subprocess.CalledProcessError:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (path / candidate).resolve()
    return candidate


def git_has_tracked_changes(path: Path, base_ref: str) -> tuple[list[str], str]:
    try:
        changed = git_run(["diff", "--name-only", base_ref], cwd=path)
        patch = git_run(["diff", "--binary", base_ref], cwd=path)
        untracked = git_run(["ls-files", "--others", "--exclude-standard"], cwd=path)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"git diff failed in {path}") from exc
    tracked_paths = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
    untracked_paths = [line.strip() for line in untracked.stdout.splitlines() if line.strip()]
    patch_parts = [patch.stdout]
    for rel_path in untracked_paths:
        extra = git_run(
            ["diff", "--binary", "--no-index", "--", "/dev/null", rel_path],
            cwd=path,
            check=False,
        )
        if extra.returncode not in {0, 1}:
            detail = extra.stderr.strip() or extra.stdout.strip() or f"git diff failed for {rel_path}"
            raise RuntimeError(detail)
        if extra.stdout:
            patch_parts.append(extra.stdout)
    changed_paths = unique_preserve_order(tracked_paths + untracked_paths)
    return changed_paths, "".join(patch_parts)


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
    provider_bin: str | None
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
    max_run_seconds: int | None
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
    notes: str = ""

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
            "aliases": provider_aliases_for(self.name),
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
            "notes": self.notes,
        }

    def preflight_timeout_seconds(self) -> int:
        return 12

    def build_preflight_command(self, *, real_bin: str, cwd: Path) -> list[str] | None:
        return [real_bin, "--version"]

    def build_exec_command(
        self,
        *,
        prompt_path: Path,
        last_message_path: Path,
        options: DispatchOptions,
    ) -> list[str]:
        raise NotImplementedError

    def detect_session_id(self, run: dict[str, Any]) -> str | None:
        stdout_path = Path(run["stdout_path"])
        json_candidates = read_jsonl_candidates(stdout_path)
        if json_candidates:
            return json_candidates[0]
        return None

    def last_message_from_stdout(self, stdout_path: Path) -> str | None:
        return extract_result_text_from_json_stream(stdout_path)

    def write_last_message(self, run: dict[str, Any]) -> None:
        text = self.last_message_from_stdout(Path(run["stdout_path"]))
        if not text:
            return
        write_text(Path(run["last_message_path"]), text.rstrip() + "\n")

    def build_resume_command(self, run: dict[str, Any], exec_mode: bool) -> str:
        raise NotImplementedError

    def launch_preflight(self, run: dict[str, Any]) -> tuple[bool, str | None]:
        cwd_raw = normalize_optional_text(run.get("cwd")) or normalize_optional_text(run.get("source_cwd")) or str(Path.cwd())
        provider_bin_raw = normalize_optional_text(run.get("provider_bin")) or self.resolved_bin()
        try:
            real_bin = resolve_executable(provider_bin_raw, cwd=Path(cwd_raw))
        except RuntimeError as exc:
            return False, f"provider-bin-unavailable: {short_summary(str(exc), max_chars=140)}"
        command = self.build_preflight_command(real_bin=real_bin, cwd=Path(cwd_raw))
        if not command:
            return True, None
        ok, detail = run_command_healthcheck(
            command,
            cwd=Path(cwd_raw),
            timeout_seconds=self.preflight_timeout_seconds(),
        )
        if not ok:
            return False, f"provider-env: {detail or 'self-check failed'}"
        return True, None

    def runtime_launch_failure(self, run: dict[str, Any]) -> str | None:
        return None


@dataclass(frozen=True)
class ExecProviderSpec:
    build_exec_args: Callable[[DispatchOptions, Path, Path], list[str]]
    build_resume_args: Callable[[dict[str, Any], bool], list[str]]
    build_preflight_args: Callable[[str, Path], list[str] | None] | None = None
    last_message_mode: str = "json_result"


class ExecProviderAdapter(ProviderAdapter):
    spec: ExecProviderSpec

    def __init__(
        self,
        *,
        name: str,
        session_label: str,
        bin_env_var: str,
        default_bin: str,
        capabilities: ProviderCapabilities,
        notes: str,
        spec: ExecProviderSpec,
    ) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "session_label", session_label)
        object.__setattr__(self, "bin_env_var", bin_env_var)
        object.__setattr__(self, "default_bin", default_bin)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "notes", notes)
        object.__setattr__(self, "spec", spec)

    def build_preflight_command(self, *, real_bin: str, cwd: Path) -> list[str] | None:
        builder = self.spec.build_preflight_args
        if builder is None:
            return super().build_preflight_command(real_bin=real_bin, cwd=cwd)
        return builder(real_bin, cwd)

    def build_exec_command(
        self,
        *,
        prompt_path: Path,
        last_message_path: Path,
        options: DispatchOptions,
    ) -> list[str]:
        return [self.resolved_bin(), *self.spec.build_exec_args(options, prompt_path, last_message_path)]

    def last_message_from_stdout(self, stdout_path: Path) -> str | None:
        if self.spec.last_message_mode == "stdout_text":
            return read_text_if_exists(stdout_path)
        return super().last_message_from_stdout(stdout_path)

    def build_resume_command(self, run: dict[str, Any], exec_mode: bool) -> str:
        cwd = shlex.quote(run["cwd"])
        return f"cd {cwd} && {quote_command(self.spec.build_resume_args(run, exec_mode))}"


def build_codex_exec_args(
    options: DispatchOptions,
    prompt_path: Path,
    last_message_path: Path,
) -> list[str]:
    del prompt_path
    args = [
        "exec",
        "--json",
        "--output-last-message",
        str(last_message_path),
        "--cd",
        str(options.cd),
    ]
    if options.sandbox:
        args.extend(["--sandbox", options.sandbox])
    if options.model:
        args.extend(["--model", options.model])
    if options.profile:
        args.extend(["--profile", options.profile])
    for add_dir in options.add_dirs:
        args.extend(["--add-dir", str(add_dir)])
    for config in options.configs:
        args.extend(["--config", config])
    for feature in options.enables:
        args.extend(["--enable", feature])
    for feature in options.disables:
        args.extend(["--disable", feature])
    for image in options.images:
        args.extend(["--image", str(image)])
    if options.search:
        args.append("--search")
    if options.skip_git_repo_check:
        args.append("--skip-git-repo-check")
    if options.ephemeral:
        args.append("--ephemeral")
    if options.full_auto:
        args.append("--full-auto")
    if options.dangerous:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    args.append("-")
    return args


def build_codex_resume_args(run: dict[str, Any], exec_mode: bool) -> list[str]:
    session_id = require_session_id_for_resume(run)
    args = [normalize_optional_text(run.get("provider_bin")) or get_provider("codex").resolved_bin()]
    if exec_mode:
        args.extend(["exec", "resume", session_id, "-"])
        return args
    args.extend(["resume", session_id])
    return args


class CodexProvider(ExecProviderAdapter):
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
            notes="Codex exec/resume adapter with preserved backend reachability and thread detection behavior.",
            spec=ExecProviderSpec(
                build_exec_args=build_codex_exec_args,
                build_resume_args=build_codex_resume_args,
            ),
        )

    def detect_session_id(self, run: dict[str, Any]) -> str | None:
        stdout_path = Path(run["stdout_path"])
        json_candidates = read_jsonl_candidates(stdout_path)
        if json_candidates:
            return json_candidates[0]
        provider_bin = normalize_optional_text(run.get("provider_bin")) or self.resolved_bin()
        if os.path.basename(provider_bin) != "codex":
            return None
        started_epoch = run.get("started_epoch")
        if started_epoch is None:
            return None
        known = known_thread_ids()
        for candidate in infer_thread_ids_from_logs(int(started_epoch)):
            if candidate in known:
                return candidate
        candidates = infer_thread_ids_from_logs(int(started_epoch))
        if len(candidates) == 1:
            return candidates[0]
        return None

    def write_last_message(self, run: dict[str, Any]) -> None:
        return

    def launch_preflight(self, run: dict[str, Any]) -> tuple[bool, str | None]:
        cwd_raw = normalize_optional_text(run.get("cwd")) or normalize_optional_text(run.get("source_cwd")) or str(Path.cwd())
        try:
            resolve_executable(
                normalize_optional_text(run.get("provider_bin")) or self.resolved_bin(),
                cwd=Path(cwd_raw),
            )
        except RuntimeError as exc:
            return False, f"provider-bin-unavailable: {short_summary(str(exc), max_chars=140)}"
        try:
            with socket.create_connection((CODEX_BACKEND_HOST, CODEX_BACKEND_PORT), timeout=2):
                pass
        except OSError as exc:
            detail = short_summary(str(exc), max_chars=140)
            return (
                False,
                "provider-env: codex backend unreachable "
                f"({CODEX_BACKEND_HOST}:{CODEX_BACKEND_PORT}) :: {detail}. "
                "Run team-leader where Codex has network access.",
            )
        return True, None

    def runtime_launch_failure(self, run: dict[str, Any]) -> str | None:
        launched_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))
        if launched_epoch is None or epoch_now() - launched_epoch < 6:
            return None
        stderr_path = Path(run["stderr_log"])
        if not stderr_path.exists():
            return None
        text = read_tail_text(stderr_path, max_bytes=32768).lower()
        if text.count("failed to connect to websocket") < 2:
            return None
        if "backend-api/codex/responses" not in text:
            return None
        if (
            "failed to lookup address information" not in text
            and "reconnecting..." not in text
            and "could not create otel exporter" not in text
        ):
            return None
        return (
            "provider-env: codex child could not reach the backend after launch. "
            f"Run team-leader where Codex can reach {CODEX_BACKEND_HOST}:{CODEX_BACKEND_PORT}."
        )


def require_session_id_for_resume(run: dict[str, Any]) -> str:
    session_id = get_session_id(run)
    if not session_id:
        raise RuntimeError("run has no detected session_id; use reconcile or attach-session first")
    return session_id


def build_claude_exec_args(
    options: DispatchOptions,
    prompt_path: Path,
    last_message_path: Path,
) -> list[str]:
    del prompt_path
    del last_message_path
    sandbox = options.sandbox or "read-only"
    args = ["-p", "--verbose", "--output-format", "stream-json"]
    if options.model:
        args.extend(["--model", options.model])
    for add_dir in options.add_dirs:
        args.extend(["--add-dir", str(add_dir)])
    if options.dangerous or sandbox == "danger-full-access":
        args.extend(["--permission-mode", "bypassPermissions"])
    elif sandbox == "read-only":
        args.extend(["--permission-mode", "plan"])
    else:
        args.extend(
            [
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Bash,Read,Glob,Grep,Edit,Write",
            ]
        )
    return args


def build_claude_resume_args(run: dict[str, Any], exec_mode: bool) -> list[str]:
    session_id = require_session_id_for_resume(run)
    args = [normalize_optional_text(run.get("provider_bin")) or get_provider("claude").resolved_bin(), "-r", session_id]
    if exec_mode:
        args.extend(["--verbose", "--output-format", "stream-json", "-p"])
    return args


def build_cursor_exec_args(
    options: DispatchOptions,
    prompt_path: Path,
    last_message_path: Path,
) -> list[str]:
    del prompt_path
    del last_message_path
    sandbox = options.sandbox or "read-only"
    args = ["-p", "--output-format", "stream-json"]
    if options.model:
        args.extend(["--model", options.model])
    if options.full_auto or options.dangerous or sandbox != "read-only":
        args.append("--force")
    return args


def build_cursor_resume_args(run: dict[str, Any], exec_mode: bool) -> list[str]:
    session_id = require_session_id_for_resume(run)
    args = [
        normalize_optional_text(run.get("provider_bin")) or get_provider("cursor").resolved_bin(),
        "--resume",
        session_id,
    ]
    if exec_mode:
        args.extend(["--output-format", "stream-json", "-p"])
    return args


def build_cursor_preflight_args(real_bin: str, cwd: Path) -> list[str] | None:
    del cwd
    return [real_bin, "status"]


def build_kiro_exec_args(
    options: DispatchOptions,
    prompt_path: Path,
    last_message_path: Path,
) -> list[str]:
    del last_message_path
    sandbox = options.sandbox or "read-only"
    args = ["chat", "--no-interactive"]
    if options.dangerous or sandbox == "danger-full-access":
        args.append("--trust-all-tools")
    else:
        trusted_tools = ["read", "glob", "grep"]
        if options.search:
            trusted_tools.extend(["web_search", "web_fetch"])
        if options.full_auto or sandbox != "read-only":
            trusted_tools.extend(["write", "shell"])
        args.extend(["--trust-tools", ",".join(unique_preserve_order(trusted_tools))])
    args.append(prompt_path.read_text(encoding="utf-8"))
    return args


def build_kiro_resume_args(run: dict[str, Any], exec_mode: bool) -> list[str]:
    args = [normalize_optional_text(run.get("provider_bin")) or get_provider("kiro").resolved_bin(), "chat", "--resume"]
    if exec_mode:
        args.append("--no-interactive")
    return args


def build_kiro_preflight_args(real_bin: str, cwd: Path) -> list[str] | None:
    del cwd
    return [real_bin, "whoami"]


class ClaudeProvider(ExecProviderAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="claude",
            session_label="session",
            bin_env_var="CLAUDE_BIN",
            default_bin="claude",
            capabilities=ProviderCapabilities(
                sandbox_modes=("read-only", "workspace-write", "danger-full-access"),
                supports_full_auto=True,
                supports_dangerous=True,
                supports_model=True,
                supports_add_dir=True,
                supports_exec_resume=True,
            ),
            notes="Claude Code headless adapter using `claude -p` and `claude -r <session-id>`.",
            spec=ExecProviderSpec(
                build_exec_args=build_claude_exec_args,
                build_resume_args=build_claude_resume_args,
            ),
        )


class CursorProvider(ExecProviderAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="cursor",
            session_label="session",
            bin_env_var="CURSOR_AGENT_BIN",
            default_bin="cursor-agent",
            capabilities=ProviderCapabilities(
                sandbox_modes=("read-only", "workspace-write", "danger-full-access"),
                supports_full_auto=True,
                supports_dangerous=True,
                supports_model=True,
                supports_exec_resume=True,
            ),
            notes="Cursor Agent headless adapter using `cursor-agent -p` and `--resume`.",
            spec=ExecProviderSpec(
                build_exec_args=build_cursor_exec_args,
                build_resume_args=build_cursor_resume_args,
                build_preflight_args=build_cursor_preflight_args,
            ),
        )


class KiroProvider(ExecProviderAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="kiro",
            session_label="session",
            bin_env_var="KIRO_BIN",
            default_bin="kiro-cli",
            capabilities=ProviderCapabilities(
                sandbox_modes=("read-only", "workspace-write", "danger-full-access"),
                supports_search=True,
                supports_full_auto=True,
                supports_dangerous=True,
                supports_exec_resume=True,
            ),
            notes="Kiro CLI headless adapter using `kiro-cli chat --no-interactive`; resume is directory-scoped.",
            spec=ExecProviderSpec(
                build_exec_args=build_kiro_exec_args,
                build_resume_args=build_kiro_resume_args,
                build_preflight_args=build_kiro_preflight_args,
                last_message_mode="stdout_text",
            ),
        )


PROVIDERS: dict[str, ProviderAdapter] = {
    DEFAULT_PROVIDER: CodexProvider(),
    "claude": ClaudeProvider(),
    "cursor": CursorProvider(),
    "kiro": KiroProvider(),
}


def provider_aliases_for(name: str) -> list[str]:
    return sorted(alias for alias, canonical in PROVIDER_ALIASES.items() if canonical == name)


def normalize_provider_alias(name: str | None) -> str | None:
    value = normalize_optional_text(name)
    if value is None:
        return None
    lowered = value.lower()
    return PROVIDER_ALIASES.get(lowered, lowered)


def provider_names_for_help() -> str:
    names = []
    for name in sorted(PROVIDERS):
        aliases = provider_aliases_for(name)
        if aliases:
            names.append(f"{name} ({', '.join(aliases)})")
        else:
            names.append(name)
    return ", ".join(names)


def get_provider(name: str | None) -> ProviderAdapter:
    provider_name = normalize_provider_alias(name) or DEFAULT_PROVIDER
    adapter = PROVIDERS.get(provider_name)
    if adapter is None:
        supported = provider_names_for_help()
        raise RuntimeError(
            f"unsupported provider: {provider_name}. supported providers: {supported}"
        )
    return adapter


def validate_provider_name(name: str | None, *, field_name: str = "provider") -> str | None:
    value = normalize_optional_text(name)
    if value is None:
        return None
    provider_name = normalize_provider_alias(value)
    try:
        get_provider(provider_name)
    except RuntimeError as exc:
        raise RuntimeError(f"invalid {field_name}: {value}. {exc}") from exc
    return provider_name


def normalize_provider_list(value: Any, field_name: str) -> list[str]:
    names = []
    for item in normalize_str_list(value, field_name):
        normalized = validate_provider_name(item, field_name=field_name)
        if normalized:
            names.append(normalized)
    return unique_preserve_order(names)


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


def provider_preflight_cache_seconds(run: dict[str, Any]) -> int:
    status = normalize_optional_text(run.get("provider_preflight_status"))
    if status == "ok":
        return provider_preflight_ok_seconds()
    return provider_preflight_fail_seconds()


def provider_launch_ready(run: dict[str, Any]) -> tuple[bool, str | None]:
    checked_at = normalize_optional_text(run.get("provider_preflight_checked_at"))
    checked_epoch = parse_timestamp_epoch(checked_at)
    cache_seconds = provider_preflight_cache_seconds(run)
    if checked_epoch is not None and epoch_now() - checked_epoch < cache_seconds:
        status = normalize_optional_text(run.get("provider_preflight_status"))
        note = normalize_optional_text(run.get("provider_preflight_note"))
        return status != "blocked", note
    ok, note = provider_for_run(run).launch_preflight(run)
    run["provider_preflight_checked_at"] = utc_now()
    run["provider_preflight_status"] = "ok" if ok else "blocked"
    run["provider_preflight_note"] = note
    return ok, note


def workspace_preflight_cache_seconds(run: dict[str, Any]) -> int:
    status = normalize_optional_text(run.get("workspace_preflight_status"))
    if status == "ok":
        return provider_preflight_ok_seconds()
    return provider_preflight_fail_seconds()


def worktree_write_preflight(run: dict[str, Any]) -> tuple[bool, str | None]:
    if not run_requires_workspace_isolation(run):
        return True, None
    repo_root = project_git_root_for_run(run)
    if not repo_root:
        return False, "workspace-preflight: missing source repo root for worktree setup"
    common_dir = git_common_dir(repo_root)
    if not common_dir:
        return False, f"workspace-preflight: unable to resolve git metadata directory for {repo_root}"
    probe = common_dir / f".team-leader-write-test-{os.getpid()}-{epoch_now()}"
    try:
        probe.write_text("ok\n", encoding="utf-8")
    except OSError as exc:
        detail = short_summary(str(exc), max_chars=140)
        return (
            False,
            "workspace-preflight: git metadata not writable "
            f"at {common_dir} :: {detail}. Writer worktrees and integration commits "
            "need write access there; under Codex workspace-write this may require escalation.",
        )
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return True, None


def workspace_launch_ready(run: dict[str, Any]) -> tuple[bool, str | None]:
    if not run_requires_workspace_isolation(run):
        return True, None
    checked_at = normalize_optional_text(run.get("workspace_preflight_checked_at"))
    checked_epoch = parse_timestamp_epoch(checked_at)
    cache_seconds = workspace_preflight_cache_seconds(run)
    if checked_epoch is not None and epoch_now() - checked_epoch < cache_seconds:
        status = normalize_optional_text(run.get("workspace_preflight_status"))
        note = normalize_optional_text(run.get("workspace_preflight_note"))
        return status != "blocked", note
    ok, note = worktree_write_preflight(run)
    run["workspace_preflight_checked_at"] = utc_now()
    run["workspace_preflight_status"] = "ok" if ok else "blocked"
    run["workspace_preflight_note"] = note
    return ok, note


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
        run.setdefault("started_epoch", None)
        run.setdefault("owned_paths", [])
        run.setdefault("depends_on", [])
        run.setdefault("dispatch_state", None)
        run.setdefault("dispatch_state_changed_at", run.get("created_at") or run.get("launched_at"))
        run.setdefault("blocked_on", [])
        run.setdefault("blocked_seconds", 0)
        run.setdefault("queued_seconds", 0)
        run.setdefault("planner_source", None)
        run.setdefault("planner_reason", None)
        run.setdefault("planner_default_child_provider", None)
        run.setdefault("planner_default_child_provider_bin", None)
        run.setdefault("planner_allowed_providers", [])
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
        run.setdefault("integration_applied_paths", [])
        run.setdefault("integration_dropped_paths", [])
        run.setdefault("artifact_sizes", {})
        run.setdefault("output_warnings", [])
        run.setdefault("last_message_truncated", False)
        run.setdefault("last_message_original_bytes", None)
        run.setdefault("compacted_last_message_preview", None)
        run.setdefault("max_run_seconds", None)
        run.setdefault("timed_out_at", None)
        run.setdefault("timeout_reason", None)
        run.setdefault("heartbeat_path", str(Path(run["run_dir"]) / "heartbeat.txt"))
        run.setdefault("heartbeat_at", None)
        run.setdefault("heartbeat_lag_seconds", None)
        run.setdefault("runtime_health", None)
        run.setdefault("runtime_health_note", None)
        run.setdefault("question_records", [])
        run.setdefault("question_source_bytes", None)
        run.setdefault("question_source_mtime_ns", None)
        run.setdefault("provider_preflight_status", None)
        run.setdefault("provider_preflight_checked_at", None)
        run.setdefault("provider_preflight_note", None)
        run.setdefault("workspace_preflight_status", None)
        run.setdefault("workspace_preflight_checked_at", None)
        run.setdefault("workspace_preflight_note", None)
        run.setdefault("compacted_at", None)
        run.setdefault("compaction_reason", None)
        run.setdefault("compaction_removed", [])
        run.setdefault("workspace_released_at", None)
        run.setdefault("workspace_release_error", None)
        if get_session_id(run):
            set_session_id(run, get_session_id(run))
    return data


def save_index(root: Path, data: dict[str, Any]) -> None:
    ensure_root(root)
    data["version"] = INDEX_VERSION
    write_text(
        index_path(root),
        json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )


def dispatch_wait_field(dispatch_state: str | None) -> str | None:
    state = normalize_optional_text(dispatch_state)
    if state == "blocked":
        return "blocked_seconds"
    if state == "queued":
        return "queued_seconds"
    return None


def set_run_dispatch_state(
    run: dict[str, Any],
    dispatch_state: str,
    blocked_on: list[str] | None = None,
    *,
    changed_at: str | None = None,
) -> None:
    now_text = changed_at or utc_now()
    old_state = normalize_optional_text(run.get("dispatch_state"))
    old_since_text = normalize_optional_text(run.get("dispatch_state_changed_at")) or normalize_optional_text(run.get("created_at")) or now_text
    if old_state and old_state != dispatch_state:
        wait_field = dispatch_wait_field(old_state)
        if wait_field:
            now_epoch = parse_timestamp_epoch(now_text) or epoch_now()
            old_since_epoch = parse_timestamp_epoch(old_since_text) or now_epoch
            if now_epoch > old_since_epoch:
                run[wait_field] = int(run.get(wait_field) or 0) + (now_epoch - old_since_epoch)
        run["dispatch_state_changed_at"] = now_text
    elif not normalize_optional_text(run.get("dispatch_state_changed_at")):
        run["dispatch_state_changed_at"] = old_since_text
    run["dispatch_state"] = dispatch_state
    run["blocked_on"] = list(blocked_on or [])


def accumulated_dispatch_wait_seconds(run: dict[str, Any], dispatch_state: str, *, now_epoch: int | None = None) -> int:
    field = dispatch_wait_field(dispatch_state)
    if not field:
        return 0
    total = int(run.get(field) or 0)
    current_state = normalize_optional_text(run.get("dispatch_state"))
    if current_state != dispatch_state:
        return total
    since_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("dispatch_state_changed_at")))
    current_epoch = now_epoch or epoch_now()
    if since_epoch is not None and current_epoch > since_epoch:
        total += current_epoch - since_epoch
    return total


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
    existing = read_text_if_exists(path)
    if existing == content:
        return
    path.write_text(content, encoding="utf-8")


def child_prompt_guard(prompt_text: str) -> str:
    lines = [
        "You are a child CLI session launched by team-leader.",
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
    for candidate in PROVIDERS.values():
        blocked_names.add(candidate.default_bin)
        blocked_names.add(os.path.basename(candidate.resolved_bin()))
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


def max_plan_runs_per_wave() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_MAX_PLAN_RUNS_PER_WAVE"))
    if raw is None:
        return max(DEFAULT_MAX_PLAN_RUNS_PER_WAVE, max_parallel_sessions())
    try:
        return max(1, int(raw))
    except ValueError:
        return max(DEFAULT_MAX_PLAN_RUNS_PER_WAVE, max_parallel_sessions())


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


def run_heartbeat_interval_seconds() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_RUN_HEARTBEAT_INTERVAL_SECONDS"))
    if raw is None:
        return DEFAULT_RUN_HEARTBEAT_INTERVAL_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RUN_HEARTBEAT_INTERVAL_SECONDS


def run_heartbeat_stale_seconds() -> int:
    fallback = max(DEFAULT_RUN_HEARTBEAT_STALE_SECONDS, run_heartbeat_interval_seconds() * 3)
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_RUN_HEARTBEAT_STALE_SECONDS"))
    if raw is None:
        return fallback
    try:
        return max(run_heartbeat_interval_seconds() + 2, int(raw))
    except ValueError:
        return fallback


def provider_preflight_ok_seconds() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_PROVIDER_PREFLIGHT_OK_SECONDS"))
    if raw is None:
        return DEFAULT_PROVIDER_PREFLIGHT_OK_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PROVIDER_PREFLIGHT_OK_SECONDS


def provider_preflight_fail_seconds() -> int:
    raw = normalize_optional_text(os.environ.get("TEAM_LEADER_PROVIDER_PREFLIGHT_FAIL_SECONDS"))
    if raw is None:
        return DEFAULT_PROVIDER_PREFLIGHT_FAIL_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PROVIDER_PREFLIGHT_FAIL_SECONDS


def delete_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def delete_tree_if_exists(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{field_name} must be a positive integer")
    return parsed


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


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


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


def project_metrics_md_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_METRICS_MD


def project_history_path(project_dir: Path) -> Path:
    return project_dir / "history.md"


def project_default_detail_path(project_dir: Path) -> Path:
    dashboard = project_dir / "dashboard.md"
    history = project_history_path(project_dir)
    if dashboard.exists() or not history.exists():
        return dashboard
    return history


def default_project_brief(project_name: str) -> dict[str, Any]:
    return {
        "project": project_name,
        "created_at": utc_now(),
        "goal": None,
        "repo_paths": [],
        "spec_paths": [],
        "notes": [],
        "constraints": [],
        "autonomy_mode": "manual",
        "clarification_mode": "auto",
        "validation_commands": [],
        "completion_sentinel": None,
        "max_work_seconds": None,
        "max_planner_rounds": DEFAULT_PROJECT_MAX_PLANNER_ROUNDS,
        "max_auto_fix_rounds": DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS,
        "planner_provider": None,
        "planner_provider_bin": None,
        "child_provider": None,
        "child_provider_bin": None,
        "allowed_providers": [],
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
    brief["created_at"] = normalize_optional_text(payload.get("created_at")) or brief["created_at"]
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
    brief["max_work_seconds"] = normalize_optional_positive_int(
        payload.get("max_work_seconds"),
        "max_work_seconds",
    )
    max_rounds_raw = payload.get("max_planner_rounds")
    try:
        max_rounds = int(max_rounds_raw)
    except (TypeError, ValueError):
        max_rounds = DEFAULT_PROJECT_MAX_PLANNER_ROUNDS
    brief["max_planner_rounds"] = max(1, max_rounds)
    max_auto_fix_rounds_raw = payload.get("max_auto_fix_rounds")
    try:
        max_auto_fix_rounds = int(max_auto_fix_rounds_raw)
    except (TypeError, ValueError):
        max_auto_fix_rounds = DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS
    brief["max_auto_fix_rounds"] = max(0, max_auto_fix_rounds)
    brief["planner_provider"] = validate_provider_name(
        normalize_optional_text(payload.get("planner_provider")),
        field_name="planner_provider",
    )
    brief["planner_provider_bin"] = normalize_optional_text(payload.get("planner_provider_bin"))
    brief["child_provider"] = validate_provider_name(
        normalize_optional_text(payload.get("child_provider")),
        field_name="child_provider",
    )
    brief["child_provider_bin"] = normalize_optional_text(payload.get("child_provider_bin"))
    brief["allowed_providers"] = normalize_provider_list(
        payload.get("allowed_providers"),
        "allowed_providers",
    )
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
    max_work_seconds: int | None = None,
    max_planner_rounds: int | None = None,
    max_auto_fix_rounds: int | None = None,
    planner_provider: str | None = None,
    planner_provider_bin: str | None = None,
    child_provider: str | None = None,
    child_provider_bin: str | None = None,
    allowed_providers: list[str] | None = None,
) -> dict[str, Any]:
    brief = dict(existing or default_project_brief(project_name))
    brief["project"] = project_name
    brief["created_at"] = normalize_optional_text(brief.get("created_at")) or utc_now()
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
    if max_work_seconds is not None:
        brief["max_work_seconds"] = normalize_optional_positive_int(
            max_work_seconds,
            "max_work_seconds",
        )
    if max_planner_rounds is not None:
        brief["max_planner_rounds"] = max(1, int(max_planner_rounds))
    if max_auto_fix_rounds is not None:
        brief["max_auto_fix_rounds"] = max(0, int(max_auto_fix_rounds))
    if planner_provider is not None:
        brief["planner_provider"] = validate_provider_name(
            planner_provider,
            field_name="planner_provider",
        )
    if planner_provider_bin is not None:
        brief["planner_provider_bin"] = normalize_optional_text(planner_provider_bin)
    if child_provider is not None:
        brief["child_provider"] = validate_provider_name(
            child_provider,
            field_name="child_provider",
        )
    if child_provider_bin is not None:
        brief["child_provider_bin"] = normalize_optional_text(child_provider_bin)
    if allowed_providers is not None:
        brief["allowed_providers"] = normalize_provider_list(
            allowed_providers,
            "allowed_providers",
        )
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
    max_work_seconds = normalize_optional_positive_int(brief.get("max_work_seconds"), "max_work_seconds")
    max_planner_rounds = brief.get("max_planner_rounds")
    max_auto_fix_rounds = brief.get("max_auto_fix_rounds")
    planner_provider = validate_provider_name(
        normalize_optional_text(brief.get("planner_provider")),
        field_name="planner_provider",
    )
    planner_provider_bin = normalize_optional_text(brief.get("planner_provider_bin"))
    child_provider = validate_provider_name(
        normalize_optional_text(brief.get("child_provider")),
        field_name="child_provider",
    )
    child_provider_bin = normalize_optional_text(brief.get("child_provider_bin"))
    allowed_providers = normalize_provider_list(brief.get("allowed_providers"), "allowed_providers")
    lines = [
        f"# {brief.get('project') or 'Project'} Brief",
        "",
        f"- Created: `{normalize_optional_text(brief.get('created_at')) or utc_now()}`",
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
    lines.append(f"- Max planned child runs per wave: `{max_plan_runs_per_wave()}`")
    lines.append(f"- Release throttle per cycle: `{max_releases_per_cycle()}`")
    lines.append(f"- Release throttle window: `{max_release_window_seconds()}` seconds")
    lines.append(f"- Max project work time: `{format_duration(max_work_seconds) if max_work_seconds is not None else '-'}`")
    lines.append(f"- Max planner rounds: `{max_planner_rounds}`")
    lines.append(f"- Max auto-fix rounds: `{max_auto_fix_rounds}`")
    lines.append(f"- Completion sentinel: `{completion_sentinel or '-'}`")
    lines.append(f"- Planner provider: `{planner_provider or DEFAULT_PROVIDER}`")
    lines.append(f"- Planner provider bin: `{planner_provider_bin or '-'}`")
    lines.append(f"- Default child provider: `{child_provider or planner_provider or DEFAULT_PROVIDER}`")
    lines.append(f"- Default child provider bin: `{child_provider_bin or '-'}`")
    lines.append(
        f"- Allowed child providers: {format_inline_list(allowed_providers or sorted(PROVIDERS))}"
    )
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
                str(item.get("provider") or "-"),
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
            markdown_table(
                ["provider", "task", "summary", "role", "sandbox", "depends_on", "owned_paths"],
                rows or [["-", "-", "-", "-", "-", "-", "-"]],
            ),
            "",
        ]
    )
    return "\n".join(lines)


def project_state_policy_markdown(project_name: str, project_dir: Path, *, compacted: bool = False) -> list[str]:
    return [
        "## State Policy",
        "",
        f"- Project slug: `{project_slug(project_name)}`",
        f"- Reused folder: `{project_dir}`",
        "- Same project name reuses this folder and its tracked history.",
        "- Generated markdown files here are persistent manager state, not disposable temp files.",
        "- Normal continuation: keep the files and let the manager refresh them.",
        (
            "- Once a project settles cleanly, team-leader compacts transient dashboards, question scratchpads, per-run reports, and disposable child-run artifacts."
            if compacted
            else "- Once a project settles cleanly, team-leader may compact transient dashboards, question scratchpads, per-run reports, and disposable child-run artifacts."
        ),
        f"- Planner waves are capped at `{max_plan_runs_per_wave()}` child runs.",
        f"- Large child last messages are truncated to `{max_last_message_bytes()}` bytes with head/tail preservation.",
        f"- Launches are rate-limited to `{max_releases_per_cycle()}` new child sessions per `{max_release_window_seconds()}` seconds.",
        f"- Session-id log scans are capped at `{max_jsonl_scan_bytes()}` bytes per run refresh.",
        "- Human-edited file: `answers.md`.",
        "- Use `cleanup` when you want the manager to compact failed or standalone runs explicitly.",
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
        "- settled projects may be compacted automatically to reduce leftover files",
        f"- planner waves are capped at {max_plan_runs_per_wave()} child runs",
        f"- last messages are capped at {max_last_message_bytes()} bytes with head/tail preservation",
        f"- launches are capped at {max_releases_per_cycle()} per {max_release_window_seconds()}s window",
        f"- session-id scans are capped at {max_jsonl_scan_bytes()} bytes",
        "- normal continuation: keep the files; only answers.md is meant for human edits",
        "- use cleanup to compact failed or standalone runs explicitly",
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


def run_is_unsettled(run: dict[str, Any]) -> bool:
    status = str(run.get("status") or "")
    dispatch_state = str(run.get("dispatch_state") or "")
    if status in {"running", "prepared", "blocked"}:
        return True
    return dispatch_state in {"ready", "blocked", "queued"}


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
    run["integration_applied_paths"] = []
    run["integration_dropped_paths"] = []


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


def mark_integration_state(run: dict[str, Any], state: str, note: str | None) -> None:
    run["integration_state"] = state
    run["integration_note"] = note
    run["integration_updated_at"] = utc_now()


def integration_apply_args(paths: list[str]) -> list[str]:
    args = ["apply", "--3way", "--whitespace=nowarn"]
    for rel_path in paths:
        args.append(f"--include={rel_path}")
    args.append("-")
    return args


def clean_integration_workspace(integration_dir: Path) -> None:
    git_run(["reset", "--hard", "HEAD"], cwd=integration_dir, check=False)
    git_run(["clean", "-fd"], cwd=integration_dir, check=False)


def git_error_looks_like_write_failure(message: str | None) -> bool:
    text = (message or "").lower()
    if not text:
        return False
    return any(
        needle in text
        for needle in (
            "index.lock",
            "permission denied",
            "operation not permitted",
            "unable to create",
            "could not write",
            "read-only file system",
        )
    )


def repair_run_integration(root: Path, run: dict[str, Any], *, retry_conflict: bool = False) -> None:
    if not run_requires_workspace_isolation(run):
        raise RuntimeError("run does not use an isolated worktree")
    if str(run.get("status") or "") != "completed":
        raise RuntimeError("run is not completed yet")
    state = normalize_optional_text(run.get("integration_state")) or "pending"
    if state in INTEGRATION_READY_STATES:
        return
    if state == "conflict" and not retry_conflict:
        raise RuntimeError(
            "integration state is conflict; retry with --retry-conflict after you have manually "
            "prepared the worktree or integration branch. Not every worktree conflict is safely auto-retryable."
        )
    mark_integration_state(run, "pending", "manual retry requested")
    run["integration_applied_paths"] = []
    run["integration_dropped_paths"] = []
    apply_run_to_integration(root, run)


def apply_run_to_integration(root: Path, run: dict[str, Any]) -> None:
    if not run_requires_workspace_isolation(run):
        return
    if str(run.get("status") or "") != "completed":
        return
    integration_state = normalize_optional_text(run.get("integration_state"))
    if integration_state in INTEGRATION_READY_STATES:
        return
    if integration_state in INTEGRATION_ALERT_STATES:
        return
    worktree_path_raw = normalize_optional_text(run.get("worktree_path"))
    base_ref = normalize_optional_text(run.get("workspace_base_ref"))
    if not worktree_path_raw or not base_ref:
        return
    worktree_path = Path(worktree_path_raw)
    if not worktree_path.exists():
        mark_integration_state(run, "apply-failed", "worktree path is missing")
        return
    try:
        changed_paths, patch = git_has_tracked_changes(worktree_path, base_ref)
    except RuntimeError as exc:
        mark_integration_state(run, "apply-failed", short_summary(str(exc), max_chars=180))
        return
    run["changed_paths"] = changed_paths
    run["integration_applied_paths"] = []
    run["integration_dropped_paths"] = []
    if not patch.strip():
        mark_integration_state(run, "no-changes", "writer completed without diff against the integration base")
        return
    owned_paths = relative_owned_paths(run)
    selected_paths = list(changed_paths)
    outside_scope: list[str] = []
    if owned_paths:
        selected_paths = [path for path in changed_paths if path_within_owned_paths(path, owned_paths)]
        outside_scope = [path for path in changed_paths if path not in selected_paths]
    run["integration_applied_paths"] = list(selected_paths)
    run["integration_dropped_paths"] = list(outside_scope)
    if outside_scope and not selected_paths:
        mark_integration_state(
            run,
            "scope-violation",
            "all changed paths were outside declared ownership: " + ", ".join(outside_scope[:6]),
        )
        return
    try:
        integration_dir = ensure_integration_workspace(root, run)
    except RuntimeError as exc:
        mark_integration_state(run, "apply-failed", short_summary(str(exc), max_chars=180))
        return
    clean_integration_workspace(integration_dir)
    try:
        git_run(integration_apply_args(selected_paths), cwd=integration_dir, input_text=patch)
    except subprocess.CalledProcessError as exc:
        clean_integration_workspace(integration_dir)
        detail = exc.stderr or exc.stdout or "git apply failed"
        failure_state = "apply-failed" if git_error_looks_like_write_failure(detail) else "conflict"
        mark_integration_state(run, failure_state, short_summary(detail, max_chars=180))
        return
    try:
        git_run(["add", "-A"], cwd=integration_dir)
        commit_prefix = "team-leader integrate subset" if outside_scope else "team-leader integrate"
        git_run(
            [
                "-c",
                "user.name=team-leader",
                "-c",
                "user.email=team-leader@local",
                "commit",
                "-m",
                f"{commit_prefix} {run['run_id']}: {short_summary(normalize_optional_text(run.get('summary')), max_chars=72)}",
            ],
            cwd=integration_dir,
        )
    except subprocess.CalledProcessError as exc:
        clean_integration_workspace(integration_dir)
        mark_integration_state(run, "commit-failed", short_summary(exc.stderr or exc.stdout or "git commit failed", max_chars=180))
        return
    if outside_scope:
        mark_integration_state(
            run,
            "applied-subset",
            "applied owned subset into "
            f"{integration_dir}; dropped out-of-scope paths: {', '.join(outside_scope[:6])}",
        )
        return
    mark_integration_state(run, "applied", f"applied into {integration_dir}")


def maybe_integrate_completed_runs(root: Path, index: dict[str, Any]) -> None:
    for run in sorted(index["runs"], key=run_sort_key):
        apply_run_to_integration(root, run)


def delete_git_branch_if_present(repo_root: Path, branch_name: str) -> None:
    git_run(["branch", "-D", branch_name], cwd=repo_root, check=False)


def maybe_release_run_worktree(run: dict[str, Any], *, allow_terminal_cleanup: bool = False) -> None:
    if not run_requires_workspace_isolation(run):
        return
    if normalize_optional_text(run.get("workspace_released_at")):
        return
    status = str(run.get("status") or "")
    integration_state = normalize_optional_text(run.get("integration_state"))
    if allow_terminal_cleanup:
        if status not in AUTO_COMPACT_RUN_STATUSES:
            return
        if status == "completed" and integration_state not in INTEGRATION_READY_STATES:
            return
    else:
        if status != "completed":
            return
        if integration_state not in INTEGRATION_READY_STATES:
            return
    repo_root = project_git_root_for_run(run)
    worktree_path_raw = normalize_optional_text(run.get("worktree_path"))
    if not repo_root or not worktree_path_raw:
        return
    worktree_path = Path(worktree_path_raw)
    if worktree_path.exists():
        try:
            git_run(["worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
        except subprocess.CalledProcessError as exc:
            run["workspace_release_error"] = short_summary(
                exc.stderr or exc.stdout or f"failed to remove worktree {worktree_path}",
                max_chars=180,
            )
            return
    git_run(["worktree", "prune"], cwd=repo_root, check=False)
    delete_git_branch_if_present(repo_root, run_worktree_branch(run))
    worktrees_dir = worktree_path.parent
    if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
        worktrees_dir.rmdir()
    source_cwd = normalize_optional_text(run.get("source_cwd"))
    if source_cwd:
        run["cwd"] = source_cwd
    run["workspace_released_at"] = utc_now()
    run["workspace_release_error"] = None


def maybe_release_completed_worktrees(index: dict[str, Any]) -> None:
    for run in sorted(index["runs"], key=run_sort_key):
        maybe_release_run_worktree(run)


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
        "provider": validate_provider_name(
            normalize_optional_text(item.get("provider")),
            field_name="plan provider",
        ),
        "provider_bin": normalize_optional_text(item.get("provider_bin")),
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
        in AUTO_FIX_PLANNER_REASONS
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
    planner_provider = validate_provider_name(
        normalize_optional_text(brief.get("planner_provider")),
        field_name="planner_provider",
    ) or DEFAULT_PROVIDER
    default_child_provider = default_child_provider_for_context(brief)
    default_child_provider_bin = default_child_provider_bin_for_context(brief)
    allowed_child_providers = allowed_child_providers_for_context(brief)
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
    lines.extend(["", *provider_policy_lines(
        allowed_providers=allowed_child_providers,
        default_child_provider=default_child_provider,
        default_child_provider_bin=default_child_provider_bin,
        planner_provider=planner_provider,
    )])
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
            f"- Only use child providers from this allowed list: {format_inline_list(allowed_child_providers)}.",
            f"- If a run omits `provider`, the manager will use `{default_child_provider}` by default.",
            "- Set `provider_bin` only when the user explicitly asked for a non-default executable path.",
            "- Use as few child sessions as necessary.",
            f"- Do not emit more than {max_plan_runs_per_wave()} child runs in one plan; if more work is needed, schedule only the next wave.",
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
            f'      "provider": "{default_child_provider}",',
            '      "provider_bin": "/optional/custom/provider/path",',
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


def normalize_cached_question_records(run: dict[str, Any], cached: Any) -> list[dict[str, str]]:
    if not isinstance(cached, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in cached:
        if not isinstance(item, dict):
            continue
        text = normalize_optional_text(item.get("text"))
        if not text:
            continue
        normalized.append(
            {
                "id": normalize_optional_text(item.get("id")) or question_id_for(run, text),
                "run_id": normalize_optional_text(item.get("run_id")) or str(run["run_id"]),
                "task_id": normalize_optional_text(item.get("task_id")) or str(run.get("task_id") or run["run_id"]),
                "summary": normalize_optional_text(item.get("summary")) or str(run.get("summary") or "-"),
                "text": text,
            }
        )
    run["question_records"] = normalized
    return normalized


def question_records_for_run(run: dict[str, Any]) -> list[dict[str, str]]:
    path = Path(run["last_message_path"])
    cached = run.get("question_records")
    if not path.exists():
        normalized = normalize_cached_question_records(run, cached)
        if normalized:
            run["question_source_bytes"] = None
            run["question_source_mtime_ns"] = None
            return normalized
        run["question_records"] = []
        run["question_source_bytes"] = None
        run["question_source_mtime_ns"] = None
        return []
    stat = path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns
    if (
        isinstance(cached, list)
        and run.get("question_source_bytes") == size
        and run.get("question_source_mtime_ns") == mtime_ns
    ):
        return normalize_cached_question_records(run, cached)
    records = [build_question_record(run, question_text) for question_text in extract_questions(last_message_for_run(run))]
    run["question_records"] = records
    run["question_source_bytes"] = size
    run["question_source_mtime_ns"] = mtime_ns
    return records


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
    cached = normalize_optional_text(run.get("compacted_last_message_preview"))
    if cached:
        return cached
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
        if state not in INTEGRATION_ALERT_STATES:
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
    workspace_ready, workspace_note = workspace_launch_ready(run)
    if not workspace_ready and workspace_note and workspace_note not in blocked_on:
        blocked_on.append(workspace_note)
    provider_ready, provider_note = provider_launch_ready(run)
    if not provider_ready and provider_note and provider_note not in blocked_on:
        blocked_on.append(provider_note)
    if blocked_on:
        return "blocked", blocked_on
    return "ready", []


def update_dispatch_metadata(index: dict[str, Any]) -> None:
    for run in index["runs"]:
        dispatch_state, blocked_on = compute_dispatch_state(index, run)
        set_run_dispatch_state(run, dispatch_state, blocked_on)


def apply_parallel_limit_metadata(index: dict[str, Any]) -> None:
    active_count = sum(1 for run in index["runs"] if str(run.get("status") or "") == "running")
    parallel_limit = max_parallel_sessions()
    for run in sorted(index["runs"], key=run_sort_key):
        if str(run.get("status") or "") not in PRELAUNCH_STATUSES:
            continue
        if str(run.get("dispatch_state") or "") != "ready":
            continue
        if active_count >= parallel_limit:
            set_run_dispatch_state(run, "queued", [f"parallel-limit:{parallel_limit}"])
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
            set_run_dispatch_state(run, "queued", [f"release-throttle:{release_budget}/{release_window}s"])
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
        for record in question_records_for_run(run):
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
    project_budget_reached = project_time_budget_reached(brief, runs)
    remaining_work_seconds = project_remaining_work_seconds(brief, runs)
    integration_conflicts = [
        run for run in sorted(runs, key=run_sort_key)
        if normalize_optional_text(run.get("integration_state")) in INTEGRATION_ALERT_STATES
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
    elif project_budget_reached:
        current_stage = "time-budget-reached"
        stage_reason = "The configured project work budget has been exhausted"
        next_action = "Review current outputs or increase `max_work_seconds` before launching more work"
        focus = format_duration(remaining_work_seconds)
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


def project_start_timestamp(brief: dict[str, Any] | None, runs: list[dict[str, Any]]) -> str | None:
    candidates: list[tuple[int, str]] = []
    if brief:
        for key in ("created_at", "updated_at"):
            value = normalize_optional_text(brief.get(key))
            epoch = parse_timestamp_epoch(value)
            if value and epoch is not None:
                candidates.append((epoch, value))
                break
    for run in runs:
        value = normalize_optional_text(run.get("created_at")) or normalize_optional_text(run.get("launched_at"))
        epoch = parse_timestamp_epoch(value)
        if value and epoch is not None:
            candidates.append((epoch, value))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def project_work_budget_seconds(brief: dict[str, Any] | None) -> int | None:
    if not brief:
        return None
    return normalize_optional_positive_int(brief.get("max_work_seconds"), "max_work_seconds")


def project_elapsed_seconds(
    brief: dict[str, Any] | None,
    runs: list[dict[str, Any]],
    *,
    now_epoch: int | None = None,
) -> int | None:
    started_at = project_start_timestamp(brief, runs)
    started_epoch = parse_timestamp_epoch(started_at)
    if started_epoch is None:
        return None
    current_epoch = now_epoch or epoch_now()
    return max(0, current_epoch - started_epoch)


def project_remaining_work_seconds(
    brief: dict[str, Any] | None,
    runs: list[dict[str, Any]],
    *,
    now_epoch: int | None = None,
) -> int | None:
    budget_seconds = project_work_budget_seconds(brief)
    if budget_seconds is None:
        return None
    elapsed_seconds = project_elapsed_seconds(brief, runs, now_epoch=now_epoch)
    if elapsed_seconds is None:
        return budget_seconds
    return max(0, budget_seconds - elapsed_seconds)


def project_time_budget_reached(brief: dict[str, Any] | None, runs: list[dict[str, Any]]) -> bool:
    remaining_seconds = project_remaining_work_seconds(brief, runs)
    return remaining_seconds is not None and remaining_seconds <= 0


def first_useful_result_timestamp(runs: list[dict[str, Any]]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for run in runs:
        finished_at = normalize_optional_text(run.get("finished_at"))
        finished_epoch = parse_timestamp_epoch(finished_at)
        if not finished_at or finished_epoch is None:
            continue
        last_message_path = Path(run["last_message_path"])
        has_report = last_message_path.exists() and last_message_path.stat().st_size > 0
        if has_report or question_records_for_run(run) or str(run.get("status") or "") == "completed":
            candidates.append((finished_epoch, finished_at))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def project_concurrency_metrics(runs: list[dict[str, Any]], *, now_epoch: int | None = None) -> dict[str, int]:
    current_epoch = now_epoch or epoch_now()
    events: list[tuple[int, int]] = []
    total_child_seconds = 0
    launched_runs = 0
    for run in runs:
        launched_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))
        if launched_epoch is None:
            continue
        finished_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("finished_at")))
        if finished_epoch is None and str(run.get("status") or "") == "running":
            finished_epoch = current_epoch
        if finished_epoch is None or finished_epoch < launched_epoch:
            continue
        launched_runs += 1
        total_child_seconds += finished_epoch - launched_epoch
        events.append((launched_epoch, 1))
        events.append((finished_epoch, -1))
    if not events:
        return {
            "launched_runs": launched_runs,
            "max_concurrent_runs": 0,
            "parallel_overlap_seconds": 0,
            "total_child_seconds": total_child_seconds,
        }
    events.sort(key=lambda item: (item[0], 0 if item[1] < 0 else 1))
    active = 0
    previous_epoch: int | None = None
    max_concurrent = 0
    overlap_seconds = 0
    for timestamp, delta in events:
        if previous_epoch is not None and timestamp > previous_epoch and active > 1:
            overlap_seconds += timestamp - previous_epoch
        active += delta
        if active > max_concurrent:
            max_concurrent = active
        previous_epoch = timestamp
    return {
        "launched_runs": launched_runs,
        "max_concurrent_runs": max_concurrent,
        "parallel_overlap_seconds": overlap_seconds,
        "total_child_seconds": total_child_seconds,
    }


def project_wait_metrics(runs: list[dict[str, Any]], *, now_epoch: int | None = None) -> dict[str, int]:
    current_epoch = now_epoch or epoch_now()
    blocked_seconds = 0
    queued_seconds = 0
    prelaunch_delay_seconds = 0
    for run in runs:
        blocked_seconds += accumulated_dispatch_wait_seconds(run, "blocked", now_epoch=current_epoch)
        queued_seconds += accumulated_dispatch_wait_seconds(run, "queued", now_epoch=current_epoch)
        created_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("created_at")))
        launched_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))
        if created_epoch is None:
            continue
        if launched_epoch is not None and launched_epoch > created_epoch:
            tracked_wait = int(run.get("blocked_seconds") or 0) + int(run.get("queued_seconds") or 0)
            prelaunch_delay_seconds += max(0, launched_epoch - created_epoch - tracked_wait)
        elif launched_epoch is None and str(run.get("status") or "") in PRELAUNCH_STATUSES:
            tracked_wait = (
                accumulated_dispatch_wait_seconds(run, "blocked", now_epoch=current_epoch)
                + accumulated_dispatch_wait_seconds(run, "queued", now_epoch=current_epoch)
            )
            prelaunch_delay_seconds += max(0, current_epoch - created_epoch - tracked_wait)
    return {
        "blocked_seconds": blocked_seconds,
        "queued_seconds": queued_seconds,
        "prelaunch_delay_seconds": prelaunch_delay_seconds,
        "stuck_time_seconds": blocked_seconds + queued_seconds + prelaunch_delay_seconds,
    }


def build_project_metrics(root: Path, project_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    slug = str(runs[0].get("project_slug") or project_slug(project_name)) if runs else project_slug(project_name)
    project_dir = project_workspace_dir(root, project_name, slug)
    brief = load_project_brief(project_dir, project_name)
    validation = load_project_validation(project_dir)
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    answered = answered_questions(question_records, answers)
    counts = run_status_counts(runs)
    now_epoch = epoch_now()
    start_at = project_start_timestamp(brief, runs)
    start_epoch = parse_timestamp_epoch(start_at)
    first_result_at = first_useful_result_timestamp(runs)
    first_result_epoch = parse_timestamp_epoch(first_result_at)
    validation_at = None
    if validation and (
        project_is_machine_complete(brief, validation) is True
        or normalize_optional_text(validation.get("status")) == "passed"
    ):
        validation_at = normalize_optional_text(validation.get("validated_at")) or normalize_optional_text(validation.get("updated_at"))
    validation_epoch = parse_timestamp_epoch(validation_at)
    concurrency = project_concurrency_metrics(runs, now_epoch=now_epoch)
    waits = project_wait_metrics(runs, now_epoch=now_epoch)
    return {
        "project": project_name,
        "updated_at": utc_now(),
        "workspace": str(project_dir),
        "metrics_file": str(project_metrics_md_path(project_dir)),
        "project_started_at": start_at,
        "project_age_seconds": (now_epoch - start_epoch) if start_epoch is not None else None,
        "time_to_first_useful_result_seconds": (
            first_result_epoch - start_epoch
            if start_epoch is not None and first_result_epoch is not None and first_result_epoch >= start_epoch
            else None
        ),
        "first_useful_result_at": first_result_at,
        "time_to_validated_completion_seconds": (
            validation_epoch - start_epoch
            if start_epoch is not None and validation_epoch is not None and validation_epoch >= start_epoch
            else None
        ),
        "validated_completion_at": validation_at,
        "human_touch_count": len(answered) + len(open_questions),
        "open_question_count": len(open_questions),
        "answered_question_count": len(answered),
        "parallel_value": concurrency,
        "stuck_time": waits,
        "total_runs": len(runs),
        "planner_rounds": planner_round_count(runs),
        "auto_fix_rounds": auto_fix_round_count(runs),
        "running_runs": counts["running"],
        "completed_runs": counts["completed"],
        "failed_runs": counts["failed"],
        "cancelled_runs": counts["cancelled"],
        "validation_status": normalize_optional_text(validation.get("status")) if validation else None,
    }


def render_project_metrics(metrics: dict[str, Any]) -> str:
    parallel = metrics.get("parallel_value") or {}
    stuck = metrics.get("stuck_time") or {}
    lines = [
        f"# {metrics['project']} Metrics",
        "",
        f"- Updated: `{metrics['updated_at']}`",
        f"- Workspace: `{metrics['workspace']}`",
        "",
        "## Scorecard",
        "",
        f"- `project_age`: `{format_duration(metrics.get('project_age_seconds'))}`",
        "  How long this tracked project has existed since the first brief or child run.",
        f"- `time_to_first_useful_result`: `{format_duration(metrics.get('time_to_first_useful_result_seconds'))}`",
        "  Approximate delay from project start to the first finished child report or human-question output.",
        f"- `time_to_validated_completion`: `{format_duration(metrics.get('time_to_validated_completion_seconds'))}`",
        "  Delay from project start to a passed validation or machine-complete delivery signal, when available.",
        f"- `human_touch_count`: `{metrics.get('human_touch_count', 0)}`",
        "  Total human coordination so far: answered questions plus any still-open questions.",
        (
            f"- `parallel_value`: max_concurrent=`{parallel.get('max_concurrent_runs', 0)}` "
            f"overlap=`{format_duration(parallel.get('parallel_overlap_seconds'))}`"
        ),
        "  Real overlap achieved by child sessions, which is a better speed signal than raw run count alone.",
        (
            f"- `stuck_time`: blocked=`{format_duration(stuck.get('blocked_seconds'))}` "
            f"queued=`{format_duration(stuck.get('queued_seconds'))}` "
            f"prelaunch=`{format_duration(stuck.get('prelaunch_delay_seconds'))}` "
            f"total=`{format_duration(stuck.get('stuck_time_seconds'))}`"
        ),
        "  Time work spent waiting on dependencies, release throttles, parallel caps, or other prelaunch delay.",
        "",
        "## Details",
        "",
        f"- Project started: `{metrics.get('project_started_at') or '-'}`",
        f"- First useful result at: `{metrics.get('first_useful_result_at') or '-'}`",
        f"- Validated completion at: `{metrics.get('validated_completion_at') or '-'}`",
        f"- Validation status: `{metrics.get('validation_status') or 'not-run'}`",
        f"- Total runs: `{metrics.get('total_runs', 0)}`",
        f"- Running: `{metrics.get('running_runs', 0)}`",
        f"- Completed: `{metrics.get('completed_runs', 0)}`",
        f"- Failed: `{metrics.get('failed_runs', 0)}`",
        f"- Cancelled: `{metrics.get('cancelled_runs', 0)}`",
        f"- Planner rounds: `{metrics.get('planner_rounds', 0)}`",
        f"- Auto-fix rounds: `{metrics.get('auto_fix_rounds', 0)}`",
        f"- Open questions: `{metrics.get('open_question_count', 0)}`",
        f"- Answered questions: `{metrics.get('answered_question_count', 0)}`",
        f"- Total child-seconds: `{format_duration(parallel.get('total_child_seconds'))}`",
        f"- Launched runs: `{parallel.get('launched_runs', 0)}`",
        "",
    ]
    return "\n".join(lines)


def render_team_metrics_cli(metrics: dict[str, Any]) -> str:
    parallel = metrics.get("parallel_value") or {}
    stuck = metrics.get("stuck_time") or {}
    lines = [
        f"project={metrics['project']}",
        f"updated_at={metrics['updated_at']}",
        f"workspace={metrics['workspace']}",
        f"metrics_file={metrics['metrics_file']}",
        "",
        "scorecard:",
        f"- project_age={format_duration(metrics.get('project_age_seconds'))} :: age of this tracked project",
        f"- time_to_first_useful_result={format_duration(metrics.get('time_to_first_useful_result_seconds'))} :: delay to the first finished child output or human question",
        f"- time_to_validated_completion={format_duration(metrics.get('time_to_validated_completion_seconds'))} :: delay to passed validation or machine-complete delivery",
        f"- human_touch_count={metrics.get('human_touch_count', 0)} :: answered questions plus open questions still waiting on a human",
        (
            f"- parallel_value=max_concurrent:{parallel.get('max_concurrent_runs', 0)} "
            f"overlap:{format_duration(parallel.get('parallel_overlap_seconds'))} "
            f"child_time:{format_duration(parallel.get('total_child_seconds'))} "
            ":: real overlap achieved by child sessions"
        ),
        (
            f"- stuck_time=blocked:{format_duration(stuck.get('blocked_seconds'))} "
            f"queued:{format_duration(stuck.get('queued_seconds'))} "
            f"prelaunch:{format_duration(stuck.get('prelaunch_delay_seconds'))} "
            f"total:{format_duration(stuck.get('stuck_time_seconds'))} "
            ":: time work spent waiting instead of progressing"
        ),
        "",
        "details:",
        f"- project_started_at={metrics.get('project_started_at') or '-'}",
        f"- first_useful_result_at={metrics.get('first_useful_result_at') or '-'}",
        f"- validated_completion_at={metrics.get('validated_completion_at') or '-'}",
        f"- validation_status={metrics.get('validation_status') or 'not-run'}",
        f"- total_runs={metrics.get('total_runs', 0)} running={metrics.get('running_runs', 0)} completed={metrics.get('completed_runs', 0)} failed={metrics.get('failed_runs', 0)} cancelled={metrics.get('cancelled_runs', 0)}",
        f"- planner_rounds={metrics.get('planner_rounds', 0)} auto_fix_rounds={metrics.get('auto_fix_rounds', 0)}",
        f"- open_questions={metrics.get('open_question_count', 0)} answered_questions={metrics.get('answered_question_count', 0)}",
    ]
    return "\n".join(lines)


def render_project_overview(
    project_name: str,
    project_dir: Path,
    runs: list[dict[str, Any]],
    *,
    compacted: bool = False,
) -> str:
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
    intro = (
        "This project is settled. team-leader compacted the transient dashboard, question scratchpads, and per-run report files. Use `history.md` for the compact run history and `manager-summary.md` for the latest aggregate view."
        if compacted
        else "Start here with `dashboard.md` for live progress. While children are active, the manager keeps these markdown files refreshed in the background. Use `manager-summary.md` for the latest manager synthesis and `questions.md` for anything the human needs to answer."
    )
    file_lines = [
        "- `brief.md`: project goal, repo paths, spec paths, notes, and constraints",
        "- `launch-plan.md`: latest planner-produced child launch plan",
        "- `validation.md`: latest validation results and delivery status",
        "- `metrics.md`: scorecard for timing, human-touch, parallelism, and waiting overhead",
        "- `tasks.md`: task-oriented ledger with summaries and ownership",
        "- `manager-summary.md`: concise manager snapshot",
        "- `answers.md`: human-maintained answers keyed by question id",
    ]
    if compacted:
        file_lines.extend(
            [
                "- `history.md`: compact single-file run history used after the project settles",
                "- `integration/`: manager-owned combined checkout for validation and final inspection when writer runs were used",
            ]
        )
    else:
        file_lines.extend(
            [
                "- `dashboard.md`: live run table, active notes, questions, and conflict alerts",
                "- `questions.md`: human-facing questions and blockers",
                "- `answers-template.md`: copy-ready answer lines for open questions",
                "- `conflicts.md`: ownership overlap and conflict-risk notes",
                "- `reports/`: one markdown report per child run",
            ]
        )
    return "\n".join(
        [
            f"# {project_name}",
            "",
            intro,
            "",
            "## Metadata",
            "",
            f"- Updated: `{utc_now()}`",
            f"- Project folder: `{project_dir}`",
            f"- Brief file: `{project_brief_md_path(project_dir)}`",
            f"- Launch plan: `{project_launch_plan_md_path(project_dir)}`",
            f"- Validation: `{project_validation_md_path(project_dir)}`",
            f"- Metrics: `{project_metrics_md_path(project_dir)}`",
            f"- Detail page: `{project_history_path(project_dir) if compacted else project_default_detail_path(project_dir)}`",
            f"- Goal: {goal or '_No goal recorded yet._'}",
            f"- Autonomy mode: `{autonomy_mode or 'manual'}`",
            f"- Clarification mode: `{clarification_mode or 'auto'}`",
            f"- Max parallel sessions: `{max_parallel_sessions()}`",
            f"- Release throttle per cycle: `{max_releases_per_cycle()}`",
            f"- Release throttle window: `{max_release_window_seconds()}` seconds",
            (
                f"- Max auto-fix rounds: `{max_auto_fix_rounds}`"
                if max_auto_fix_rounds is not None
                else f"- Max auto-fix rounds: `{DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS}`"
            ),
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
            *file_lines,
            "",
            *project_state_policy_markdown(project_name, project_dir, compacted=compacted),
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
        f"dashboard={project_default_detail_path(project_dir)}",
        f"metrics={project_metrics_md_path(project_dir)}",
        f"brief={project_brief_md_path(project_dir)}",
        f"launch_plan={project_launch_plan_md_path(project_dir)}",
        f"validation={project_validation_md_path(project_dir)}",
        f"goal={normalize_optional_text(brief.get('goal')) if brief else '-'}",
        f"autonomy_mode={normalize_optional_text(brief.get('autonomy_mode')) if brief else 'manual'}",
        f"clarification_mode={normalize_optional_text(brief.get('clarification_mode')) if brief else 'auto'}",
        f"parallel_limit={max_parallel_sessions()}",
        f"release_throttle={max_releases_per_cycle()}",
        f"release_window_seconds={max_release_window_seconds()}",
        (
            f"max_auto_fix_rounds={brief.get('max_auto_fix_rounds')}"
            if brief
            else f"max_auto_fix_rounds={DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS}"
        ),
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


def render_team_status_summary(root: Path, project_name: str, runs: list[dict[str, Any]]) -> str:
    slug = str(runs[0].get("project_slug") or project_slug(project_name)) if runs else project_slug(project_name)
    project_dir = project_workspace_dir(root, project_name, slug)
    watcher_state, watcher_heartbeat = monitor_state(root)
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    conflicts = detect_conflict_risks(runs)
    integration_issues = integration_alerts(runs)
    warning_runs = output_warning_runs(runs)
    brief = load_project_brief(project_dir, project_name)
    validation = load_project_validation(project_dir)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
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
    lines = [
        f"project={project_name}",
        f"stage={stage['current_stage']}",
        f"stage_reason={stage['stage_reason']}",
        f"progress={stage['progress']}",
        f"next_action={stage['next_action']}",
        f"current_focus={stage['focus']}",
        f"workspace={project_dir}",
        f"dashboard={project_default_detail_path(project_dir)}",
        f"metrics={project_metrics_md_path(project_dir)}",
    ]
    watcher_line = f"watcher={watcher_state}"
    if watcher_heartbeat:
        watcher_line += f" heartbeat={watcher_heartbeat}"
    lines.append(watcher_line)
    lines.extend(["", "active_runs:"])
    if not active:
        lines.append("- none")
    else:
        for run in active:
            lines.append(f"- {run.get('task_id') or run['run_id']}: {run.get('summary') or '-'}")
            live_note = short_summary(latest_live_note(run), max_chars=100)
            if live_note != "-":
                lines.append(f"  note: {live_note}")
    lines.extend(["", "blocked_runs:"])
    if not blocked:
        lines.append("- none")
    else:
        for run in blocked:
            waiting_on = format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on"))
            lines.append(f"- {run.get('task_id') or run['run_id']}: {waiting_on}")
    lines.extend(["", "queued_runs:"])
    if not queued:
        lines.append("- none")
    else:
        for run in queued:
            waiting_on = format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on"))
            lines.append(f"- {run.get('task_id') or run['run_id']}: {waiting_on}")
    lines.extend(["", "open_questions:"])
    if not open_questions:
        lines.append("- none")
    else:
        for question in open_questions[:5]:
            lines.append(f"- {question['id']} [{question['task_id']}] {short_summary(question['text'], max_chars=90)}")
    lines.extend(["", "conflicts:"])
    if not conflicts:
        lines.append("- none")
    else:
        for conflict in conflicts:
            lines.append(f"- {conflict['left_task']} vs {conflict['right_task']} on {conflict['paths']}")
    lines.extend(["", "integration:"])
    if not integration_issues:
        lines.append("- none")
    else:
        for item in integration_issues[:5]:
            lines.append(f"- {item['task_id']} [{item['state']}] {short_summary(item['note'], max_chars=90)}")
    lines.extend(["", "output_warnings:"])
    if not warning_runs:
        lines.append("- none")
    else:
        for run in warning_runs[:5]:
            warnings = format_inline_list(normalize_str_list(run.get("output_warnings"), "output_warnings"))
            lines.append(f"- {run.get('task_id') or run['run_id']}: {warnings}")
    return "\n".join(lines)


def build_team_status_snapshot(root: Path, project_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    slug = str(runs[0].get("project_slug") or project_slug(project_name)) if runs else project_slug(project_name)
    project_dir = project_workspace_dir(root, project_name, slug)
    watcher_state, _watcher_heartbeat = monitor_state(root)
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    conflicts = detect_conflict_risks(runs)
    integration_issues = integration_alerts(runs)
    warning_runs = output_warning_runs(runs)
    brief = load_project_brief(project_dir, project_name)
    validation = load_project_validation(project_dir)
    stage = project_stage_snapshot(runs, question_records, answers, conflicts, brief, validation)
    active = []
    blocked = []
    queued = []
    statuses: dict[str, dict[str, str]] = {}
    for run in sorted(runs, key=run_sort_key):
        label = str(run.get("task_id") or run["run_id"])
        note = short_summary(latest_live_note(run), max_chars=100)
        statuses[label] = {
            "status": str(run.get("status") or "-"),
            "dispatch_state": str(run.get("dispatch_state") or "-"),
            "integration_state": normalize_optional_text(run.get("integration_state")) or "-",
            "summary": str(run.get("summary") or "-"),
            "note": note,
        }
        if str(run.get("status") or "") == "running":
            active.append({"label": label, "summary": str(run.get("summary") or "-"), "note": note})
        if str(run.get("dispatch_state") or "") == "blocked":
            blocked.append(
                {
                    "label": label,
                    "waiting_on": format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on")),
                }
            )
        if str(run.get("dispatch_state") or "") == "queued":
            queued.append(
                {
                    "label": label,
                    "waiting_on": format_inline_list(normalize_str_list(run.get("blocked_on"), "blocked_on")),
                }
            )
    return {
        "project": project_name,
        "stage": str(stage["current_stage"]),
        "stage_reason": str(stage["stage_reason"]),
        "progress": str(stage["progress"]),
        "next_action": str(stage["next_action"]),
        "current_focus": str(stage["focus"]),
        "watcher_state": watcher_state,
        "active": active,
        "blocked": blocked,
        "queued": queued,
        "statuses": statuses,
        "open_questions": [
            {
                "id": str(question["id"]),
                "task_id": str(question["task_id"]),
                "text": str(question["text"]),
            }
            for question in open_questions
        ],
        "conflicts": [
            {
                "left_task": str(conflict["left_task"]),
                "right_task": str(conflict["right_task"]),
                "paths": str(conflict["paths"]),
            }
            for conflict in conflicts
        ],
        "integration": [
            {
                "task_id": str(item["task_id"]),
                "state": str(item["state"]),
                "note": str(item["note"]),
            }
            for item in integration_issues
        ],
        "warnings": [
            {
                "task_id": str(run.get("task_id") or run["run_id"]),
                "warnings": format_inline_list(normalize_str_list(run.get("output_warnings"), "output_warnings")),
            }
            for run in warning_runs
        ],
    }


def render_team_status_milestones(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    lines: list[str] = [f"project={current['project']}"]
    if previous is None:
        lines.extend(
            [
                f"milestone=initial stage={current['stage']}",
                f"reason={current['stage_reason']}",
                f"progress={current['progress']}",
                f"next_action={current['next_action']}",
                f"current_focus={current['current_focus']}",
            ]
        )
        if current["active"]:
            lines.append("active:")
            for item in current["active"]:
                lines.append(f"- {item['label']}: {item['summary']}")
                if item["note"] != "-":
                    lines.append(f"  note: {item['note']}")
        return "\n".join(lines)
    if current["stage"] != previous["stage"]:
        lines.append(f"milestone=stage-change {previous['stage']} -> {current['stage']}")
        lines.append(f"reason={current['stage_reason']}")
        lines.append(f"progress={current['progress']}")
        lines.append(f"next_action={current['next_action']}")
        lines.append(f"current_focus={current['current_focus']}")
    previous_statuses = previous.get("statuses", {})
    current_statuses = current.get("statuses", {})
    run_events: list[str] = []
    for label in sorted(set(previous_statuses) | set(current_statuses)):
        old = previous_statuses.get(label)
        new = current_statuses.get(label)
        if old is None and new is not None:
            run_events.append(f"- {label}: discovered as {new['status']} dispatch={new['dispatch_state']} summary={new['summary']}")
            continue
        if new is None and old is not None:
            run_events.append(f"- {label}: no longer tracked")
            continue
        assert old is not None and new is not None
        if old["status"] != new["status"]:
            run_events.append(f"- {label}: status {old['status']} -> {new['status']}")
        if old["dispatch_state"] != new["dispatch_state"]:
            run_events.append(f"- {label}: dispatch {old['dispatch_state']} -> {new['dispatch_state']}")
        if old["integration_state"] != new["integration_state"]:
            run_events.append(f"- {label}: integration {old['integration_state']} -> {new['integration_state']}")
        if new["status"] == "running" and old["note"] != new["note"] and new["note"] != "-":
            run_events.append(f"- {label}: note {new['note']}")
    if run_events:
        lines.append("runs:")
        lines.extend(run_events)
    previous_questions = {item["id"]: item for item in previous.get("open_questions", [])}
    current_questions = {item["id"]: item for item in current.get("open_questions", [])}
    new_questions = [item for qid, item in current_questions.items() if qid not in previous_questions]
    resolved_questions = [item for qid, item in previous_questions.items() if qid not in current_questions]
    if new_questions:
        lines.append("questions_opened:")
        for item in new_questions[:5]:
            lines.append(f"- {item['id']} [{item['task_id']}] {short_summary(item['text'], max_chars=100)}")
    if resolved_questions:
        lines.append("questions_resolved:")
        for item in resolved_questions[:5]:
            lines.append(f"- {item['id']} [{item['task_id']}]")
    previous_conflicts = {f"{item['left_task']}|{item['right_task']}|{item['paths']}" for item in previous.get("conflicts", [])}
    current_conflicts = {f"{item['left_task']}|{item['right_task']}|{item['paths']}" for item in current.get("conflicts", [])}
    new_conflicts = sorted(current_conflicts - previous_conflicts)
    if new_conflicts:
        lines.append("conflicts:")
        for item in new_conflicts[:5]:
            left_task, right_task, paths = item.split("|", 2)
            lines.append(f"- {left_task} vs {right_task} on {paths}")
    previous_integration = {f"{item['task_id']}|{item['state']}|{item['note']}" for item in previous.get("integration", [])}
    current_integration = {f"{item['task_id']}|{item['state']}|{item['note']}" for item in current.get("integration", [])}
    new_integration = sorted(current_integration - previous_integration)
    if new_integration:
        lines.append("integration:")
        for item in new_integration[:5]:
            task_id, state, note = item.split("|", 2)
            lines.append(f"- {task_id} [{state}] {short_summary(note, max_chars=100)}")
    previous_warnings = {item['task_id']: item['warnings'] for item in previous.get("warnings", [])}
    current_warnings = {item['task_id']: item['warnings'] for item in current.get("warnings", [])}
    warning_events = []
    for task_id in sorted(set(previous_warnings) | set(current_warnings)):
        old = previous_warnings.get(task_id)
        new = current_warnings.get(task_id)
        if old != new:
            warning_events.append(f"- {task_id}: {new or 'cleared'}")
    if warning_events:
        lines.append("warnings:")
        lines.extend(warning_events[:5])
    if len(lines) == 1:
        lines.extend(
            [
                f"milestone=progress stage={current['stage']}",
                f"progress={current['progress']}",
                f"current_focus={current['current_focus']}",
            ]
        )
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


def cleanup_allowed_statuses(include_failed: bool) -> set[str]:
    statuses = set(DEFAULT_MANUAL_COMPACT_RUN_STATUSES)
    if include_failed:
        statuses.update(MANUAL_COMPACT_EXTRA_STATUSES)
    return statuses


def compact_run_artifacts(
    run: dict[str, Any],
    *,
    reason: str,
    include_failed: bool = False,
) -> tuple[bool, int]:
    status = str(run.get("status") or "")
    if reason == "settled-project":
        allowed_statuses = AUTO_COMPACT_RUN_STATUSES
    else:
        allowed_statuses = cleanup_allowed_statuses(include_failed)
    if status not in allowed_statuses:
        return False, 0
    run_dir = Path(run["run_dir"])
    cached_preview = preview_text(last_message_display_for_run(run), max_lines=8, max_chars=900)
    run["compacted_last_message_preview"] = cached_preview
    question_records_for_run(run)
    maybe_release_run_worktree(run, allow_terminal_cleanup=True)
    removed_names: list[str] = []
    for name in RUN_COMPACT_FILE_NAMES:
        path = run_dir / name
        if path.exists():
            delete_if_exists(path)
            removed_names.append(name)
    guard_dir = run_dir / "child-bin"
    if guard_dir.exists():
        delete_tree_if_exists(guard_dir)
        removed_names.append("child-bin/")
    if run_dir.exists() and not any(run_dir.iterdir()):
        try:
            run_dir.rmdir()
        except OSError:
            pass
        else:
            removed_names.append("run_dir/")
    was_compacted = bool(normalize_optional_text(run.get("compacted_at")))
    if removed_names or not was_compacted:
        run["compacted_at"] = utc_now()
        run["compaction_reason"] = reason
        run["compaction_removed"] = unique_preserve_order(
            normalize_str_list(run.get("compaction_removed"), "compaction_removed") + removed_names
        )
    refresh_run_artifacts(run)
    return not was_compacted and bool(normalize_optional_text(run.get("compacted_at"))), len(removed_names)


def project_cleanup_state(
    project_dir: Path,
    runs: list[dict[str, Any]],
    *,
    include_failed: bool,
) -> dict[str, Any]:
    question_records = collect_question_records(runs)
    answers = load_answers(project_dir)
    open_questions = unanswered_questions(question_records, answers)
    integration_issues = integration_alerts(runs)
    reason: str | None = None
    if project_has_unsettled_runs(runs):
        reason = "project still has active, queued, or blocked runs"
    elif integration_issues:
        reason = "project still has integration issues"
    elif open_questions:
        reason = "project still has unanswered human questions"
    return {
        "eligible": reason is None,
        "reason": reason,
        "question_records": question_records,
        "answers": answers,
        "integration_issues": integration_issues,
    }


def render_project_history(runs: list[dict[str, Any]]) -> str:
    question_records = collect_question_records(runs)
    by_run: dict[str, list[dict[str, str]]] = {}
    for record in question_records:
        by_run.setdefault(record["run_id"], []).append(record)
    lines = [
        "# Run History",
        "",
        "Settled projects are compacted into this single history file. Per-run reports, live dashboards, and transient question scratchpads are removed once the project is quiet and resolved.",
        "",
    ]
    if not runs:
        lines.extend(["_No runs recorded yet._", ""])
        return "\n".join(lines)
    for run in sorted(runs, key=run_sort_key):
        run_questions = by_run.get(str(run["run_id"]), [])
        lines.extend(
            [
                f"## {run['run_id']}",
                "",
                f"- Task: `{run.get('task_id') or run['run_id']}`",
                f"- Summary: {run.get('summary') or '-'}",
                f"- Role: `{run.get('role') or '-'}`",
                f"- Status: `{run.get('status') or '-'}`",
                f"- Session: `{run.get('session_id') or '-'}`",
                f"- Integration: `{run.get('integration_state') or '-'}`",
                f"- Worktree released: `{run.get('workspace_released_at') or '-'}`",
                f"- Artifacts compacted: `{run.get('compacted_at') or '-'}`",
                "",
                "### Output Preview",
                "",
                preview_text(last_message_display_for_run(run), max_lines=8, max_chars=900),
                "",
                "### Questions Raised",
                "",
            ]
        )
        if run_questions:
            lines.extend(f"- `{record['id']}`: {record['text']}" for record in run_questions)
        else:
            lines.append("_No human questions detected from this child._")
        lines.append("")
    return "\n".join(lines)


def apply_project_workspace_compaction(project_dir: Path, runs: list[dict[str, Any]]) -> None:
    write_text(project_history_path(project_dir), render_project_history(runs))
    delete_tree_if_exists(project_dir / "reports")
    for name in PROJECT_COMPACT_DELETE_FILES:
        delete_if_exists(project_dir / name)
    worktrees_dir = project_dir / "worktrees"
    if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
        worktrees_dir.rmdir()


def cleanup_root_artifacts(
    root: Path,
    index: dict[str, Any],
    *,
    project_filter: str | None = None,
    include_failed: bool = False,
    include_standalone: bool = False,
) -> dict[str, Any]:
    maybe_release_completed_worktrees(index)
    summary = {
        "projects_compacted": 0,
        "runs_compacted": 0,
        "files_removed": 0,
        "blocked_projects": [],
        "standalone_runs_compacted": 0,
    }
    allowed_statuses = cleanup_allowed_statuses(include_failed)
    for slug, project_name in sorted(known_projects(root, index).items()):
        if project_filter and project_filter not in {project_name, slug}:
            continue
        runs = sorted(project_runs(index, project_name), key=run_sort_key)
        if not runs:
            continue
        project_dir = project_workspace_dir(root, project_name, slug)
        state = project_cleanup_state(project_dir, runs, include_failed=include_failed)
        if not state["eligible"]:
            summary["blocked_projects"].append(
                {"project": project_name, "slug": slug, "reason": state["reason"]}
            )
            continue
        summary["projects_compacted"] += 1
        for run in runs:
            if str(run.get("status") or "") not in allowed_statuses:
                continue
            run_compacted, removed_count = compact_run_artifacts(
                run,
                reason="manual-cleanup" if include_failed else "settled-project",
                include_failed=include_failed,
            )
            if run_compacted:
                summary["runs_compacted"] += 1
            summary["files_removed"] += removed_count
    if include_standalone and not project_filter:
        for run in sorted(index["runs"], key=run_sort_key):
            if normalize_optional_text(run.get("project")):
                continue
            if str(run.get("status") or "") not in allowed_statuses:
                continue
            run_compacted, removed_count = compact_run_artifacts(
                run,
                reason="manual-cleanup",
                include_failed=include_failed,
            )
            if run_compacted:
                summary["runs_compacted"] += 1
                summary["standalone_runs_compacted"] += 1
            summary["files_removed"] += removed_count
    return summary


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
    answers_path = project_dir / "answers.md"
    if not answers_path.exists():
        write_text(answers_path, render_answers_stub())
    cleanup_state = project_cleanup_state(project_dir, runs, include_failed=False)
    if cleanup_state["eligible"]:
        question_records = cleanup_state["question_records"]
    else:
        question_records = write_project_reports(project_dir, runs)
    answers = cleanup_state["answers"]
    conflicts = detect_conflict_risks(runs)
    integration_issues = cleanup_state["integration_issues"]
    metrics = build_project_metrics(root, project_name, runs)
    delete_if_exists(project_dir / "project.md")
    write_text(
        project_dir / "README.md",
        render_project_overview(project_name, project_dir, runs, compacted=cleanup_state["eligible"]),
    )
    write_text(project_dir / "tasks.md", render_task_ledger(runs))
    write_text(
        project_dir / "manager-summary.md",
        render_manager_summary(project_name, project_dir, runs, conflicts, question_records, answers),
    )
    write_text(project_metrics_md_path(project_dir), render_project_metrics(metrics))
    if cleanup_state["eligible"]:
        apply_project_workspace_compaction(project_dir, runs)
    else:
        delete_if_exists(project_history_path(project_dir))
        write_text(
            project_dir / "dashboard.md",
            render_dashboard(project_name, project_dir, runs, conflicts, question_records, answers),
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
    cleanup_root_artifacts(root, data)
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


def allowed_child_providers_for_context(
    brief: dict[str, Any] | None,
    planner_run: dict[str, Any] | None = None,
) -> list[str]:
    providers = []
    if planner_run is not None:
        providers = normalize_provider_list(
            planner_run.get("planner_allowed_providers"),
            "planner_allowed_providers",
        )
    if not providers and brief:
        providers = normalize_provider_list(brief.get("allowed_providers"), "allowed_providers")
    return providers or sorted(PROVIDERS)


def default_child_provider_for_context(
    brief: dict[str, Any] | None,
    planner_run: dict[str, Any] | None = None,
) -> str:
    provider_name = None
    if planner_run is not None:
        provider_name = validate_provider_name(
            normalize_optional_text(planner_run.get("planner_default_child_provider")),
            field_name="planner_default_child_provider",
        )
    if provider_name is None and brief:
        provider_name = validate_provider_name(
            normalize_optional_text(brief.get("child_provider")),
            field_name="child_provider",
        )
    if provider_name is None and planner_run is not None:
        provider_name = validate_provider_name(
            normalize_optional_text(planner_run.get("provider")),
            field_name="provider",
        )
    return provider_name or DEFAULT_PROVIDER


def default_child_provider_bin_for_context(
    brief: dict[str, Any] | None,
    planner_run: dict[str, Any] | None = None,
) -> str | None:
    if planner_run is not None:
        value = normalize_optional_text(planner_run.get("planner_default_child_provider_bin"))
        if value:
            return value
    if brief:
        value = normalize_optional_text(brief.get("child_provider_bin"))
        if value:
            return value
    return None


def provider_policy_lines(
    *,
    allowed_providers: list[str],
    default_child_provider: str,
    default_child_provider_bin: str | None,
    planner_provider: str,
) -> list[str]:
    lines = [
        "Child CLI policy:",
        f"- planner_provider={planner_provider}",
        f"- default_child_provider={default_child_provider}",
        f"- default_child_provider_bin={default_child_provider_bin or '-'}",
        f"- allowed_child_providers={format_inline_list(allowed_providers)}",
        "",
        "Available providers:",
    ]
    for name in allowed_providers:
        adapter = get_provider(name)
        sandboxes = ",".join(adapter.capabilities.sandbox_modes) or "-"
        lines.append(
            f"- {name}: sandboxes={sandboxes} session_label={adapter.session_label} notes={adapter.notes}"
        )
    return lines


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
    default_child_provider = default_child_provider_for_context(brief, planner_run)
    allowed_child_providers = allowed_child_providers_for_context(brief, planner_run)
    provider_name = (
        validate_provider_name(normalize_optional_text(item.get("provider")), field_name="plan provider")
        or default_child_provider
    )
    if provider_name not in allowed_child_providers:
        allowed = ", ".join(allowed_child_providers)
        raise RuntimeError(
            f"launch plan selected disallowed provider {provider_name!r}; allowed providers: {allowed}"
        )
    provider_bin = normalize_optional_text(item.get("provider_bin"))
    if provider_bin is None and provider_name == default_child_provider:
        provider_bin = default_child_provider_bin_for_context(brief, planner_run)
    if provider_bin is None and provider_name == str(planner_run.get("provider") or DEFAULT_PROVIDER):
        provider_bin = normalize_optional_text(planner_run.get("provider_bin"))
    return DispatchOptions(
        provider=provider_name,
        provider_bin=provider_bin,
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
        max_run_seconds=normalize_optional_positive_int(item.get("max_run_seconds"), "max_run_seconds"),
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
    normalized_runs = [dict(item) for item in plan["runs"]]
    plan_limit = max_plan_runs_per_wave()
    if len(normalized_runs) > plan_limit:
        run["plan_applied_at"] = None
        run["plan_apply_error"] = (
            f"launch plan lists {len(normalized_runs)} child runs; the per-wave limit is {plan_limit}"
        )
        run["planned_run_ids"] = []
        save_project_launch_plan(
            project_dir,
            {
                "source_run_id": run["run_id"],
                "plan_summary": plan["plan_summary"],
                "runs": normalized_runs,
                "applied_at": None,
                "updated_at": utc_now(),
            },
        )
        return []
    try:
        for item in normalized_runs:
            task_id = normalize_optional_text(item.get("task_id"))
            if task_id and task_id in existing_task_ids:
                continue
            options = dispatch_options_from_plan_item(item, project_name=project_name, brief=brief, planner_run=run)
            child = materialize_run(root, index, options, announce=False)
            planned_ids.append(str(child["run_id"]))
            existing_task_ids.add(normalize_optional_text(child.get("task_id")))
    except RuntimeError as exc:
        run["plan_applied_at"] = None
        run["plan_apply_error"] = short_summary(str(exc), max_chars=180)
        run["planned_run_ids"] = planned_ids
        save_project_launch_plan(
            project_dir,
            {
                "source_run_id": run["run_id"],
                "plan_summary": plan["plan_summary"],
                "runs": normalized_runs,
                "applied_at": None,
                "updated_at": utc_now(),
            },
        )
        return planned_ids
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
    if project_time_budget_reached(brief, runs):
        return False, "max-work-seconds-reached"
    open_questions = unanswered_questions(collect_question_records(runs), load_answers(project_dir))
    if open_questions:
        return False, "waiting-for-human"
    conflicts = detect_conflict_risks(runs)
    if conflicts:
        return False, "resolve-conflicts"
    latest_planner = latest_project_planner_run(runs)
    if latest_planner and str(latest_planner.get("status") or "") in {"running", "prepared", "blocked"}:
        return False, "planner-already-running"
    if project_has_unsettled_nonplanner_runs(runs):
        return False, "worker-runs-pending"
    if latest_planner and normalize_optional_text(latest_planner.get("plan_apply_error")):
        if not answers_updated_after(project_dir, normalize_optional_text(latest_planner.get("finished_at"))):
            return False, "planner-apply-error"
    max_rounds = int(brief.get("max_planner_rounds") or DEFAULT_PROJECT_MAX_PLANNER_ROUNDS)
    if planner_round_count(runs) >= max_rounds:
        return False, "max-rounds-reached"
    max_auto_fix_rounds = max(
        0,
        int(brief.get("max_auto_fix_rounds") or DEFAULT_PROJECT_MAX_AUTO_FIX_ROUNDS),
    )
    validation = maybe_refresh_project_validation(project_dir, brief, runs) if runs else load_project_validation(project_dir)
    if project_is_machine_complete(brief, validation) is True:
        return False, "complete"
    if latest_planner and latest_planner.get("plan_applied_at") and not normalize_str_list(latest_planner.get("planned_run_ids"), "planned_run_ids"):
        if not answers_updated_after(project_dir, normalize_optional_text(latest_planner.get("finished_at"))):
            if not validation or validation.get("status") not in {"failed", "waiting-for-sentinel"}:
                return False, "planner-produced-no-work"
    if not runs:
        return True, "first-plan"
    failed_runs_present = any(str(run.get("status") or "") == "failed" for run in runs)
    integration_recovery_needed = any(
        normalize_optional_text(run.get("integration_state"))
        in {"conflict", "scope-violation", "apply-failed", "commit-failed"}
        for run in runs
    )
    validation_recovery_reason = (
        str(validation.get("status"))
        if validation and validation.get("status") in {"failed", "waiting-for-sentinel"}
        else None
    )
    if auto_fix_round_count(runs) >= max_auto_fix_rounds:
        if failed_runs_present or integration_recovery_needed or validation_recovery_reason:
            return False, "max-auto-fix-rounds-reached"
    if failed_runs_present:
        return True, "failed-runs"
    if integration_recovery_needed:
        return True, "integration-conflict"
    if validation_recovery_reason:
        return True, validation_recovery_reason
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
    planner_provider = validate_provider_name(
        normalize_optional_text(brief.get("planner_provider")),
        field_name="planner_provider",
    ) or DEFAULT_PROVIDER
    planner_options = DispatchOptions(
        provider=planner_provider,
        provider_bin=normalize_optional_text(brief.get("planner_provider_bin")),
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
        max_run_seconds=project_remaining_work_seconds(brief, runs),
        dry_run=False,
        owned_paths=[],
        depends_on=[],
    )
    return materialize_run(
        root,
        index,
        planner_options,
        announce=False,
        extra_fields={
            "planner_source": PLANNER_SOURCE,
            "planner_reason": planner_reason,
            "planner_default_child_provider": default_child_provider_for_context(brief),
            "planner_default_child_provider_bin": default_child_provider_bin_for_context(brief),
            "planner_allowed_providers": allowed_child_providers_for_context(brief),
        },
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
    if normalize_optional_text(run.get("timed_out_at")):
        warnings.append("run_timed_out")
    runtime_health = normalize_optional_text(run.get("runtime_health"))
    if runtime_health == "heartbeat-missing":
        warnings.append("heartbeat_missing")
    elif runtime_health == "heartbeat-stale":
        warnings.append("heartbeat_stale")
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


def run_heartbeat_path(run: dict[str, Any]) -> Path:
    raw = normalize_optional_text(run.get("heartbeat_path"))
    if raw:
        return Path(raw)
    return Path(run["run_dir"]) / "heartbeat.txt"


def run_elapsed_seconds(run: dict[str, Any], *, now_epoch: int | None = None) -> int | None:
    launched_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))
    if launched_epoch is None:
        return None
    if normalize_optional_text(run.get("finished_at")):
        end_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("finished_at")))
    else:
        end_epoch = now_epoch or epoch_now()
    if end_epoch is None:
        return None
    return max(0, end_epoch - launched_epoch)


def run_timeout_seconds(run: dict[str, Any]) -> int | None:
    return normalize_optional_positive_int(run.get("max_run_seconds"), "max_run_seconds")


def run_timeout_note(run: dict[str, Any], *, now_epoch: int | None = None) -> str | None:
    timeout_seconds = run_timeout_seconds(run)
    if timeout_seconds is None:
        return None
    elapsed_seconds = run_elapsed_seconds(run, now_epoch=now_epoch)
    if elapsed_seconds is None or elapsed_seconds <= timeout_seconds:
        return None
    return f"run exceeded max_run_seconds ({elapsed_seconds}s > {timeout_seconds}s)"


def run_runtime_health(run: dict[str, Any], *, now_epoch: int | None = None) -> tuple[str | None, str | None, int | None]:
    timed_out_at = normalize_optional_text(run.get("timed_out_at"))
    timeout_reason = normalize_optional_text(run.get("timeout_reason"))
    if timed_out_at:
        return "timed-out", timeout_reason or "run exceeded its time budget", None
    if str(run.get("status") or "") != "running":
        return None, None, None
    current_epoch = now_epoch or epoch_now()
    heartbeat_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("heartbeat_at")))
    launched_epoch = parse_timestamp_epoch(normalize_optional_text(run.get("launched_at")))
    stale_after = run_heartbeat_stale_seconds()
    baseline_epoch = heartbeat_epoch or launched_epoch
    lag_seconds = None if baseline_epoch is None else max(0, current_epoch - baseline_epoch)
    if heartbeat_epoch is not None:
        if lag_seconds is not None and lag_seconds > stale_after:
            return "heartbeat-stale", f"last heartbeat {lag_seconds}s ago", lag_seconds
        return "healthy", "heartbeat active", lag_seconds
    if launched_epoch is not None and lag_seconds is not None and lag_seconds > stale_after:
        return "heartbeat-missing", f"no heartbeat observed for {lag_seconds}s after launch", lag_seconds
    return "healthy", "within heartbeat grace window", lag_seconds


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


def index_has_unsettled_runs(index: dict[str, Any]) -> bool:
    return project_has_unsettled_runs(index["runs"])


def ensure_monitor(root: Path, index: dict[str, Any]) -> None:
    if os.environ.get(MONITOR_RUN_ENV) == "1":
        return
    if not index_has_unsettled_runs(index):
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
    try:
        prepare_run_workspace(root, run)
    except RuntimeError as exc:
        run["status"] = "blocked"
        set_run_dispatch_state(run, "blocked", [f"workspace-setup:{short_summary(str(exc), max_chars=140)}"])
        return
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
    launch_time = utc_now()
    set_run_dispatch_state(run, "running", [], changed_at=launch_time)
    run["launched_at"] = launch_time
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


def convert_running_launch_failure(run: dict[str, Any], note: str) -> None:
    pid = run.get("pid")
    if pid and pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    run["pid"] = None
    run["status"] = "blocked"
    run["provider_preflight_status"] = "blocked"
    run["provider_preflight_checked_at"] = utc_now()
    run["provider_preflight_note"] = note
    set_run_dispatch_state(run, "blocked", [note], changed_at=run["provider_preflight_checked_at"])
    write_text(Path(run["run_dir"]) / "state.txt", "blocked\n")


def convert_running_timeout(run: dict[str, Any], note: str) -> None:
    pid = run.get("pid")
    if pid and pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    timestamp = utc_now()
    run["pid"] = None
    run["status"] = "failed"
    run["exit_code"] = 124
    run["finished_at"] = timestamp
    run["timed_out_at"] = timestamp
    run["timeout_reason"] = note
    run["runtime_health"] = "timed-out"
    run["runtime_health_note"] = note
    run["heartbeat_lag_seconds"] = None
    set_run_dispatch_state(run, "failed", [], changed_at=timestamp)
    run_dir = Path(run["run_dir"])
    write_text(run_dir / "state.txt", "failed\n")
    write_text(run_dir / "exit_code.txt", "124\n")
    write_text(run_dir / "finished_at.txt", timestamp + "\n")


def refresh_run(run: dict[str, Any]) -> None:
    run_dir = Path(run["run_dir"])
    state = read_text_if_exists(run_dir / "state.txt")
    exit_code = read_text_if_exists(run_dir / "exit_code.txt")
    finished_at = read_text_if_exists(run_dir / "finished_at.txt")
    heartbeat_at = read_text_if_exists(run_heartbeat_path(run))
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
    run["heartbeat_at"] = heartbeat_at.strip() if heartbeat_at else None
    if str(run.get("status") or "") in TERMINAL_STATUSES:
        run["pid"] = None
    if str(run.get("status") or "") == "running":
        launch_failure = provider_for_run(run).runtime_launch_failure(run)
        if launch_failure:
            convert_running_launch_failure(run, launch_failure)
    if str(run.get("status") or "") == "running":
        timeout_note = run_timeout_note(run)
        if timeout_note:
            convert_running_timeout(run, timeout_note)
    runtime_health, runtime_health_note, heartbeat_lag_seconds = run_runtime_health(run)
    run["runtime_health"] = runtime_health
    run["runtime_health_note"] = runtime_health_note
    run["heartbeat_lag_seconds"] = heartbeat_lag_seconds
    if run_has_provider_artifacts(run) and not get_session_id(run):
        session_id = provider_for_run(run).detect_session_id(run)
        if session_id:
            set_session_id(run, session_id)
    if run_has_provider_artifacts(run):
        provider_for_run(run).write_last_message(run)
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
    heartbeat_path: Path,
    heartbeat_interval_seconds: int,
    env_exports: dict[str, str] | None = None,
    path_prefix: Path | None = None,
) -> str:
    cmd = quote_command(command)
    heartbeat_q = shlex.quote(str(heartbeat_path))
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
            f"printf '%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > {heartbeat_q}",
            f"({cmd} < {shlex.quote(str(prompt_path))}) &",
            "child_pid=$!",
            "(",
            "  while kill -0 \"$child_pid\" 2>/dev/null; do",
            f"    printf '%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > {heartbeat_q}",
            f"    sleep {max(1, heartbeat_interval_seconds)}",
            "  done",
            ") &",
            "heartbeat_pid=$!",
            "wait \"$child_pid\"",
            "status=$?",
            "kill \"$heartbeat_pid\" 2>/dev/null || true",
            "wait \"$heartbeat_pid\" 2>/dev/null || true",
            f"printf '%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > {heartbeat_q}",
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
        provider_bin=normalize_optional_text(run.get("provider_bin")),
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
        max_run_seconds=normalize_optional_positive_int(run.get("max_run_seconds"), "max_run_seconds"),
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
            heartbeat_path=run_heartbeat_path(run),
            heartbeat_interval_seconds=run_heartbeat_interval_seconds(),
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
    runtime_health = normalize_optional_text(run.get("runtime_health"))
    if runtime_health == "timed-out":
        heartbeat_text = "timeout"
    elif str(run.get("status") or "") == "running":
        heartbeat_text = {
            "healthy": "ok",
            "heartbeat-missing": "missing",
            "heartbeat-stale": "stale",
            "timed-out": "timeout",
        }.get(runtime_health or "", runtime_health or "ok")
    else:
        heartbeat_text = "-"
    return (
        f"{run['run_id']:<30} {run['status']:<10} provider={run.get('provider', DEFAULT_PROVIDER):<6} "
        f"pid={run.get('pid') or '-':<8} hb={heartbeat_text:<8} exit={exit_text:<4} task={task_text:<18} "
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


def project_has_unsettled_nonplanner_runs(runs: list[dict[str, Any]]) -> bool:
    return any(not run_is_planner(run) and run_is_unsettled(run) for run in runs)


def project_has_unsettled_runs(runs: list[dict[str, Any]]) -> bool:
    return any(run_is_unsettled(run) for run in runs)


def project_has_state_files(root: Path, project_name: str) -> bool:
    project_dir = project_workspace_dir(root, project_name, project_slug(project_name))
    return bool(
        project_brief_path(project_dir).exists()
        or project_launch_plan_md_path(project_dir).exists()
        or project_validation_path(project_dir).exists()
        or project_validation_md_path(project_dir).exists()
        or project_metrics_md_path(project_dir).exists()
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        data = load_index(root)
        save_index_and_sync(root, data)
    print(root)
    return 0


def parse_provider_bin_overrides(items: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise RuntimeError("provider bin overrides must use provider=path format")
        provider_raw, path_raw = raw.split("=", 1)
        provider_name = validate_provider_name(provider_raw, field_name="provider bin override")
        if provider_name is None:
            raise RuntimeError("provider bin override is missing provider name")
        path_text = path_raw.strip()
        if not path_text:
            raise RuntimeError(f"provider bin override for {provider_name} is missing a path")
        overrides[provider_name] = path_text
    return overrides


def provider_check_record(
    provider_name: str,
    *,
    cwd: Path,
    provider_bin: str | None = None,
) -> dict[str, Any]:
    adapter = get_provider(provider_name)
    raw_bin = provider_bin or adapter.resolved_bin()
    resolved_bin: str | None = None
    try:
        resolved_bin = resolve_executable(raw_bin, cwd=cwd)
    except RuntimeError:
        resolved_bin = None
    ok, note = adapter.launch_preflight(
        {
            "provider": provider_name,
            "provider_bin": raw_bin,
            "cwd": str(cwd),
            "source_cwd": str(cwd),
        }
    )
    return {
        "provider": provider_name,
        "provider_bin": raw_bin,
        "resolved_bin": resolved_bin,
        "status": "ok" if ok else "blocked",
        "note": note,
        "notes": adapter.notes,
        "session_label": adapter.session_label,
        "supported_sandbox_modes": list(adapter.capabilities.sandbox_modes),
    }


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
        aliases = ",".join(record.get("aliases") or []) or "-"
        print(
            f"{record['name']}{suffix}: session_label={record['session_label']} "
            f"bin_env={record['bin_env_var']} default_bin={record['default_bin']} "
            f"sandboxes={sandboxes} exec_resume={str(record['supports_exec_resume']).lower()} "
            f"aliases={aliases} notes={record['notes']}"
        )
    return 0


def cmd_provider_check(args: argparse.Namespace) -> int:
    cwd = resolve_path(args.cd) if args.cd else Path.cwd()
    overrides = parse_provider_bin_overrides(list(args.bin))
    provider_names = (
        [validate_provider_name(name, field_name="provider") for name in args.provider]
        if args.provider
        else sorted(PROVIDERS)
    )
    payload = [
        provider_check_record(name, cwd=cwd, provider_bin=overrides.get(name))
        for name in provider_names
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if all(item["status"] == "ok" for item in payload) else 1
    for item in payload:
        sandboxes = ",".join(item["supported_sandbox_modes"]) or "-"
        print(
            f"{item['provider']}: status={item['status']} "
            f"bin={item['provider_bin']} resolved={item['resolved_bin'] or '-'} "
            f"session_label={item['session_label']} sandboxes={sandboxes} "
            f"note={item['note'] or 'ok'}"
        )
    return 0 if all(item["status"] == "ok" for item in payload) else 1


def default_smoke_prompt(expected_text: str) -> str:
    return (
        "Reply with exactly the following text and nothing else:\n\n"
        f"{expected_text}\n"
    )


def smoke_test_options(
    *,
    provider_name: str,
    provider_bin: str | None,
    cwd: Path,
    prompt_text: str,
    sandbox: str,
) -> DispatchOptions:
    return DispatchOptions(
        provider=provider_name,
        provider_bin=provider_bin,
        name=f"smoke-{provider_name}",
        project=None,
        task_id=None,
        role="reviewer",
        summary=derive_summary(prompt_text),
        prompt_text=prompt_text,
        cd=cwd,
        sandbox=sandbox,
        model=None,
        profile=None,
        add_dirs=[],
        configs=[],
        enables=[],
        disables=[],
        images=[],
        search=False,
        skip_git_repo_check=False,
        ephemeral=False,
        full_auto=False,
        dangerous=False,
        max_run_seconds=None,
        dry_run=False,
        owned_paths=[],
        depends_on=[],
    )


def wait_for_run_settle(
    *,
    root: Path,
    run_id: str,
    timeout_seconds: int,
    poll_interval_seconds: float = 1.0,
) -> tuple[dict[str, Any], bool]:
    deadline = time.time() + max(1, timeout_seconds)
    last_run: dict[str, Any] | None = None
    while True:
        with root_lock(root):
            index = load_index(root)
            refresh_index_state(root, index)
            run = resolve_run(index, run_id)
            last_run = dict(run)
        status = str(last_run.get("status") or "")
        dispatch_state = str(last_run.get("dispatch_state") or "")
        if status in TERMINAL_STATUSES or dispatch_state == "blocked":
            return last_run, False
        if time.time() >= deadline:
            return last_run, True
        time.sleep(max(0.2, poll_interval_seconds))


def smoke_test_payload(
    *,
    root: Path,
    run: dict[str, Any],
    provider_check: dict[str, Any],
    expected_text: str | None,
    timed_out: bool,
) -> dict[str, Any]:
    last_message = normalize_optional_text(last_message_for_run(run))
    expected = normalize_optional_text(expected_text)
    matched = expected is None or last_message == expected
    status = str(run.get("status") or "")
    success = (not timed_out) and status == "completed" and matched
    return {
        "provider": str(run.get("provider") or provider_check.get("provider") or ""),
        "success": success,
        "timed_out": timed_out,
        "root": str(root),
        "provider_check": provider_check,
        "run_id": str(run["run_id"]),
        "status": status,
        "dispatch_state": str(run.get("dispatch_state") or ""),
        "exit_code": run.get("exit_code"),
        "session_id": get_session_id(run),
        "expected_text": expected,
        "matched_expected_text": matched,
        "last_message": last_message,
    }


def cmd_provider_smoke_test(args: argparse.Namespace) -> int:
    provider_name = validate_provider_name(args.provider, field_name="provider") or DEFAULT_PROVIDER
    cwd = resolve_path(args.cd) if args.cd else Path.cwd()
    root = resolve_path(args.root) if args.root else Path(tempfile.mkdtemp(prefix=f"team-leader-smoke-{provider_name}-"))
    provider_bin = normalize_optional_text(args.provider_bin)
    provider_check = provider_check_record(provider_name, cwd=cwd, provider_bin=provider_bin)
    if provider_check["status"] != "ok":
        payload = {
            "provider": provider_name,
            "success": False,
            "timed_out": False,
            "root": str(root),
            "provider_check": provider_check,
            "run_id": None,
            "status": "preflight-blocked",
            "dispatch_state": "blocked",
            "exit_code": None,
            "session_id": None,
            "expected_text": normalize_optional_text(args.expect_text),
            "matched_expected_text": False,
            "last_message": None,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(
                f"{provider_name}: smoke_test=false status=preflight-blocked "
                f"root={root} note={provider_check['note'] or 'preflight blocked'}"
            )
        return 1
    prompt_text = args.prompt or default_smoke_prompt(args.expect_text)
    options = smoke_test_options(
        provider_name=provider_name,
        provider_bin=provider_bin,
        cwd=cwd,
        prompt_text=prompt_text,
        sandbox=args.sandbox,
    )
    with root_lock(root):
        index = load_index(root)
        run = materialize_run(root, index, options, announce=False)
    settled_run, timed_out = wait_for_run_settle(
        root=root,
        run_id=str(run["run_id"]),
        timeout_seconds=args.timeout,
        poll_interval_seconds=max(0.2, float(args.poll_interval)),
    )
    payload = smoke_test_payload(
        root=root,
        run=settled_run,
        provider_check=provider_check,
        expected_text=args.expect_text,
        timed_out=timed_out,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(
            f"{provider_name}: smoke_test={str(payload['success']).lower()} "
            f"status={payload['status']} exit={payload['exit_code'] if payload['exit_code'] is not None else '-'} "
            f"session={payload['session_id'] or '-'} root={root}"
        )
        if payload["last_message"]:
            print(payload["last_message"])
    if payload["success"]:
        return 0
    if payload["timed_out"]:
        return 124
    return 1


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
            max_work_seconds=args.max_work_seconds,
            max_planner_rounds=args.max_planner_rounds,
            max_auto_fix_rounds=args.max_auto_fix_rounds,
            planner_provider=normalize_optional_text(args.planner_provider),
            planner_provider_bin=normalize_optional_text(args.planner_provider_bin),
            child_provider=normalize_optional_text(args.child_provider),
            child_provider_bin=normalize_optional_text(args.child_provider_bin),
            allowed_providers=list(args.allow_provider) if args.allow_provider else None,
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
            max_work_seconds=args.max_work_seconds,
            max_planner_rounds=args.max_planner_rounds,
            max_auto_fix_rounds=args.max_auto_fix_rounds,
            planner_provider=normalize_optional_text(args.planner_provider),
            planner_provider_bin=normalize_optional_text(args.planner_provider_bin),
            child_provider=normalize_optional_text(args.child_provider),
            child_provider_bin=normalize_optional_text(args.child_provider_bin),
            allowed_providers=list(args.allow_provider) if args.allow_provider else None,
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
        planner_provider = (
            validate_provider_name(normalize_optional_text(args.provider), field_name="provider")
            or validate_provider_name(normalize_optional_text(brief.get("planner_provider")), field_name="planner_provider")
            or DEFAULT_PROVIDER
        )
        planner_provider_bin = normalize_optional_text(args.provider_bin) or normalize_optional_text(brief.get("planner_provider_bin"))
        default_child_provider = (
            validate_provider_name(normalize_optional_text(brief.get("child_provider")), field_name="child_provider")
            or planner_provider
        )
        default_child_provider_bin = normalize_optional_text(brief.get("child_provider_bin"))
        allowed_child_providers = normalize_provider_list(brief.get("allowed_providers"), "allowed_providers")
        planner_options = DispatchOptions(
            provider=planner_provider,
            provider_bin=planner_provider_bin,
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
            max_run_seconds=project_remaining_work_seconds(brief, runs),
            dry_run=bool(args.dry_run),
            owned_paths=[],
            depends_on=[],
        )
        materialize_run(
            root,
            index,
            planner_options,
            extra_fields={
                "planner_source": PLANNER_SOURCE,
                "planner_reason": "manual-orchestrate",
                "planner_default_child_provider": default_child_provider,
                "planner_default_child_provider_bin": default_child_provider_bin,
                "planner_allowed_providers": allowed_child_providers,
            },
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
    project_dir: Path | None = None
    brief: dict[str, Any] | None = None
    effective_max_run_seconds = options.max_run_seconds
    if options.project:
        project_dir = ensure_project_workspace(root, options.project, project_slug(options.project))
        brief = load_project_brief(project_dir, options.project)
        remaining_work_seconds = project_remaining_work_seconds(brief, project_runs(index, options.project))
        if remaining_work_seconds is not None:
            if remaining_work_seconds <= 0:
                raise RuntimeError(
                    f"project {options.project!r} has exhausted max_work_seconds; increase the budget before launching more work"
                )
            if effective_max_run_seconds is None:
                effective_max_run_seconds = remaining_work_seconds
            else:
                effective_max_run_seconds = min(effective_max_run_seconds, remaining_work_seconds)
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
        if project_dir is None:
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
    heartbeat_path = run_dir / "heartbeat.txt"
    runner_path = run_dir / "runner.sh"

    write_text(prompt_path, child_prompt_guard(options.prompt_text))

    command = adapter.build_exec_command(
        prompt_path=prompt_path,
        last_message_path=last_message_path,
        options=options,
    )
    provider_bin_raw = options.provider_bin or adapter.resolved_bin()
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
            heartbeat_path=heartbeat_path,
            heartbeat_interval_seconds=run_heartbeat_interval_seconds(),
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
        "heartbeat_path": str(heartbeat_path),
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
        "max_run_seconds": effective_max_run_seconds,
        "add_dirs": [str(path) for path in options.add_dirs],
        "configs": list(options.configs),
        "enables": list(options.enables),
        "disables": list(options.disables),
        "images": [str(path) for path in options.images],
        "owned_paths": list(options.owned_paths),
        "depends_on": list(options.depends_on),
        "dispatch_state": "dry-run" if options.dry_run else "ready",
        "dispatch_state_changed_at": utc_now(),
        "blocked_on": [],
        "blocked_seconds": 0,
        "queued_seconds": 0,
        "planner_source": None,
        "planner_reason": None,
        "plan_applied_at": None,
        "plan_apply_error": None,
        "planned_run_ids": [],
        "integration_state": None,
        "integration_note": None,
        "integration_updated_at": None,
        "changed_paths": [],
        "integration_applied_paths": [],
        "integration_dropped_paths": [],
        "timed_out_at": None,
        "timeout_reason": None,
        "heartbeat_at": None,
        "heartbeat_lag_seconds": None,
        "runtime_health": None,
        "runtime_health_note": None,
        "workspace_preflight_status": None,
        "workspace_preflight_checked_at": None,
        "workspace_preflight_note": None,
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
        print(f"dashboard={project_default_detail_path(project_dir)}")
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
            provider=validate_provider_name(args.provider, field_name="provider") or DEFAULT_PROVIDER,
            provider_bin=normalize_optional_text(getattr(args, "provider_bin", None)),
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
            max_run_seconds=normalize_optional_positive_int(getattr(args, "max_run_seconds", None), "max_run_seconds"),
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
            temp_args.provider_bin = spec.get("provider_bin", getattr(args, "provider_bin", None))
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
                unsettled = project_has_unsettled_runs(runs)
            if view_key != last_view or args.once:
                if should_clear:
                    print("\033[2J\033[H", end="")
                print(view)
                last_view = view_key
            if args.once:
                return 0
            if args.exit_when_settled and not unsettled:
                return 0
            time.sleep(max(1, int(args.interval)))
    finally:
        if use_alt_screen:
            print("\033[?1049l", end="", flush=True)


def cmd_team_status(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name = args.project.strip()
    is_tty = sys.stdout.isatty()
    max_updates = args.max_updates
    use_milestones = bool(args.milestones or not args.full)
    if max_updates is None and not is_tty:
        max_updates = 20
    printed_updates = 0
    last_view: str | None = None
    previous_snapshot: dict[str, Any] | None = None
    while True:
        with root_lock(root):
            index = load_index(root)
            refresh_index_state(root, index)
            runs = project_runs(index, project_name)
            if not runs and not project_has_state_files(root, project_name):
                print("no runs")
                return 0
            if runs:
                project_name = str(runs[0].get("project") or runs[0].get("project_slug") or project_name)
            snapshot = build_team_status_snapshot(root, project_name, runs)
            if use_milestones:
                view = render_team_status_milestones(previous_snapshot, snapshot)
                view_key = json.dumps(snapshot, ensure_ascii=True, sort_keys=True)
            else:
                view = render_team_status_summary(root, project_name, runs)
                view_key = watch_view_key(view)
            unsettled = project_has_unsettled_runs(runs)
        if view_key != last_view or args.once:
            if printed_updates:
                print()
            print(f"[{utc_now()}]")
            print(view)
            last_view = view_key
            previous_snapshot = snapshot
            printed_updates += 1
        if args.once:
            return 0
        if args.exit_when_settled and not unsettled:
            return 0
        if max_updates is not None and max_updates > 0 and printed_updates >= max_updates:
            if unsettled:
                print()
                print("note=team-status stopped after max-updates; rerun for more progress or use a real terminal for an uncapped stream")
            return 0
        time.sleep(max(1, int(args.interval)))


def cmd_team_metrics(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    project_name = args.project.strip()
    with root_lock(root):
        index = load_index(root)
        refresh_index_state(root, index)
        runs = project_runs(index, project_name)
        if not runs and not project_has_state_files(root, project_name):
            print("no runs")
            return 0
        if runs:
            project_name = str(runs[0].get("project") or runs[0].get("project_slug") or project_name)
        metrics = build_project_metrics(root, project_name, runs)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    print(render_team_metrics_cli(metrics))
    return 0


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
        compacted_at = normalize_optional_text(run.get("compacted_at"))
        if compacted_at:
            print(
                f"log was removed during cleanup at {compacted_at}: {path}",
                file=sys.stderr,
            )
        else:
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


def cmd_repair_integration(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        run = resolve_run(index, args.run)
        refresh_run(run)
        repair_run_integration(root, run, retry_conflict=bool(args.retry_conflict))
        save_index_and_sync(root, index)
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


def cmd_cleanup(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    with root_lock(root):
        index = load_index(root)
        for run in index["runs"]:
            refresh_run(run)
        maybe_integrate_completed_runs(root, index)
        summary = cleanup_root_artifacts(
            root,
            index,
            project_filter=args.project,
            include_failed=bool(args.include_failed),
            include_standalone=bool(args.include_standalone),
        )
        save_index(root, index)
        sync_projects(root, index)
    print(f"root={root}")
    if args.project:
        print(f"project={args.project}")
    print(f"projects_compacted={summary['projects_compacted']}")
    print(f"runs_compacted={summary['runs_compacted']}")
    print(f"standalone_runs_compacted={summary['standalone_runs_compacted']}")
    print(f"files_removed={summary['files_removed']}")
    if summary["blocked_projects"]:
        print()
        print("blocked_projects:")
        for item in summary["blocked_projects"]:
            print(f"- {item['project']} ({item['slug']}): {item['reason']}")
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
                if not index_has_unsettled_runs(index):
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
        default=DEFAULT_PROVIDER,
        metavar="PROVIDER",
        help=f"CLI provider adapter for this run. Supported names: {provider_names_for_help()}",
    )
    parser.add_argument("--provider-bin", help="Executable override for the selected provider")
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
    parser.add_argument("--max-run-seconds", type=int, help="Maximum wall-clock seconds this child run may execute before timeout")
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
    parser.add_argument("--max-work-seconds", type=int, help="Maximum total wall-clock seconds the project may keep launching work before timing out")
    parser.add_argument("--max-planner-rounds", type=int, help="Maximum number of manager planning rounds before stopping")
    parser.add_argument("--max-auto-fix-rounds", type=int, help="Maximum automatic validation-fix or recovery planning rounds")
    parser.add_argument(
        "--planner-provider",
        metavar="PROVIDER",
        help=f"Provider to use for future planner runs for this project. Supported names: {provider_names_for_help()}",
    )
    parser.add_argument("--planner-provider-bin", help="Executable override for the planner provider")
    parser.add_argument(
        "--child-provider",
        metavar="PROVIDER",
        help=f"Default provider for child runs launched from planner output. Supported names: {provider_names_for_help()}",
    )
    parser.add_argument("--child-provider-bin", help="Executable override for the default child provider")
    parser.add_argument(
        "--allow-provider",
        action="append",
        metavar="PROVIDER",
        default=[],
        help=f"Limit planner-produced child runs to these providers. Supported names: {provider_names_for_help()}",
    )


def add_orchestrate_options(parser: argparse.ArgumentParser) -> None:
    add_project_capture_options(parser)
    parser.add_argument(
        "--provider",
        default=None,
        metavar="PROVIDER",
        help=f"CLI provider adapter to use for the manager planner child. Supported names: {provider_names_for_help()}",
    )
    parser.add_argument("--provider-bin", help="Executable override for the planner child provider")
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
        description="Manage real child CLI sessions as subsessions through a provider adapter layer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize the controller directory")
    init_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    init_p.set_defaults(func=cmd_init)

    providers_p = sub.add_parser("providers", help="List supported provider adapters")
    providers_p.add_argument("--json", action="store_true", help="Print provider metadata as JSON")
    providers_p.set_defaults(func=cmd_providers)

    provider_check_p = sub.add_parser("provider-check", help="Validate provider executables and basic CLI readiness")
    provider_check_p.add_argument(
        "--provider",
        action="append",
        metavar="PROVIDER",
        help=f"Provider to check (repeatable; default: all). Supported names: {provider_names_for_help()}",
    )
    provider_check_p.add_argument("--bin", action="append", default=[], help="Override executable using provider=path")
    provider_check_p.add_argument("--cd", help="Working directory used for relative executable paths")
    provider_check_p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    provider_check_p.set_defaults(func=cmd_provider_check)

    provider_smoke_p = sub.add_parser("provider-smoke-test", help="Launch one real child run and wait for a terminal smoke-test result")
    provider_smoke_p.add_argument(
        "--provider",
        required=True,
        metavar="PROVIDER",
        help=f"Provider to validate end to end. Supported names: {provider_names_for_help()}",
    )
    provider_smoke_p.add_argument("--provider-bin", help="Executable override for the selected provider")
    provider_smoke_p.add_argument("--root", help="Controller root used for the smoke-test run (default: a fresh temp directory)")
    provider_smoke_p.add_argument("--cd", help="Working directory for the child run")
    provider_smoke_p.add_argument("--sandbox", default="read-only", help="Sandbox mode for the child run")
    provider_smoke_p.add_argument("--prompt", help="Explicit smoke-test prompt to send instead of the default exact-text prompt")
    provider_smoke_p.add_argument("--expect-text", default="OK", help="Exact text expected in the final last_message")
    provider_smoke_p.add_argument("--timeout", type=int, default=60, help="Maximum seconds to wait for a terminal result")
    provider_smoke_p.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds while waiting")
    provider_smoke_p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    provider_smoke_p.set_defaults(func=cmd_provider_smoke_test)

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

    team_status_p = sub.add_parser("team-status", help="Show compact project progress updates and child activity")
    team_status_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    team_status_p.add_argument("--project", required=True, help="Project name or project slug")
    team_status_p.add_argument("--interval", type=int, default=2, help="Seconds between refreshes")
    team_status_p.add_argument("--once", action="store_true", help="Render one update and exit")
    team_status_p.add_argument("--milestones", action="store_true", help="Explicitly request milestone mode; this is now the default behavior")
    team_status_p.add_argument("--full", action="store_true", help="Print the fuller compact project summary instead of milestone-only updates")
    team_status_p.add_argument("--exit-when-settled", action="store_true", help="Exit when the project has no running, queued, ready, prepared, or blocked runs")
    team_status_p.add_argument("--max-updates", type=int, help="Maximum number of changed updates to print before exiting; defaults to 20 when stdout is captured")
    team_status_p.set_defaults(func=cmd_team_status)

    team_metrics_p = sub.add_parser("team-metrics", help="Show a project scorecard for speed, coordination, and waiting overhead")
    team_metrics_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME})")
    team_metrics_p.add_argument("--project", required=True, help="Project name or project slug")
    team_metrics_p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the descriptive scorecard")
    team_metrics_p.set_defaults(func=cmd_team_metrics)

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

    repair_integration_p = sub.add_parser("repair-integration", help="Retry one run's integration after a failed or manual-review state")
    repair_integration_p.add_argument("run", help="Run id or unique prefix")
    repair_integration_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    repair_integration_p.add_argument(
        "--retry-conflict",
        action="store_true",
        help="Allow retrying a run currently in `conflict`; use this only after manual cleanup because not every worktree conflict is safely retryable",
    )
    repair_integration_p.set_defaults(func=cmd_repair_integration)

    cancel_p = sub.add_parser("cancel", help="Cancel a running child session")
    cancel_p.add_argument("run", help="Run id or unique prefix")
    cancel_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    cancel_p.add_argument("--force", action="store_true", help="Use SIGKILL instead of SIGTERM")
    cancel_p.set_defaults(func=cmd_cancel)

    cleanup_p = sub.add_parser("cleanup", help="Compact settled project state and disposable child artifacts")
    cleanup_p.add_argument("--root", help=f"Controller root directory (default: ./{DEFAULT_ROOT_NAME}; legacy roots still recognized: {LEGACY_ROOTS_LABEL})")
    cleanup_p.add_argument("--project", help="Limit cleanup to one project name or project slug")
    cleanup_p.add_argument("--include-failed", action="store_true", help="Also compact failed or exited runs once the project is otherwise settled")
    cleanup_p.add_argument("--include-standalone", action="store_true", help="Also compact completed standalone runs that are not attached to a project")
    cleanup_p.set_defaults(func=cmd_cleanup)

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
