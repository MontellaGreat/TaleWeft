# Modified for TaleWeft on 2026-10-22: package the unchanged license and retained notices.
"""Build the real Tauri resource tree with PyInstaller (no user data included)."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PACKAGES = ('application', 'domain', 'infrastructure', 'interfaces', 'engine')


def build_command():
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--name', 'plotpilot-backend', '--paths', str(ROOT),
               '--additional-hooks-dir', str(ROOT / 'scripts' / 'pyinstaller_hooks'),
               '--exclude-module', 'ebooklib',
               '--exclude-module', 'lxml.isoschematron',
               '--distpath', str(ROOT / 'out' / 'tauri'),
               '--workpath', str(ROOT / 'out' / 'pyinstaller' / 'work'),
               '--specpath', str(ROOT / 'out' / 'pyinstaller')]
    # PyInstaller's own package discovery handles dynamic composition-root imports.
    # Collect only source packages and explicitly allowlisted runtime resources.
    for package in PACKAGES:
        command += ['--collect-submodules', package]
        for path in sorted((ROOT / package).rglob('*')):
            if path.is_file() and path.suffix in {'.sql', '.yaml', '.yml', '.json', '.md', '.jinja2', '.j2'}:
                if '__pycache__' not in path.parts:
                    command += ['--add-data', f'{path}:{path.parent.relative_to(ROOT).as_posix()}']
    for relative in ('release-manifest.json', 'LICENSE', 'NOTICE', 'CONTRIBUTORS.md',
                     'MODIFICATIONS.md', 'THIRD_PARTY_NOTICES', 'config', 'shared', 'frontend/dist'):
        path = ROOT / relative
        if not path.exists():
            raise RuntimeError(f'Missing bundle resource: {relative}; run frontend build first')
        target = '.' if path.is_file() else relative
        command += ['--add-data', f'{path}:{target}']
    command += ['--collect-submodules', 'uvicorn', '--hidden-import', 'uvicorn.logging',
                str(ROOT / 'scripts' / 'backend_entry.py')]
    return command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check the existing resource manifest')
    args = parser.parse_args()
    if args.check:
        executable = ROOT / 'out/tauri/plotpilot-backend' / ('plotpilot-backend.exe' if sys.platform == 'win32' else 'plotpilot-backend')
        if not executable.is_file():
            raise SystemExit('Backend executable missing; run this script without --check')
        return
    # Passing hundreds of explicit resources on Windows exceeds CreateProcess'
    # command-line limit; use PyInstaller's public Python entry point.
    from PyInstaller.__main__ import run
    run(build_command()[3:])


if __name__ == '__main__':
    main()
