"""Explicit text workers for configurable third-party inference APIs (stdlib only)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "team-leader-api-plan/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential to an endpoint not selected in the plan.
        return None


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def cost(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("cost estimates must be finite nonnegative numbers")
    return value


def endpoint(config):
    base = config.get("base_url", "")
    if not isinstance(base, str):
        raise ValueError("base_url must be a URL")
    url = urllib.parse.urlsplit(base)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "https" and not (url.scheme == "http" and local)) or not url.hostname:
        raise ValueError("base_url requires HTTPS (HTTP is allowed for loopback servers)")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("base_url must not contain credentials, query parameters, or fragments")
    protocol = config.get("protocol", "openai-compatible")
    if protocol not in {"openai-compatible", "anthropic"}:
        raise ValueError("protocol must be openai-compatible or anthropic")
    return base.rstrip("/") + ("/messages" if protocol == "anthropic" else "/chat/completions")


def validate(plan):
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError(f"plan schema must be {SCHEMA}")
    providers = plan.get("providers")
    tasks = plan.get("tasks")
    if not isinstance(providers, dict) or not providers or not isinstance(tasks, list) or not tasks:
        raise ValueError("plan requires providers and tasks")
    positive_int(plan.get("max_parallel", 1), "max_parallel")
    ids = set()
    keys = set()
    total = 0.0
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("tasks must be objects")
        ident = task.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", ident) or ident.lower() in ids | {"run"}:
            raise ValueError("task ids must be unique safe filenames; run is reserved")
        ids.add(ident.lower())
        provider = task.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            raise ValueError("every task must name a configured provider")
        config = providers[provider]
        if not isinstance(config, dict):
            raise ValueError("provider configuration must be an object")
        endpoint(config)
        key = config.get("api_key_env")
        if key is not None:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("api_key_env must name an environment variable")
            keys.add(key)
        elif urllib.parse.urlsplit(config["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote providers require api_key_env")
        if config.get("token_parameter", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported token_parameter")
        for name in ("model", "prompt_file"):
            if not isinstance(task.get(name), str) or not task[name].strip():
                raise ValueError(f"every task requires {name}")
        if "system_file" in task and not isinstance(task["system_file"], str):
            raise ValueError("system_file must be a path")
        positive_int(task.get("max_output_tokens"), "max_output_tokens")
        total += cost(task.get("estimated_max_cost_usd"))
    if not math.isclose(cost(plan.get("estimated_max_cost_usd")), total, abs_tol=1e-9):
        raise ValueError("plan cost must equal the sum of task estimates")
    return keys


def request_for(task, config, env):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = env.get(config.get("api_key_env"), "")
    messages = [{"role": "user", "content": task["prompt"]}]
    body = {"model": task["model"], "messages": messages, "stream": False}
    if config.get("protocol") == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body["max_tokens"] = task["max_output_tokens"]
        if task.get("system"):
            body["system"] = task["system"]
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
        body[config.get("token_parameter", "max_tokens")] = task["max_output_tokens"]
        if task.get("system"):
            messages.insert(0, {"role": "system", "content": task["system"]})
    return urllib.request.Request(endpoint(config), data=json.dumps(body).encode("utf-8"), headers=headers)


def response_text(response, protocol):
    if protocol == "anthropic":
        text = "\n".join(block["text"] for block in response["content"] if block.get("type") == "text")
        finish = response.get("stop_reason")
    else:
        choice = response["choices"][0]
        text = choice["message"]["content"]
        finish = choice.get("finish_reason")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("response contains no text")
    return {"text": text, "usage": response.get("usage", {}), "finish_reason": finish}


def run_one(task, config, env, timeout):
    result = {"id": task["id"], "provider": task["provider"], "model": task["model"]}
    try:
        request = request_for(task, config, env)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.load(response)
        result.update(status="success", response=response_text(payload, config.get("protocol")))
    except urllib.error.HTTPError as exc:
        result.update(status="failed", error=f"HTTP {exc.code}")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        # Server error bodies and exception URLs can echo credentials or prompts.
        result.update(status="failed", error="request failed or response was invalid")
    return result


def run(args):
    path = Path(args.plan).expanduser().resolve()
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("cannot read API plan JSON") from None
    keys = validate(plan)
    parallel = positive_int(args.max_parallel or plan.get("max_parallel", 1), "max_parallel")
    positive_int(args.timeout, "timeout")
    if args.max_parallel is not None:
        positive_int(args.max_parallel, "max_parallel")
    for task in plan["tasks"]:
        print(f"{task['id']}: provider={task['provider']} model={task['model']} estimated=${task['estimated_max_cost_usd']:.6f}")
    if not args.execute:
        print("DRY RUN: no external model calls were made")
        return 0
    if not args.output_dir or args.max_estimated_cost_usd is None:
        raise ValueError("--execute requires --output-dir and --max-estimated-cost-usd")
    if plan["estimated_max_cost_usd"] > cost(args.max_estimated_cost_usd):
        raise ValueError("plan estimate exceeds the cost guard")
    env = os.environ.copy()
    env_path = Path(args.env_file).expanduser() if args.env_file else args.default_env_file
    if args.env_file or env_path.exists():
        settings = {}
        try:
            args.load_env(env_path, settings, allowed_keys=keys)
        except OSError:
            raise ValueError("cannot read API credential file") from None
        env.update({key: value for key, value in settings.items() if value})
    if any(not env.get(key) for key in keys):
        raise ValueError("missing API credential; configure the plan's api_key_env variables")
    tasks = []
    for original in plan["tasks"]:
        task = dict(original)
        for field, dest in (("prompt_file", "prompt"), ("system_file", "system")):
            if task.get(field):
                try:
                    task[dest] = (path.parent / Path(task[field]).expanduser()).read_text(encoding="utf-8")
                except (OSError, ValueError):
                    raise ValueError("cannot read worker prompt/system file") from None
        tasks.append(task)
    output = Path(args.output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=min(parallel, len(tasks), 32)) as pool:
        results = list(pool.map(lambda task: run_one(task, plan["providers"][task["provider"]], env, args.timeout), tasks))
    for result in results:
        (output / (result["id"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "run.json").write_text(json.dumps({"schema": "team-leader-api-run/v1", "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    success = sum(result["status"] == "success" for result in results)
    print(f"workers={len(results)} success={success} failed={len(results)-success} output={output}")
    return 0 if success == len(results) else 1


def add_parser(subparsers, credentials):
    parser = subparsers.add_parser("api", help="Configurable third-party API text workers")
    actions = parser.add_subparsers(dest="api_action", required=True)
    runner = actions.add_parser("run", help="Preview or execute an explicit API worker plan")
    runner.add_argument("plan")
    runner.add_argument("--env-file")
    runner.add_argument("--execute", action="store_true")
    runner.add_argument("--output-dir")
    runner.add_argument("--max-estimated-cost-usd", type=float)
    runner.add_argument("--max-parallel", type=int)
    runner.add_argument("--timeout", type=int, default=600)
    runner.set_defaults(func=run_cli, load_env=credentials.load_env, default_env_file=credentials.DEFAULT_ENV_FILE)


def run_cli(args):
    try:
        return run(args)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    except OSError:
        raise RuntimeError("API worker file operation failed; use a new writable output directory") from None
