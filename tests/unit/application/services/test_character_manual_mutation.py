"""Author edits must persist and rename live references through the real writer."""
import json
from dataclasses import asdict
from contextlib import contextmanager

import pytest

from application.world.services.bible_service import BibleService
from application.world.services.character_manual_mutation import CharacterManualMutation, CharacterEditConflict
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


@pytest.fixture
def book(writer):
    queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','Test','mutation-book')")
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('other','Other','mutation-other')")
    bible = BibleService(SqliteBibleRepository(db))
    bible.create_bible('bible', 'book')
    bible.add_character('book', 'hero', '林舟', '林舟是守门人。', voice_profile={'style': '简洁'})
    bible.add_character('book', 'friend', '赵禾', '赵禾信任林舟。')
    bible.add_character('book', 'long-name', '林舟远', '林舟远是另一人。')
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,outline) VALUES ('ch1','book',1,'林舟归来','林舟见到赵禾，林舟远也在。','林舟回城')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('ch2','book',2,'再次相遇','赵禾记得林舟。')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('other-ch','other',1,'林舟没有改变。')")
    db.execute("INSERT INTO narrative_entities(id,novel_id,canonical_name) VALUES ('hero','book','林舟')")
    db.execute("INSERT INTO memory_atoms(id,novel_id,entity_id,status,payload_json,text_span) VALUES ('m1','book','friend','confirmed',?,'赵禾记得林舟。')", (json.dumps({'fact':'林舟救过赵禾。','target_id':'hero'},ensure_ascii=False),))
    db.execute("INSERT INTO memory_projections(novel_id,entity_id,projection_json) VALUES ('book','friend',?)", (json.dumps({'memory':'林舟是朋友。'},ensure_ascii=False),))
    db.execute("INSERT INTO triples(id,novel_id,subject,predicate,object,subject_entity_id,object_entity_id) VALUES ('tr','book','赵禾','朋友','林舟','friend','hero')")
    db.execute("INSERT INTO unified_character_relationships(character_id,target_id,target_name,relation,description) VALUES ('friend','hero','林舟','朋友','林舟救过我')")
    db.commit()
    return queue, db, bible, CharacterManualMutation(db)


def snapshot(bible, cid='hero'):
    return asdict(next(c for c in bible.get_bible_by_novel('book').characters if c.id==cid))


def test_rename_updates_live_chapters_memories_relationships_but_preserves_history(book):
    queue, db, bible, edits = book
    before = snapshot(bible)
    old_versions = db.fetch_all("SELECT * FROM content_versions WHERE chapter_id='ch1' ORDER BY revision")
    result = edits.update('book','hero', {'name':'沈砚','personality':'沉稳'}, before)
    saved = snapshot(bible)
    assert saved['name']=='沈砚' and saved['personality']=='沉稳'
    assert saved['voice_profile']==before['voice_profile']
    assert db.fetch_one("SELECT content,title,outline,revision FROM chapters WHERE id='ch1'")=={'content':'沈砚见到赵禾，林舟远也在。','title':'沈砚归来','outline':'沈砚回城','revision':1}
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch2'")['content']=='赵禾记得沈砚。'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='other-ch'")['content']=='林舟没有改变。'
    assert db.fetch_all("SELECT * FROM content_versions WHERE chapter_id='ch1' AND revision<=0 ORDER BY revision")==old_versions
    assert '沈砚' in db.fetch_one("SELECT description FROM unified_characters WHERE id='friend'")['description']
    assert db.fetch_one("SELECT target_name,target_id FROM unified_character_relationships WHERE character_id='friend'")=={'target_name':'沈砚','target_id':'hero'}
    atom=db.fetch_one("SELECT payload_json,text_span FROM memory_atoms WHERE id='m1'")
    assert json.loads(atom['payload_json'])=={'fact':'沈砚救过赵禾。','target_id':'hero'}
    assert atom['text_span']=='赵禾记得沈砚。'
    assert '沈砚' in db.fetch_one("SELECT projection_json FROM memory_projections WHERE entity_id='friend'")['projection_json']
    assert db.fetch_one("SELECT object,object_entity_id FROM triples WHERE id='tr'")=={'object':'沈砚','object_entity_id':'hero'}
    assert result['changed_chapters']==2
    assert db.fetch_all("SELECT status FROM chapter_derivation_jobs WHERE novel_id='book'")==[{'status':'pending'},{'status':'pending'}]
    assert queue._stats['failed']==0


def test_stale_editor_and_duplicate_name_do_not_write_anything(book):
    _, db, bible, edits = book
    before=snapshot(bible)
    db.execute("UPDATE unified_characters SET personality='作者已改' WHERE id='hero'")
    with pytest.raises(CharacterEditConflict): edits.update('book','hero',{'name':'沈砚'},before)
    assert snapshot(bible)['name']=='林舟'
    with pytest.raises(ValueError,match='同名'): edits.update('book','hero',{'name':'赵禾'},snapshot(bible))
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch1'")['content'].startswith('林舟')


def test_setup_or_running_writer_blocks_edit(book):
    _, db, bible, edits=book
    from application.engine.services.execution_lease import claim_execution_lease
    assert claim_execution_lease('book','autopilot','busy',120,db)
    with pytest.raises(CharacterEditConflict): edits.update('book','hero',{'personality':'改变'},snapshot(bible))
    assert snapshot(bible)['personality']==''


def test_delete_removes_roster_and_relationships_without_erasing_prose(book):
    _, db, bible, edits=book
    before=snapshot(bible)
    edits.delete('book','hero',before)
    assert 'hero' not in [c.id for c in bible.get_bible_by_novel('book').characters]
    assert db.fetch_one("SELECT id FROM unified_character_relationships WHERE target_id='hero'") is None
    assert db.fetch_one("SELECT lifecycle_status FROM narrative_entities WHERE id='hero'")['lifecycle_status']=='deleted'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch1'")['content'].startswith('林舟')
    assert snapshot(bible,'friend')['name']=='赵禾'


def test_non_name_edits_queue_reanalysis_without_rewriting_prose(book):
    _, db, bible, edits=book
    edits.update('book','hero',{'background':'曾为医师'},snapshot(bible))
    assert snapshot(bible)['background']=='曾为医师'
    assert db.fetch_one("SELECT revision FROM chapters WHERE id='ch1'")['revision']==0
    assert len(db.fetch_all("SELECT job_id FROM chapter_derivation_jobs WHERE novel_id='book'"))==2


def test_noop_does_not_create_versions_or_jobs(book):
    _, db,bible,edits=book
    result=edits.update('book','hero',{'name':'林舟'},snapshot(bible))
    assert result['changed_chapters']==0
    assert result['derivation_jobs']==0 and result['derivation_job_ids']==[]
    assert db.fetch_one("SELECT revision FROM chapters WHERE id='ch1'")['revision']==0
    assert db.fetch_all("SELECT job_id FROM chapter_derivation_jobs")==[]


def test_late_roster_change_is_protected_when_building_name_replacements(book,monkeypatch):
    _,db,bible,edits=book
    commit=edits._commit
    def changed_roster(*args,**kwargs):
        db.execute("UPDATE unified_characters SET name='林舟新',description='林舟新是独立角色' WHERE id='friend'")
        db.commit()
        return commit(*args,**kwargs)
    monkeypatch.setattr(edits,'_commit',changed_roster)
    edits.update('book','hero',{'name':'沈砚'},snapshot(bible))
    assert snapshot(bible,'friend')['name']=='林舟新'
    assert snapshot(bible,'friend')['description']=='林舟新是独立角色'


@pytest.mark.parametrize('field',['description','personality','voice_profile','moral_taboos','relationships'])
def test_null_patch_is_rejected_without_changing_saved_character(book,field):
    _,db,bible,edits=book
    before=snapshot(bible)
    with pytest.raises(ValueError,match='null'):
        edits.update('book','hero',{field:None},before)
    assert snapshot(bible)==before
    assert db.fetch_all('SELECT job_id FROM chapter_derivation_jobs')==[]


def test_writer_side_change_rejects_entire_batch(book,monkeypatch):
    _,db,bible,edits=book
    before=snapshot(bible)
    transaction=db.transaction
    @contextmanager
    def raced():
        db.execute("UPDATE chapters SET content='作者并发改稿' WHERE id='ch1'")
        db.commit()
        with transaction() as conn: yield conn
    monkeypatch.setattr(db,'transaction',raced)
    with pytest.raises(CharacterEditConflict): edits.update('book','hero',{'name':'沈砚'},before)
    assert snapshot(bible)['name']=='林舟'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch1'")['content']=='作者并发改稿'
    assert db.fetch_one("SELECT text_span FROM memory_atoms WHERE id='m1'")['text_span']=='赵禾记得林舟。'
    assert db.fetch_all('SELECT * FROM character_manual_mutations')==[]


def test_relationship_edit_preserves_ids_and_explicit_empty_fields(book):
    _,db,bible,edits=book
    before=snapshot(bible)
    edits.update('book','hero',{'voice_profile':{},'relationships':[{'target':'赵禾','relation':'朋友','description':'相识'}]},before)
    saved=snapshot(bible)
    assert saved['voice_profile']=={}
    assert saved['relationships']==[{'target':'赵禾','relation':'朋友','description':'相识'}]
    relation=db.fetch_one("SELECT id,target_id FROM unified_character_relationships WHERE character_id='hero'")
    assert relation['id'] and relation['target_id']=='friend'


def test_rename_appends_variable_version_and_lineage_without_rewriting_history(book):
    _,db,bible,edits=book
    db.execute("INSERT INTO variable_definitions(id,variable_key,value_type,scope_level) VALUES('rename-var','novel.premise','string','novel')")
    db.execute("INSERT INTO variable_values(id,variable_key,scope_level,scope_key,value_json,version_number) VALUES('rename-old','novel.premise','novel','novel_id:book',?,1)",
               (json.dumps('林舟守城。',ensure_ascii=False),))
    result=edits.update('book','hero',{'name':'沈砚'},snapshot(bible))
    rows=db.fetch_all("SELECT id,value_json,version_number,is_current FROM variable_values WHERE variable_key='novel.premise' ORDER BY version_number")
    assert [(json.loads(r['value_json']),r['version_number'],r['is_current']) for r in rows]==[
        ('林舟守城。',1,0),('沈砚守城。',2,1)]
    lineage=db.fetch_one('SELECT source_trace_id,source_node_key,lineage_json FROM variable_lineage WHERE variable_value_id=?',(rows[1]['id'],))
    assert lineage['source_trace_id']==result['mutation_id']
    assert lineage['source_node_key']=='character_manual_edit'
    assert json.loads(lineage['lineage_json'])['previous_value_id']=='rename-old'


@pytest.mark.asyncio
async def test_http_patch_roundtrip_and_automatic_background_analysis(book,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from interfaces.api.v1.world import bible as routes
    from interfaces.api import dependencies
    _,db,bible,_edits=book
    calls=[]
    class Pipeline:
        async def run_after_chapter_saved(self,novel_id,number,content,**kwargs):
            calls.append((novel_id,number,content))
            return {'narrative_sync_status':'ok'}
    monkeypatch.setattr(dependencies,'get_chapter_aftermath_pipeline',lambda:Pipeline())
    monkeypatch.setattr(dependencies,'get_knowledge_service',lambda:object())
    app=FastAPI();app.include_router(routes.router,prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        url='/api/v1/bible/novels/book/bible/characters/hero'
        before=snapshot(bible)
        response=await client.patch(url,json={'changes':{'name':'沈砚'},'expected_character':before})
        assert response.status_code==200,response.text
        assert next(c for c in response.json()['characters'] if c['id']=='hero')['name']=='沈砚'
        assert response.json()['character_mutation']['changed_chapters']==2
        assert [n for _,n,_ in calls]==[1,2]
        assert all('林舟' not in text.replace('林舟远','') for _,_,text in calls)
        assert db.fetch_all("SELECT status FROM chapter_derivation_jobs WHERE novel_id='book'")==[{'status':'completed'},{'status':'completed'}]
        conflict=await client.patch(url,json={'changes':{'personality':'旧草稿'},'expected_character':before})
        assert conflict.status_code==409
        deleted=await client.request('DELETE',url,json={'expected_character':snapshot(bible)})
        assert deleted.status_code==200,deleted.text
        assert 'hero' not in [c['id'] for c in deleted.json()['characters']]


@pytest.mark.asyncio
async def test_analysis_initialization_failure_preserves_saved_role_and_retryable_job(book,monkeypatch):
    from interfaces.api.v1.world import bible as routes
    from interfaces.api import dependencies
    _,db,bible,edits=book
    result=edits.update('book','hero',{'name':'沈砚'},snapshot(bible))
    def fail(): raise RuntimeError('synthetic unavailable')
    monkeypatch.setattr(dependencies,'get_chapter_aftermath_pipeline',fail)
    await routes._run_character_derivations(db,result['derivation_job_ids'])
    assert snapshot(bible)['name']=='沈砚'
    assert db.fetch_all("SELECT status FROM chapter_derivation_jobs WHERE novel_id='book'")==[{'status':'unavailable'},{'status':'unavailable'}]
