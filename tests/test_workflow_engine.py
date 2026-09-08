import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import os
import signal
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/team-leader/scripts/team_leader.py'
spec = importlib.util.spec_from_file_location('workflow_test_controller', SCRIPT)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)
engine = api.workflow_engine()


def do(prompt='Inspect {{part}} {{view}}', **kw):
    return dict(kind='do', prompt=prompt, **kw)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = {'runs': [], 'workflows': {}}
        self.workers = engine.normalize_workers({'default': {'max_parallel': 2, 'model': 'test-model'}})
        self.materializer = mock.patch.object(api, 'materialize_run', side_effect=self.materialize)
        self.materializer.start()
        self.addCleanup(self.materializer.stop)

    def materialize(self, root, index, options, announce=False, extra_fields=None):
        run_id = str(len(index['runs']))
        run = dict(run_id=run_id, run_dir=str(root / run_id), status='running', exit_code=None,
                   sandbox=options.sandbox, model=options.model, prompt=options.prompt_text,
                   session_id='session-' + run_id, **extra_fields)
        index['runs'].append(run)
        return run

    def workflow(self, body, **kw):
        wf = dict(name='test', cwd=str(self.root), workers=self.workers, max_workers=2,
                  spec={'body': body}, state={}, env={}, sessions={}, status='running', **kw)
        engine.validate(wf['spec'], wf['workers'])
        self.index['workflows']['test'] = wf
        return wf

    def tick(self):
        with mock.patch.object(api, 'last_message_for_run', side_effect=lambda r: json.dumps(r['result'])):
            engine.tick(api, self.root, self.index)

    def finish(self, verdict='pass', data=None):
        for r in self.index['runs']:
            if r['status'] == 'running':
                r.update(status='completed', exit_code=0,
                         result=dict(verdict=verdict, evidence='Measured fixture v1', data=data or {}))

    def test_nested_loops_lazy_pool_restart_and_session_reuse(self):
        wf = self.workflow(dict(kind='for_each', var='part', items=list(range(30)), parallel=True,
            body=dict(kind='for_each', var='view', items=['front', 'side'],
                      body=do(session='part-{{part}}'))))
        self.tick()
        self.assertEqual(len(self.index['runs']), 2)
        self.assertEqual(len(wf['state']['children']), 2)
        self.finish()
        # Save/load a fresh runtime, then consume accepted results and resume contexts.
        api.save_index(self.root, self.index)
        self.index = api.load_index(self.root)
        self.tick()
        self.assertEqual(len(self.index['runs']), 4)
        self.assertEqual([r['workflow_resume_id'] for r in self.index['runs'][2:]], ['session-0', 'session-1'])
        for _ in range(65):
            self.finish()
            self.tick()
            active = [r for r in self.index['runs'] if r['status'] == 'running']
            self.assertLessEqual(len(active), 2)
            if self.index['workflows']['test']['status'] == 'completed':
                break
        self.assertEqual(self.index['workflows']['test']['status'], 'completed')
        self.assertEqual(len(self.index['runs']), 60)
        self.tick()
        self.assertEqual(len(self.index['runs']), 60)

    def test_dynamic_discovery_condition_and_scoped_prompt(self):
        wf = self.workflow(dict(kind='sequence', steps=[do('Discover', save_as='discovery'),
            dict(kind='for_each', var='part', items={'ref': 'discovery.data.parts'}, body=
                 dict(kind='sequence', steps=[do('Check {{part}}', save_as='check'),
                     dict(kind='if', condition={'ref': 'check.verdict', 'equals': 'fail'},
                          then=do('Fix {{part}}'))]))]))
        self.tick()
        self.finish(data={'parts': ['elbow']})
        self.tick()
        self.assertIn('Check elbow', self.index['runs'][-1]['prompt'])
        self.finish('fail')
        self.tick()
        self.assertIn('Fix elbow', self.index['runs'][-1]['prompt'])
        self.finish()
        self.tick()
        self.assertEqual(wf['status'], 'completed')

    def test_uncertain_does_not_take_false_branch(self):
        wf = self.workflow(dict(kind='sequence', steps=[do('Check', save_as='check'),
            dict(kind='if', condition={'ref': 'check.verdict', 'equals': 'pass'},
                 then=do('Accept'), **{'else': do('Change')})]))
        self.tick()
        self.finish('uncertain')
        self.tick()
        self.assertEqual(wf['status'], 'blocked')
        self.assertIn('uncertain', wf['error'])
        self.assertEqual(len(self.index['runs']), 1)

    def test_repeat_stops_at_limit(self):
        wf = self.workflow(dict(kind='repeat_until', max_iterations=2,
            condition={'ref': 'check.verdict', 'equals': 'pass'}, body=do('Round {{iteration}}', save_as='check')))
        for _ in range(5):
            self.tick()
            self.finish('fail')
        self.assertEqual(wf['status'], 'blocked')
        self.assertIn('repeat limit', wf['error'])
        self.assertEqual(len(self.index['runs']), 2)

    def test_worker_models_and_per_type_limits(self):
        self.workers = engine.normalize_workers({'reviewer': {'model': 'review-model', 'max_parallel': 1}})
        self.workflow(dict(kind='for_each', var='part', items=[1, 2, 3], parallel=True,
                           body=do('Check {{part}}', worker='reviewer')))
        self.tick()
        self.assertEqual(len(self.index['runs']), 1)
        self.assertEqual(self.index['runs'][0]['model'], 'review-model')

    def test_pause_schedules_nothing_and_resume_adopts_existing_run(self):
        wf = self.workflow(do('Once'))
        self.tick()
        wf['status'] = 'paused'
        self.finish()
        self.tick()
        self.assertNotEqual(wf['state'].get('done'), True)
        wf['status'] = 'running'
        wf['state'] = {}  # simulate crash after durable run registration
        self.tick()
        self.assertEqual(wf['status'], 'completed')
        self.assertEqual(len(self.index['runs']), 1)

    def test_shared_writers_serialize(self):
        self.workers['default']['sandbox'] = 'workspace-write'
        self.workflow(dict(kind='for_each', var='part', items=[1, 2], parallel=True, body=do('Fix {{part}}')))
        self.tick()
        self.assertEqual(len(self.index['runs']), 1)
        self.finish()
        self.tick()
        self.assertEqual(len(self.index['runs']), 2)

    def test_session_rotation(self):
        self.workers['default']['max_session_steps'] = 1
        self.workflow(dict(kind='sequence', steps=[do('A', session='x'), do('B', session='x')]))
        self.tick()
        self.finish()
        self.tick()
        self.assertIsNone(self.index['runs'][-1]['workflow_resume_id'])

    def test_invalid_and_missing_inputs_block(self):
        for body in [dict(kind='oops'), do('x', worker='missing'),
                     dict(kind='repeat_until', max_iterations=0, body=do('x'), condition={'ref':'x', 'equals':True})]:
            with self.assertRaises(RuntimeError):
                engine.validate({'body':body}, self.workers)
        wf = self.workflow(do('Missing {{reference}}'))
        self.tick()
        self.assertEqual(wf['status'], 'blocked')
        self.assertEqual(self.index['runs'], [])

    def test_uncertain_leaf_and_malformed_result_block(self):
        wf = self.workflow(do("Check"))
        self.tick()
        self.finish("uncertain")
        self.tick()
        self.assertEqual(wf["status"], "blocked")
        wf["status"] = "running"
        self.index["runs"][0]["result"] = {"verdict": "pass", "data": {}}
        self.tick()
        self.assertEqual(wf["status"], "blocked")
        self.assertIn("valid verdict/evidence/data", wf["error"])

    def test_empty_loop_and_false_branch(self):
        wf = self.workflow(dict(kind="sequence", steps=[
            dict(kind="for_each", var="part", items=[], body=do("Never")),
            dict(kind="if", condition={"ref": "flag", "equals": True},
                 then=do("Wrong"), **{"else": do("Correct")})]))
        wf["env"]["flag"] = False
        self.tick()
        self.assertEqual(len(self.index["runs"]), 1)
        self.assertIn("Correct", self.index["runs"][0]["prompt"])
        self.finish()
        self.tick()
        self.assertEqual(wf["status"], "completed")

    def test_compiler_produces_workflow_then_executes(self):
        self.workers['__planner__'] = engine.normalize_workers({'p': {}})['p']
        wf = dict(name='test', cwd=str(self.root), workers=self.workers, max_workers=2,
                  state={}, env={}, sessions={}, status='compiling', instruction='For each part compare each view')
        self.index['workflows']['test'] = wf
        self.tick()
        self.assertEqual(self.index['runs'][0]['workflow_worker'], '__planner__')
        self.index['runs'][0].update(status='completed', exit_code=0,
            result={'body': dict(kind='for_each', var='part', items=['elbow'],
                body=dict(kind='for_each', var='view', items=['side'], body=do()))})
        self.tick()
        self.assertEqual(wf['status'], 'running')
        self.assertIn('Inspect elbow side', self.index['runs'][-1]['prompt'])
        self.finish()
        self.tick()
        self.assertEqual(wf['status'], 'completed')

    def test_runner_finishes_without_waiting_for_watchdog_deadline(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("test")
        callback_path = self.root / "callback.json"
        callback = [sys.executable, "-c", "import os,json,pathlib; pathlib.Path(" + repr(str(callback_path)) + ").write_text(json.dumps({'guard':os.environ.get('WORKFLOW_TEST_BIN'),'child':os.environ.get('TEAM_LEADER_CHILD_RUN')}))"]
        script = api.build_runner_script(command=["/usr/bin/true"], prompt_path=prompt,
            state_path=self.root / "state", exit_code_path=self.root / "exit",
            started_path=self.root / "started", finished_path=self.root / "finished",
            heartbeat_path=self.root / "heartbeat", heartbeat_interval_seconds=1,
            timed_out_path=self.root / "timeout", timeout_reason_path=self.root / "reason",
            max_run_seconds=30, manager_tick_command=callback,
            env_exports={"WORKFLOW_TEST_BIN": "blocked"}, path_prefix=self.root)
        path = self.root / "runner.sh"
        path.write_text(script)
        with (self.root / "output").open("w") as output:
            proc = subprocess.Popen(["/bin/bash", str(path)], stdout=output, stderr=output,
                                    start_new_session=True)
            try:
                self.assertEqual(proc.wait(timeout=3), 0)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertEqual((self.root / "state").read_text().strip(), "completed")
        self.assertFalse((self.root / "timeout").exists())
        self.assertEqual(json.loads(callback_path.read_text()), {"guard": None, "child": None})
        self.assertIn(f"env -u {api.CHILD_RUN_ENV}", script)

    def test_paused_workflow_does_not_keep_idle_monitor_alive(self):
        wf = self.workflow(do("Once"))
        wf["status"] = "paused"
        self.index["runs"].append(dict(workflow="test", status="blocked"))
        self.assertFalse(api.index_has_unsettled_runs(self.index))
        self.index["runs"][0]["status"] = "running"
        self.assertTrue(api.index_has_unsettled_runs(self.index))


if __name__ == '__main__':
    unittest.main()
