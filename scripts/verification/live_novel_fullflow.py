"""Real HTTP acceptance of the existing PlotPilot backend, isolated from author data.

Reuses PlotPilot's open-source backend entry point and its public HTTP routes.
No provider SDK or alternative generation path is used here. --dry-run never
opens the source database or calls a model; a successful dry-run is not live QA.
Credentials are copied by reference/ciphertext and never included in the report.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
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
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
REFERENCE = re.compile(r"ppcred:v1:([0-9a-f]{32})")
STAGES = ["configuration", "novel", "worldbuilding", "characters", "locations",
          "plot_outline", "macro", "act_chapters", "chapter_1", "chapter_2",
          "edit_chapter_1", "cascade", "manual_retry", "linked_evidence", "downstream_semantics", "holder_linkage_inspection"]
OLD_MARKER, NEW_MARKER = "七十四", "九十二"


class CheckFailed(RuntimeError):
    """Only locally authored, non-secret diagnostics may be exposed."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def has_key_marker(text, chinese, number):
    return chinese in text or bool(re.search(r'(?<!\d)' + str(number) + r'(?!\d)', text))


def semantic_review_evidence(content, atoms, snapshot, conflicts, review):
    """Report reanalysis separately from literary/semantic consistency.

    Unchanged later prose may legitimately reintroduce an earlier fact. Never
    delete that evidence or claim that rerunning extraction corrected the book.
    """
    active = [row for row in atoms if row['status'] in ('candidate', 'confirmed')]
    def marker_rows(chinese, number, chapter=None):
        return sum(has_key_marker((row.get('payload_json') or '') + (row.get('text_span') or ''), chinese, number)
                   for row in active if chapter is None or row['chapter_number'] == chapter)
    old_in_prose = has_key_marker(content, OLD_MARKER, 74)
    old_any, new_any = marker_rows(OLD_MARKER, 74), marker_rows(NEW_MARKER, 92)
    snapshot_conflicts = len(json.loads((snapshot or {}).get('conflicts_json') or '[]'))
    needs_review = old_in_prose or bool(old_any and new_any) or bool(conflicts) or bool(snapshot_conflicts)
    return {
        'scope': 'chapter_2_after_chapter_1_author_edit',
        'later_prose_was_not_rewritten': True,
        'chapter_2_prose_mentions_obsolete_number': old_in_prose,
        'chapter_2_active_automatic_memory': {
            'old_number_records': marker_rows(OLD_MARKER, 74, 2),
            'new_number_records': marker_rows(NEW_MARKER, 92, 2),
        },
        'cross_chapter_active_automatic_memory_has_both_numbers': bool(old_any and new_any),
        'chapter_2_latest_evolution_status': (snapshot or {}).get('status'),
        'chapter_2_latest_evolution_conflicts': snapshot_conflicts,
        'open_evolution_conflicts': len(conflicts),
        'open_evolution_conflict_types': sorted({row['conflict_type'] for row in conflicts}),
        'chapter_2_review_status': (review or {}).get('status'),
        'chapter_2_review_memo_present': bool((review or {}).get('memo')),
        'automatic_semantic_correction_verified': False,
        'full_book_consistency': 'requires_author_review' if needs_review else 'not_established_by_reanalysis',
        'limitation': 'Reanalysis refreshes derived records. It does not establish semantic agreement between unchanged later prose and the edited chapter.',
    }


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def readonly_db(path):
    # mode=ro also reads the current WAL; immutable=1 would silently miss it.
    absolute = path.resolve()
    if absolute.drive.startswith('\\\\'):
        # pathlib.as_uri() turns \\server\share into file://server/share.
        # SQLite interprets/strips that authority. Preserve the UNC filename
        # as a double-leading-slash path: file:////server/share/filename.
        filename = absolute.as_posix()
        uri = 'file://' + urllib.parse.quote(filename, safe='/:')
    else:
        uri = absolute.as_uri()
    connection = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def copy_configuration(source, target, profile_id=None, endpoint_kind=None):
    """Read only two config tables, never copy novels or call source get_config()."""
    require(source.is_file(), "Source configuration database does not exist")
    require(not target.exists(), "Isolation database must not already exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    references = set()
    with readonly_db(source) as original, sqlite3.connect(target) as isolated:
        for table in ("llm_profiles", "llm_config_meta"):
            ddl = original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            require(ddl is not None, "Source database does not have model configuration tables")
            isolated.execute(ddl[0])
            rows = original.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                columns = ",".join('"' + column + '"' for column in rows[0].keys())
                placeholders = ",".join("?" for _ in rows[0])
                isolated.executemany(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', [tuple(r) for r in rows])
            for row in rows:
                for value in row:
                    if isinstance(value, str):
                        references.update(REFERENCE.findall(value))
        profiles = isolated.execute('SELECT id,protocol,api_key,base_url,model FROM llm_profiles ORDER BY sort_order').fetchall()
        require(bool(profiles), "Source has no saved model profiles")
        active_row = isolated.execute("SELECT value FROM llm_config_meta WHERE key='active_profile_id'").fetchone()
        selected = profile_id or (active_row[0] if active_row else profiles[0][0])
        active = next((row for row in profiles if row[0] == selected), None)
        require(active is not None, "Requested profile is not present in source configuration")
        require(bool(active[2] and active[3] and active[4]), "Selected profile needs key, base URL and model")
        isolated.execute("INSERT INTO llm_config_meta(key,value) VALUES('active_profile_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (selected,))
        if endpoint_kind:
            require(active[1] == "openai", "C/R endpoint choice requires an OpenAI compatible profile")
            key = "profile_options:" + selected
            row = isolated.execute('SELECT value FROM llm_config_meta WHERE key=?', (key,)).fetchone()
            options = json.loads(row[0]) if row else {}
            options['endpoint_kind'] = endpoint_kind
            isolated.execute('INSERT INTO llm_config_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(options)))
            isolated.execute('UPDATE llm_profiles SET preset_key=?,use_legacy_chat_completions=? WHERE id=?',
                ('custom-openai-compatible' if endpoint_kind == 'chat' else 'custom-openai-responses', int(endpoint_kind == 'chat'), selected))
    if references:
        vault = target.parent / '.credentials'
        vault.mkdir()
        for reference in references:
            original_file = source.parent / '.credentials' / (reference + '.bin')
            require(original_file.is_file(), "A saved credential reference has no ciphertext file")
            shutil.copy2(original_file, vault / original_file.name)
    return selected


class Client:
    def __init__(self, base, token, timeout):
        self.base, self.token, self.timeout = base, token, timeout

    def open(self, path, method='GET', payload=None, extra_headers=None):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token}
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            # Do not propagate provider errors that can contain credential-bearing URLs.
            error.read()
            raise CheckFailed(f'HTTP {error.code} at local route {path.split("?")[0]}') from None
        except OSError:
            raise CheckFailed('Local HTTP connection failed or timed out') from None

    def json(self, path, method='GET', payload=None, headers=None):
        with self.open(path, method, payload, headers) as response:
            return json.load(response)

    def sse(self, path, method='GET', payload=None):
        with self.open(path, method, payload, {'Accept': 'text/event-stream'}) as response:
            name, lines = 'message', []
            for raw in response:
                line = raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('event:'):
                    name = line[6:].strip()
                elif line.startswith('data:'):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    joined = '\n'.join(lines)
                    if joined != '[DONE]':
                        yield name, json.loads(joined)
                    name, lines = 'message', []
            if lines and '\n'.join(lines) != '[DONE]':
                yield name, json.loads('\n'.join(lines))


class Run:
    def __init__(self, directory, client, timeout, *, retry_failed=False):
        self.directory, self.client, self.timeout = directory, client, timeout
        self.retry_failed = retry_failed
        self.db = directory / 'data' / 'plotpilot.db'
        self.checkpoint_path = directory / 'checkpoint.json'
        self.state = read_json(self.checkpoint_path, {'novel_id': 'live-' + uuid.uuid4().hex[:14], 'passed': [], 'evidence': {}})
        self.novel = self.state['novel_id']
        self.n = '/api/v1/novels/' + self.novel
        self.stage = 'configuration'

    def persist(self):
        write_json(self.checkpoint_path, self.state)

    def mark(self, stage, evidence=None):
        if stage not in self.state['passed']:
            self.state['passed'].append(stage)
        if evidence is not None:
            self.state['evidence'][stage] = evidence
        self.persist()
        print('PASS ' + stage, flush=True)

    def rows(self, sql, params=()):
        with readonly_db(self.db) as connection:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    def poll(self, callback, label):
        deadline, next_progress = time.monotonic() + self.timeout, 0
        while time.monotonic() < deadline:
            value = callback()
            if value is not None:
                return value
            if time.monotonic() >= next_progress:
                print('WAIT ' + label, flush=True)
                next_progress = time.monotonic() + 30
            time.sleep(1)
        raise CheckFailed('Timed out waiting for ' + label)

    def invocation(self, session_id):
        route = '/api/v1/ai-invocations/' + session_id
        last_status = None

        def advance():
            nonlocal last_status
            payload = self.client.json(route)
            status = payload['session']['status']
            if status != last_status:
                print('INVOCATION ' + self.stage + ' ' + status, flush=True)
                last_status = status
            if status == 'awaiting_pre_call_review':
                self.client.json(route + '/resume', 'POST', {'resumed_by': 'user', 'config': {}})
            elif status == 'awaiting_acceptance':
                attempt = payload.get('attempt') or {}
                require(attempt.get('status') == 'succeeded', 'Invocation attempt did not succeed')
                self.client.json(route + '/accept', 'POST', {'attempt_id': attempt['id'], 'accepted_by': 'user', 'commit_variable_outputs': True})
            elif status == 'awaiting_commit':
                decision = payload.get('decision') or {}
                require(bool(decision.get('id')), 'Invocation has no adoption decision')
                self.client.json(route + '/commits', 'POST', {'decision_id': decision['id']})
            elif status == 'completed':
                require((payload.get('commit') or {}).get('status') == 'succeeded', 'Invocation commit did not succeed')
                return {'session_id': session_id, 'status': status, 'commit_status': 'succeeded',
                    'explicit_retries': (self.state.get('invocation_retries') or {}).get(session_id, 0)}
            elif status in ('failed', 'cancelled') and self.retry_failed:
                retries = self.state.setdefault('invocation_retries', {})
                require(retries.get(session_id, 0) < 1, 'Invocation retry limit reached (one explicit retry per session)')
                # Register the retry before HTTP dispatch. A timeout or restart
                # must never accidentally submit a second paid retry.
                retries[session_id] = retries.get(session_id, 0) + 1
                self.persist()
                print('RETRY_ONCE ' + self.stage, flush=True)
                self.client.json(route + '/retry', 'POST', {'resumed_by': 'user', 'config': {}})
            elif status in ('failed', 'blocked', 'cancelled', 'aborted'):
                raise CheckFailed('Invocation ended with status ' + status)
            return None
        return self.poll(advance, self.stage)

    def new_invocation(self, stage, path):
        self.stage = stage
        if stage in self.state['passed']:
            return
        key = stage + '_session'
        if not self.state.get(key):
            response = self.client.json(path, 'POST', {})
            session_id = response.get('invocation_session_id') or (response.get('session') or {}).get('id')
            require(bool(session_id), 'Setup did not create an AI invocation session')
            self.state[key] = session_id
            self.persist()
        self.mark(stage, self.invocation(self.state[key]))

    def chapter(self, number):
        return self.client.json(self.n + '/chapters/' + str(number))

    def jobs(self, number):
        return self.client.json(self.n + '/chapters/' + str(number) + '/derivation-jobs')

    def wait_job(self, number, revision, minimum_attempts=1, *, require_cascade=False):
        def check():
            rows = self.jobs(number)
            row = next((r for r in rows if r['revision'] == revision), None)
            if row and row['attempts'] >= minimum_attempts:
                if require_cascade and row.get('cascade_status') == 'unavailable':
                    raise CheckFailed(f'Chapter {number} downstream reconciliation is unavailable')
                if row['status'] == 'completed':
                    if require_cascade and row.get('cascade_status') != 'completed':
                        return None
                    return {k: row[k] for k in ('job_id', 'revision', 'attempts', 'status', 'cascade_status') if k in row}
                if row['status'] in ('unavailable', 'superseded', 'failed'):
                    raise CheckFailed(f'Chapter {number} derived work ended with {row["status"]}')
            return None
        return self.poll(check, 'chapter_' + str(number) + '_derived_work')

    def save(self, number, content, title=None, beats=None, proof=None):
        chapter = self.chapter(number)
        request_id = uuid.uuid4().hex
        payload = {'content': content, 'expected_revision': chapter['revision'],
            'expected_hash': chapter['content_hash'], 'save_request_id': request_id}
        if title is not None:
            payload['title'] = title
        if beats:
            payload['micro_beats'] = beats
        if proof:
            payload['prose_execution_evidence'] = proof
        saved = self.client.json(self.n + '/chapters/' + str(number), 'PUT', payload,
            {'If-Match': chapter['etag']} if chapter.get('etag') else None)
        require(saved['revision'] == chapter['revision'] + 1, 'Chapter revision did not advance')
        require(saved['content_hash'] == sha(content), 'Saved content hash differs')
        if title is not None:
            require(saved['title'] == title, 'Chapter title was not saved')
        receipt = self.client.json(self.n + f'/chapters/{number}/save-requests/' + request_id)
        require(receipt.get('status') == 'committed', 'Save has no committed durable receipt')
        job = self.wait_job(number, saved['revision'], require_cascade=True)
        return saved, job

    def verify_prose(self, number, done):
        from application.ai_invocation.prose_execution_evidence import verify_prose_result_receipts

        class Reader:
            def __init__(self, run):
                self.run = run
            def fetch_one(self, sql, params):
                rows = self.run.rows(sql, params)
                return rows[0] if rows else None

        proof = done.get('prose_execution_evidence') or {}
        verified = verify_prose_result_receipts(Reader(self), novel_id=self.novel,
            chapter_number=number, content=done['content'], proof=proof)
        require(verified.get('verified') is True, 'Missing real Anti-AI workflow receipts')
        return verified

    def execute(self):
        panel = self.client.json('/api/v1/llm-control')
        runtime = panel['runtime']
        require(runtime.get('status') == 'configured' and not runtime.get('using_mock'), 'Runtime is not configured for a real provider')
        self.profile = panel['config']['active_profile_id']
        self.mark('configuration', {'real_provider': True, 'protocol': runtime.get('protocol'), 'endpoint_kind': runtime.get('endpoint_kind')})
        if 'novel' not in self.state['passed']:
            self.stage = 'novel'
            self.client.json('/api/v1/novels/', 'POST', {
                'novel_id': self.novel, 'title': '雾港修表人', 'author': '本地全流程验收',
                'target_chapters': 10, 'target_words_per_chapter': 500, 'genre': '悬疑',
                'premise': '雾港修表人沈舟与调查员林雁调查停摆的潮汐钟。第一章沈舟把编号七十四的验潮钥匙交给林雁，二人共同查验旧船闸；第二章他们发现钟表账簿里的假潮时。故事以合理物证和人物行动推动，不出现超能力。',
                'world_preset': '近代架空港口，技术与社会规则清楚，普通人可验证线索。',
                'story_structure': '十章短篇悬疑，人物行动发现证据、纠正误判、收束谜底。',
                'pacing_control': '每章一个具体目标、阻碍和新线索，开篇迅速进入事件。',
                'writing_style': '简洁具体，少解释，多动作和有目的的对话。',
                'special_requirements': '主角沈舟、重要配角林雁。第一章明确写沈舟将编号七十四的验潮钥匙交给林雁。',
            })
            self.mark('novel')
        for stage in ('worldbuilding', 'characters', 'locations'):
            self.new_invocation(stage, f'/api/v1/bible/novels/{self.novel}/generate?stage={stage}')
        bible = self.client.json(f'/api/v1/bible/novels/{self.novel}/bible')
        characters, locations = bible.get('characters') or [], bible.get('locations') or []
        require(bool(characters) and bool(locations), 'Setup has no committed characters or locations')
        location_ids = {r['id'] for r in locations}
        require(len(location_ids) == len(locations), 'Location IDs are not unique')
        require(all(not r.get('parent_id') or r['parent_id'] in location_ids for r in locations), 'Location parent is not committed')
        self.new_invocation('plot_outline', self.n + '/setup/generate-plot-outline')
        outline = self.client.json(self.n + '/setup/plot-outline')
        require(bool(outline.get('plot_outline')), 'Setup plot outline was not committed')
        self.stage = 'macro'
        macro_path = f'/api/v1/planning/novels/{self.novel}/macro'
        if 'macro' not in self.state['passed']:
            # Macro proposals/progress currently live in backend process
            # memory. --resume owns a new backend and must request a proposal
            # again when no confirmed checkpoint was reached.
            self.client.json(macro_path + '/generate', 'POST', {'target_chapters': 10,
                'structure': {'parts': 1, 'volumes_per_part': 1, 'acts_per_volume': 1}})

            def macro_result():
                result = self.client.json(macro_path + '/result')['data']
                progress = self.client.json(macro_path + '/progress')['data']
                require(progress.get('status') not in ('failed', 'error', 'cancelled'), 'Macro planning failed')
                return result if result.get('ready') else None
            result = self.poll(macro_result, 'macro_planning')
            self.client.json(macro_path + '/confirm', 'POST', {'structure': result['result']['structure']})
            self.mark('macro')
        tree = self.client.json(f'/api/v1/planning/novels/{self.novel}/structure')['data']['nodes']

        def flatten(nodes):
            for node in nodes:
                yield node
                yield from flatten(node.get('children') or [])
        acts = [r for r in flatten(tree) if r.get('node_type') == 'act']
        require(bool(acts), 'Confirmed structure has no act')
        self.stage = 'act_chapters'
        if 'act_chapters' not in self.state['passed']:
            planned = None
            for event, payload in self.client.sse('/api/v1/planning/acts/' + acts[0]['id'] + '/chapters/stream?chapter_count=2'):
                require(event != 'error', 'Act chapter planning returned an SSE error')
                if event == 'done':
                    planned = payload
            require(planned and planned.get('success') and len(planned.get('chapters') or []) >= 2, 'Act planning did not produce two chapters')
            self.client.json('/api/v1/planning/acts/' + acts[0]['id'] + '/chapters/confirm', 'POST', {'chapters': planned['chapters']})
            self.state['planned_chapters'] = planned['chapters']
            self.mark('act_chapters')
        for number in (1, 2):
            self.stage = stage = 'chapter_' + str(number)
            if stage in self.state['passed']:
                continue
            result_path = self.directory / (stage + '_generation.json')
            done = read_json(result_path)
            if not done:
                planned = self.state['planned_chapters'][number - 1]
                chapter_outline = planned.get('outline') or planned.get('description') or json.dumps(planned, ensure_ascii=False)
                if number == 1:
                    chapter_outline += '\n必须明确写出：沈舟将编号七十四的验潮钥匙交给林雁，林雁收好钥匙。'
                request_id = self.state.get(stage + '_request') or uuid.uuid4().hex
                resuming = bool(self.state.get(stage + '_request'))
                self.state[stage + '_request'] = request_id
                self.persist()
                if resuming:
                    events = self.client.sse(self.n + '/generation-requests/' + request_id + '/events?after=0')
                else:
                    events = self.client.sse(self.n + '/generate-chapter-stream', 'POST', {'request_id': request_id,
                        'chapter_number': number, 'outline': chapter_outline, 'profile_id': self.profile})
                terminal, sequences, event_types = None, set(), set()
                for _, payload in events:
                    seq = payload.get('sequence')
                    if seq is not None and seq in sequences:
                        continue
                    sequences.add(seq)
                    kind = payload.get('type')
                    if kind not in event_types:
                        print('SSE ' + stage + ' ' + str(kind), flush=True)
                        event_types.add(kind)
                    require(kind != 'error', 'Chapter generation returned an SSE error')
                    if kind == 'done':
                        done = payload
                    if kind == 'terminal':
                        terminal = payload.get('status')
                require(terminal == 'completed' and done and bool(done.get('content', '').strip()), 'Chapter generation lacks completed terminal and body')
                write_json(result_path, done)
            proof = self.verify_prose(number, done)
            current = self.chapter(number)
            if current.get('content_hash') != sha(done['content']):
                saved, job = self.save(number, done['content'], beats=done.get('beats'), proof=done.get('prose_execution_evidence'))
            else:
                saved, job = current, self.wait_job(number, current['revision'], require_cascade=True)
            self.mark(stage, {'revision': saved['revision'], 'chars': len(done['content']), 'anti_ai': proof, 'derivation': job})

        self.stage = 'edit_chapter_1'
        if 'edit_chapter_1' not in self.state['passed']:
            first, second = self.chapter(1), self.chapter(2)
            if not self.state.get('edit_payload'):
                baseline_jobs = self.jobs(2)
                self.state['second_before_edit'] = {'content_hash': second['content_hash'], 'revision': second['revision'],
                    'attempts': max([r['attempts'] for r in baseline_jobs if r['revision'] == second['revision']] or [0])}
                self.state['old_ai_atom_ids'] = [r['id'] for r in self.rows("SELECT id FROM memory_atoms WHERE novel_id=? AND chapter_number=1 AND status='candidate' AND source IN ('chapter_extract','dialogue_extract','triple_extract','causal_extract')", (self.novel,))]
                # Replace the obsolete number everywhere, then give the new
                # state a clear final scene via the same author-edit PUT.
                edited = first['content'].replace(OLD_MARKER, NEW_MARKER).replace('74', '92')
                edited += '\n\n林雁把验潮钥匙放回沈舟手里，撤回此前的代管约定。沈舟将编号九十二的验潮钥匙锁进自己的工具箱。钥匙现在由沈舟独自保管，林雁没有钥匙。两人在交接簿上改正了持有人和编号，接着去核对船闸账簿。'
                self.state['edit_payload'] = {'content': edited, 'title': '九十二号钥匙的新持有人',
                    'before_revision': first['revision'], 'before_title': first['title']}
                self.persist()
            edit = self.state['edit_payload']
            if first['content_hash'] == sha(edit['content']) and first['title'] == edit['title']:
                saved, job = first, self.wait_job(1, first['revision'], require_cascade=True)
            else:
                saved, job = self.save(1, edit['content'], title=edit['title'])
            self.state['edited_revision'] = saved['revision']
            versions = self.client.json(self.n + '/chapters/1/versions')
            old_version = next((v for v in versions if v['revision'] == edit['before_revision']), None)
            new_version = next((v for v in versions if v['revision'] == saved['revision']), None)
            require(old_version and old_version['title'] == edit['before_title'], 'Previous chapter title is missing from version history')
            require(new_version and new_version['title'] == edit['title'], 'Edited chapter title is missing from version history')
            require(self.chapter(1)['title'] == edit['title'], 'Renamed title did not read back')
            structure_title = self.rows("SELECT title FROM story_nodes WHERE novel_id=? AND node_type='chapter' AND number=1", (self.novel,))
            require(structure_title and all(r['title'] == edit['title'] for r in structure_title), 'Renamed title did not propagate to the story outline')
            self.mark('edit_chapter_1', {'revision': saved['revision'], 'title_saved': True, 'content_hash': saved['content_hash'], 'derivation': job})
        self.stage = 'cascade'
        if 'cascade' not in self.state['passed']:
            baseline = self.state['second_before_edit']
            job = self.wait_job(2, baseline['revision'], baseline['attempts'] + 1)
            second = self.chapter(2)
            require(second['content_hash'] == baseline['content_hash'], 'Derived refresh changed the author chapter text')
            self.mark('cascade', {'chapter_2_reanalysed': True, 'derivation': job})
        self.stage = 'manual_retry'
        if 'manual_retry' not in self.state['passed']:
            revision = self.chapter(1)['revision']
            before = next(r for r in self.jobs(1) if r['revision'] == revision)
            second_before = next(r for r in self.jobs(2) if r['revision'] == self.chapter(2)['revision'])
            self.client.json(self.n + '/chapters/1/derivation-jobs', 'POST', {'action': 'retry_aftermath'})
            job = self.wait_job(1, revision, before['attempts'] + 1, require_cascade=True)
            following = self.wait_job(2, second_before['revision'], second_before['attempts'] + 1)
            self.mark('manual_retry', {'chapter_1': job, 'chapter_2': following})
        self.stage = 'linked_evidence'
        if 'linked_evidence' not in self.state['passed']:
            atoms = self.rows("SELECT id,status,payload_json,text_span FROM memory_atoms WHERE novel_id=? AND chapter_number=1 AND source IN ('chapter_extract','dialogue_extract','triple_extract','causal_extract')", (self.novel,))
            active_atoms = [r for r in atoms if r['status'] in ('candidate', 'confirmed')]
            active_text = json.dumps([{'payload': r['payload_json'], 'text_span': r['text_span']} for r in active_atoms], ensure_ascii=False)
            snapshots = self.rows('SELECT bundle_json FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=1', (self.novel,))
            knowledge = self.client.json(self.n + '/knowledge')
            evidence_text = active_text + json.dumps(snapshots, ensure_ascii=False) + json.dumps(knowledge, ensure_ascii=False)
            require(NEW_MARKER in evidence_text or '92' in evidence_text, 'New author fact is missing from regenerated linked data')
            require(bool(active_atoms), 'No active AI memory was generated from the edited chapter')
            require(OLD_MARKER not in active_text and not re.search(r'(?<!\d)74(?!\d)', active_text), 'Obsolete numbered-key fact remains active in generated memory')
            old_ids = set(self.state.get('old_ai_atom_ids') or [])
            superseded = [r for r in atoms if r['id'] in old_ids and r['status'] == 'superseded']
            require(not old_ids or bool(superseded), 'Changed generated memory has no superseded old evidence')
            self.mark('linked_evidence', {'new_fact_in_derived_data': True, 'obsolete_fact_in_active_memory': False,
                'superseded_old_atoms': len(superseded), 'active_atoms': sum(r['status'] in ('candidate', 'confirmed') for r in atoms),
                'characters': len(characters), 'locations': len(locations)})
        self.stage = 'downstream_semantics'
        if 'downstream_semantics' not in self.state['passed']:
            second = self.chapter(2)
            require(second['content_hash'] == self.state['second_before_edit']['content_hash'],
                'Semantic inspection found an unexpected later-prose rewrite')
            atoms = self.rows("SELECT chapter_number,status,payload_json,text_span FROM memory_atoms WHERE novel_id=? AND chapter_number IN (1,2) AND source IN ('chapter_extract','dialogue_extract','triple_extract','causal_extract')", (self.novel,))
            snapshots = self.rows("SELECT status,conflicts_json FROM chapter_evolution_snapshots WHERE novel_id=? AND branch_id='main' AND chapter_number=2 ORDER BY rowid DESC LIMIT 1", (self.novel,))
            conflicts = self.rows("SELECT conflict_type FROM chapter_evolution_conflicts WHERE novel_id=? AND branch_id='main' AND chapter_number>=2 AND resolution_status='open'", (self.novel,))
            reviews = self.rows('SELECT status,memo FROM chapter_reviews WHERE novel_id=? AND chapter_number=2', (self.novel,))
            self.mark('downstream_semantics', semantic_review_evidence(second['content'], atoms,
                snapshots[0] if snapshots else None, conflicts, reviews[0] if reviews else None))
        self.stage = 'holder_linkage_inspection'
        if 'holder_linkage_inspection' not in self.state['passed']:
            from linked_holder_evidence import inspect_holder
            with readonly_db(self.db) as connection:
                connection.execute('BEGIN')
                holder = inspect_holder(connection, self.novel, '沈舟', 92)
            require(holder['original_generated_chapter']['old_74_number_present'] is True,
                'Original generated first chapter did not establish the old numbered-key fact')
            require(holder['explicit_expected_final_owner_in_edited_tail'],
                'Edited chapter does not explicitly establish its expected final owner')
            write_json(self.directory / 'holder-evidence.json', holder)
            self.mark('holder_linkage_inspection', holder)
        return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'out' / 'verification' / ('live-fullflow-' + time.strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--profile-id')
    parser.add_argument('--endpoint-kind', choices=('chat', 'responses'))
    parser.add_argument('--stage-timeout', type=int, default=1200)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--prepare-only', action='store_true', help='Copy configuration and start/stop backend without calling a model')
    parser.add_argument('--resume', action='store_true', help='Continue an existing isolated run from its saved checkpoint')
    parser.add_argument('--retry-failed', action='store_true', help='With --resume, retry a failed/cancelled invocation at most once per session; the bound survives restart')
    args = parser.parse_args()
    require(args.stage_timeout >= 60, 'Stage timeout must be at least 60 seconds')
    require(not args.retry_failed or args.resume, '--retry-failed requires --resume')
    directory = args.output_dir.resolve()
    require(directory.is_relative_to(ROOT), 'Acceptance output must remain inside the project')
    if args.dry_run:
        print(json.dumps({'status': 'dry_run_only_NOT_live_acceptance', 'source_database_opened': False,
            'model_calls': 0, 'stages': STAGES}, ensure_ascii=False, indent=2))
        return 0
    require(args.source_db is not None or args.resume, '--source-db is required for a new live run')
    if not args.resume:
        require(not directory.exists(), 'Output directory exists; choose a new directory or --resume')
        directory.mkdir(parents=True)
        copy_configuration(args.source_db.resolve(), directory / 'data' / 'plotpilot.db', args.profile_id, args.endpoint_kind)
    else:
        require((directory / 'data' / 'plotpilot.db').is_file(), 'Resume isolation database is missing')
    token = secrets.token_urlsafe(32)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    client = Client(f'http://127.0.0.1:{port}', token, args.stage_timeout)
    runner = Run(directory, client, args.stage_timeout, retry_failed=args.retry_failed)
    env = {**os.environ, 'PLOTPILOT_SESSION_TOKEN': token,
        'PLOTPILOT_PROD_DATA_DIR': str(directory / 'data'), 'PYTHONIOENCODING': 'utf-8',
        'LOG_FILE': str(directory / 'logs' / 'plotpilot.log')}
    process = None
    try:
        with (directory / 'backend.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'backend_entry.py'), str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            def ready():
                require(process.poll() is None, 'Owned backend exited before readiness; inspect project-local backend.log')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                        return True if response.status == 200 else None
                except OSError:
                    return None
            runner.poll(ready, 'backend_readiness')
            if args.prepare_only:
                panel = client.json('/api/v1/llm-control')
                runtime = panel['runtime']
                result = {'status': 'prepared_only_NOT_live_acceptance', 'model_calls': 0,
                    'configured': runtime.get('status') == 'configured', 'using_mock': runtime.get('using_mock'),
                    'protocol': runtime.get('protocol'), 'endpoint_kind': runtime.get('endpoint_kind')}
            else:
                state = runner.execute()
                result = {'status': 'passed_real_provider', 'novel_id': state['novel_id'],
                    'passed': state['passed'], 'evidence': state['evidence'],
                    'complete_holder_linkage_verified': state['evidence']['holder_linkage_inspection']['complete_holder_linkage_verified'],
                    'limitations': ['Literary quality and absence of AI style are not established by execution receipts.',
                        'Downstream analysis is rerun; existing later chapter prose is kept for author review.',
                        'Final holder linkage is verified separately across automatic memory, chapter prop state and evolution owner/inventory. See complete_holder_linkage_verified and coverage_gaps.']}
            write_json(directory / 'result.json', result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
    except Exception as error:
        # Suppress raw third-party exception strings and their URLs/secrets.
        result = {'status': 'failed', 'stage': runner.stage,
            'error_type': type(error).__name__, 'message': str(error) if isinstance(error, CheckFailed) else 'Inspect project-local backend.log for details',
            'passed': runner.state['passed']}
        write_json(directory / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 1
    finally:
        if process is not None and process.poll() is None:
            try:
                client.json('/internal/shutdown', 'POST', {})
                process.wait(timeout=30)
            except Exception:
                # This exact Popen PID belongs to this run. Never kill by name.
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
