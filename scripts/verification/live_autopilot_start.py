"""Exercise the real owned daemon against saved provider configuration in isolation.

Hand-authored Bible fixtures keep this check focused on managed writing. No API
or executor acknowledgement is mocked. Credential material stays in out/.
"""
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

from live_novel_fullflow import Client, CheckFailed, copy_configuration, readonly_db, require, write_json

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=2400)
    args = parser.parse_args()
    output = args.output.resolve()
    require(not output.exists(), 'Choose a new project-local output directory')
    output.mkdir(parents=True)
    database = output / 'data' / 'plotpilot.db'
    copy_configuration(args.source_db.resolve(), database)
    token = secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    client = Client(f'http://127.0.0.1:{port}', token, 25)
    env = {**os.environ, 'PLOTPILOT_SESSION_TOKEN': token, 'DISABLE_AUTO_DAEMON': '0',
           'TALEWEFT_PROD_DATA_DIR': str(database.parent), 'PLOTPILOT_PROD_DATA_DIR': str(database.parent),
           'PYTHONIOENCODING': 'utf-8', 'LOG_FILE': str(output / 'runtime.log')}
    novel = 'autopilot-live-check'
    base = '/api/v1/autopilot/' + novel
    report = {'status': 'running', 'novel_id': novel, 'real_provider': False,
              'start_acknowledged': False, 'stopped': False, 'stages': [], 'chapters': []}
    process = None
    try:
        with (output / 'backend.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/backend_entry.py'), str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 140
            while time.monotonic() < deadline:
                require(process.poll() is None, 'Owned backend exited before readiness')
                try:
                    if client.json('/health').get('status') == 'healthy':
                        break
                except CheckFailed:
                    pass
                time.sleep(1)
            panel = client.json('/api/v1/llm-control')
            report['real_provider'] = panel['runtime'].get('status') == 'configured' and not panel['runtime'].get('using_mock')
            require(report['real_provider'], 'Saved provider is not configured')
            client.json('/api/v1/novels/', 'POST', {
                'novel_id': novel, 'title': '托管验收：旧站台的失物', 'author': '本地托管验收',
                'target_chapters': 2, 'target_words_per_chapter': 500, 'genre': '现实悬疑',
                'premise': '两章完结的短篇。旧站值班员陈禾在末班车后捡到一封写错日期的失物认领信，与修表师叶青核对站台时钟和寄存记录，找到失主。第一章发现日期矛盾，第二章通过可核对的物证解决误会，失物归还。',
                'world_preset': '当代小城火车站，没有超自然力量。',
                'story_structure': '仅两章，一个疑问，一次核实，一次归还，完整收束。',
                'pacing_control': '每章一个具体行动目标，避免扩展支线。',
                'writing_style': '简洁具体，以动作和对话推动，避免抽象感悟。',
                'special_requirements': '两章内完结，不得扩展为长篇。',
            })
            bible = '/api/v1/bible/novels/' + novel + '/bible'
            client.json(bible, 'POST', {'novel_id': novel, 'bible_id': 'managed-test-bible'})
            client.json(bible + '/characters', 'POST', {'character_id': 'chen', 'name': '陈禾', 'description': '旧站值班员，认真核对记录。'})
            client.json(bible + '/characters', 'POST', {'character_id': 'ye', 'name': '叶青', 'description': '修表师，了解站台时钟。'})
            client.json(bible + '/world-settings', 'POST', {'setting_id': 'station-rule', 'name': '旧站管理', 'description': '失物有纸质寄存记录，查验认领凭证后归还。', 'setting_type': 'rule'})
            client.json(bible + '/locations', 'POST', {'location_id': 'station', 'name': '旧站台', 'description': '设有值班室和旧时钟的小城站台。', 'location_type': 'station'})
            client.json(bible + '/style-notes', 'POST', {'note_id': 'style', 'category': '文风公约', 'content': '使用具体物件、动作和有目的的对话，不堆砌形容词，不使用模板感悟。'})
            client.json('/api/v1/novels/' + novel + '/auto-approve-mode', 'PATCH', {'auto_approve_mode': True})
            # Startup may still be initializing model dependencies; wait for its loop.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if client.json(base + '/status').get('daemon_alive'):
                    break
                time.sleep(1)
            accepted = client.json(base + '/start', 'POST', {'target_chapters': 2, 'target_words_per_chapter': 500, 'max_auto_chapters': 2})
            require(accepted.get('accepted') is True and accepted.get('execution_confirmed') is False, 'Start did not return an honest pending receipt')
            report['command_id'] = accepted['command_id']
            print('PASS real pending receipt', flush=True)
            deadline, last_progress = time.monotonic() + args.timeout, 0
            while time.monotonic() < deadline:
                require(process.poll() is None, 'Owned backend exited during managed writing')
                status = client.json(base + '/status')
                receipt = client.json(base + '/commands/' + accepted['command_id'])
                if receipt.get('execution_confirmed'):
                    require(receipt.get('command_status') == 'completed', 'Executor confirmation is not durable')
                    if not report['start_acknowledged']:
                        report['start_acknowledged'] = True
                        print('PASS real executor acknowledgement', flush=True)
                require(receipt.get('command_status') not in ('failed', 'superseded'), 'Start command failed')
                stage = status.get('current_stage')
                if stage and stage not in report['stages']:
                    report['stages'].append(stage)
                    print('STAGE ' + stage, flush=True)
                with readonly_db(database) as db:
                    rows = db.execute('SELECT number,title,status,length(content) AS chars FROM chapters WHERE novel_id=? ORDER BY number', (novel,)).fetchall()
                    report['chapters'] = [dict(row) for row in rows]
                    report['model_attempts'] = db.execute('SELECT COUNT(*) FROM ai_invocation_attempts').fetchone()[0]
                if time.monotonic() - last_progress > 40:
                    print('PROGRESS ' + json.dumps({'stage': stage, 'substep': status.get('writing_substep'), 'chapters': report['chapters'], 'attempts': report['model_attempts']}, ensure_ascii=False), flush=True)
                    last_progress = time.monotonic()
                report['latest_status'] = {key: status.get(key) for key in ('autopilot_status', 'current_stage', 'writing_substep', 'pipeline_error', 'completed_chapters', 'total_words', 'consecutive_error_count')}
                write_json(output / 'result.json', report)
                require(status.get('autopilot_status') != 'error', 'Managed writing entered error state')
                if stage == 'paused_for_review':
                    raise CheckFailed('Managed writing unexpectedly requires review; inspect isolated workflow evidence')
                if stage == 'completed' and len(report['chapters']) >= 2:
                    require(all(row['chars'] and row['chars'] >= 200 for row in report['chapters'][:2]), 'Saved manuscript is empty or too short')
                    break
                time.sleep(2)
            else:
                raise CheckFailed('Managed writing did not finish within the bounded live verification window')
            stop = client.json(base + '/stop', 'POST', {})
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if client.json(base + '/commands/' + stop['command_id']).get('execution_confirmed'):
                    report['stopped'] = True
                    break
                time.sleep(1)
            require(report['stopped'], 'Executor did not acknowledge stop')
            report['status'] = 'passed_real_autopilot'
            print('PASS managed manuscript saved and executor stopped', flush=True)
    except Exception as error:
        report['status'] = 'failed'
        report['error_type'] = type(error).__name__
        report['message'] = str(error) if isinstance(error, CheckFailed) else 'Inspect project-local backend log'
    finally:
        write_json(output / 'result.json', report)
        if process is not None and process.poll() is None:
            try:
                client.json(base + '/stop', 'POST', {})
                client.json('/internal/shutdown', 'POST', {})
                process.wait(timeout=25)
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed_real_autopilot' else 1


if __name__ == '__main__':
    raise SystemExit(main())
