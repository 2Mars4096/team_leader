#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    return Path.cwd() / ".codex-subsessions"


def index_path(root: Path) -> Path:
    return root / "index.json"


def ensure_root(root: Path) -> None:
    (root / "runs").mkdir(parents=True, exist_ok=True)


def load_index(root: Path) -> dict[str, Any]:
    path = index_path(root)
    if not path.exists():
        return {"version": 1, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid index file: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid index file: {path}")
    runs = data.get("runs")
    if not isinstance(runs, list):
        data["runs"] = []
    data.setdefault("version", 1)
    return data


def save_index(root: Path, data: dict[str, Any]) -> None:
    ensure_root(root)
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
    if not run.get("thread_id"):
        thread_id = detect_thread_id(run)
        if thread_id:
            run["thread_id"] = thread_id


def detect_thread_id(run: dict[str, Any]) -> str | None:
    stdout_path = Path(run["stdout_jsonl"])
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


def build_codex_command(
    *,
    prompt_path: Path,
    last_message_path: Path,
    cd: Path,
    sandbox: str | None,
    model: str | None,
    profile: str | None,
    add_dirs: list[Path],
    configs: list[str],
    enables: list[str],
    disables: list[str],
    images: list[Path],
    search: bool,
    skip_git_repo_check: bool,
    ephemeral: bool,
    full_auto: bool,
    dangerous: bool,
) -> list[str]:
    command = [
        os.environ.get("CODEX_BIN", "codex"),
        "exec",
        "--json",
        "--output-last-message",
        str(last_message_path),
        "--cd",
        str(cd),
    ]
    if sandbox:
        command.extend(["--sandbox", sandbox])
    if model:
        command.extend(["--model", model])
    if profile:
        command.extend(["--profile", profile])
    for add_dir in add_dirs:
        command.extend(["--add-dir", str(add_dir)])
    for config in configs:
        command.extend(["--config", config])
    for feature in enables:
        command.extend(["--enable", feature])
    for feature in disables:
        command.extend(["--disable", feature])
    for image in images:
        command.extend(["--image", str(image)])
    if search:
        command.append("--search")
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if ephemeral:
        command.append("--ephemeral")
    if full_auto:
        command.append("--full-auto")
    if dangerous:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.append("-")
    return command


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
    thread = run.get("thread_id") or "-"
    exit_code = run.get("exit_code")
    exit_text = str(exit_code) if exit_code is not None else "-"
    print(
        f"{run['run_id']:<30} {run['status']:<10} pid={run.get('pid') or '-':<8} "
        f"exit={exit_text:<4} thread={thread}"
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    ensure_root(root)
    data = load_index(root)
    save_index(root, data)
    print(root)
    return 0


def materialize_run(
    root: Path,
    index: dict[str, Any],
    *,
    name: str | None,
    prompt_text: str,
    cd: Path,
    sandbox: str | None,
    model: str | None,
    profile: str | None,
    add_dirs: list[Path],
    configs: list[str],
    enables: list[str],
    disables: list[str],
    images: list[Path],
    search: bool,
    skip_git_repo_check: bool,
    ephemeral: bool,
    full_auto: bool,
    dangerous: bool,
    dry_run: bool,
) -> dict[str, Any]:
    ensure_root(root)
    run_id = make_run_id({run["run_id"] for run in index["runs"]}, name)
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

    write_text(prompt_path, prompt_text)

    command = build_codex_command(
        prompt_path=prompt_path,
        last_message_path=last_message_path,
        cd=cd,
        sandbox=sandbox,
        model=model,
        profile=profile,
        add_dirs=add_dirs,
        configs=configs,
        enables=enables,
        disables=disables,
        images=images,
        search=search,
        skip_git_repo_check=skip_git_repo_check,
        ephemeral=ephemeral,
        full_auto=full_auto,
        dangerous=dangerous,
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
        "name": name or run_id,
        "status": "dry-run" if dry_run else "prepared",
        "run_dir": str(run_dir),
        "cwd": str(cd),
        "prompt_path": str(prompt_path),
        "stdout_jsonl": str(stdout_path),
        "stderr_log": str(stderr_path),
        "last_message_path": str(last_message_path),
        "runner_path": str(runner_path),
        "thread_id": None,
        "pid": None,
        "exit_code": None,
        "launched_at": utc_now(),
        "finished_at": None,
        "started_epoch": epoch_now(),
        "sandbox": sandbox,
        "model": model,
        "profile": profile,
        "search": search,
        "skip_git_repo_check": skip_git_repo_check,
        "ephemeral": ephemeral,
        "full_auto": full_auto,
        "dangerous": dangerous,
        "add_dirs": [str(path) for path in add_dirs],
        "configs": list(configs),
        "enables": list(enables),
        "disables": list(disables),
        "images": [str(path) for path in images],
    }

    if not dry_run:
        stdout_fh = stdout_path.open("w", encoding="utf-8")
        stderr_fh = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            ["/bin/bash", str(runner_path)],
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=str(cd),
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
        "name": args.name,
        "prompt_text": parse_prompt(args),
        "cd": resolve_path(args.cd) if args.cd else Path.cwd(),
        "sandbox": args.sandbox,
        "model": args.model,
        "profile": args.profile,
        "add_dirs": [resolve_path(path) for path in args.add_dir],
        "configs": list(args.config),
        "enables": list(args.enable),
        "disables": list(args.disable),
        "images": [resolve_path(path) for path in args.image],
        "search": bool(args.search),
        "skip_git_repo_check": bool(args.skip_git_repo_check),
        "ephemeral": bool(args.ephemeral),
        "full_auto": bool(args.full_auto),
        "dangerous": bool(args.dangerous),
        "dry_run": bool(args.dry_run),
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
    thread_id = run.get("thread_id")
    if not thread_id:
        raise RuntimeError("run has no detected thread_id; use reconcile or attach-thread first")
    cwd = shlex.quote(run["cwd"])
    if args.exec:
        print(f"cd {cwd} && codex exec resume {shlex.quote(thread_id)} -")
    else:
        print(f"cd {cwd} && codex resume {shlex.quote(thread_id)}")
    return 0


def cmd_attach_thread(args: argparse.Namespace) -> int:
    root = resolve_path(args.root) if args.root else default_root()
    index = load_index(root)
    run = resolve_run(index, args.run)
    run["thread_id"] = args.thread_id
    save_index(root, index)
    print(f"{run['run_id']} -> {args.thread_id}")
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
    parser.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    parser.add_argument("--name", help="Human-friendly run label")
    parser.add_argument("--prompt", help="Prompt text for the child Codex session")
    parser.add_argument("--prompt-file", help="Path to a prompt file for the child session")
    parser.add_argument("--cd", help="Working directory for the child Codex session")
    parser.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Codex sandbox mode for the child session",
    )
    parser.add_argument("--model", help="Codex model override")
    parser.add_argument("--profile", help="Codex config profile override")
    parser.add_argument("--add-dir", action="append", default=[], help="Additional writable directory")
    parser.add_argument("--config", action="append", default=[], help="Pass through codex --config")
    parser.add_argument("--enable", action="append", default=[], help="Pass through codex --enable")
    parser.add_argument("--disable", action="append", default=[], help="Pass through codex --disable")
    parser.add_argument("--image", action="append", default=[], help="Image path to attach")
    parser.add_argument("--search", action="store_true", help="Enable Codex live web search")
    parser.add_argument(
        "--skip-git-repo-check",
        action="store_true",
        help="Allow Codex child sessions outside a Git repository",
    )
    parser.add_argument("--ephemeral", action="store_true", help="Run child without persisting Codex session files")
    parser.add_argument("--full-auto", action="store_true", help="Run child with Codex full-auto mode")
    parser.add_argument(
        "--dangerous",
        action="store_true",
        help="Run child with Codex dangerous no-sandbox mode",
    )
    parser.add_argument("--dry-run", action="store_true", help="Create the run directory but do not launch Codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage real Codex child sessions as subsessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize the controller directory")
    init_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    init_p.set_defaults(func=cmd_init)

    dispatch_p = sub.add_parser("dispatch", help="Dispatch one Codex child session")
    add_common_run_options(dispatch_p)
    dispatch_p.set_defaults(func=cmd_dispatch)

    batch_p = sub.add_parser("batch", help="Dispatch multiple Codex child sessions from a JSON manifest")
    add_common_run_options(batch_p)
    batch_p.add_argument("--file", required=True, help="Path to a JSON manifest")
    batch_p.set_defaults(func=cmd_batch)

    status_p = sub.add_parser("status", help="Show tracked runs")
    status_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    status_p.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    status_p.set_defaults(func=cmd_status)

    show_p = sub.add_parser("show", help="Show one run and its last message")
    show_p.add_argument("run", help="Run id or unique prefix")
    show_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    show_p.set_defaults(func=cmd_show)

    tail_p = sub.add_parser("tail", help="Tail stdout or stderr for one run")
    tail_p.add_argument("run", help="Run id or unique prefix")
    tail_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    tail_p.add_argument("-n", "--lines", type=int, default=20, help="Number of lines to print")
    tail_p.add_argument("--stderr", action="store_true", help="Show stderr instead of stdout JSONL")
    tail_p.set_defaults(func=cmd_tail)

    resume_p = sub.add_parser("resume-cmd", help="Print a shell-ready resume command")
    resume_p.add_argument("run", help="Run id or unique prefix")
    resume_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    resume_p.add_argument("--exec", action="store_true", help="Print a non-interactive codex exec resume command")
    resume_p.set_defaults(func=cmd_resume_cmd)

    attach_p = sub.add_parser("attach-thread", help="Manually attach a Codex thread id to a run")
    attach_p.add_argument("run", help="Run id or unique prefix")
    attach_p.add_argument("thread_id", help="Codex thread/session id")
    attach_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    attach_p.set_defaults(func=cmd_attach_thread)

    reconcile_p = sub.add_parser("reconcile", help="Refresh run statuses and infer missing thread ids")
    reconcile_p.add_argument("run", nargs="?", help="Optional run id or unique prefix")
    reconcile_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
    reconcile_p.set_defaults(func=cmd_reconcile)

    cancel_p = sub.add_parser("cancel", help="Cancel a running child session")
    cancel_p.add_argument("run", help="Run id or unique prefix")
    cancel_p.add_argument("--root", help="Controller root directory (default: ./.codex-subsessions)")
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
