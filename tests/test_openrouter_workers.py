import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/team-leader/scripts"
spec = importlib.util.spec_from_file_location("openrouter_entry_test", SCRIPTS / "openrouter_workers.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class OpenRouterTests(unittest.TestCase):
    def test_model_defaults_and_override_precedence(self):
        bundle = SCRIPTS / "model_intelligence/scripts"
        sys.path.insert(0, str(bundle))
        try:
            from plan_workers import model_override
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(model_override({}, {}), "deepseek/deepseek-v4.1-flash")
                self.assertIsNone(model_override({}, {"default_model": "auto"}))
            with mock.patch.dict(os.environ, {"MODEL_INTELLIGENCE_OPENROUTER_MODEL": "test/env"}):
                self.assertEqual(model_override({}, {}), "test/env")
                self.assertEqual(model_override({}, {"default_model": "test/manifest"}), "test/manifest")
                self.assertEqual(model_override({"model": "test/task"}, {"default_model": "auto"}), "test/task")
        finally:
            sys.path.remove(str(bundle))

    def test_env_is_literal_allowlisted_and_preserves_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENROUTER_API_KEY='file-key'\nOPENROUTER_PDF_ENGINE='$(touch stolen)'\nPATH=bad\n")
            env = {"OPENROUTER_API_KEY": "exported-key"}
            entry.load_env(path, env)
            self.assertEqual(env["OPENROUTER_API_KEY"], "exported-key")
            self.assertEqual(env["OPENROUTER_PDF_ENGINE"], "$(touch stolen)")
            self.assertNotIn("PATH", env)

    def test_shared_credentials_load_from_unrelated_project(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared.env"
            shared.write_text("OPENROUTER_API_KEY='shared-key'\n")
            explicit = Path(tmp) / "explicit.env"
            explicit.write_text("OPENROUTER_API_KEY='explicit-key'\n")
            args = argparse.Namespace(env_file=None, cache_dir=tmp, action="run", worker_args=["plan.json"])
            with mock.patch.object(entry, "DEFAULT_ENV_FILE", shared), mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "inherited-key"}), mock.patch.object(entry.subprocess, "run") as run:
                run.return_value.returncode = 0
                entry.run(args)
                self.assertEqual(run.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "shared-key")
                args.env_file = str(explicit)
                entry.run(args)
                self.assertEqual(run.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "explicit-key")
                self.assertNotIn("explicit-key", repr(run.call_args.args))
                args.env_file = str(Path(tmp) / "missing.env")
                with self.assertRaises(RuntimeError):
                    entry.run(args)

    def test_run_passes_credentials_in_environment_only(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENROUTER_API_KEY='test-secret'\n")
            args = argparse.Namespace(env_file=str(path), cache_dir=tmp,
                                      action="run", worker_args=["plan.json"])
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(entry.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(entry.run(args), 0)
            self.assertEqual(run.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "test-secret")
            self.assertNotIn("test-secret", repr(run.call_args.args))

    def test_codex_default_ignores_other_cli_hints_and_api_key(self):
        env = {**os.environ, "CLAUDECODE": "1", "OPENROUTER_API_KEY": "test-secret"}
        env.pop("TEAM_LEADER_LAUNCHER_PROVIDER", None)
        result = subprocess.run([sys.executable, str(SCRIPTS / "team_leader.py"), "providers", "--json"],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        providers = json.loads(result.stdout)
        self.assertEqual([p["name"] for p in providers if p["default"]], ["codex"])

    def test_preview_and_invalid_guards_do_not_call_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            plan = {"schema": "model-intelligence-worker-plan/v1", "objective": "test", "strategy": "economy",
                    "max_parallel": 1, "estimated_max_cost_usd": 0.01,
                    "tasks": [{"id": "one", "profile": "general", "difficulty": "simple", "model": "test/model",
                               "quality_score": None, "quality_floor": None, "estimated_max_cost_usd": 0.01,
                               "max_output_tokens": 10, "prompt_file": str(Path(tmp) / "absent.txt")}]}
            path.write_text(json.dumps(plan))
            cmd = [sys.executable, str(SCRIPTS / "team_leader.py"), "openrouter", "run", str(path)]
            env = {**os.environ, "OPENROUTER_API_KEY": "test-secret"}
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY RUN", result.stdout)
            for guard in ("nan", "-1", "0.001"):
                result = subprocess.run(cmd + ["--execute", "--max-estimated-cost-usd", guard,
                                               "--output-dir", str(Path(tmp) / "output")],
                                        text=True, capture_output=True, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(tmp) / "output").exists())
                self.assertNotIn("test-secret", result.stderr)


if __name__ == "__main__":
    unittest.main()
