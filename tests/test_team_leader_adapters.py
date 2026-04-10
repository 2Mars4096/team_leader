import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "team-leader" / "scripts" / "team_leader.py"
SPEC = importlib.util.spec_from_file_location("team_leader_script", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
team_leader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = team_leader
SPEC.loader.exec_module(team_leader)


class TeamLeaderAdapterTests(unittest.TestCase):
    def make_options(self, **overrides):
        payload = {
            "provider": "codex",
            "provider_bin": None,
            "name": "worker",
            "project": "demo",
            "task_id": "worker",
            "role": "implementation",
            "summary": "Demo worker",
            "prompt_text": "fix it",
            "cd": Path("/repo"),
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "profile": "default",
            "add_dirs": [Path("/extra")],
            "configs": ["foo=bar"],
            "enables": ["feature-a"],
            "disables": ["feature-b"],
            "images": [Path("/tmp/image.png")],
            "search": True,
            "skip_git_repo_check": True,
            "ephemeral": True,
            "full_auto": True,
            "dangerous": True,
            "dry_run": False,
            "owned_paths": ["src"],
            "depends_on": ["plan"],
        }
        payload.update(overrides)
        return team_leader.DispatchOptions(**payload)

    def test_codex_exec_command_regression_shape(self):
        provider = team_leader.CodexProvider()
        options = self.make_options()
        with mock.patch.dict(os.environ, {"CODEX_BIN": "codex-test"}, clear=False):
            command = provider.build_exec_command(
                prompt_path=Path("/tmp/prompt.md"),
                last_message_path=Path("/tmp/last_message.md"),
                options=options,
            )
        self.assertEqual(
            command,
            [
                "codex-test",
                "exec",
                "--json",
                "--output-last-message",
                "/tmp/last_message.md",
                "--cd",
                "/repo",
                "--sandbox",
                "workspace-write",
                "--model",
                "gpt-test",
                "--profile",
                "default",
                "--add-dir",
                "/extra",
                "--config",
                "foo=bar",
                "--enable",
                "feature-a",
                "--disable",
                "feature-b",
                "--image",
                "/tmp/image.png",
                "--search",
                "--skip-git-repo-check",
                "--ephemeral",
                "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox",
                "-",
            ],
        )

    def test_codex_resume_command_regression_shape(self):
        provider = team_leader.CodexProvider()
        run = {
            "cwd": "/repo/path",
            "provider": "codex",
            "provider_bin": None,
            "session_id": "thread-123",
        }
        with mock.patch.dict(os.environ, {"CODEX_BIN": "codex-test"}, clear=False):
            interactive = provider.build_resume_command(run, exec_mode=False)
            non_interactive = provider.build_resume_command(run, exec_mode=True)
        self.assertEqual(interactive, "cd /repo/path && codex-test resume thread-123")
        self.assertEqual(non_interactive, "cd /repo/path && codex-test exec resume thread-123 -")

    def test_claude_stream_json_includes_verbose(self):
        provider = team_leader.ClaudeProvider()
        options = self.make_options(
            provider="claude",
            provider_bin=None,
            sandbox="read-only",
            model=None,
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
        )
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": "claude-test"}, clear=False):
            command = provider.build_exec_command(
                prompt_path=Path("/tmp/prompt.md"),
                last_message_path=Path("/tmp/last_message.md"),
                options=options,
            )
        self.assertEqual(
            command[:5],
            ["claude-test", "-p", "--verbose", "--output-format", "stream-json"],
        )

    def test_provider_aliases_normalize_to_canonical_names(self):
        self.assertEqual(team_leader.validate_provider_name("cc"), "claude")
        self.assertEqual(team_leader.validate_provider_name("claude-code"), "claude")
        self.assertEqual(team_leader.validate_provider_name("cursor-agent"), "cursor")
        self.assertEqual(team_leader.validate_provider_name("kiro-cli"), "kiro")
        self.assertEqual(team_leader.validate_provider_name("codex-cli"), "codex")

    def test_plan_item_can_switch_child_provider(self):
        brief = {
            "repo_paths": ["/repo"],
            "child_provider": "codex",
            "child_provider_bin": None,
            "allowed_providers": ["codex", "claude"],
        }
        planner_run = {
            "provider": "codex",
            "provider_bin": None,
            "add_dirs": [],
            "configs": [],
            "enables": [],
            "disables": [],
            "images": [],
            "search": False,
            "skip_git_repo_check": False,
            "ephemeral": False,
            "full_auto": True,
            "dangerous": False,
            "planner_default_child_provider": "codex",
            "planner_default_child_provider_bin": None,
            "planner_allowed_providers": ["codex", "claude"],
        }
        item = {
            "provider": "claude",
            "task_id": "review",
            "name": "review",
            "role": "reviewer",
            "summary": "Review changes",
            "cwd": ".",
            "sandbox": "read-only",
            "owned_paths": [],
            "depends_on": [],
            "prompt": "Review findings only.",
            "search": False,
            "skip_git_repo_check": False,
            "full_auto": True,
            "dangerous": False,
        }
        options = team_leader.dispatch_options_from_plan_item(
            item,
            project_name="demo",
            brief=brief,
            planner_run=planner_run,
        )
        self.assertEqual(options.provider, "claude")
        self.assertEqual(options.cd, Path("/repo"))

    def test_provider_smoke_test_parser_wires_defaults(self):
        parser = team_leader.build_parser()
        args = parser.parse_args(["provider-smoke-test", "--provider", "cc"])
        self.assertIs(args.func, team_leader.cmd_provider_smoke_test)
        self.assertEqual(args.provider, "cc")
        self.assertEqual(args.expect_text, "OK")
        self.assertEqual(args.timeout, 60)
        self.assertEqual(args.poll_interval, 1.0)
        self.assertEqual(args.sandbox, "read-only")

    def test_dispatch_parser_accepts_provider_aliases(self):
        parser = team_leader.build_parser()
        args = parser.parse_args(
            [
                "dispatch",
                "--provider",
                "cc",
                "--prompt",
                "Review only.",
            ]
        )
        payload = team_leader.common_dispatch_kwargs(args)
        self.assertEqual(payload["options"].provider, "claude")

    def test_provider_check_returns_nonzero_when_any_provider_is_blocked(self):
        args = SimpleNamespace(provider=["cc", "codex-cli"], bin=[], cd=None, json=False)
        with mock.patch.object(
            team_leader,
            "provider_check_record",
            side_effect=[
                {
                    "provider": "claude",
                    "provider_bin": "claude",
                    "resolved_bin": "/usr/local/bin/claude",
                    "status": "ok",
                    "note": None,
                    "notes": "ok",
                    "session_label": "session",
                    "supported_sandbox_modes": ["read-only"],
                },
                {
                    "provider": "codex",
                    "provider_bin": "codex",
                    "resolved_bin": None,
                    "status": "blocked",
                    "note": "provider-env: blocked",
                    "notes": "blocked",
                    "session_label": "thread",
                    "supported_sandbox_modes": ["read-only"],
                },
            ],
        ), mock.patch("builtins.print"):
            exit_code = team_leader.cmd_provider_check(args)
        self.assertEqual(exit_code, 1)

    def test_smoke_test_payload_requires_exact_last_message(self):
        with mock.patch.object(team_leader, "last_message_for_run", return_value="OK"):
            payload = team_leader.smoke_test_payload(
                root=Path("/tmp/team-leader-smoke"),
                run={
                    "run_id": "run-1",
                    "provider": "claude",
                    "status": "completed",
                    "dispatch_state": "finished",
                    "exit_code": 0,
                    "session_id": "session-1",
                },
                provider_check={"provider": "claude", "status": "ok"},
                expected_text="OK",
                timed_out=False,
            )
        self.assertTrue(payload["success"])
        self.assertTrue(payload["matched_expected_text"])
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["last_message"], "OK")

        with mock.patch.object(team_leader, "last_message_for_run", return_value="OK\nextra"):
            mismatch = team_leader.smoke_test_payload(
                root=Path("/tmp/team-leader-smoke"),
                run={
                    "run_id": "run-2",
                    "provider": "claude",
                    "status": "completed",
                    "dispatch_state": "finished",
                    "exit_code": 0,
                    "session_id": "session-2",
                },
                provider_check={"provider": "claude", "status": "ok"},
                expected_text="OK",
                timed_out=False,
            )
        self.assertFalse(mismatch["success"])
        self.assertFalse(mismatch["matched_expected_text"])


if __name__ == "__main__":
    unittest.main()
