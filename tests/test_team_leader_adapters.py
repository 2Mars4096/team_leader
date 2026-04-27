import importlib.util
import os
from pathlib import Path
import tempfile
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
            "max_run_seconds": None,
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
                "--max-run-seconds",
                "90",
                "--prompt",
                "Review only.",
            ]
        )
        payload = team_leader.common_dispatch_kwargs(args)
        self.assertEqual(payload["options"].provider, "claude")
        self.assertEqual(payload["options"].max_run_seconds, 90)

    def test_batch_manifest_wires_per_run_max_run_seconds(self):
        parser = team_leader.build_parser()
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "manifest.json"
            manifest.write_text(
                '{"runs":[{"name":"worker","prompt":"Do it.","max_run_seconds":45}]}',
                encoding="utf-8",
            )
            args = parser.parse_args(["batch", "--root", str(Path(td) / ".team-leader"), "--file", str(manifest)])
            with mock.patch.object(team_leader, "materialize_run") as materialize:
                self.assertEqual(team_leader.cmd_batch(args), 0)
        options = materialize.call_args.kwargs["options"]
        self.assertEqual(options.max_run_seconds, 45)

    def test_project_remaining_work_seconds_uses_project_start(self):
        brief = {
            "created_at": "2026-01-01T00:00:00Z",
            "max_work_seconds": 600,
        }
        runs = [
            {
                "created_at": "2026-01-01T00:02:00Z",
                "launched_at": "2026-01-01T00:02:10Z",
            }
        ]
        remaining = team_leader.project_remaining_work_seconds(
            brief,
            runs,
            now_epoch=team_leader.parse_timestamp_epoch("2026-01-01T00:05:00Z"),
        )
        self.assertEqual(remaining, 300)

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

    def test_build_runner_script_includes_heartbeat_loop(self):
        script = team_leader.build_runner_script(
            command=["demo-cli", "exec", "-"],
            prompt_path=Path("/tmp/prompt.md"),
            state_path=Path("/tmp/state.txt"),
            exit_code_path=Path("/tmp/exit_code.txt"),
            started_path=Path("/tmp/started_at.txt"),
            finished_path=Path("/tmp/finished_at.txt"),
            heartbeat_path=Path("/tmp/heartbeat.txt"),
            heartbeat_interval_seconds=7,
            timed_out_path=Path("/tmp/timed_out_at.txt"),
            timeout_reason_path=Path("/tmp/timeout_reason.txt"),
        )
        self.assertIn("child_pid=$!", script)
        self.assertIn("heartbeat_pid=$!", script)
        self.assertIn("/tmp/heartbeat.txt", script)
        self.assertIn("sleep 7", script)

    def test_build_runner_script_enforces_timeout_and_ticks_manager(self):
        script = team_leader.build_runner_script(
            command=["demo-cli", "exec", "-"],
            prompt_path=Path("/tmp/prompt.md"),
            state_path=Path("/tmp/state.txt"),
            exit_code_path=Path("/tmp/exit_code.txt"),
            started_path=Path("/tmp/started_at.txt"),
            finished_path=Path("/tmp/finished_at.txt"),
            heartbeat_path=Path("/tmp/heartbeat.txt"),
            heartbeat_interval_seconds=7,
            timed_out_path=Path("/tmp/timed_out_at.txt"),
            timeout_reason_path=Path("/tmp/timeout_reason.txt"),
            max_run_seconds=30,
            timeout_grace_seconds=2,
            manager_tick_command=["python3", "team_leader.py", "tick", "--root", "/tmp/root"],
        )
        self.assertIn("sleep 30", script)
        self.assertIn("/tmp/timed_out_at.txt", script)
        self.assertIn("/tmp/timeout_reason.txt", script)
        self.assertIn("printf '%s\\n' 124", script)
        self.assertIn("kill -TERM -- -$$", script)
        self.assertIn("sleep 2", script)
        self.assertIn("team_leader.py tick --root /tmp/root", script)

    def test_run_runtime_health_detects_healthy_and_stale_states(self):
        healthy_now = team_leader.parse_timestamp_epoch("2026-01-01T00:00:35Z")
        missing_now = team_leader.parse_timestamp_epoch("2026-01-01T00:00:40Z")
        stale_now = team_leader.parse_timestamp_epoch("2026-01-01T00:00:50Z")
        with mock.patch.object(team_leader, "run_heartbeat_stale_seconds", return_value=30):
            healthy_state = team_leader.run_runtime_health(
                {
                    "status": "running",
                    "launched_at": "2026-01-01T00:00:00Z",
                    "heartbeat_at": "2026-01-01T00:00:20Z",
                },
                now_epoch=healthy_now,
            )
            missing_state = team_leader.run_runtime_health(
                {
                    "status": "running",
                    "launched_at": "2026-01-01T00:00:00Z",
                    "heartbeat_at": None,
                },
                now_epoch=missing_now,
            )
            stale_state = team_leader.run_runtime_health(
                {
                    "status": "running",
                    "launched_at": "2026-01-01T00:00:00Z",
                    "heartbeat_at": "2026-01-01T00:00:10Z",
                },
                now_epoch=stale_now,
            )
        self.assertEqual(healthy_state[0], "healthy")
        self.assertEqual(missing_state[0], "heartbeat-missing")
        self.assertEqual(stale_state[0], "heartbeat-stale")

    def test_run_timeout_note_and_runtime_health_track_timeout(self):
        timeout_note = team_leader.run_timeout_note(
            {
                "launched_at": "2026-01-01T00:00:00Z",
                "max_run_seconds": 30,
            },
            now_epoch=team_leader.parse_timestamp_epoch("2026-01-01T00:00:45Z"),
        )
        self.assertEqual(timeout_note, "run exceeded max_run_seconds (45s > 30s)")

        health = team_leader.run_runtime_health(
            {
                "status": "failed",
                "timed_out_at": "2026-01-01T00:00:45Z",
                "timeout_reason": timeout_note,
            }
        )
        self.assertEqual(health[0], "timed-out")
        self.assertEqual(health[1], timeout_note)

    def test_run_summary_shows_timeout_marker(self):
        text = team_leader.run_summary_text(
            {
                "run_id": "run-1",
                "status": "failed",
                "provider": "codex",
                "pid": None,
                "exit_code": 124,
                "task_id": "worker",
                "summary": "Timed out worker",
                "dispatch_state": "failed",
                "blocked_on": [],
                "integration_state": None,
                "runtime_health": "timed-out",
                "session_id": None,
                "thread_id": None,
            }
        )
        self.assertIn("hb=timeout", text)

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

    def test_should_not_spawn_planner_while_worker_runs_are_still_pending(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            brief = {
                "goal": "ship it",
                "max_planner_rounds": 10,
                "max_auto_fix_rounds": 2,
            }
            runs = [
                {
                    "run_id": "planner-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-1",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "plan_applied_at": "2026-01-01T00:00:00Z",
                    "planned_run_ids": ["worker-1"],
                    "last_message_path": str(project_dir / "planner.md"),
                    "summary": "planner",
                },
                {
                    "run_id": "worker-1",
                    "status": "failed",
                    "dispatch_state": "failed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-1",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-1.md"),
                    "summary": "failed worker",
                },
                {
                    "run_id": "worker-2",
                    "status": "blocked",
                    "dispatch_state": "queued",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-2",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-2.md"),
                    "summary": "queued worker",
                },
            ]
            self.assertEqual(
                team_leader.should_spawn_planner_for_project(project_dir, brief, runs),
                (False, "worker-runs-pending"),
            )

    def test_auto_fix_round_cap_blocks_failed_run_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            brief = {
                "goal": "ship it",
                "max_planner_rounds": 10,
                "max_auto_fix_rounds": 2,
            }
            runs = [
                {
                    "run_id": "planner-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-1",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "planner_reason": "failed-runs",
                    "plan_applied_at": "2026-01-01T00:00:00Z",
                    "planned_run_ids": ["worker-1"],
                    "last_message_path": str(project_dir / "planner-1.md"),
                    "summary": "planner 1",
                },
                {
                    "run_id": "planner-2",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-2",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "planner_reason": "failed-runs",
                    "plan_applied_at": "2026-01-01T00:01:00Z",
                    "planned_run_ids": ["worker-2"],
                    "last_message_path": str(project_dir / "planner-2.md"),
                    "summary": "planner 2",
                },
                {
                    "run_id": "worker-1",
                    "status": "failed",
                    "dispatch_state": "failed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-1",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-1.md"),
                    "summary": "failed worker",
                },
            ]
            self.assertEqual(
                team_leader.should_spawn_planner_for_project(project_dir, brief, runs),
                (False, "max-auto-fix-rounds-reached"),
            )

    def test_continuous_budget_allows_bounded_followup_after_successful_wave(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            brief = {
                "goal": "ship it",
                "max_work_seconds": 600,
                "max_planner_rounds": 10,
                "max_auto_fix_rounds": 2,
                "created_at": "2026-01-01T00:00:00Z",
            }
            runs = [
                {
                    "run_id": "planner-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-1",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "plan_applied_at": "2026-01-01T00:00:00Z",
                    "planned_run_ids": ["worker-1"],
                    "last_message_path": str(project_dir / "planner-1.md"),
                    "summary": "planner 1",
                },
                {
                    "run_id": "worker-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-1",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-1.md"),
                    "summary": "successful worker",
                },
            ]
            with mock.patch.object(team_leader, "project_time_budget_reached", return_value=False):
                self.assertEqual(
                    team_leader.should_spawn_planner_for_project(project_dir, brief, runs),
                    (True, "time-budget-continuation"),
                )

    def test_continuous_followup_requires_project_time_budget(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            brief = {
                "goal": "ship it",
                "max_planner_rounds": 10,
                "max_auto_fix_rounds": 2,
            }
            runs = [
                {
                    "run_id": "planner-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-1",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "plan_applied_at": "2026-01-01T00:00:00Z",
                    "planned_run_ids": ["worker-1"],
                    "last_message_path": str(project_dir / "planner-1.md"),
                    "summary": "planner 1",
                },
                {
                    "run_id": "worker-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-1",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-1.md"),
                    "summary": "successful worker",
                },
            ]
            self.assertEqual(
                team_leader.should_spawn_planner_for_project(project_dir, brief, runs),
                (False, "manual-review"),
            )

    def test_planner_apply_error_pauses_continuous_retries(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            brief = {
                "goal": "ship it",
                "max_planner_rounds": 10,
                "max_auto_fix_rounds": 2,
            }
            runs = [
                {
                    "run_id": "planner-1",
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "manager-plan-1",
                    "role": "manager",
                    "planner_source": "team-leader-planner",
                    "planner_reason": "failed-runs",
                    "plan_apply_error": "launch plan lists 99 child runs",
                    "finished_at": "2026-01-01T00:00:00Z",
                    "last_message_path": str(project_dir / "planner-1.md"),
                    "summary": "planner 1",
                },
                {
                    "run_id": "worker-1",
                    "status": "failed",
                    "dispatch_state": "failed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": "worker-1",
                    "role": "implementation",
                    "last_message_path": str(project_dir / "worker-1.md"),
                    "summary": "failed worker",
                },
            ]
            self.assertEqual(
                team_leader.should_spawn_planner_for_project(project_dir, brief, runs),
                (False, "planner-apply-error"),
            )

    def test_apply_planner_run_rejects_oversized_plans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".team-leader"
            team_leader.ensure_root(root)
            project_dir = team_leader.ensure_project_workspace(root, "demo", "demo")
            brief = team_leader.default_project_brief("demo")
            brief["goal"] = "ship it"
            team_leader.save_project_brief(project_dir, brief)
            planner_output = project_dir / "planner-output.md"
            planner_output.write_text("plan\n", encoding="utf-8")
            run = {
                "run_id": "planner-1",
                "project": "demo",
                "project_slug": "demo",
                "last_message_path": str(planner_output),
                "plan_applied_at": None,
                "plan_apply_error": None,
                "planned_run_ids": [],
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
                "planner_allowed_providers": ["codex"],
            }
            index = {"version": 1, "runs": [run]}
            oversized_plan = {
                "plan_summary": "too many runs",
                "runs": [
                    {
                        "task_id": f"task-{idx}",
                        "name": f"task-{idx}",
                        "role": "reviewer",
                        "summary": f"Task {idx}",
                        "cwd": ".",
                        "sandbox": "read-only",
                        "owned_paths": [],
                        "depends_on": [],
                        "prompt": "Review only.",
                    }
                    for idx in range(3)
                ],
            }
            with mock.patch.object(team_leader, "extract_launch_plan", return_value=oversized_plan), mock.patch.object(
                team_leader, "max_plan_runs_per_wave", return_value=2
            ):
                planned_ids = team_leader.apply_planner_run(root, index, run)
            self.assertEqual(planned_ids, [])
            self.assertIn("per-wave limit is 2", run["plan_apply_error"])
            self.assertEqual(run["planned_run_ids"], [])

    def test_compaction_caches_preview_and_question_records(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            last_message_path = run_dir / "last_message.md"
            last_message_path.write_text(
                "Done.\n\nQuestions For Humans\n- Should we keep the fallback path?\n",
                encoding="utf-8",
            )
            run = {
                "run_id": "run-1",
                "status": "failed",
                "run_dir": str(run_dir),
                "stdout_path": str(run_dir / "stdout.jsonl"),
                "stderr_log": str(run_dir / "stderr.log"),
                "last_message_path": str(last_message_path),
                "workspace_mode": "direct",
                "compacted_at": None,
                "compaction_removed": [],
                "summary": "failed worker",
                "task_id": "worker-1",
            }
            changed, removed = team_leader.compact_run_artifacts(run, reason="settled-project")
            self.assertTrue(changed)
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(last_message_path.exists())
            self.assertFalse(run_dir.exists())
            self.assertIn("Done.", team_leader.last_message_display_for_run(run))
            records = team_leader.question_records_for_run(run)
            self.assertEqual(len(records), 1)
            self.assertIn("fallback path", records[0]["text"])

    def test_dashboard_render_is_stable_across_monitor_heartbeat_updates(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            runs = [
                {
                    "run_id": "run-1",
                    "task_id": "run-1",
                    "summary": "Running task",
                    "role": "implementation",
                    "status": "running",
                    "dispatch_state": "running",
                    "integration_state": None,
                    "session_id": None,
                    "owned_paths": [],
                    "launched_at": None,
                    "workspace_mode": "direct",
                    "run_dir": str(project_dir / "runs" / "run-1"),
                    "stdout_path": str(project_dir / "runs" / "run-1" / "stdout.jsonl"),
                    "last_message_path": str(project_dir / "runs" / "run-1" / "last_message.md"),
                }
            ]
            with mock.patch.object(team_leader, "monitor_state", return_value=("active", "2026-01-01T00:00:00Z")):
                first = team_leader.render_dashboard("demo", project_dir, runs, [], [], {})
            with mock.patch.object(team_leader, "monitor_state", return_value=("active", "2026-01-01T00:00:30Z")):
                second = team_leader.render_dashboard("demo", project_dir, runs, [], [], {})
            self.assertEqual(first, second)

    def test_project_overview_render_is_stable_across_monitor_heartbeat_updates(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            runs = [
                {
                    "run_id": "run-1",
                    "task_id": "run-1",
                    "summary": "Running task",
                    "role": "implementation",
                    "status": "running",
                    "dispatch_state": "running",
                    "cwd": "/repo",
                    "stdout_path": str(project_dir / "runs" / "run-1" / "stdout.jsonl"),
                    "last_message_path": str(project_dir / "runs" / "run-1" / "last_message.md"),
                }
            ]
            with mock.patch.object(team_leader, "monitor_state", return_value=("active", "2026-01-01T00:00:00Z")):
                first = team_leader.render_project_overview("demo", project_dir, runs)
            with mock.patch.object(team_leader, "monitor_state", return_value=("active", "2026-01-01T00:00:30Z")):
                second = team_leader.render_project_overview("demo", project_dir, runs)
            self.assertEqual(first, second)

    def test_project_metrics_are_quantized_while_runs_are_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".team-leader"
            team_leader.ensure_root(root)
            runs = [
                {
                    "run_id": "run-1",
                    "project": "demo",
                    "project_slug": "demo",
                    "status": "running",
                    "dispatch_state": "running",
                    "created_at": "2026-01-01T00:00:05Z",
                    "launched_at": "2026-01-01T00:00:10Z",
                    "last_message_path": str(root / "runs" / "run-1" / "last_message.md"),
                }
            ]
            with mock.patch.object(team_leader, "epoch_now", return_value=61):
                first = team_leader.build_project_metrics(root, "demo", runs)
            with mock.patch.object(team_leader, "epoch_now", return_value=119):
                second = team_leader.build_project_metrics(root, "demo", runs)
            self.assertEqual(first["updated_at"], second["updated_at"])
            self.assertEqual(first["project_age_seconds"], second["project_age_seconds"])

    def test_worktree_cap_queues_extra_writer_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".team-leader"
            held_worktree = root / "projects" / "demo" / "worktrees" / "run-held"
            held_worktree.mkdir(parents=True, exist_ok=True)
            (held_worktree / ".git").write_text("gitdir: /tmp/demo\n", encoding="utf-8")
            index = {
                "version": 1,
                "runs": [
                    {
                        "run_id": "run-held",
                        "status": "running",
                        "dispatch_state": "running",
                        "project": "demo",
                        "project_slug": "demo",
                        "workspace_mode": "worktree",
                        "worktree_path": str(held_worktree),
                        "workspace_released_at": None,
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "run_id": "run-next",
                        "status": "blocked",
                        "dispatch_state": "ready",
                        "dispatch_state_changed_at": "2026-01-01T00:01:00Z",
                        "blocked_on": [],
                        "project": "demo",
                        "project_slug": "demo",
                        "workspace_mode": "worktree",
                        "worktree_path": str(root / "projects" / "demo" / "worktrees" / "run-next"),
                        "workspace_released_at": None,
                        "created_at": "2026-01-01T00:01:00Z",
                    },
                ],
            }
            with mock.patch.object(team_leader, "max_project_worktrees", return_value=1):
                team_leader.apply_worktree_cap_metadata(index)
            self.assertEqual(index["runs"][1]["dispatch_state"], "queued")
            self.assertEqual(index["runs"][1]["blocked_on"], ["worktree-cap:1"])

    def test_active_project_artifact_budget_compacts_older_terminal_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".team-leader"
            team_leader.ensure_root(root)

            def make_run(run_id: str, created_at: str) -> dict[str, str | None]:
                run_dir = root / "runs" / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                last_message_path = run_dir / "last_message.md"
                last_message_path.write_text(f"{run_id} output\n", encoding="utf-8")
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "dispatch_state": "completed",
                    "project": "demo",
                    "project_slug": "demo",
                    "task_id": run_id,
                    "summary": run_id,
                    "run_dir": str(run_dir),
                    "stdout_path": str(run_dir / "stdout.jsonl"),
                    "stderr_log": str(run_dir / "stderr.log"),
                    "last_message_path": str(last_message_path),
                    "workspace_mode": "direct",
                    "compacted_at": None,
                    "compaction_removed": [],
                    "created_at": created_at,
                }

            run1 = make_run("run-1", "2026-01-01T00:00:00Z")
            run2 = make_run("run-2", "2026-01-01T00:01:00Z")
            run3 = make_run("run-3", "2026-01-01T00:02:00Z")
            index = {"version": 1, "runs": [run1, run2, run3]}
            with mock.patch.object(team_leader, "max_project_active_run_artifacts", return_value=1):
                team_leader.apply_active_project_artifact_budgets(root, index)
            self.assertIsNotNone(run1["compacted_at"])
            self.assertIsNotNone(run2["compacted_at"])
            self.assertIsNone(run3["compacted_at"])
            self.assertFalse(Path(run1["run_dir"]).exists())
            self.assertFalse(Path(run2["run_dir"]).exists())
            self.assertTrue(Path(run3["run_dir"]).exists())
            self.assertIn("run-1 output", team_leader.last_message_display_for_run(run1) or "")

    def test_write_project_reports_prunes_old_terminal_reports(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            reports_dir = project_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)

            def make_run(run_id: str, created_at: str) -> dict[str, str | None]:
                run_dir = project_dir / "runs" / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                last_message_path = run_dir / "last_message.md"
                last_message_path.write_text(f"{run_id} report\n", encoding="utf-8")
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "dispatch_state": "completed",
                    "task_id": run_id,
                    "summary": run_id,
                    "role": "implementation",
                    "session_id": None,
                    "owned_paths": [],
                    "workspace_mode": "direct",
                    "worktree_path": None,
                    "integration_state": None,
                    "depends_on": [],
                    "output_warnings": [],
                    "run_dir": str(run_dir),
                    "last_message_path": str(last_message_path),
                    "created_at": created_at,
                }

            runs = [
                make_run("run-1", "2026-01-01T00:00:00Z"),
                make_run("run-2", "2026-01-01T00:01:00Z"),
                make_run("run-3", "2026-01-01T00:02:00Z"),
            ]
            for run in runs:
                (reports_dir / f"{run['run_id']}.md").write_text("stale\n", encoding="utf-8")
            with mock.patch.object(team_leader, "max_project_report_files", return_value=1):
                team_leader.write_project_reports(project_dir, runs)
            self.assertFalse((reports_dir / "run-1.md").exists())
            self.assertFalse((reports_dir / "run-2.md").exists())
            self.assertTrue((reports_dir / "run-3.md").exists())


if __name__ == "__main__":
    unittest.main()
