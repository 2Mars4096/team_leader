"""Persistent, lazily expanded natural-language workflows for Codex workers."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

FORMAT = '''Return ONLY a JSON workflow object with "body" and optional "inputs".
Nodes:
{"kind":"do","id":"check","worker":"reviewer","prompt":"Compare {{part}} from {{view}}.","save_as":"check","session":"part-{{part}}"}
{"kind":"sequence","steps":[nodes...]}
{"kind":"for_each","var":"part","items":["arm","shoe"],"parallel":false,"body":node}
items may instead be {"ref":"inputs.parts"}; references use dotted object paths.
{"kind":"if","condition":{"ref":"check.verdict","equals":"fail"},"then":node,"else":node}
{"kind":"repeat_until","max_iterations":3,"condition":{"ref":"check.verdict","equals":"pass"},"body":node}
Each do returns {"verdict":"pass|fail|uncertain","evidence":"concrete evidence","data":{...}}.
Use {{variable.path}} interpolation in prompts and session keys. Only explicit references are resolved.
for_each iterations have isolated variables; the loop's optional save_as collects their environments.
sequence, if and repeat share their enclosing environment. repeat exposes iteration (1-based).
A do may specify locks:["asset-{{part}}"] for exclusive resources. Writers always serialize within a workflow.
Use the same session key for related steps of the SAME worker type to reuse context.
Only parallelize independent comparisons; sequence shared revisions and cross-view verification.
Missing references or uncertain branch predicates block; never fabricate assets, views, criteria or evidence.
Preserve the user's loops and decisions explicitly; do not flatten a loop into one large do prompt.
Use preparatory do steps returning data collections when items must be discovered from actual inputs.
Set explicit finite repeat limits. If source assets are unavailable, discovery must report uncertainty.
'''


def positive(value, label):
    if type(value) is not int or value < 1:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def validate(spec, workers):
    if not isinstance(spec, dict) or not isinstance(spec.get("inputs", {}), dict):
        raise RuntimeError("workflow must be an object with object inputs")
    count = 0
    def visit(n, depth=0):
        nonlocal count
        count += 1
        if depth > 32 or count > 1000 or not isinstance(n, dict):
            raise RuntimeError("invalid or excessively large workflow")
        k = n.get("kind")
        if k == "do":
            if n.get("worker", "default") not in workers:
                raise RuntimeError(f"unknown worker type: {n.get('worker')}")
            if not isinstance(n.get("prompt"), str) or not n["prompt"].strip():
                raise RuntimeError("do requires a prompt")
            if not isinstance(n.get("locks", []), list) or not all(isinstance(x, str) for x in n.get("locks", [])):
                raise RuntimeError("locks must be a list of strings")
            if "session" in n and not isinstance(n["session"], str):
                raise RuntimeError("session must be a string")
        elif k == "sequence":
            if not isinstance(n.get("steps"), list):
                raise RuntimeError("sequence requires steps")
            for child in n["steps"]:
                visit(child, depth + 1)
        elif k == "for_each":
            if not isinstance(n.get("var"), str) or not re.fullmatch(r"[A-Za-z_]\w*", n["var"]):
                raise RuntimeError("for_each requires a variable name")
            if not isinstance(n.get("items"), (list, dict)):
                raise RuntimeError("for_each requires items or a reference")
            if isinstance(n["items"], dict) and (set(n["items"]) != {"ref"} or not isinstance(n["items"]["ref"], str)):
                raise RuntimeError("items reference requires only ref")
            if type(n.get("parallel", False)) is not bool:
                raise RuntimeError("parallel must be boolean")
            visit(n.get("body"), depth + 1)
        elif k in ("if", "repeat_until"):
            c = n.get("condition")
            if not isinstance(c, dict) or not isinstance(c.get("ref"), str) or "equals" not in c:
                raise RuntimeError("condition requires ref and equals")
            if k == "if":
                visit(n.get("then"), depth + 1)
                if "else" in n:
                    visit(n["else"], depth + 1)
            else:
                positive(n.get("max_iterations"), "max_iterations")
                visit(n.get("body"), depth + 1)
        else:
            raise RuntimeError(f"unknown workflow kind: {k}")
        if "save_as" in n and (not isinstance(n["save_as"], str) or not re.fullmatch(r"[A-Za-z_]\w*", n["save_as"])):
            raise RuntimeError("save_as must be a variable name")
    visit(spec.get("body"))


def reference(env, path):
    value = env
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise RuntimeError(f"missing workflow reference: {path}")
        value = value[key]
    return value


def render(text, env):
    def replace(m):
        v = reference(env, m.group(1).strip())
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return re.sub(r"\{\{([^{}]+)\}\}", replace, text)


def condition(c, env):
    value = reference(env, c["ref"])
    if value == "uncertain":
        raise RuntimeError(f"uncertain condition: {c['ref']}")
    return value == c["equals"]


def parse_result(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("worker must return one JSON object") from exc
    if not isinstance(result, dict):
        raise RuntimeError("worker result must be an object")
    return result


def normalize_workers(raw, model=None):
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("workers must be a nonempty object")
    out = copy.deepcopy(raw)
    for name, w in out.items():
        if not isinstance(w, dict) or w.get("provider", "codex") != "codex":
            raise RuntimeError("workflow workers currently support only Codex")
        w.setdefault("model", model)
        w.setdefault("role", name)
        w.setdefault("instructions", "")
        w.setdefault("sandbox", "read-only")
        w.setdefault("max_parallel", 1)
        w.setdefault("max_session_steps", 12)
        w.setdefault("max_run_seconds", 900)
        for key in ("role", "instructions"):
            if not isinstance(w[key], str):
                raise RuntimeError(f"worker {key} must be text")
        if w["model"] is not None and not isinstance(w["model"], str):
            raise RuntimeError("worker model must be text")
        if w["sandbox"] not in ("read-only", "workspace-write"):
            raise RuntimeError("worker sandbox must be read-only or workspace-write")
        for key in ("max_parallel", "max_session_steps", "max_run_seconds"):
            positive(w[key], key)
    return out


def options(api, wf, worker, prompt, task):
    return api.DispatchOptions(
        provider="codex", provider_bin=wf.get("codex_bin"), name=f"{wf['name']}-{task}",
        project=None, task_id=task, role=worker["role"], summary=task,
        prompt_text=prompt, cd=Path(wf["cwd"]), sandbox=worker["sandbox"],
        model=worker.get("model"), profile=None, add_dirs=[], configs=['approval_policy="never"'], enables=[],
        disables=[], images=[], search=False, skip_git_repo_check=True, ephemeral=False,
        full_auto=False, dangerous=False, max_run_seconds=worker["max_run_seconds"],
        dry_run=False, owned_paths=[], depends_on=[])


def launch(api, root, index, wf, worker_name, prompt, task, session=None, locks=None):
    worker = wf["workers"][worker_name]
    # Persist the intent before materialization. A restart adopts a run with this key.
    key = f"{wf['name']}:{task}"
    existing = next((r for r in index["runs"] if r.get("workflow_task_key") == key), None)
    if existing:
        return existing
    info = wf["sessions"].get(session, {}) if session else {}
    resume_id = info.get("id") if info.get("steps", 0) < worker["max_session_steps"] else None
    extra = {"workflow": wf["name"], "workflow_task_key": key,
             "workflow_worker": worker_name, "workflow_locks": locks or [],
             "workflow_resume_id": resume_id}
    api.save_index(root, index)
    return api.materialize_run(root, index, options(api, wf, worker, prompt, task),
                               announce=False, extra_fields=extra)


def runnable(index, wf, worker_name, locks, session):
    active = [r for r in index["runs"] if r.get("workflow") == wf["name"]
              and r.get("status") not in ("completed", "failed", "cancelled", "exited")]
    if len(active) >= wf["max_workers"]:
        return False
    if sum(r.get("workflow_worker") == worker_name for r in active) >= wf["workers"][worker_name]["max_parallel"]:
        return False
    resources = set(locks + ([f"session:{session}"] if session else []))
    return not any(resources.intersection(r.get("workflow_locks", [])) for r in active)


def advance(api, root, index, wf, n, state, env, path="body"):
    if state.get("done"):
        return True
    k = n["kind"]
    if k == "do":
        worker_name = n.get("worker", "default")
        worker = wf["workers"][worker_name]
        session = f"{worker_name}:{render(n['session'], env)}" if n.get("session") else None
        locks = [render(x, env) for x in n.get("locks", [])]
        # Shared writers serialize; read/write overlap also waits for exclusive writer use.
        writer = worker["sandbox"] != "read-only"
        active = [r for r in index["runs"] if r.get("workflow") == wf["name"] and r.get("status") not in api.TERMINAL_STATUSES]
        run = next((r for r in index["runs"] if r.get("workflow_task_key") == f"{wf['name']}:{path}"), None)
        if run is None:
            if not runnable(index, wf, worker_name, locks, session):
                return False
            if active and (writer or any(r.get("sandbox") != "read-only" for r in active)):
                return False
            prompt = (f"Worker type: {worker_name}. Role: {worker['role']}.\n{worker['instructions']}\n"
                      f"Workflow: {wf['name']}; iteration: {path}.\n"
                      + render(n["prompt"], env)
                      + "\n\nCurrent scoped inputs and prior results:\n" + json.dumps(env, ensure_ascii=False)
                      + '\nReturn ONLY JSON: {"verdict":"pass|fail|uncertain","evidence":"specific checks, artifact paths and assessed version","data":{}}. '
                        'Report missing evidence as uncertain. Do not delegate or run other agents. '
                        'Work only on this step; the controller schedules the remaining workflow.')
            run = launch(api, root, index, wf, worker_name, prompt, path, session,
                         locks + ([f"session:{session}"] if session else []))
        state["run_id"] = run["run_id"]
        if run["status"] not in api.TERMINAL_STATUSES:
            return False
        if run["status"] != "completed" or run.get("exit_code") != 0:
            raise RuntimeError(f"step {path} did not complete successfully: {run['run_id']}")
        result = parse_result(api.last_message_for_run(run))
        if result.get("verdict") not in ("pass", "fail", "uncertain") or not isinstance(result.get("evidence"), str) or not result["evidence"].strip() or not isinstance(result.get("data"), dict):
            raise RuntimeError(f"step {path} returned no valid verdict/evidence/data")
        env[n.get("save_as", n.get("id", "last"))] = result
        state["result"] = result
        if result["verdict"] == "uncertain":
            raise RuntimeError(f"uncertain result at {path}: {result['evidence']}")
        if session:
            previous = wf["sessions"].get(session, {})
            wf["sessions"][session] = {"id": api.get_session_id(run),
                "steps": previous.get("steps", 0) + 1 if run.get("workflow_resume_id") else 1}
    elif k == "sequence":
        children = state.setdefault("children", {})
        for i, child in enumerate(n["steps"]):
            if not advance(api, root, index, wf, child, children.setdefault(str(i), {}), env, f"{path}.{i}"):
                return False
    elif k == "for_each":
        if "items" not in state:
            items = reference(env, n["items"]["ref"]) if isinstance(n["items"], dict) else n["items"]
            if not isinstance(items, list) or len(items) > 10000:
                raise RuntimeError("loop items must be a list with at most 10000 entries")
            state["items"] = copy.deepcopy(items)
        children = state.setdefault("children", {})
        all_done = True
        # Expand only a pool-sized window, never the entire Cartesian product.
        pending = 0
        for i, item in enumerate(state["items"]):
            key = str(i)
            if key not in children:
                if pending >= (wf["max_workers"] if n.get("parallel") else 1):
                    all_done = False
                    break
                children[key] = {"env": {**copy.deepcopy(env), n["var"]: item}, "state": {}}
            child = children[key]
            done = advance(api, root, index, wf, n["body"], child["state"], child["env"], f"{path}[{i}]")
            if not done:
                all_done = False
                pending += 1
                if not n.get("parallel"):
                    break
        if not all_done:
            return False
        if n.get("save_as"):
            env[n["save_as"]] = [children[str(i)]["env"] for i in range(len(state["items"]))]
    elif k == "if":
        if "branch" not in state:
            state["branch"] = "then" if condition(n["condition"], env) else "else"
        branch = state["branch"]
        if branch in n and not advance(api, root, index, wf, n[branch], state.setdefault("child", {}), env, f"{path}.{branch}"):
            return False
    elif k == "repeat_until":
        i = state.setdefault("iteration", 1)
        env["iteration"] = i
        if not advance(api, root, index, wf, n["body"], state.setdefault("child", {}), env, f"{path}.round{i}"):
            return False
        if not condition(n["condition"], env):
            if i >= n["max_iterations"]:
                raise RuntimeError(f"repeat limit reached at {path}")
            state["iteration"] += 1
            state["child"] = {}
            return False
    state["done"] = True
    return True


def tick(api, root, index):
    for wf in index.get("workflows", {}).values():
        if wf["status"] not in ("running", "compiling"):
            continue
        try:
            if wf["status"] == "compiling":
                run = next((r for r in index["runs"] if r.get("workflow_task_key") == f"{wf['name']}:compile"), None)
                if run is None:
                    prompt = (FORMAT + "\nAvailable worker types (use these names exactly):\n" + json.dumps(wf["workers"])
                              + "\nDissect this user instruction into executable control flow. Inspect the workspace read-only if necessary.\n" + wf["instruction"])
                    run = launch(api, root, index, wf, "__planner__", prompt, "compile")
                if run["status"] not in api.TERMINAL_STATUSES:
                    continue
                if run["status"] != "completed" or run.get("exit_code") != 0:
                    raise RuntimeError("workflow compiler failed")
                spec = parse_result(api.last_message_for_run(run))
                validate(spec, {k: v for k, v in wf["workers"].items() if k != "__planner__"})
                wf.update(spec=spec, env={"inputs": spec.get("inputs", {})}, status="running")
            if advance(api, root, index, wf, wf["spec"]["body"], wf["state"], wf["env"]):
                wf["status"] = "completed"
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            wf["status"] = "blocked"
            wf["error"] = str(exc)
        api.save_index(root, index)
    sync(api, root, index)


def sync(api, root, index):
    for name, wf in index.get("workflows", {}).items():
        directory = root / "workflows" / name
        api.write_text(directory / "workflow.json", json.dumps(wf.get("spec", {}), indent=2, ensure_ascii=False) + "\n")
        runs = [r for r in index["runs"] if r.get("workflow") == name]
        lines = [f"# {name}", "", f"Status: {wf['status']}", f"Pool limit: {wf['max_workers']}",
                 f"Completed steps: {sum(r.get('status') == 'completed' for r in runs)}",
                 f"Error: {wf.get('error', '')}", "", "| Type | Model | Limit | Sandbox |", "|---|---|---|---|"]
        lines += [f"| {k} | {v.get('model') or 'CLI default'} | {v['max_parallel']} | {v['sandbox']} |" for k, v in wf["workers"].items()]
        api.write_text(directory / "README.md", "\n".join(lines) + "\n")



def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read JSON file {path}: {exc}") from exc


def command(api, args):
    root = api.resolve_path(args.root) if args.root else api.default_root()
    name = api.slugify(args.name)
    with api.root_lock(root):
        index = api.load_index(root)
        workflows = index.setdefault("workflows", {})
        if args.action == "start":
            if name in workflows:
                raise RuntimeError("workflow already exists; use resume or a new name")
            raw = read_json_file(args.pool_file) if args.pool_file else {"default": {"max_parallel": args.max_workers}}
            if isinstance(raw, dict) and "__planner__" in raw:
                raise RuntimeError("__planner__ is a reserved worker name")
            workers = normalize_workers(raw, args.model)
            workers["__planner__"] = normalize_workers({"__planner__": {"model": args.planner_model or args.model}})["__planner__"]
            positive(args.max_workers, "max_workers")
            if bool(args.file) == bool(args.prompt):
                raise RuntimeError("provide exactly one of --file or --prompt")
            spec = read_json_file(args.file) if args.file else None
            if args.file:
                validate(spec, {k: v for k, v in workers.items() if k != "__planner__"})
            wf = {"name": name, "cwd": str(api.resolve_path(args.cd or ".")), "max_workers": args.max_workers,
                  "workers": workers, "codex_bin": args.codex_bin, "instruction": args.prompt,
                  "spec": spec, "status": "running" if spec else "compiling", "state": {},
                  "env": {"inputs": spec.get("inputs", {})} if spec else {}, "sessions": {}}
            if args.dry_run:
                print(json.dumps(wf, indent=2))
                return 0
            workflows[name] = wf
        else:
            if name not in workflows:
                raise RuntimeError(f"unknown workflow: {name}")
            wf = workflows[name]
            if args.action == "pause":
                wf["paused_from"] = wf["status"]
                wf["status"] = "paused"
            elif args.action == "resume":
                if wf["status"] != "completed":
                    wf["status"] = "running" if wf.get("spec") else "compiling"
                    wf.pop("error", None)
        api.save_index(root, index)
        if args.action in ("start", "resume"):
            api.refresh_index_state(root, index)
        sync(api, root, index)
        print(json.dumps(workflows[name], indent=2, ensure_ascii=False))
    return 0


def add_parser(sub, api):
    parser = sub.add_parser("workflow", help="Compile and run persistent Codex control-flow workflows")
    parser.add_argument("action", choices=["start", "status", "pause", "resume"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--root")
    parser.add_argument("--file", help="Structured workflow JSON")
    parser.add_argument("--prompt", help="Natural-language instruction to compile and execute")
    parser.add_argument("--pool-file", help="JSON object mapping worker type names to model/role/limits")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--model", help="Default worker model; omitted uses Codex's configured default")
    parser.add_argument("--planner-model")
    parser.add_argument("--codex-bin")
    parser.add_argument("--cd")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview configuration without launching")
    parser.set_defaults(func=lambda args: command(api, args))
