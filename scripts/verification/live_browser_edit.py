"""Real Vue/Playwright edit acceptance using a COPY of a two-chapter live run.

No mocks, intercepted routes, alternate frontend harness, or author database
writes. Reuses the project's installed open-source Vite and Playwright. Dry-run
does not read credentials, start services, launch browsers, or call models.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_novel_fullflow import ROOT, CheckFailed, Client, read_json, readonly_db, require, write_json

VERIFICATION = ROOT / 'out' / 'verification'
AUTOMATIC_SOURCES = "('chapter_extract','dialogue_extract','triple_extract','causal_extract')"


def validate_paths(source, output):
    source, output = source.resolve(), output.resolve()
    require(source.is_relative_to(VERIFICATION) and source != VERIFICATION,
        'Source must be an isolated run below project out/verification')
    require(output.is_relative_to(VERIFICATION) and output != VERIFICATION,
        'Browser evidence must be below project out/verification')
    require(not output.is_relative_to(source) and not source.is_relative_to(output),
        'Source and browser output must use separate sibling directories')
    return source, output


def copy_live_run(source, output):
    """Back up a completed isolated database; never copy the author data path."""
    state = read_json(source / 'checkpoint.json', {})
    require({'chapter_1', 'chapter_2'}.issubset(state.get('passed') or []),
        'Source must have genuinely generated and saved two live chapters')
    novel = state.get('novel_id', '')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]+', novel)), 'Source has no valid isolated novel ID')
    source_db, target_db = source / 'data' / 'plotpilot.db', output / 'data' / 'plotpilot.db'
    require(source_db.is_file(), 'Source live-run database is missing')
    require(not output.exists(), 'Choose a fresh browser evidence directory')
    with readonly_db(source_db) as original:
        novels = original.execute('SELECT id FROM novels').fetchall()
        require(len(novels) == 1 and novels[0]['id'] == novel,
            'Source database must contain only the isolated test novel')
        chapters = original.execute('SELECT number,content,revision FROM chapters WHERE novel_id=? AND number IN (1,2)', (novel,)).fetchall()
        require(len(chapters) == 2 and all(row['content'].strip() and row['revision'] > 0 for row in chapters),
            'Source lacks two saved nonempty chapters')
        jobs = original.execute('SELECT j.status,j.cascade_status FROM chapter_derivation_jobs j JOIN chapters c ON c.id=j.chapter_id AND c.revision=j.revision WHERE j.novel_id=? AND c.number IN (1,2)', (novel,)).fetchall()
        require(len(jobs) == 2 and all(row['status'] == 'completed' and row['cascade_status'] in ('completed', 'not_requested') for row in jobs),
            'Wait until both source chapter derivations and cascades have completed')
        unfinished = original.execute("SELECT COUNT(*) FROM chapter_derivation_jobs WHERE status IN ('pending','running','unavailable') OR (status='completed' AND cascade_status IN ('running','unavailable'))").fetchone()[0]
        require(not unfinished, 'Source contains recoverable jobs; finish them before browser acceptance')
        baseline_atoms = original.execute(f"SELECT id FROM memory_atoms WHERE novel_id=? AND chapter_number=1 AND status='candidate' AND source IN {AUTOMATIC_SOURCES}", (novel,)).fetchall()
        output.mkdir(parents=True)
        target_db.parent.mkdir()
        with sqlite3.connect(target_db) as target:
            original.backup(target)
    source_vault = source / 'data' / '.credentials'
    if source_vault.is_dir():
        # Ciphertext stays in the isolated data directory and never enters QA JSON.
        shutil.copytree(source_vault, target_db.parent / '.credentials')
    return novel, [row['id'] for row in baseline_atoms]


def inspect_derived_data(database, novel, previous_atom_ids):
    with readonly_db(database) as connection:
        atoms = [dict(row) for row in connection.execute(f"SELECT id,chapter_number,status,payload_json,text_span FROM memory_atoms WHERE novel_id=? AND chapter_number IN (1,2) AND source IN {AUTOMATIC_SOURCES}", (novel,))]
        first_active = [row for row in atoms if row['chapter_number'] == 1 and row['status'] in ('candidate', 'confirmed')]
        first_text = json.dumps([{'payload': row['payload_json'], 'text_span': row['text_span']} for row in first_active], ensure_ascii=False)
        snapshots = connection.execute('SELECT bundle_json FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=1', (novel,)).fetchall()
        extraction = ''.join(row['bundle_json'] for row in snapshots)
        require(bool(first_active), 'Edited chapter has no active automatic memory')
        require('一百零七' in first_text + extraction or re.search(r'(?<!\d)107(?!\d)', first_text + extraction),
            'Browser edit fact is missing from regenerated chapter memory and extraction')
        require('七十四' not in first_text and '九十二' not in first_text
                and not re.search(r'(?<!\d)(?:74|92)(?!\d)', first_text),
            'Edited chapter still has obsolete numbered-key facts in active automatic memory')
        previous = set(previous_atom_ids)
        superseded = [row for row in atoms if row['id'] in previous and row['status'] == 'superseded']
        require(not previous or bool(superseded), 'No prior automatic evidence was superseded after the browser edit')
        title_rows = connection.execute("SELECT title FROM story_nodes WHERE novel_id=? AND node_type='chapter' AND number=1", (novel,)).fetchall()
        require(title_rows and all(row['title'] == '浏览器验收·一百零七号钥匙' for row in title_rows),
            'Browser title edit did not propagate to outline chapter nodes')
        open_conflicts = connection.execute("SELECT conflict_type FROM chapter_evolution_conflicts WHERE novel_id=? AND branch_id='main' AND chapter_number>=2 AND resolution_status='open'", (novel,)).fetchall()
        second_prose = connection.execute('SELECT content FROM chapters WHERE novel_id=? AND number=2', (novel,)).fetchone()['content']
        old_second_prose = any(marker in second_prose for marker in ('七十四', '九十二')) or bool(re.search(r'(?<!\d)(?:74|92)(?!\d)', second_prose))
        second_active = [row for row in atoms if row['chapter_number'] == 2 and row['status'] in ('candidate', 'confirmed')]
        second_text = json.dumps([{'payload': row['payload_json'], 'text_span': row['text_span']} for row in second_active], ensure_ascii=False)
        old_second_memory = any(marker in second_text for marker in ('七十四', '九十二')) or bool(re.search(r'(?<!\d)(?:74|92)(?!\d)', second_text))
    return {
        'chapter_1_new_fact_in_derived_data': True,
        'chapter_1_active_automatic_memory_has_old_number': False,
        'chapter_1_superseded_old_atoms': len(superseded),
        'outline_title_changed': True,
        'downstream_semantics': {
            'chapter_2_original_prose_mentions_previous_number': old_second_prose,
            'chapter_2_active_automatic_memory_mentions_previous_number': old_second_memory,
            'open_evolution_conflicts': len(open_conflicts),
            'open_evolution_conflict_types': sorted({row['conflict_type'] for row in open_conflicts}),
            'automatic_semantic_correction_verified': False,
            'full_book_consistency': 'requires_author_review' if old_second_prose or old_second_memory or open_conflicts else 'not_established_by_reanalysis',
        },
    }


def setup_registered_prop_baseline(client, database, novel):
    """Declare the registered-prop scenario through the real public API.

    This setup is explicitly separate from UI actions and is not automatic
    discovery. SQLite is read only; no chapter or derived state is seeded.
    """
    with readonly_db(database) as connection:
        connection.execute('BEGIN')
        chapter = connection.execute('SELECT content FROM chapters WHERE novel_id=? AND number=1', (novel,)).fetchone()
        require(chapter and '钥匙现在由沈舟独自保管' in chapter['content'][-400:],
            'Registered-prop baseline requires the source first chapter to end with Shen Zhou holding the key')
        characters = connection.execute('SELECT id FROM unified_characters WHERE novel_id=? AND name=? ORDER BY COALESCE(last_updated_chapter,0) DESC,id', (novel, '沈舟')).fetchall()
        require(bool(characters), 'Registered-prop baseline has no actual unified character for Shen Zhou')
        holder = characters[0]['id']
    route = '/api/v1/novels/' + novel + '/props'
    props = client.json(route)
    existing = [prop for prop in props if prop.get('name') == '验潮钥匙']
    require(len(existing) <= 1, 'Registered-prop baseline has ambiguous duplicate key records')
    writes = []
    if existing:
        prop = existing[0]
        mode = 'reused_existing_registered_prop'
        if prop.get('holder_character_id') != holder or prop.get('introduced_chapter') != 1:
            prop = client.json(route + '/' + prop['id'], 'PATCH', {'holder_character_id': holder, 'introduced_chapter': 1})
            writes.append('PATCH existing prop holder and introduced chapter')
    else:
        prop = client.json(route, 'POST', {'name': '验潮钥匙', 'description': '用于检修验潮井的钥匙。',
            'prop_category': 'TOOL', 'holder_character_id': holder, 'introduced_chapter': 1})
        mode = 'created_registered_prop'
        writes.append('POST new registered prop')
    reread = client.json(route + '/' + prop['id'])
    require(reread.get('holder_character_id') == holder and reread.get('introduced_chapter') == 1,
        'Public API did not preserve the declared registered-prop baseline')
    return {'scenario': 'registered_prop_lifecycle', 'mode': mode, 'public_API_writes': writes,
        'prop_id': prop['id'], 'name': '验潮钥匙', 'baseline_holder': '沈舟', 'baseline_holder_character_id': holder,
        'introduced_chapter': 1, 'automatic_prop_discovery_tested': False, 'database_writes_by_driver': False,
        'chapter_edits_and_rerun_use_UI': True, 'model_calls_by_setup': 0,
        'unregistered_items_scope': 'Unregistered named items can be represented in evolution state. Automatic catalogue registration is not established by this baseline setup.'}


def free_port():
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        return reserve.getsockname()[1]


def wait_http(url, process, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require(process.poll() is None, 'Owned local service exited before readiness; inspect its project-local log')
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise CheckFailed('Owned local service did not become ready')


def stop_owned(process):
    if process is None or process.poll() is not None:
        return
    if os.name == 'nt':
        # Only the exact Popen PID and its children, never a shared process name.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=VERIFICATION / ('live-browser-' + time.strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--browser-executable', type=Path)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--stage-timeout', type=int, default=1200)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    source, output = validate_paths(args.source_run, args.output_dir)
    require(args.stage_timeout >= 60, 'Stage timeout must be at least 60 seconds')
    if args.dry_run:
        print(json.dumps({'status': 'dry_run_only_NOT_live_acceptance', 'source_database_opened': False,
            'model_calls': 0, 'services_started': 0, 'browsers_launched': 0,
            'stages': ['copy_completed_isolated_run', 'public_API_baseline_setup', 'real_vue_edit_title_and_body', 'ui_save_receipt',
                'reload_ui', 'automatic_derivation_and_cascade', 'ui_manual_rerun', 'linked_data_and_semantic_review']}, indent=2))
        return 0
    require(bool(args.node) and Path(args.node).is_file(), 'An existing Node executable is required')
    if args.browser_executable:
        require(args.browser_executable.is_file(), 'Requested existing browser executable is missing')
    frontend = ROOT / 'frontend'
    require((frontend / 'node_modules' / 'vite' / 'bin' / 'vite.js').is_file(), 'Install the project frontend dependencies first')
    novel, old_atoms = copy_live_run(source, output)
    token = secrets.token_urlsafe(32)
    backend_port, vite_port = free_port(), free_port()
    while vite_port == backend_port:
        vite_port = free_port()
    backend_origin, vite_origin = f'http://127.0.0.1:{backend_port}', f'http://127.0.0.1:{vite_port}'
    config = output / 'vite.acceptance.config.mjs'
    config.write_text(
        'import base from ' + json.dumps((frontend / 'vite.config.ts').as_posix()) + ';\n'
        + 'export default { ...base, root: ' + json.dumps(str(frontend)) + ', server: { ...base.server, host: "127.0.0.1", port: '
        + str(vite_port) + ', strictPort: true, proxy: Object.fromEntries(Object.entries(base.server.proxy).map(([key,value]) => [key,{...value,target:'
        + json.dumps(backend_origin) + '}])) }};\n', encoding='utf-8')
    env = {**os.environ, 'PLOTPILOT_SESSION_TOKEN': token,
        'PLOTPILOT_PROD_DATA_DIR': str(output / 'data'), 'PYTHONIOENCODING': 'utf-8',
        'LOG_FILE': str(output / 'logs' / 'plotpilot.log'),
        'PLOTPILOT_QA_FRONTEND': str(frontend), 'PLOTPILOT_QA_UI_ORIGIN': vite_origin,
        'PLOTPILOT_QA_NOVEL': novel, 'PLOTPILOT_QA_OUTPUT': str(output),
        'PLOTPILOT_QA_TIMEOUT_MS': str(args.stage_timeout * 1000),
        'PLOTPILOT_QA_HEADED': '1' if args.headed else '0',
        'VITE_API_BASE_URL': '/api/v1',
        # The acceptance browser uses an isolated random Vite port. Add that
        # exact origin to the backend allowlist for this owned test process;
        # production defaults remain the fixed local launcher origins.
        'CORS_ORIGINS': vite_origin}
    if args.browser_executable:
        env['PLOTPILOT_QA_BROWSER_EXECUTABLE'] = str(args.browser_executable.resolve())
    backend = vite = None
    client = Client(backend_origin, token, 10)
    stage = 'local_service_startup'
    try:
        with (output / 'backend.log').open('ab') as backend_log, (output / 'vite.log').open('ab') as vite_log:
            backend = subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'backend_entry.py'), str(backend_port)],
                cwd=ROOT, env=env, stdout=backend_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            # Prompt/package bootstrap can take more than a minute on a cold
            # Python 3.14 process (SQLite migrations and prompt seeding run
            # before /health is exposed).  Keep the check bounded, but do not
            # report a false startup failure during that normal initialization.
            wait_http(backend_origin + '/health', backend,
                      timeout=max(180, min(args.stage_timeout, 300)))
            stage = 'public_API_baseline_setup'
            baseline = setup_registered_prop_baseline(client, output / 'data' / 'plotpilot.db', novel)
            write_json(output / 'public-api-baseline-setup.json', baseline)
            stage = 'frontend_service_startup'
            vite = subprocess.Popen([args.node, str(frontend / 'node_modules' / 'vite' / 'bin' / 'vite.js'), '--config', str(config)],
                cwd=frontend, env=env, stdout=vite_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            wait_http(vite_origin, vite, timeout=120)
            stage = 'real_browser_edit'
            # The child prints only safe stage names and its own authored errors.
            completed = subprocess.run([args.node, str(Path(__file__).with_suffix('.mjs'))],
                cwd=frontend, env=env, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            browser_result = read_json(output / 'browser-result.json', {})
            require(completed.returncode == 0 and browser_result.get('status') == 'passed_real_browser',
                'Real browser acceptance failed; inspect browser-result.json')
            stage = 'linked_data_inspection'
            linked = inspect_derived_data(output / 'data' / 'plotpilot.db', novel, old_atoms)
            from linked_holder_evidence import inspect_holder
            with readonly_db(output / 'data' / 'plotpilot.db') as connection:
                connection.execute('BEGIN')
                holder = inspect_holder(connection, novel, '林雁', 107)
            write_json(output / 'holder-evidence.json', holder)
            require(holder['original_generated_chapter']['old_74_number_present'] is True,
                'Original first chapter lacks the old 74 numbered-key fact')
            require(holder['complete_holder_linkage_verified'],
                'Edited final holder is not verified across active memory, chapter prop state and evolution inventory; inspect holder-evidence.json')
            require(holder['registered_prop_lifecycle']['registered_prop_lifecycle_verified'],
                'Registered prop has no matching real AUTO_LLM transfer and chapter state for its final holder; inspect holder-evidence.json')
            linked['holder_linkage'] = holder
            result = {**browser_result, 'linked_data': linked,
                'limitations': ['Executed against the source Vue frontend and local backend, without API mocks. Registered-prop baseline setup uses public POST/PATCH APIs; chapter editing, saving and manual reruns use UI.',
                    'Registered prop lifecycle is exercised after explicit baseline registration; automatic catalogue discovery of unregistered items is not established.',
                    'Reanalysis does not silently rewrite later prose or prove full-book semantic consistency.',
                    'Browser source acceptance does not establish desktop installer/WebView behavior.']}
            write_json(output / 'result.json', result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
    except Exception as error:
        result = {'status': 'failed', 'stage': stage, 'error_type': type(error).__name__,
            'message': str(error) if isinstance(error, CheckFailed) else 'Inspect project-local evidence logs'}
        write_json(output / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 1
    finally:
        stop_owned(vite)
        if backend is not None and backend.poll() is None:
            try:
                client.json('/internal/shutdown', 'POST', {})
                backend.wait(timeout=20)
            except Exception:
                stop_owned(backend)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
