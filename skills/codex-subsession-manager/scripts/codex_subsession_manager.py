#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
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
INDEX_VERSION = 2
DEFAULT_PROVIDER = "codex"
GENERIC_ROOT_NAME = ".agent-subsessions"
LEGACY_ROOT_NAME = ".codex-subsessions"


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
    generic = cwd / GENERIC_ROOT_NAME
    legacy = cwd / LEGACY_ROOT_NAME
    if generic.exists():
        return generic
    if legacy.exists():
        return legacy
    return generic


def index_path(root: Path) -> Path:
    return root / "index.json"


def ensure_root(root: Path) -> None:
    (root / "runs").mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class DispatchOptions:
    provider: str
    name: str | None
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


def run_has_provider_artifacts(run: dict[str, Any]) -> bool:
    status = str(run.get("status") or "")
    if status in {"dry-run", "prepared"}:
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
    print(
        f"{run['run_id']:<30} {run['status']:<10} provider={run.get('provider', DEFAULT_PROVIDER):<6} "
        f"pid={run.get('pid') or '-':<8} exit={exit_text:<4} session={session_id}"
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    ensure_root(root)
    data = load_index(root)
    save_index(root, data)
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
        "launched_at": utc_now(),
        "finished_at": None,
        "started_epoch": epoch_now(),
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
    }

    if not options.dry_run:
        stdout_fh = stdout_path.open("w", encoding="utf-8")
        stderr_fh = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            ["/bin/bash", str(runner_path)],
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=str(options.cd),
            start_new_session=True,
        )
        stdout_fh.close()
        stderr_fh.close()
        run["pid"] = process.pid
        run["status"] = "running"
        write_text(state_path, "running\n")

    index["runs"].append(run)
    save_index(root, index)
    print_run_summary(run)
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
        ),
    }


def cmd_dispatch(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
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
    index = load_index(root)
    manifest_path = resolve_path(args.file)
    specs = load_manifest(manifest_path)

    for spec in specs:
        prompt, prompt_file = merged_prompt_spec(spec)
        temp_args = argparse.Namespace(**vars(args))
        temp_args.name = spec.get("name", args.name)
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
        materialize_run(root, index, **common_dispatch_kwargs(temp_args))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    for run in index["runs"]:
        refresh_run(run)
    save_index(root, index)
    if args.json:
        print(json.dumps(index["runs"], ensure_ascii=True, indent=2))
        return 0
    if not index["runs"]:
        print("no runs")
        return 0
    for run in index["runs"]:
        print_run_summary(run)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    run = resolve_run(index, args.run)
    refresh_run(run)
    save_index(root, index)
    print(json.dumps(run, ensure_ascii=True, indent=2, sort_keys=True))
    last_message = read_text_if_exists(Path(run["last_message_path"]))
    if last_message:
        print()
        print(last_message.rstrip())
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    run = resolve_run(index, args.run)
    refresh_run(run)
    save_index(root, index)
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
    index = load_index(root)
    run = resolve_run(index, args.run)
    refresh_run(run)
    save_index(root, index)
    print(provider_for_run(run).build_resume_command(run, args.exec))
    return 0


def cmd_attach_session(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    run = resolve_run(index, args.run)
    set_session_id(run, args.session_id)
    save_index(root, index)
    print(f"{run['run_id']} -> {args.session_id}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    targets = [resolve_run(index, args.run)] if args.run else index["runs"]
    for run in targets:
        refresh_run(run)
        print_run_summary(run)
    save_index(root, index)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
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
    save_index(root, index)
    print_run_summary(run)
    return 0


def add_common_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME}, with legacy {LEGACY_ROOT_NAME} support)")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help="CLI provider adapter. Currently codex is implemented; the control plane is shaped for future providers.",
    )
    parser.add_argument("--name", help="Human-friendly run label")
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
    init_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME}, with legacy {LEGACY_ROOT_NAME} support)")
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
    status_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    status_p.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    status_p.set_defaults(func=cmd_status)

    show_p = sub.add_parser("show", help="Show one run and its last message")
    show_p.add_argument("run", help="Run id or unique prefix")
    show_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    show_p.set_defaults(func=cmd_show)

    tail_p = sub.add_parser("tail", help="Tail stdout or stderr for one run")
    tail_p.add_argument("run", help="Run id or unique prefix")
    tail_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    tail_p.add_argument("-n", "--lines", type=int, default=20, help="Number of lines to print")
    tail_p.add_argument("--stderr", action="store_true", help="Show stderr instead of stdout JSONL")
    tail_p.set_defaults(func=cmd_tail)

    resume_p = sub.add_parser("resume-cmd", help="Print a shell-ready provider resume command")
    resume_p.add_argument("run", help="Run id or unique prefix")
    resume_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    resume_p.add_argument("--exec", action="store_true", help="Print a non-interactive provider resume command")
    resume_p.set_defaults(func=cmd_resume_cmd)

    attach_p = sub.add_parser("attach-session", help="Manually attach a provider session id to a run")
    attach_p.add_argument("run", help="Run id or unique prefix")
    attach_p.add_argument("session_id", help="Provider session id")
    attach_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    attach_p.set_defaults(func=cmd_attach_session)

    attach_thread_p = sub.add_parser("attach-thread", help="Backward-compatible alias for Codex thread ids")
    attach_thread_p.add_argument("run", help="Run id or unique prefix")
    attach_thread_p.add_argument("session_id", help="Codex thread id")
    attach_thread_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME})")
    attach_thread_p.set_defaults(func=cmd_attach_session)

    reconcile_p = sub.add_parser("reconcile", help="Refresh run statuses and infer missing session ids")
    reconcile_p.add_argument("run", nargs="?", help="Optional run id or unique prefix")
    reconcile_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME}, with legacy {LEGACY_ROOT_NAME} support)")
    reconcile_p.set_defaults(func=cmd_reconcile)

    cancel_p = sub.add_parser("cancel", help="Cancel a running child session")
    cancel_p.add_argument("run", help="Run id or unique prefix")
    cancel_p.add_argument("--root", help=f"Controller root directory (default: ./{GENERIC_ROOT_NAME}, with legacy {LEGACY_ROOT_NAME} support)")
    cancel_p.add_argument("--force", action="store_true", help="Use SIGKILL instead of SIGTERM")
    cancel_p.set_defaults(func=cmd_cancel)

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
