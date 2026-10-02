import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/team-leader/scripts'
spec = importlib.util.spec_from_file_location('api_workers_test', SCRIPTS / 'api_workers.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def plan():
    return {'schema': api.SCHEMA, 'providers': {
        'vendor': {'base_url': 'https://example.com/v1', 'api_key_env': 'VENDOR_KEY'}},
        'max_parallel': 2, 'estimated_max_cost_usd': 0.01,
        'tasks': [{'id': 'review', 'provider': 'vendor', 'model': 'chosen-model',
                   'prompt_file': 'prompt.txt', 'max_output_tokens': 100,
                   'estimated_max_cost_usd': 0.01}]}


class ApiWorkersTests(unittest.TestCase):
    def test_protocol_requests_and_responses(self):
        task = {**plan()['tasks'][0], 'prompt': 'hello', 'system': 'review'}
        config = plan()['providers']['vendor']
        request = api.request_for(task, config, {'VENDOR_KEY': 'secret'})
        body = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://example.com/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'review'})
        self.assertEqual(body['max_tokens'], 100)
        config = {**config, 'protocol': 'anthropic'}
        request = api.request_for(task, config, {'VENDOR_KEY': 'secret'})
        self.assertEqual(request.full_url, 'https://example.com/v1/messages')
        self.assertEqual(request.get_header('X-api-key'), 'secret')
        self.assertEqual(json.loads(request.data)['system'], 'review')
        self.assertEqual(len(json.loads(request.data)['messages']), 1)
        self.assertEqual(api.response_text({'content': [{'type': 'thinking'}, {'type': 'text', 'text': 'ok'}], 'usage': {'input_tokens': 2}}, 'anthropic')['text'], 'ok')
        self.assertEqual(api.response_text({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'length'}]}, None)['finish_reason'], 'length')

    def test_token_parameter_override(self):
        request = api.request_for({**plan()['tasks'][0], 'prompt': 'hello'},
                                  {**plan()['providers']['vendor'], 'token_parameter': 'max_completion_tokens'}, {})
        self.assertEqual(json.loads(request.data)['max_completion_tokens'], 100)
        self.assertNotIn('max_tokens', json.loads(request.data))

    def test_invalid_configurations(self):
        mutations = [
            lambda p: p['tasks'].append(copy.deepcopy(p['tasks'][0])),
            lambda p: p['tasks'][0].update(id='../bad'),
            lambda p: p['tasks'][0].update(id='run'),
            lambda p: p['tasks'][0].update(estimated_max_cost_usd=float('nan')),
            lambda p: p.update(estimated_max_cost_usd=3),
            lambda p: p.update(max_parallel=0),
            lambda p: p['tasks'][0].update(max_output_tokens=True),
            lambda p: p['providers']['vendor'].update(base_url='http://example.com'),
            lambda p: p['providers']['vendor'].update(base_url='https://secret@example.com'),
            lambda p: p['providers']['vendor'].update(base_url='https://example.com?key=secret'),
            lambda p: p['providers']['vendor'].update(protocol='unsupported'),
            lambda p: p['providers']['vendor'].update(api_key_env='invalid-key'),
        ]
        for mutation in mutations:
            candidate = plan()
            mutation(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                api.validate(candidate)

    def test_errors_do_not_leak_server_details(self):
        task = {**plan()['tasks'][0], 'prompt': 'hello'}
        error = urllib.error.HTTPError('https://example.com?secret', 401, 'secret', {}, None)
        with mock.patch.object(api.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = error
            result = api.run_one(task, plan()['providers']['vendor'], {'VENDOR_KEY': 'secret'}, 1)
        self.assertEqual(result['error'], 'HTTP 401')
        self.assertNotIn('secret', json.dumps(result))

    def test_empty_response_fails_and_redirects_are_disabled(self):
        with self.assertRaises(ValueError):
            api.response_text({'choices': [{'message': {'content': None}}]}, None)
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.com'))

    def test_preview_and_guards_need_no_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'plan.json'
            path.write_text(json.dumps(plan()))
            cmd = [sys.executable, str(SCRIPTS / 'team_leader.py'), 'api', 'run', str(path)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('DRY RUN', result.stdout)
            for limit in ('nan', '-1', '0.001'):
                result = subprocess.run(cmd + ['--execute', '--output-dir', str(root / 'output'), '--max-estimated-cost-usd', limit], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((root / 'output').exists())

    def test_cli_mixed_batch_against_local_http_server(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path, dict(self.headers), body))
                if self.path.endswith('/messages'):
                    payload = {'content': [{'type': 'text', 'text': 'claude answer'}]}
                else:
                    payload = {'choices': [{'message': {'content': 'chat answer'}}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate = plan()
                candidate['providers']['vendor']['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
                candidate['providers']['claude'] = {**candidate['providers']['vendor'], 'protocol': 'anthropic'}
                candidate['tasks'].append({**candidate['tasks'][0], 'id': 'second', 'provider': 'claude'})
                candidate['estimated_max_cost_usd'] = 0.02
                (root / 'plan.json').write_text(json.dumps(candidate))
                (root / 'prompt.txt').write_text('supplied context')
                (root / 'keys.env').write_text("VENDOR_KEY='test-secret'\nPATH=bad\n")
                cmd = [sys.executable, str(SCRIPTS / 'team_leader.py'), 'api', 'run', str(root / 'plan.json'), '--execute', '--output-dir', str(root / 'output'), '--env-file', str(root / 'keys.env'), '--max-estimated-cost-usd', '0.02']
                completed = subprocess.run(cmd, cwd='/tmp', capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                results = json.loads((root / 'output/run.json').read_text())['results']
                self.assertEqual([r['response']['text'] for r in results], ['chat answer', 'claude answer'])
                self.assertNotIn('test-secret', (root / 'output/run.json').read_text())
                self.assertEqual(len(received), 2)
                self.assertTrue(all(r[2]['messages'][-1]['content'] == 'supplied context' for r in received))
                again = subprocess.run(cmd, capture_output=True, text=True)
                self.assertNotEqual(again.returncode, 0)
                self.assertEqual(len(received), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
