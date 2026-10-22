import json
from pathlib import Path
from unittest.mock import Mock

from scripts.install import utils


def manifest(tmp_path, pid=123):
    value = dict(pid=pid, port=8005, project=str(tmp_path.resolve()),
                 executable='python.exe', started_at='2026-10-12T12:00:00')
    (tmp_path / utils.LOCK_FILE).write_text(json.dumps(value), encoding='utf-8')
    return value


def test_pid_reuse_cannot_authorize_termination(tmp_path, monkeypatch):
    manifest(tmp_path)
    monkeypatch.setattr(utils, 'get_proj_dir', lambda: str(tmp_path))
    monkeypatch.setattr(utils, '_process_identity', lambda pid: dict(executable='python.exe', started_at='later'))
    runner = Mock()
    monkeypatch.setattr(utils.subprocess, 'run', runner)
    assert utils.read_lock(tmp_path) is None
    assert utils.kill_process(123) is False
    runner.assert_not_called()


def test_old_instance_exit_keeps_new_instance_lock(tmp_path):
    manifest(tmp_path, 456)
    assert utils.remove_lock(tmp_path, expected_pid=123) is False
    assert (tmp_path / utils.LOCK_FILE).is_file()
    assert utils.remove_lock(tmp_path, expected_pid=456) is True


def test_other_project_cannot_authorize_termination(tmp_path, monkeypatch):
    value = manifest(tmp_path)
    value['project'] = str(tmp_path / 'other')
    (tmp_path / utils.LOCK_FILE).write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(utils, '_process_identity', lambda pid: value)
    assert utils.read_lock(tmp_path) is None


def test_port_occupancy_never_kills_process(monkeypatch):
    monkeypatch.setattr(utils, 'port_in_use', lambda port: True)
    runner = Mock()
    monkeypatch.setattr(utils.subprocess, 'run', runner)
    assert utils.kill_port(8005) is False
    runner.assert_not_called()


def test_real_dpapi_session_lock_contains_no_plaintext(tmp_path):
    import os
    import pytest
    if os.name != 'nt':
        pytest.skip('Windows DPAPI')
    token = 'session-secret-' + 'a' * 40
    utils.write_lock(os.getpid(), 18465, tmp_path, session_token=token)
    raw = (tmp_path / utils.LOCK_FILE).read_text(encoding='utf-8')
    assert token not in raw
    assert 'build_id' in json.loads(raw)
    assert utils.owned_browser_url(os.getpid(), 18465, tmp_path).endswith(token)


def test_legacy_lock_cannot_authorize_any_process(tmp_path):
    (tmp_path / utils.LOCK_FILE).write_text('123|8005', encoding='utf-8')
    assert utils.read_lock(tmp_path) is None
    assert utils.remove_lock(tmp_path, expected_pid=123) is False


def test_daemon_cleanup_never_terminates_processes_by_keywords(monkeypatch):
    from interfaces import daemon_manager
    runner = Mock()
    monkeypatch.setattr(daemon_manager.subprocess, 'run', runner)
    daemon_manager.cleanup_orphan_python_processes()
    runner.assert_not_called()
