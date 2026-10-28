"""Atomic author-owned character edits and live-reference reconciliation.

Reuse the canonical SQLite character store, chapter version triggers and durable
aftermath outbox. Historical model output, audits and content versions are never
rewritten. Every prepared write is gated by one writer-side snapshot receipt.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, fields
from uuid import uuid4

from application.ai_invocation.dtos import stable_hash
from application.core.services.chapter_version_service import content_hash
from application.onboarding.setup_edit_guard import assert_setup_editable
from application.world.dtos.bible_dto import CharacterDTO
from application.world.services.bible_service import BibleService
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository


class CharacterEditConflict(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


# Only live creative state. Evidence archives, prompts, model outputs, previous
# variable versions, drafts and historical chapter versions intentionally absent.
_LIVE_FIELDS = {
    'novels': 'premise generation_prefs_json',
    'unified_characters': ('name description public_profile hidden_profile gender age appearance personality background '
        'core_motivation inner_lack role verbal_tic idle_behavior voice_style sentence_pattern speech_tempo '
        'core_belief moral_taboos_json active_wounds_json mental_state_reason emotional_arc_json current_state_summary voice_profile_json'),
    'unified_character_relationships': 'target_name relation description',
    'story_nodes': 'title description content outline themes key_events narrative_arc conflicts metadata',
    'chapter_scenes': 'summary purpose content characters',
    'knowledge': 'premise_lock',
    'chapter_summaries': 'summary key_events open_threads consistency_note beat_sections micro_beats',
    'narrative_entities': 'canonical_name aliases_json',
    'memory_atoms': 'payload_json text_span',
    'memory_projections': 'projection_json',
    'memory_engine_state': 'state_json',
    'character_states': 'base_traits scars motivations emotional_arc current_state_summary',
    'triples': 'subject predicate object note description',
    'narrative_events': 'event_summary mutations tags',
    'story_characters': 'character_name role',
    'chapter_entity_mentions': 'display_label',
    'entity_base': 'name core_attributes',
    'causal_edges': 'source_event_summary target_event_summary state_change involved_characters',
    'narrative_debts': 'description involved_entities context',
    'storylines': 'extensions',
    'canonical_storylines': 'title aliases_json goal conflict promise_tags_json',
    'storyline_milestones': 'title description milestone_triggers',
    'plot_arcs': 'display_name extensions',
    'plot_points': 'description',
    'beat_sheets': 'data',
    'unified_props': 'description attributes_json',
    'foreshadows': 'description',
    'novel_foreshadow_registry': 'payload',
    'worldbuilding': ('power_system physics_rules magic_tech terrain climate resources ecology politics economy '
        'class_system history religion taboos food_clothing language_slang entertainment dimensions'),
    'bible_world_settings': 'name description',
    'bible_locations': 'name description',
    'bible_timeline_notes': 'event time_point description',
    'bible_style_notes': 'content',
    'chapter_bridges': 'bridge_data',
    'narrative_contracts': 'title_promise core_question theme_anchors_json forbidden_early_payoffs_json reveal_budget_json',
}
_JSON_FIELDS = set(('moral_taboos_json active_wounds_json emotional_arc_json aliases_json payload_json projection_json '
    'state_json metadata key_events open_threads beat_sections micro_beats mutations tags core_attributes '
    'involved_characters involved_entities extensions promise_tags_json dimensions payload bridge_data '
    'theme_anchors_json forbidden_early_payoffs_json reveal_budget_json characters generation_prefs_json '
    'attributes_json data voice_profile_json base_traits scars motivations emotional_arc state_change '
    'themes narrative_arc conflicts').split())
_COLUMN_MAP = {'moral_taboos': 'moral_taboos_json', 'active_wounds': 'active_wounds_json'}
_JSON_ID_KEY = re.compile(r'(^id$|_ids?$|_hash$|^hash$|_at$|^source_attempt|^source_session|^source_commit)')


class CharacterManualMutation:
    def __init__(self, db):
        self.db = db
        self.bible = BibleService(SqliteBibleRepository(db))

    def _columns(self, table):
        return [r['name'] for r in self.db.fetch_all(f'PRAGMA table_info({_quote(table)})')]

    def _scope(self, table, novel_id):
        columns = self._columns(table)
        if not columns:
            return None
        if table=='novels':
            return 'id=?',(novel_id,)
        if 'novel_id' in columns:
            return 'novel_id=?', (novel_id,)
        if table == 'unified_character_relationships':
            return 'character_id IN (SELECT id FROM unified_characters WHERE novel_id=?)', (novel_id,)
        if table == 'chapter_summaries':
            return 'knowledge_id IN (SELECT id FROM knowledge WHERE novel_id=?)', (novel_id,)
        if table in ('chapter_scenes', 'chapter_elements'):
            return 'chapter_id IN (SELECT id FROM story_nodes WHERE novel_id=?)', (novel_id,)
        if table=='beat_sheets':
            return 'chapter_id IN (SELECT id FROM chapters WHERE novel_id=?)',(novel_id,)
        if table == 'storyline_milestones':
            return 'storyline_id IN (SELECT id FROM storylines WHERE novel_id=?)', (novel_id,)
        if table=='plot_points':
            return 'plot_arc_id IN (SELECT id FROM plot_arcs WHERE novel_id=?)',(novel_id,)
        return None

    def _rows(self, table, novel_id):
        scope = self._scope(table, novel_id)
        if scope is None:
            return []
        where, params = scope
        return self.db.fetch_all(f'SELECT rowid AS _edit_rowid_,* FROM {_quote(table)} WHERE {where} ORDER BY rowid',params)

    def _guard_snapshot(self, table, where, params):
        columns = self._columns(table)
        names = ','.join(_quote(c) for c in columns)
        expr = (f'(SELECT json_group_array(json_array({names})) FROM '
                f'(SELECT * FROM {_quote(table)} WHERE {where} ORDER BY rowid))')
        snapshot = self.db.fetch_one(f'SELECT {expr} AS value',params)['value']
        return f'{expr} IS ?', (*params, snapshot)

    def _current(self, novel_id, character_id, expected):
        dto = self.bible.get_bible_by_novel(novel_id)
        if dto is None:
            raise LookupError('作品设定不存在')
        character = next((c for c in dto.characters if c.id == character_id), None)
        if character is None:
            raise LookupError('角色不存在或已删除')
        if not isinstance(expected,dict) or asdict(character) != expected:
            raise CharacterEditConflict('角色已被其他操作修改，请刷新档案后再保存，当前草稿已保留')
        assert_setup_editable(novel_id, 'characters', db=self.db)
        self._assert_idle(novel_id)
        return dto, asdict(character)

    def _assert_idle(self, novel_id):
        if self._columns('novel_execution_leases') and self.db.fetch_one(
            "SELECT 1 AS busy FROM novel_execution_leases WHERE novel_id=? AND expires_at>(julianday('now')-2440587.5)*86400",(novel_id,)):
            raise CharacterEditConflict('本书正在生成或审核，请停止并等待执行退出后再编辑角色')
        if self.db.fetch_one("SELECT 1 AS busy FROM chapter_derivation_jobs WHERE novel_id=? AND status='running'",(novel_id,)):
            raise CharacterEditConflict('本书关联分析正在执行，请稍后重试')

    @staticmethod
    def _renamer(old, new, names):
        # Longest canonical name wins: changing 林舟 never alters 林舟远.
        # Protect the new name too (林舟 -> 林舟云 must never become 林舟云云).
        alternatives = sorted({name for name in [old,new,*names] if name},key=len,reverse=True)
        patterns=[]
        for name in alternatives:
            escaped=re.escape(name)
            if name and name[0].isascii() and name[0].isalnum(): escaped=r'(?<![A-Za-z0-9_])'+escaped
            if name and name[-1].isascii() and name[-1].isalnum(): escaped+=r'(?![A-Za-z0-9_])'
            patterns.append(escaped)
        regex=re.compile('|'.join(patterns))
        return lambda text: regex.sub(lambda m:new if m.group(0)==old else m.group(0),text)

    @staticmethod
    def _transform(value, rename, key=''):
        if _JSON_ID_KEY.search(key):
            return value
        if isinstance(value,str): return rename(value)
        if isinstance(value,list): return [CharacterManualMutation._transform(v,rename,key) for v in value]
        if isinstance(value,dict):
            return {rename(k):CharacterManualMutation._transform(v,rename,k) for k,v in value.items()}
        return value

    def update(self, novel_id, character_id, changes, expected):
        dto,before=self._current(novel_id,character_id,expected)
        allowed={f.name for f in fields(CharacterDTO)}-{'id'}
        if not isinstance(changes,dict) or set(changes)-allowed:
            raise ValueError('包含不支持的角色字段，角色 ID 不可修改')
        if any(value is None and key != 'reveal_chapter' for key,value in changes.items()):
            raise ValueError('角色字段不能为 null；清空文本请传空字符串，列表或声线请传空集合')
        after={**before,**changes}
        after['name']=str(after['name'] or '').strip()
        if not after['name']: raise ValueError('角色名称不能为空')
        if any(c.id!=character_id and c.name==after['name'] for c in dto.characters):
            raise ValueError('本书已有同名角色，请使用唯一名称')
        # Validate the complete public DTO including reveal_chapter.
        CharacterDTO(**after)
        if before==after:
            return {'changed_chapters':0,'refreshed_records':0,'derivation_jobs':0,
                    'derivation_job_ids':[],'renamed':False,'deleted':False}
        rename=None
        if before['name']!=after['name']:
            rename=self._renamer(before['name'],after['name'],[c.name for c in dto.characters])
        return self._commit(novel_id,character_id,before,after,rename,delete=False)

    def delete(self, novel_id, character_id, expected):
        _,before=self._current(novel_id,character_id,expected)
        return self._commit(novel_id,character_id,before,None,None,delete=True)

    def _commit(self,novel_id,character_id,before,after,rename,*,delete):
        from application.engine.services.execution_lease import ensure_execution_schema
        from application.onboarding.setup_dag import SetupDAGStore
        ensure_execution_schema(self.db)
        SetupDAGStore(self.db).ensure()
        self.db.execute('''CREATE TABLE IF NOT EXISTS character_manual_mutations (
            mutation_id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, character_id TEXT NOT NULL,
            operation TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT,
            summary_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()
        mutation_id=uuid4().hex
        gate='EXISTS(SELECT 1 FROM character_manual_mutations WHERE mutation_id=?)'
        operations=[]
        guards=[]; guard_params=[]
        tables={**_LIVE_FIELDS,'chapters':'','chapter_elements':'','chapter_derivation_jobs':'',
                'chapter_evolution_snapshots':'','setup_dag_runs':''}
        cached={}
        for table in tables:
            scope=self._scope(table,novel_id)
            if scope is None: continue
            condition,params=self._guard_snapshot(table,*scope)
            guards.append(condition); guard_params.extend(params)
            cached[table]=self._rows(table,novel_id)
        if self._columns('novel_execution_leases'):
            guards.append("NOT EXISTS(SELECT 1 FROM novel_execution_leases WHERE novel_id=? AND expires_at>(julianday('now')-2440587.5)*86400)")
            guard_params.append(novel_id)
        guards.append("NOT EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE novel_id=? AND status='running')")
        guard_params.append(novel_id)
        # Snapshot collection happens before the DTO read so a concurrent edit
        # between the editor check and snapshot collection cannot be adopted.
        self._current(novel_id,character_id,before)
        if rename:
            # A rename also changes world/plot text. Each affected setup stage
            # must have finished its audit before any of its inputs are edited.
            for stage in ('worldbuilding','locations','plot_outline'):
                assert_setup_editable(novel_id,stage,db=self.db)
            names=[row['name'] for row in cached['unified_characters']]
            if any(row['id']!=character_id and row['name']==after['name']
                   for row in cached['unified_characters']):
                raise ValueError('本书已有同名角色，请使用唯一名称')
            rename=self._renamer(before['name'],after['name'],names)
            after=self._transform(after,rename)
            after['id']=character_id
        refreshed=0; changed_chapters=0
        def update_row(table,row,patch):
            if not patch: return
            assignments=','.join(f'{_quote(k)}=?' for k in patch)
            operations.append((f'UPDATE {_quote(table)} SET {assignments} WHERE rowid=? AND {gate}',
                               (*patch.values(),row['_edit_rowid_'],mutation_id)))
        if rename:
            for table,field_names in _LIVE_FIELDS.items():
                for row in cached.get(table,[]):
                    if table=='memory_atoms' and row.get('status') in ('rejected','superseded'): continue
                    patch={}
                    for field in field_names.split():
                        value=row.get(field)
                        if not isinstance(value,str) or not value: continue
                        if field in _JSON_FIELDS and value.lstrip().startswith(('{','[')):
                            try: parsed=json.loads(value)
                            except ValueError: raise ValueError(f'{table} 中存在无法读取的关联资料，请修复后重试')
                            changed=self._transform(parsed,rename)
                            if changed!=parsed and table=='novels' and field=='generation_prefs_json':
                                from application.core.taxonomy.market_selection import normalize_market_preferences
                                changed=normalize_market_preferences(changed)
                            revised=_json(changed) if changed!=parsed else value
                        else: revised=rename(value)
                        if revised!=value: patch[field]=revised
                    if patch:
                        update_row(table,row,patch); refreshed+=1
            for row in cached.get('chapters',[]):
                patch={key:rename(row.get(key) or '') for key in ('content','title','outline','generation_hint')
                       if rename(row.get(key) or '')!=(row.get(key) or '')}
                if patch:
                    patch.update(content_hash=content_hash(patch.get('content',row['content'])),
                                 revision=int(row['revision'] or 0)+1,version_source='character_rename',
                                 word_count=len(patch.get('content',row['content']) or ''))
                    update_row('chapters',row,patch); changed_chapters+=1
        target=next(row for row in cached['unified_characters'] if row['id']==character_id)
        if delete:
            operations.append((f'DELETE FROM unified_character_relationships WHERE '
                f'(character_id=? OR (character_id IN (SELECT id FROM unified_characters WHERE novel_id=?) '
                f'AND (target_id=? OR target_name=?))) AND {gate}',
                (character_id,novel_id,character_id,before['name'],mutation_id)))
            operations.append((f'DELETE FROM chapter_elements WHERE element_type=\'character\' AND element_id=? '
                f'AND chapter_id IN(SELECT id FROM story_nodes WHERE novel_id=?) AND {gate}',(character_id,novel_id,mutation_id)))
            operations.append((f'UPDATE story_nodes SET pov_character_id=NULL WHERE novel_id=? AND pov_character_id=? AND {gate}',(novel_id,character_id,mutation_id)))
            operations.append((f'DELETE FROM story_characters WHERE novel_id=? AND character_id=? AND {gate}',(novel_id,character_id,mutation_id)))
            operations.append((f'''INSERT INTO narrative_entities(id,novel_id,canonical_name,lifecycle_status)
                SELECT ?,?,?,'deleted' WHERE {gate}
                ON CONFLICT(id) DO UPDATE SET lifecycle_status='deleted',canonical_name=excluded.canonical_name,
                updated_at=CURRENT_TIMESTAMP WHERE narrative_entities.novel_id=excluded.novel_id''',
                (character_id,novel_id,before['name'],mutation_id)))
            # Other characters' projections can embed this character in their
            # relationship view. Rebuild all current projections from the now
            # updated canonical roster instead of leaving stale cached roles.
            operations.append((f'DELETE FROM memory_projections WHERE novel_id=? AND {gate}',(novel_id,mutation_id)))
            operations.append((f"UPDATE memory_atoms SET status='superseded' WHERE novel_id=? AND entity_id=? AND status NOT IN ('rejected','superseded') AND {gate}",(novel_id,character_id,mutation_id)))
            operations.append((f'DELETE FROM triples WHERE novel_id=? AND (subject_entity_id=? OR object_entity_id=? OR subject=? OR object=?) AND {gate}',
                (novel_id,character_id,character_id,before['name'],before['name'],mutation_id)))
            operations.append((f'UPDATE unified_props SET holder_character_id=NULL WHERE novel_id=? AND holder_character_id=? AND {gate}',(novel_id,character_id,mutation_id)))
            operations.append((f'DELETE FROM character_states WHERE novel_id=? AND character_id=? AND {gate}',(novel_id,character_id,mutation_id)))
            operations.append((f'DELETE FROM memory_engine_state WHERE novel_id=? AND {gate}',(novel_id,mutation_id)))
            operations.append((f"UPDATE chapter_derivation_jobs SET status='superseded',last_error='角色已删除，旧角色名单下的关联任务已失效',updated_at=CURRENT_TIMESTAMP WHERE novel_id=? AND status IN ('pending','unavailable') AND {gate}",(novel_id,mutation_id)))
            operations.append((f'DELETE FROM unified_characters WHERE novel_id=? AND id=? AND {gate}',(novel_id,character_id,mutation_id)))
        else:
            patch={}
            for key,value in after.items():
                if key in ('id','relationships','voice_profile'): continue
                column=_COLUMN_MAP.get(key,key)
                patch[column]=_json(value) if key in _COLUMN_MAP else value
            for key,column in (('style','voice_style'),('sentence_pattern','sentence_pattern'),('speech_tempo','speech_tempo')):
                patch[column]=str((after.get('voice_profile') or {}).get(key,'') or '')
            if 'voice_profile_json' in self._columns('unified_characters'):
                patch['voice_profile_json']=_json(after.get('voice_profile') or {})
            update_row('unified_characters',target,patch)
            if before['relationships']!=after['relationships']:
                operations.append((f'DELETE FROM unified_character_relationships WHERE character_id=? AND {gate}',(character_id,mutation_id)))
                names={r['name']:r['id'] for r in cached['unified_characters']}
                names[after['name']]=character_id
                for relation in after['relationships']:
                    rel=relation if isinstance(relation,dict) else {'target':str(relation),'relation':'','description':''}
                    name=str(rel.get('target') or '')
                    operations.append((f'INSERT INTO unified_character_relationships(id,character_id,target_id,target_name,relation,description) SELECT ?,?,?,?,?,? WHERE {gate}',
                        (uuid4().hex,character_id,names.get(name),name,str(rel.get('relation') or ''),str(rel.get('description') or ''),mutation_id)))
        # Current variable projections receive new immutable value versions,
        # including the full edited roster. Historical invocation bindings stay.
        vwhere="is_current=1 AND instr('|'||scope_key||'|',?)>0"
        cond,params=self._guard_snapshot('variable_values',vwhere,('|novel_id:'+novel_id+'|',))
        guards.append(cond);guard_params.extend(params)
        variable_rows=self.db.fetch_all("SELECT rowid AS _edit_rowid_,* FROM variable_values WHERE "+vwhere,('|novel_id:'+novel_id+'|',))
        roster=[after if c.id==character_id else self._transform(asdict(c),rename) if rename else asdict(c)
                for c in self.bible.get_bible_by_novel(novel_id).characters if not(delete and c.id==character_id)]
        for row in variable_rows:
            raw=json.loads(row['value_json'])
            key=row['variable_key']
            if key in ('characters.list','characters'): revised=roster
            elif key=='characters.protagonist': revised=next((c for c in roster if c['id']==(raw.get('id') if isinstance(raw,dict) else None)),roster[0] if roster else None)
            else: revised=self._transform(raw,rename) if rename else raw
            if revised==raw: continue
            new_id=uuid4().hex
            operations.append((f'UPDATE variable_values SET is_current=0 WHERE id=? AND {gate}',(row['id'],mutation_id)))
            operations.append((f'''INSERT INTO variable_values(id,variable_key,scope_level,scope_key,value_json,value_hash,version_number,is_current,
                source_trace_id,source_node_key,metadata_json) SELECT ?,?,?,?,?,?,?,1,?,'character_manual_edit',? WHERE {gate}''',
                (new_id,key,row['scope_level'],row['scope_key'],_json(revised),stable_hash(revised),row['version_number']+1,
                 mutation_id,_json({'character_mutation_id':mutation_id}),mutation_id)))
            operations.append((f'''INSERT INTO variable_lineage(id,variable_value_id,source_trace_id,source_node_key,lineage_json)
                SELECT ?,?,?,'character_manual_edit',? WHERE {gate}''',
                (uuid4().hex,new_id,mutation_id,_json({'source':'character_manual_edit',
                    'previous_value_id':row['id'],'character_id':character_id}),mutation_id)))
        jobs=[]
        if not delete:
            for row in cached.get('chapters',[]):
                content=rename(row.get('content') or '') if rename else row.get('content') or ''
                if not content.strip(): continue
                job_id=uuid4().hex
                payload=_json({'content':content,'operation':'retry_aftermath','character_mutation_id':mutation_id})
                operations.append((f'''INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json)
                    SELECT ?,id,novel_id,number,revision,? FROM chapters WHERE id=? AND {gate}
                    ON CONFLICT(chapter_id,revision) DO UPDATE SET status='pending',payload_json=excluded.payload_json,
                    last_error='',cascade_status='not_requested',cascade_error='',updated_at=CURRENT_TIMESTAMP''',
                    (job_id,payload,row['id'],mutation_id)))
                jobs.append(row['id'])
            operations.append((f"UPDATE chapter_summaries SET sync_status='stale' WHERE knowledge_id IN(SELECT id FROM knowledge WHERE novel_id=?) AND {gate}",(novel_id,mutation_id)))
            operations.append((f"UPDATE chapter_evolution_snapshots SET status='stale' WHERE novel_id=? AND {gate}",(novel_id,mutation_id)))
        summary={'mutation_id':mutation_id,'renamed':bool(rename),'changed_chapters':changed_chapters,
                 'refreshed_records':refreshed,'derivation_jobs':jobs,'deleted':delete}
        with self.db.transaction() as conn:
            receipt=conn.execute('''INSERT INTO character_manual_mutations(mutation_id,novel_id,character_id,operation,before_json,after_json,summary_json)
                SELECT ?,?,?,?,?,?,? WHERE '''+' AND '.join(guards),
                (mutation_id,novel_id,character_id,'delete' if delete else 'update',_json(before),_json(after),_json(summary),*guard_params))
            for sql,params in operations: conn.execute(sql,params)
        if receipt.rowcount!=1:
            raise CharacterEditConflict('保存期间作品资料、章节或执行状态已变化，未写入任何改动，请刷新后重试')
        current_jobs=self.db.fetch_all("SELECT job_id FROM chapter_derivation_jobs WHERE novel_id=? AND chapter_id IN (SELECT value FROM json_each(?)) AND status='pending' ORDER BY chapter_number",
            (novel_id,_json(jobs))) if jobs else []
        summary['derivation_job_ids']=[j['job_id'] for j in current_jobs]
        summary['derivation_jobs']=len(current_jobs)
        return summary
