"""Explicit OpenRouter entry point; Codex CLI remains the default worker path."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

BUNDLE = Path(__file__).with_name("model_intelligence") / "scripts"
DEFAULT_ENV_FILE = Path.home() / ".config" / "team-leader" / ".env"
ACTIONS = {"plan": "plan_workers.py", "run": "run_workers.py",
           "query": "query.py", "refresh": "refresh.py"}
ENV_KEYS = {"OPENROUTER_API_KEY", "ARTIFICIAL_ANALYSIS_API_KEY",
            "MODEL_INTELLIGENCE_OPENROUTER_MODEL", "OPENROUTER_PDF_ENGINE"}


def load_env(path: Path, env: dict[str, str]) -> None:
    """Read literal assignments without shell execution or overriding exported keys."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in ENV_KEYS:
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise RuntimeError(f"invalid env assignment at line {number}") from None
        if len(parts) > 1:
            raise RuntimeError(f"quote the env value at line {number}")
        env.setdefault(key, parts[0] if parts else "")


def run(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env_path = Path(args.env_file).expanduser() if args.env_file else DEFAULT_ENV_FILE
    if args.env_file or env_path.exists():
        try:
            settings: dict[str, str] = {}
            load_env(env_path, settings)
            env.update({key: value for key, value in settings.items() if value})
        except OSError:
            raise RuntimeError(f"cannot read team-leader credential file: {env_path}") from None
    env["MODEL_INTELLIGENCE_DATA_DIR"] = str(Path(
        args.cache_dir or env.get("MODEL_INTELLIGENCE_DATA_DIR")
        or ".team-leader/model-intelligence"
    ).expanduser().resolve())
    forwarded = args.worker_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    return subprocess.run([sys.executable, str(BUNDLE / ACTIONS[args.action]),
                           *forwarded], env=env, check=False).returncode


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("openrouter", help="Opt-in API workers: plan, preview, execute, or query models")
    parser.add_argument("--env-file", help=f"Override the shared credential file (default: {DEFAULT_ENV_FILE}); nonempty file settings take precedence over environment variables")
    parser.add_argument("--cache-dir", help="Model evidence cache (default: .team-leader/model-intelligence)")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("worker_args", nargs=argparse.REMAINDER, help="Arguments for the selected action; ACTION --help lists them")
    parser.set_defaults(func=run)
