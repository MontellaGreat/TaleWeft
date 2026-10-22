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
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    base = f'http://127.0.0.1:{port}'

    def request(path, method='GET', payload=None, authenticated=False, origin=None):
        headers = {'Content-Type':'application/json'}
        if authenticated:
            headers['Authorization'] = f'Bearer {token}'
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    logfile = args.data_dir / 'backend-smoke.log'
    children = []
    with logfile.open('wb') as output:
        process = subprocess.Popen([str(args.executable.resolve()), str(port)], cwd=ROOT,
            env={**os.environ, 'PLOTPILOT_SESSION_TOKEN':token,
                 'PLOTPILOT_PROD_DATA_DIR':str(args.data_dir.resolve()),
                 # This smoke covers the packaged API and its owned process
                 # shutdown. The autopilot daemon has a separate lifecycle
                 # and is not needed for the API, auth, or location probes.
                 'DISABLE_AUTO_DAEMON':'1', 'PYTHONIOENCODING':'utf-8'},
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
            novel_id = 'smoke-' + secrets.token_hex(6)
            status, body = request('/api/v1/novels/', 'POST',
                dict(novel_id=novel_id,title='安装包隔离验证',author='Smoke',target_chapters=2), True)
            assert status == 201, (status, body[:300])
            assert request(f'/api/v1/novels/{novel_id}', authenticated=True)[0] == 200
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
