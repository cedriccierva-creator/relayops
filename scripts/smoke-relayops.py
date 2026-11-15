"""Production smoke test against temporary SQLite and a local mock destination.

Run with the backend venv Python after building the frontend; all services stop
in finally, and existing services on ports 8000/3000 are never used.
"""
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
FRONTEND = Path(os.environ.get("RELAYOPS_SMOKE_FRONTEND", ROOT / "frontend"))
def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]

api_port = free_port()
frontend_port = free_port()

state = {'requests': [], 'paths': [], 'status': 503}
class MockDestination(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        state['requests'].append((dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
        state['paths'].append(self.path)
        self.send_response(state['status'])
        self.send_header('Content-Type', 'application/json')
        self.end_headers()

mock = http.server.ThreadingHTTPServer(('127.0.0.1', 0), MockDestination)
threading.Thread(target=mock.serve_forever, daemon=True).start()
processes = []

def wait_for(client, path, predicate, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            response = client.get(path)
            if predicate(response):
                return response
        except httpx.HTTPError:
            pass
        time.sleep(.15)
    raise AssertionError(f'Timed out waiting for {path}')

try:
    with tempfile.TemporaryDirectory(prefix='relayops-smoke-') as temp:
        env = {**os.environ, 'SECRET_KEY': secrets.token_urlsafe(48),
               'DATABASE_URL': f'sqlite:///{temp}/smoke.db', 'ENVIRONMENT': 'development',
               'DEV_ALLOWED_ORIGINS': json.dumps([f'http://127.0.0.1:{mock.server_port}']),
               'DELIVERY_MAX_ATTEMPTS': '2', 'RETRY_BASE_SECONDS': '1', 'DELIVERY_TIMEOUT_SECONDS': '2',
               'REGISTRATION_ENABLED': 'true', 'COOKIE_SECURE': 'false', 'RELAYOPS_API_PORT': str(api_port), 'DASHBOARD_ORIGINS': json.dumps([f'http://127.0.0.1:{frontend_port}'])}
        with open(f'{temp}/services.log', 'w') as log:
            processes.append(subprocess.Popen([PYTHON, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(api_port), '--no-access-log'], cwd=ROOT / 'backend', env=env, stdout=log, stderr=log))
            with httpx.Client(base_url=f'http://127.0.0.1:{api_port}', timeout=5) as backend:
                wait_for(backend, '/api/health', lambda r: r.status_code == 200)
            processes.append(subprocess.Popen(['node', 'node_modules/next/dist/bin/next', 'start', '--hostname', '127.0.0.1', '--port', str(frontend_port)], cwd=FRONTEND, env=env, stdout=log, stderr=log))
            processes.append(subprocess.Popen([PYTHON, 'worker.py'], cwd=ROOT / 'backend', env=env, stdout=log, stderr=log))
            with httpx.Client(base_url=f'http://127.0.0.1:{frontend_port}', timeout=10) as client:
                home = wait_for(client, '/', lambda r: r.status_code == 200)
                assert 'RelayOps' in home.text and 'Study Sprint' not in home.text
                assert client.get('/api/events').status_code == 401
                credentials = {'email': 'smoke@example.com', 'password': secrets.token_urlsafe(24)}
                assert client.post('/api/auth/register', json=credentials).status_code == 201
                login = client.post('/api/auth/login', json=credentials)
                assert login.status_code == 200 and 'httponly' in login.headers['set-cookie'].lower()
                assert client.get('/api/auth/me').status_code == 200
                secret = secrets.token_urlsafe(48)
                source_res = client.post('/api/sources', json={'name': 'Smoke source', 'secret': secret})
                assert source_res.status_code == 201 and secret not in source_res.text
                source_id = source_res.json()['id']
                destination = client.post('/api/destinations', json={'source_id': source_id, 'name': 'Mock n8n', 'url': f'http://127.0.0.1:{mock.server_port}/webhook/orders'})
                assert destination.status_code == 201, destination.status_code
                hook_headers = {'X-Webhook-Secret': secret, 'Idempotency-Key': 'smoke-001'}
                ingested = client.post(f'/api/hooks/{source_id}', json={'order': 123, 'token': 'redact-me'}, headers=hook_headers)
                assert ingested.status_code == 202
                event_id = ingested.json()['event_id']
                assert client.post(f'/api/hooks/{source_id}', json={'order': 123, 'token': 'redact-me'}, headers=hook_headers).status_code == 200
                detail = wait_for(client, f'/api/events/{event_id}', lambda r: r.status_code == 200 and r.json()['status'] == 'failed')
                assert detail.json()['payload']['token'] == '[REDACTED]'
                job = detail.json()['jobs'][0]
                attempts = client.get(f'/api/deliveries/{job["id"]}/attempts').json()
                assert len(attempts) == 2 and all(a['status_code'] == 503 for a in attempts)
                diagnostics = subprocess.run([PYTHON, 'diagnostics.py'], cwd=ROOT / 'backend',
                                             env=env, capture_output=True, text=True, timeout=10)
                assert diagnostics.returncode == 0, diagnostics.stdout
                operations = json.loads(diagnostics.stdout)
                assert operations['worker']['status'] == 'healthy'
                assert operations['deliveries']['failed'] == 1
                assert operations['deliveries']['outstanding'] == 0
                state['status'] = 204
                assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 202
                detail = wait_for(client, f'/api/events/{event_id}', lambda r: r.status_code == 200 and r.json()['status'] == 'delivered')
                assert detail.json()['jobs'][0]['attempts_total'] == 3
                assert client.get('/api/stats').json()['delivered'] == 1
                assert len(client.get('/api/events?limit=1&offset=0').json()) == 1
                assert client.get('/api/events?limit=1&offset=1').json() == []
                assert len(state['requests']) == 3
                for headers, payload in state['requests']:
                    assert headers['Idempotency-Key'] == job['id']
                    assert 'X-Webhook-Secret' not in headers and 'Cookie' not in headers
                    assert json.loads(payload)['token'] == 'redact-me'
                # Phase 2: permanent failure, safe edits, preserved history and replay.
                state['status'] = 404
                permanent = client.post(f'/api/hooks/{source_id}', json={'order': 'permanent'}, headers={'X-Webhook-Secret': secret})
                assert permanent.status_code == 202
                permanent_id = permanent.json()['event_id']
                failed = wait_for(client, f'/api/events/{permanent_id}', lambda r: r.status_code == 200 and r.json()['status'] == 'failed').json()['jobs'][0]
                assert failed['attempts_total'] == 1
                history_path = f'/api/deliveries/{failed["id"]}/attempts'
                old_history = client.get(history_path).json()
                assert old_history[0]['error'] == 'permanent_http_error'
                destination_path = f'/api/destinations/{destination.json()["id"]}'
                assert client.patch(destination_path, json={'url': 'http://169.254.169.254/'}).status_code == 422
                updated = client.patch(destination_path, json={'name': 'Corrected mock n8n', 'url': f'http://127.0.0.1:{mock.server_port}/webhook/fixed', 'enabled': False})
                assert updated.status_code == 200 and updated.json()['id'] == destination.json()['id']
                assert client.get(history_path).json() == old_history
                assert client.post(f'/api/deliveries/{failed["id"]}/retry').status_code == 409
                assert client.patch(destination_path, json={'enabled': True}).status_code == 200
                state['status'] = 200
                assert client.post(f'/api/deliveries/{failed["id"]}/retry').status_code == 202
                delivered = wait_for(client, f'/api/events/{permanent_id}', lambda r: r.status_code == 200 and r.json()['status'] == 'delivered').json()['jobs'][0]
                assert delivered['attempts_total'] == 2 and delivered['cycle_attempts'] == 1
                assert state['paths'][-1] == '/webhook/fixed'
                assert client.get(history_path).json()[1] == old_history[0]
                # Restart only the temporary API to verify registration disabled with existing accounts.
                processes[0].terminate()
                processes[0].wait(timeout=10)
                env['REGISTRATION_ENABLED'] = 'false'
                processes.append(subprocess.Popen([PYTHON, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(api_port), '--no-access-log'], cwd=ROOT / 'backend', env=env, stdout=log, stderr=log))
                wait_for(client, '/api/auth/policy', lambda r: r.status_code == 200 and r.json() == {'registration_enabled': False})
                assert client.post('/api/auth/register', json={'email': 'blocked@example.com', 'password': 'test-password-only'}).status_code == 403
                assert client.get('/api/auth/me').status_code == 200
                assert client.post('/api/auth/login', json=credentials).status_code == 200
                assert client.post('/api/auth/logout').status_code == 200
                assert client.get('/api/events').status_code == 401
                print('Production smoke passed: page, cookie proxy, source/destination setup, ingestion, deduplication, retry exhaustion, permanent failure, destination editing, SSRF rejection, manual replay, history, registration restriction, existing login, pagination, redaction, logout.')
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    mock.shutdown()
    mock.server_close()
    print('All temporary services stopped.')
