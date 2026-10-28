"""Exercise a real packaged backend using an isolated project-local data directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def normalize_process_rows(value):
    return [value] if isinstance(value, dict) else value


def capture_owned_children(parent_pid: int):
    """Hold handles to this launch's descendants, never authorize cleanup by name."""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes
    result = subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress'
    ], capture_output=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW)
    processes = json.loads(result.stdout.decode('utf-8-sig'))
    processes = normalize_process_rows(processes)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    owned = {parent_pid}
    children = []
    while True:
        discovered = {int(row['ProcessId']) for row in processes
                      if int(row['ParentProcessId']) in owned} - owned
        if not discovered:
            break
        owned.update(discovered)
        for pid in discovered:
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                children.append((pid, handle))
            elif ctypes.get_last_error() != 87:  # process already exited
                raise ctypes.WinError(ctypes.get_last_error())
    return children


def assert_children_exited(children, timeout=5):
    if not children:
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    deadline = time.monotonic() + timeout
    while True:
        alive = []
        for pid, handle in children:
            state = kernel.WaitForSingleObject(handle, 0)
            if state == 258:
                alive.append(pid)
            elif state != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        if not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Backend exited but owned child processes remain: {alive}')
        time.sleep(0.05)


def close_child_handles(children):
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for _, handle in children:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, default=ROOT / 'out/tauri/plotpilot-backend/plotpilot-backend.exe')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'out/smoke/backend-data')
    parser.add_argument('--expected-build')
    parser.add_argument('--local-ai', action='store_true', help='Require prepared local dependencies and exercise offline semantic search')
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    base = f'http://127.0.0.1:{port}'

    def request(path, method='GET', payload=None, authenticated=False, origin=None, timeout=3):
        headers = {'Content-Type':'application/json'}
        if authenticated:
            headers['Authorization'] = f'Bearer {token}'
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    logfile = args.data_dir / 'backend-smoke.log'
    children = []
    with logfile.open('wb') as output:
        smoke_env = {**os.environ, 'PLOTPILOT_SESSION_TOKEN':token,
                 'PLOTPILOT_PROD_DATA_DIR':str(args.data_dir.resolve()),
                 # This smoke covers the packaged API and its owned process
                 # shutdown. The autopilot daemon has a separate lifecycle
                 # and is not needed for the API, auth, or location probes.
                 'DISABLE_AUTO_DAEMON':'1', 'PYTHONIOENCODING':'utf-8'}
        if args.local_ai:
            smoke_env.update(EMBEDDING_SERVICE='local', EMBEDDING_USE_GPU='false',
                             EMBEDDING_MODEL_PATH=str((args.data_dir / 'models/bge-small-zh-v1.5').resolve()),
                             HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', VECTOR_STORE_ENABLED='true')
        process = subprocess.Popen([str(args.executable.resolve()), str(port)], cwd=ROOT,
            env=smoke_env,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'Backend exited {process.returncode}; see {logfile}')
                try:
                    if request('/health')[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError(f'Backend readiness timeout; see {logfile}')
            health = json.loads(request('/health')[1])
            children = capture_owned_children(process.pid)
            if args.expected_build:
                assert health['build_id'] == args.expected_build, health
            assert request('/api/v1/novels/')[0] == 401
            assert request('/internal/shutdown', 'POST', {})[0] == 401
            assert request('/api/v1/novels/', authenticated=True, origin='https://untrusted.example')[0] == 403
            assert request('/api/v1/novels/', authenticated=True)[0] == 200
            status, body = request('/api/v1/settings/model-readiness', authenticated=True)
            assert status == 200, (status, body[:300])
            readiness = json.loads(body)
            assert set(readiness) == {'ready', 'llm', 'embedding'}, readiness
            assert isinstance(readiness['ready'], bool), readiness
            assert readiness['ready'] == (readiness['llm']['ready'] and readiness['embedding']['ready']), readiness
            assert set(readiness['llm']) == {'ready', 'reasons'}, readiness
            assert set(readiness['embedding']) == {'ready', 'reasons', 'mode'}, readiness
            status, body = request('/api/v1/system/extensions-status', authenticated=True)
            assert status == 200, (status, body[:300])
            extensions = json.loads(body)
            assert isinstance(extensions['model_ready'], bool), extensions
            assert isinstance(extensions['all_installed'], bool), extensions
            assert extensions['local_ready'] == (extensions['model_ready'] and extensions['all_installed']), extensions
            assert not extensions['install_running'], extensions
            if args.local_ai:
                deadline = time.monotonic() + 180
                while extensions.get('checking') and time.monotonic() < deadline:
                    time.sleep(0.2)
                    status, body = request('/api/v1/system/extensions-status', authenticated=True)
                    assert status == 200, (status, body[:300])
                    extensions = json.loads(body)
                assert extensions['local_ready'], extensions
                status, body = request('/api/v1/system/install-extensions', 'POST', authenticated=True, timeout=30)
                assert status == 200, (status, body[:600])
                events = [json.loads(line[6:]) for line in body.decode('utf-8').splitlines() if line.startswith('data: ')]
                assert events and events[-1]['type'] == 'done', events
                assert events[-1]['success'] and events[-1]['local_ready'], events[-1]
            novel_id = 'smoke-' + secrets.token_hex(6)
            status, body = request('/api/v1/novels/', 'POST',
                dict(novel_id=novel_id,title='安装包隔离验证',author='Smoke',target_chapters=2), True)
            assert status == 201, (status, body[:300])
            assert request(f'/api/v1/novels/{novel_id}', authenticated=True)[0] == 200
            if args.local_ai:
                status, body = request(f'/api/v1/knowledge-graph/novels/{novel_id}/search?query=city-gate',
                                       'POST', authenticated=True, timeout=120)
                assert status == 200, (status, body[:600])
                assert json.loads(body)['success'] is True, body
            status, body = request(f'/api/v1/dag/{novel_id}/setup', authenticated=True)
            assert status == 200, (status, body[:300])
            setup = json.loads(body)
            assert [stage['id'] for stage in setup['stages']] == [
                'worldbuilding', 'characters', 'locations', 'plot_outline',
            ], setup
            assert setup['active_run'] is None, setup
            setup_node_counts = {stage['id']: len(stage['graph']['nodes']) for stage in setup['stages']}
            assert setup_node_counts == {'worldbuilding': 48, 'characters': 7, 'locations': 7, 'plot_outline': 7}, setup_node_counts
            for stage in setup['stages']:
                nodes = stage['graph']['nodes']
                units = {}
                for node in nodes:
                    unit = node['config']['prompt_variables']['unit']
                    units.setdefault(unit, []).append(node['config']['prompt_variables']['action'])
                for unit, actions in units.items():
                    expected = ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
                    if unit == 'world_review':
                        expected.remove('generate')
                    assert actions == expected, (stage['id'], unit, actions)
            bible_path = f'/api/v1/bible/novels/{novel_id}/bible'
            assert request(bible_path, authenticated=True)[0] == 200
            bible_payload = dict(characters=[], world_settings=[], timeline_notes=[], style_notes=[], locations=[
                dict(id='capital', name='都城', description='主城', location_type='城市', parent_id=None),
                dict(id='palace', name='王宫', description='城内', location_type='建筑', parent_id='capital'),
            ])
            status, body = request(bible_path, 'PUT', bible_payload, True)
            assert status == 200, (status, body[:300])
            status, body = request(bible_path, authenticated=True)
            assert status == 200, (status, body[:300])
            assert {row['id']: row.get('parent_id') for row in json.loads(body)['locations']} == {
                'capital': None, 'palace': 'capital',
            }
            assert request('/internal/shutdown', 'POST', {}, True)[0] == 200
            process.wait(timeout=15)
            assert process.returncode == 0, process.returncode
            late_children = capture_owned_children(process.pid)
            known = {pid for pid, _ in children}
            children.extend((pid, handle) for pid, handle in late_children if pid not in known)
            assert_children_exited(children)
            report = dict(status='passed', executable=str(args.executable.resolve()),
                          release=health['release'], build_id=health['build_id'],
                          authenticated_api=True, unauthenticated_rejected=True,
                          untrusted_origin_rejected=True, sqlite_create_read=True,
                          location_hierarchy_create_read=True,
                          setup_dag_routes_and_graphs=True,
                          setup_node_counts=setup_node_counts,
                          readonly_model_readiness=True, local_extension_status=True,
                          offline_local_semantic_search=args.local_ai,
                          prepared_local_install_sse=args.local_ai,
                          graceful_shutdown=True, owned_children_exited=True,
                          observed_child_pids=[pid for pid, _ in children],
                          data_directory=str(args.data_dir.resolve()))
            (args.data_dir / 'smoke-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            close_child_handles(children)


if __name__ == '__main__':
    main()
