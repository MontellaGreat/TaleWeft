# Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
"""Real NSIS build replacement/rollback in a workspace-owned install and data dir."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import winreg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_verification_installer import validate_verification_artifact


def verification_work_root(requested=None):
    work = Path(requested).resolve() if requested else ROOT / 'out/smoke/windows-installer'
    if not work.is_relative_to(ROOT.resolve()):
        raise ValueError('Installer verification work directory must stay inside the workspace')
    return work


def run_installer(argv, *, timeout):
    """Run a test installer without elevation or shell indirection.

    UAC changes the token, registry view and user profile, which can make a
    supposedly isolated test silently target a production installation.
    """
    try:
        return subprocess.run(argv, check=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        if getattr(exc, 'winerror', None) != 740:
            raise
        # Some NSIS builds retain a requireAdministrator PE manifest even
        # when the bundle install mode is currentUser. Let ShellExecute apply
        # that manifest, still with the explicitly isolated identity/path.
        ps = [
            'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            '$p=Start-Process -FilePath $args[0] -ArgumentList $args[1..($args.Length-1)] -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode',
            '--', *argv,
        ]
        return subprocess.run(ps, check=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)


def registry_snapshot(product_key):
    """Return whether either registry hive contains the verification identity."""
    path = fr'Software\Microsoft\Windows\CurrentVersion\Uninstall\{product_key}'
    found = []
    for hive_name, hive in (('HKCU', winreg.HKEY_CURRENT_USER),
                            ('HKLM', winreg.HKEY_LOCAL_MACHINE)):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                    values = {winreg.EnumValue(key, i)[0]: winreg.EnumValue(key, i)[1]
                              for i in range(winreg.QueryInfoKey(key)[1])}
                    found.append((hive_name, view, values))
            except FileNotFoundError:
                pass
    return found


def wait_for_registry_absent(product_key, *, timeout=10.0, interval=0.1):
    """Wait briefly for NSIS registry deletion to become observable.

    NSIS can return from the silent uninstaller just before the registry view
    reflects its final DeleteRegKey operation. A bounded poll keeps the QA
    assertion deterministic without treating a lingering identity as success.
    """
    deadline = time.monotonic() + timeout
    while True:
        if not registry_snapshot(product_key):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def verify_preserved_content(db_path, sentinel, *, seed=False):
    """Seed only the baseline; subsequent checks must never repair missing data."""
    if not db_path.is_file():
        raise RuntimeError('安装数据库不存在，禁止创建空库替代保留验证')
    uri = db_path.resolve().as_uri() + ('?mode=rw' if seed else '?mode=ro')
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute("SELECT content FROM chapters WHERE id='installer-sentinel'").fetchone()
        if row is None:
            if not seed:
                raise RuntimeError('安装/回滚/卸载丢失了保留稿')
            db.execute("INSERT OR IGNORE INTO novels(id,title,slug) VALUES('installer-sentinel','安装升级保留验证','installer-sentinel')")
            db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('installer-sentinel','installer-sentinel',1,'原稿',?)", (sentinel,))
            row = (sentinel,)
        if row[0] != sentinel:
            raise RuntimeError('安装/回滚修改了保留稿')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    return hashlib.sha256(row[0].encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--candidate-build', required=True)
    parser.add_argument('--product-key', required=True,
                        help='verification-only NSIS product name; never use a production identity')
    parser.add_argument('--work-dir', type=Path,
                        help='unique workspace-local directory for this isolated installer run')
    args = parser.parse_args()
    baseline = validate_verification_artifact(args.baseline.resolve(), args.product_key)
    candidate = validate_verification_artifact(args.candidate.resolve(), args.product_key)
    if baseline['identifier'] != candidate['identifier']:
        raise RuntimeError('Baseline and candidate must share the same isolated bundle identity')
    if candidate['backend_build_id'] != args.candidate_build:
        raise RuntimeError('Candidate build does not match its verification receipt')
    work = verification_work_root(args.work_dir)
    install = work / 'installed'
    data = work / 'data'
    ownership = work / 'ownership.json'
    work.mkdir(parents=True, exist_ok=True)
    assert install.resolve().is_relative_to(ROOT.resolve())
    if not args.product_key.startswith(('TaleWeft-Verify-', 'PlotPilot-Verify-')):
        raise RuntimeError('--product-key must be a verification-only identity')
    if registry_snapshot(args.product_key):
        raise RuntimeError(f'验证身份 {args.product_key!r} 已存在注册表项；请使用全新身份或清理本验证遗留项')
    if install.exists() and not ownership.exists():
        raise RuntimeError('隔离安装目录已存在且没有本验证的所有权记录')
    ownership.write_text(json.dumps({'install':str(install.resolve()),'data':str(data.resolve())}),encoding='utf-8')
    events = []
    db_path = data / 'plotpilot.db'
    sentinel = '验证稿：青石阶上的雨。\n原稿换行与空格  保留。'

    def verify_content(*, seed=False):
        # application.paths has a canonical DATABASE_PATH; discover only this
        # task's isolated data tree, never the user's AppData.
        nonlocal db_path
        databases = [path for name in ('plotpilot.db', 'aitext.db') if (path := data / name).is_file()]
        if len(databases) != 1:
            raise RuntimeError('安装后没有创建隔离数据库')
        db_path = databases[0]
        return verify_preserved_content(db_path, sentinel, seed=seed)

    sequence = [('baseline',args.baseline,baseline['backend_build_id']),
                ('upgrade',args.candidate,args.candidate_build),
                ('rollback',args.baseline,baseline['backend_build_id']),
                ('reapply',args.candidate,args.candidate_build)]
    for phase, installer, expected in sequence:
        run_installer([str(installer.resolve()),'/S','/CURRENTUSER','/NS',f'/D={install.resolve()}'],
                      timeout=240)
        backend = install / 'plotpilot-backend/plotpilot-backend.exe'
        # Historical rollback payloads retain their original executable name.
        app_exes = [install/name for name in ('taleweft.exe', 'plotpilot.exe') if (install/name).is_file()]
        assert backend.is_file() and len(app_exes) == 1
        subprocess.run([sys.executable,str(ROOT/'scripts/verify_backend_smoke.py'),
            '--executable',str(backend),'--data-dir',str(data),'--expected-build',expected],
            check=True,timeout=100,creationflags=subprocess.CREATE_NO_WINDOW)
        digest = verify_content(seed=phase == 'baseline')
        if phase in ('upgrade','reapply'):
            report = work / f'desktop-{phase}.json'
            subprocess.run([str(app_exes[0])], env={**os.environ,
                'PLOTPILOT_PROD_DATA_DIR':str(data.resolve()),
                'PLOTPILOT_DESKTOP_SMOKE_REPORT':str(report.resolve())},
                check=True,timeout=160,creationflags=subprocess.CREATE_NO_WINDOW)
            desktop = json.loads(report.read_text(encoding='utf-8'))
            assert desktop['status'] == 'passed' and desktop['owned_process_cleanup']
            assert desktop['build_id'] == expected
            assert verify_content() == digest, '桌面首次启动修改了保留稿'
        events.append({'phase':phase,'installer':str(installer.resolve()),'build_id':expected,
                       'content_sha256':digest,'integrity':'ok','foreign_keys':'ok'})
    # Remove only this test's registered installation through its own uninstaller.
    run_installer([str(install/'uninstall.exe'),'/S','/CURRENTUSER'], timeout=120)
    if not wait_for_registry_absent(args.product_key):
        raise RuntimeError('卸载后验证身份注册表项仍存在')
    app_residue = [path for path in install.rglob('*') if path.is_file()
                   and path.name not in {'ownership.json'}]
    if app_residue:
        raise RuntimeError(f'卸载后安装目录仍有程序文件: {[str(path) for path in app_residue[:5]]}')
    assert verify_content() == events[-1]['content_sha256']
    scope = ('same-build reinstall and data preservation'
             if baseline['backend_build_id'] == candidate['backend_build_id']
             else 'development build replacement and rollback; no schema downgrade guarantee')
    result={'status':'passed','scope':scope,
            'installation_directory':str(install.resolve()),'data_directory':str(data.resolve()),
            'events':events,'uninstall_preserves_data':True}
    (work/'installer-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
