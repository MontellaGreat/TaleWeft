"""Edit contract sources together, preserving the rest of the story Bible."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid

from application.ai_invocation.dtos import stable_hash
from application.core.taxonomy.market_selection import PROFILE_FIELDS, normalize_market_preferences
from application.engine.services.execution_lease import get_execution_lease
from application.onboarding.setup_edit_guard import assert_setup_editable


TEXT_FIELDS = ('locked_genre', *PROFILE_FIELDS.values())


class ContractConflict(ValueError):
    pass


class CreationContractService:
    def __init__(self, db):
        self.db = db

    def get(self, novel_id):
        # One SQLite statement gives the editor a consistent source snapshot.
        row = self.db.fetch_one('''
            SELECT n.*, (SELECT json_group_array(json_object('id',id,'category',category,'content',content))
              FROM (SELECT id,category,content FROM bible_style_notes WHERE novel_id=n.id ORDER BY id)) AS contract_notes,
              (SELECT premise_lock FROM knowledge WHERE novel_id=n.id) AS contract_premise
            FROM novels n WHERE n.id=?''', (novel_id,))
        if row is None:
            raise LookupError('作品不存在')
        prefs = json.loads(row.get('generation_prefs_json') or '{}')
        notes = json.loads(row['contract_notes'] or '[]')
        contract = {key: str(prefs.get(key) or '') for key in TEXT_FIELDS}
        contract.update(target_chapters=int(row.get('target_chapters') or 0),
                        target_words_per_chapter=int(row.get('target_words_per_chapter') or 2500),
                        style_notes='\n\n'.join(note['content'] for note in notes),
                        premise_lock=str(row['contract_premise'] or ''))
        return {'contract': contract, 'version': stable_hash({'contract': contract, 'prefs': prefs, 'notes': notes}),
                'structured_market': bool(prefs.get('canonical_genre_id') or prefs.get('canonical_theme_id') or prefs.get('custom_market_profile'))}

    @staticmethod
    def _preferences(previous, contract, confirmed):
        raw = deepcopy(previous)
        raw.update({key: contract[key].strip() for key in TEXT_FIELDS})
        changed = any(str(previous.get(key) or '').strip() != raw[key] for key in PROFILE_FIELDS.values())
        structured = bool(raw.get('canonical_genre_id') or raw.get('canonical_theme_id') or raw.get('custom_market_profile'))
        if changed and structured:
            if not confirmed:
                raise ValueError('请核对并确认修改后的完整五维画像，再保存创作契约')
            custom = deepcopy(raw.get('custom_market_profile') or {})
            if not custom:
                custom = {'scope': 'project', 'audience_mode': raw.get('audience_mode'),
                    'custom_theme': {'id': 'custom.theme.creation-contract', 'label': raw.get('locked_genre') or '作者创作契约',
                        'parent_canonical_category_id': raw.get('canonical_genre_id')}}
                if raw.get('canonical_theme_id'):
                    custom['source_canonical_theme_id'] = raw['canonical_theme_id']
                    custom['custom_theme']['source_canonical_theme_id'] = raw['canonical_theme_id']
            custom['profile_review_status'] = 'user_confirmed'
            custom['custom_theme']['status'] = 'user_confirmed'
            custom['custom_theme']['profile'] = {dimension: raw[key] for dimension, key in PROFILE_FIELDS.items()}
            raw['custom_market_profile'] = custom
        return normalize_market_preferences(raw)

    def _guard_snapshot(self, table, novel_id, *, where='novel_id=?'):
        columns = [row['name'] for row in self.db.fetch_all(f'PRAGMA table_info("{table}")')]
        if not columns:
            return "NOT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)", (table,)
        names = ','.join('"' + column + '"' for column in columns)
        expression = (f'(SELECT json_group_array(json_array({names})) FROM '
                      f'(SELECT * FROM "{table}" WHERE {where} ORDER BY rowid))')
        snapshot = self.db.fetch_one(f'SELECT {expression} AS value', (novel_id,))['value']
        return f'{expression} IS ?', (novel_id, snapshot)

    def save(self, novel_id, *, version, contract, confirm_market_profile=False):
        before = self.get(novel_id)
        if before['version'] != version:
            raise ContractConflict('创作契约已在其他页面更新，请重新加载后再保存')
        if contract == before['contract']:
            return before
        if get_execution_lease(novel_id, self.db):
            raise ContractConflict('作品正在生成，请等待当前任务结束后修改创作契约')
        for stage in ('worldbuilding', 'characters', 'locations', 'plot_outline'):
            assert_setup_editable(novel_id, stage, db=self.db)
        self.db.execute('''CREATE TABLE IF NOT EXISTS creation_contract_mutations (
            mutation_id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, before_version TEXT NOT NULL,
            contract_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()
        guards, guard_params = [], []
        for table, where in [('novels', 'id=?'), ('bibles', 'novel_id=?'), ('bible_style_notes', 'novel_id=?'),
                             ('knowledge', 'novel_id=?'), ('setup_dag_runs', 'novel_id=?'),
                             ('variable_values', 'scope_key=?')]:
            scope = f'novel_id:{novel_id}' if table == 'variable_values' else novel_id
            condition, params = self._guard_snapshot(table, scope, where=where)
            guards.append(condition)
            guard_params.extend(params)
        for table, condition in [('novel_execution_leases', "expires_at>(julianday('now')-2440587.5)*86400"),
                                 ('chapter_derivation_jobs', "status='running'")]:
            if self.db.fetch_all(f'PRAGMA table_info("{table}")'):
                guards.append(f'NOT EXISTS(SELECT 1 FROM {table} WHERE novel_id=? AND {condition})')
                guard_params.append(novel_id)
            else:
                guards.append("NOT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)")
                guard_params.append(table)
        # Capture guards before the final input read; concurrent edits can never
        # become the editor's accepted basis while waiting for the single writer.
        if self.get(novel_id)['version'] != version:
            raise ContractConflict('创作契约已在其他页面更新，请重新加载后再保存')
        row = self.db.fetch_one('SELECT generation_prefs_json FROM novels WHERE id=?', (novel_id,))
        prefs = self._preferences(json.loads(row.get('generation_prefs_json') or '{}'), contract, confirm_market_profile)
        mutation_id = uuid.uuid4().hex
        gate = 'EXISTS(SELECT 1 FROM creation_contract_mutations WHERE mutation_id=?)'
        now = datetime.now(timezone.utc).isoformat()
        genre_parts = [part.strip() for part in prefs['locked_genre'].split('/') if part.strip()]
        values = {'novel.genre_label': prefs['locked_genre'], 'novel.genre_major': genre_parts[0] if genre_parts else '',
            'novel.genre_theme': ' / '.join(genre_parts[1:]),
            'novel.target_chapters': contract['target_chapters'], 'novel.target_words_per_chapter': contract['target_words_per_chapter'],
            'worldbuilding.style': contract['style_notes'], 'knowledge.premise_lock': contract['premise_lock'],
            **{f'novel.{key.removeprefix("locked_")}': prefs[key] for key in PROFILE_FIELDS.values()}}
        with self.db.transaction() as conn:
            receipt = conn.execute('''INSERT INTO creation_contract_mutations(mutation_id,novel_id,before_version,contract_json)
                SELECT ?,?,?,? WHERE ''' + ' AND '.join(guards),
                (mutation_id, novel_id, version, json.dumps(contract, ensure_ascii=False), *guard_params))
            conn.execute(f'UPDATE novels SET generation_prefs_json=?,target_chapters=?,target_words_per_chapter=?,updated_at=? WHERE id=? AND {gate}',
                (json.dumps(prefs, ensure_ascii=False), contract['target_chapters'], contract['target_words_per_chapter'], now, novel_id, mutation_id))
            if contract['style_notes'] != before['contract']['style_notes']:
                conn.execute(f'''INSERT INTO bibles(id,novel_id,updated_at) SELECT ?,?,? WHERE {gate}
                    ON CONFLICT(novel_id) DO UPDATE SET updated_at=excluded.updated_at''', (f'bible-{uuid.uuid4().hex}', novel_id, now, mutation_id))
                conn.execute(f'DELETE FROM bible_style_notes WHERE novel_id=? AND {gate}', (novel_id, mutation_id))
                if contract['style_notes']:
                    conn.execute(f'INSERT INTO bible_style_notes(id,novel_id,category,content,updated_at) SELECT ?,?,?,?,? WHERE {gate}',
                        (f'style-{uuid.uuid4().hex}', novel_id, 'general', contract['style_notes'], now, mutation_id))
            if contract['premise_lock'] != before['contract']['premise_lock']:
                conn.execute(f'''INSERT INTO knowledge(id,novel_id,premise_lock,updated_at) SELECT ?,?,?,? WHERE {gate}
                    ON CONFLICT(novel_id) DO UPDATE SET premise_lock=excluded.premise_lock,version=knowledge.version+1,updated_at=excluded.updated_at''',
                    (f'{novel_id}-knowledge', novel_id, contract['premise_lock'], now, mutation_id))
            # Append immutable Variable Hub versions/lineage in this same writer
            # batch. Frozen sessions keep their original versions and evidence.
            for key, value in values.items():
                scope = f'novel_id:{novel_id}'
                current = self.db.fetch_one('''SELECT value_json FROM variable_values
                    WHERE variable_key=? AND scope_key=? AND is_current=1''', (key, scope))
                # A matching global fallback still needs a book-owned value,
                # otherwise later global edits can change this saved contract.
                if current is not None and json.loads(current['value_json']) == value:
                    continue
                value_id = uuid.uuid4().hex
                conn.execute(f'''INSERT INTO variable_definitions(id,variable_key,display_name,value_type,scope_level,required,description,status,metadata_json)
                    SELECT ?,?,'',?,'novel',0,'','active',? WHERE {gate}
                    ON CONFLICT(variable_key) DO UPDATE SET value_type=excluded.value_type,scope_level=excluded.scope_level,updated_at=CURRENT_TIMESTAMP''',
                    (uuid.uuid4().hex, key, 'integer' if isinstance(value, int) else 'string', json.dumps({'stage': 'setup', 'source': 'creation_contract'}), mutation_id))
                conn.execute(f'UPDATE variable_values SET is_current=0 WHERE variable_key=? AND scope_key=? AND is_current=1 AND {gate}',
                    (key, scope, mutation_id))
                conn.execute(f'''INSERT INTO variable_values(id,variable_key,scope_level,scope_key,value_json,value_hash,version_number,is_current,source_trace_id,source_node_key,metadata_json)
                    SELECT ?,?,'novel',?,?,?,COALESCE((SELECT MAX(version_number) FROM variable_values WHERE variable_key=? AND scope_key=?),0)+1,
                    1,?,'creation_contract',? WHERE {gate}''',
                    (value_id, key, scope, json.dumps(value, ensure_ascii=False), json.dumps({'value': value}, ensure_ascii=False, sort_keys=True), key, scope, mutation_id,
                     json.dumps({'stage': 'setup', 'creation_contract_mutation_id': mutation_id}), mutation_id))
                conn.execute(f'''INSERT INTO variable_lineage(id,variable_value_id,source_trace_id,source_node_key,lineage_json)
                    SELECT ?,?,?,'creation_contract',? WHERE {gate}''',
                    (uuid.uuid4().hex, value_id, mutation_id, json.dumps({'source': 'creation_contract', 'previous_contract_version': version}), mutation_id))
        if receipt.rowcount != 1:
            raise ContractConflict('保存期间作品资料或执行状态已变化，未写入任何改动，请重新加载后再保存')
        persisted = self.get(novel_id)
        expected = {**contract, **{key: prefs[key] for key in TEXT_FIELDS}}
        if persisted['contract'] != expected:
            raise ContractConflict('创作契约保存后又被更新，请重新加载并核对最新内容')
        return persisted
