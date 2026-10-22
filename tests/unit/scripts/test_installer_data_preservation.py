import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import pytest

pytest.importorskip('winreg', reason='Windows-only installer verification script')
from scripts.verify_windows_installer import verify_preserved_content
from scripts import verify_windows_installer as installer_qa


def fixture_database(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE novels(id TEXT PRIMARY KEY, title TEXT, slug TEXT)')
        db.execute('CREATE TABLE chapters(id TEXT PRIMARY KEY, novel_id TEXT, number INTEGER, title TEXT, content TEXT)')


def test_only_baseline_can_seed_and_later_missing_chapter_fails_without_repair(tmp_path):
    path = tmp_path / 'plotpilot.db'
    fixture_database(path)
    assert verify_preserved_content(path, '必须保留的稿', seed=True) == hashlib.sha256('必须保留的稿'.encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM chapters')
    with pytest.raises(RuntimeError, match='丢失'):
        verify_preserved_content(path, '必须保留的稿')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM chapters').fetchone()[0] == 0


def test_deleted_database_is_not_recreated_as_a_successful_restore(tmp_path):
    path = tmp_path / 'plotpilot.db'
    with pytest.raises(RuntimeError, match='数据库不存在'):
        verify_preserved_content(path, '原稿')
    assert not path.exists()


def test_changed_content_is_never_replaced_by_the_sentinel(tmp_path):
    path = tmp_path / 'plotpilot.db'
    fixture_database(path)
    verify_preserved_content(path, '原稿', seed=True)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE chapters SET content=?', ('被改变的稿件',))
    with pytest.raises(RuntimeError, match='修改'):
        verify_preserved_content(path, '原稿')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT content FROM chapters').fetchone()[0] == '被改变的稿件'


def test_upgrade_and_rollback_replace_existing_install_before_single_uninstall(tmp_path, monkeypatch):
    """An uninstall between phases would test clean installs instead of upgrades."""
    baseline = tmp_path / 'baseline.exe'
    candidate = tmp_path / 'candidate.exe'
    install = tmp_path / 'out/smoke/windows-installer/installed'
    data = install.parent / 'data'
    calls = []
    current_build = None

    def receipt(path, _product_key):
        return {'identifier': 'com.taleweft.verify.lifecycle',
                'backend_build_id': 'old' if path == baseline else 'new'}

    def run_installer(argv, *, timeout):
        nonlocal current_build
        executable = Path(argv[0])
        calls.append(executable.name)
        if executable.name == 'uninstall.exe':
            assert len(calls) == 5, 'Uninstall must happen after every replacement phase'
            for file in install.rglob('*'):
                if file.is_file():
                    file.unlink()
            return
        if len(calls) > 1:
            assert (install / 'taleweft.exe').is_file(), 'Upgrade needs an existing install'
            assert (install / 'uninstall.exe').is_file()
        current_build = 'old' if executable == baseline else 'new'
        (install / 'plotpilot-backend').mkdir(parents=True, exist_ok=True)
        for file in (install / 'taleweft.exe', install / 'uninstall.exe',
                     install / 'plotpilot-backend/plotpilot-backend.exe'):
            file.touch()
        if len(calls) == 1:
            data.mkdir()
            fixture_database(data / 'plotpilot.db')

    def run_smoke(argv, **kwargs):
        if Path(argv[0]).name == 'taleweft.exe':
            Path(kwargs['env']['PLOTPILOT_DESKTOP_SMOKE_REPORT']).write_text(
                json.dumps({'status': 'passed', 'owned_process_cleanup': True,
                            'build_id': current_build}), encoding='utf-8')

    monkeypatch.setattr(installer_qa, 'ROOT', tmp_path)
    monkeypatch.setattr(installer_qa, 'validate_verification_artifact', receipt)
    monkeypatch.setattr(installer_qa, 'registry_snapshot', lambda _key: [])
    monkeypatch.setattr(installer_qa, 'run_installer', run_installer)
    monkeypatch.setattr(installer_qa.subprocess, 'run', run_smoke)
    monkeypatch.setattr(sys, 'argv', ['verify_windows_installer.py',
        '--baseline', str(baseline), '--candidate', str(candidate),
        '--candidate-build', 'new', '--product-key', 'TaleWeft-Verify-lifecycle'])

    installer_qa.main()

    assert calls == ['baseline.exe', 'candidate.exe', 'baseline.exe', 'candidate.exe', 'uninstall.exe']
    report = json.loads((install.parent / 'installer-result.json').read_text('utf-8'))
    assert [e['phase'] for e in report['events']] == ['baseline', 'upgrade', 'rollback', 'reapply']
    assert len({e['content_sha256'] for e in report['events']}) == 1
    assert report['uninstall_preserves_data']


def test_installer_qa_work_directory_is_workspace_local(tmp_path):
    workspace = installer_qa.ROOT
    selected = installer_qa.verification_work_root(workspace / 'out' / 'isolated-run')
    assert selected == workspace / 'out' / 'isolated-run'
    with pytest.raises(ValueError, match='inside the workspace'):
        installer_qa.verification_work_root(tmp_path / 'outside-workspace')


def test_wait_for_registry_absent_allows_nsis_cleanup_to_settle(monkeypatch):
    states = [[('HKCU', 1, {})], []]
    monkeypatch.setattr(installer_qa, 'registry_snapshot', lambda _key: states.pop(0))
    monkeypatch.setattr(time, 'sleep', lambda _seconds: None)
    assert installer_qa.wait_for_registry_absent('TaleWeft-Verify-test', timeout=0.1)
