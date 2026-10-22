"""Exercise chapter replacement with the real queued SQLite writer."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.world.services.chapter_projection_reconciliation import record_chapter_projection_changes, rollback_chapter_projections
from application.world.services.chapter_artifact_reconciliation import replace_chapter_generated_artifacts, clear_chapter_generated_artifacts
from tests.unit.application.engine.test_persistence_commit_ack import writer


def setup_book(db):
    from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository
    from domain.novel.entities.storyline import Storyline
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.value_objects.storyline_type import StorylineType
    from domain.novel.value_objects.storyline_status import StorylineStatus
    db.execute("INSERT INTO novels(id,title,slug) VALUES('book','Book','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('chapter','book',1,'one','body')")
    SqliteStorylineRepository(db).save(Storyline('main', NovelId('book'), StorylineType.MAIN_PLOT,
                                              StorylineStatus.ACTIVE, 1, 20))


def test_reverse_suffix_projection_replay_preserves_author_fields(writer):
    _, db = writer
    setup_book(db)
    with record_chapter_projection_changes(db, 'book', 1, ('storylines',)):
        db.execute("UPDATE storylines SET progress_summary='old-one',last_active_chapter=1 WHERE id='main'")
    with record_chapter_projection_changes(db, 'book', 2, ('storylines',)):
        db.execute("UPDATE storylines SET progress_summary='old-two',last_active_chapter=2,current_milestone_index=1 WHERE id='main'")
    db.execute("UPDATE storylines SET progress_summary='author decision' WHERE id='main'")
    rollback_chapter_projections(db, 'book', 1)
    row = db.fetch_one("SELECT progress_summary,last_active_chapter,current_milestone_index FROM storylines WHERE id='main'")
    assert row == {'progress_summary': 'author decision', 'last_active_chapter': 0, 'current_milestone_index': 0}
    with record_chapter_projection_changes(db, 'book', 1, ('storylines',)):
        db.execute("UPDATE storylines SET progress_summary='new-one',last_active_chapter=1 WHERE id='main'")
    assert db.fetch_one("SELECT progress_summary FROM storylines WHERE id='main'")['progress_summary'] == 'author decision'
    for _ in range(2):
        rollback_chapter_projections(db, 'book', 1)
        with record_chapter_projection_changes(db, 'book', 1, ('storylines',)):
            db.execute("UPDATE storylines SET progress_summary='again',last_active_chapter=1 WHERE id='main'")
        journal = db.fetch_one("SELECT after_json FROM chapter_projection_changes WHERE projection_table='storylines' AND row_id='main'")
        import json
        assert json.loads(journal['after_json'])['progress_summary'] == 'author decision'


def test_changed_plot_point_is_removed_before_regeneration_and_deleted_ai_row_stays_deleted(writer):
    _, db = writer
    setup_book(db)
    db.execute("INSERT INTO plot_arcs(id,novel_id,slug) VALUES('arc','book','default')")
    db.execute("INSERT INTO plot_points(id,plot_arc_id,sort_order,chapter_number,point_type,description,tension) VALUES('author-point','arc',1,9,'climax','author plan',8)")
    with record_chapter_projection_changes(db, 'book', 1, ('plot_points',)):
        db.execute("INSERT INTO plot_points(id,plot_arc_id,sort_order,chapter_number,point_type,description,tension) VALUES('auto-point','arc',0,1,'climax','old climax',9)")
    with record_chapter_projection_changes(db, 'book', 1, ('storylines',)):
        db.execute("INSERT INTO storylines(id,novel_id,storyline_type,status,estimated_chapter_start,estimated_chapter_end) VALUES('generated','book','romance','active',1,20)")
    db.execute("DELETE FROM storylines WHERE id='generated'")
    rollback_chapter_projections(db, 'book', 1)
    assert [r['id'] for r in db.fetch_all('SELECT id FROM plot_points')] == ['author-point']
    with record_chapter_projection_changes(db, 'book', 1, ('storylines',)):
        db.execute("INSERT INTO storylines(id,novel_id,storyline_type,status,estimated_chapter_start,estimated_chapter_end) VALUES('generated','book','romance','active',1,20)")
    assert db.fetch_one("SELECT id FROM storylines WHERE id='generated'") is None


@pytest.mark.asyncio
async def test_prop_automatic_transfers_and_lifecycle_rewind_for_rewritten_suffix(writer):
    from application.prop.services.lifecycle_syncer import PropLifecycleSyncer
    from domain.prop.entities.prop import Prop
    from domain.prop.value_objects.prop_id import PropId
    from domain.prop.value_objects.prop_event import PropEvent, PropEventType, PropEventSource
    from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository
    from infrastructure.persistence.database.sqlite_prop_event_repository import SqlitePropEventRepository
    _, db = writer
    setup_book(db)
    props, events = SqliteUnifiedPropRepository(db), SqlitePropEventRepository(db)
    prop_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
    props.save(Prop(PropId(prop_id), 'book', '铜铃', holder_character_id='hero'))
    def event(chapter, kind, **kwargs):
        return PropEvent(f'auto-{chapter}', prop_id, 'book', chapter, kind, PropEventSource.AUTO_LLM, **kwargs)
    extraction = SimpleNamespace(priority=1, name='model', extract=AsyncMock(side_effect=[
        [event(1, PropEventType.INTRODUCED)],
        [event(2, PropEventType.TRANSFERRED, from_holder_id='hero', to_holder_id='rival')],
        [],
    ]))
    syncer = PropLifecycleSyncer(props, events, [extraction])
    assert not (await syncer.sync('book', 1, '铜铃出现'))['errors']
    assert not (await syncer.sync('book', 2, '铜铃转交'))['errors']
    assert props.get(PropId(prop_id)).holder_character_id == 'rival'
    events.save(PropEvent('manual-note', prop_id, 'book', 2, PropEventType.UPGRADED, PropEventSource.MANUAL))
    rollback_chapter_projections(db, 'book', 1)
    assert props.get(PropId(prop_id)).holder_character_id == 'hero'
    assert props.get(PropId(prop_id)).lifecycle_state.value == 'DORMANT'
    assert [e.id for e in events.list_for_prop(prop_id)] == ['manual-note']
    assert not (await syncer.sync('book', 1, '删除铜铃情节'))['errors']
    assert props.get(PropId(prop_id)).lifecycle_state.value == 'DORMANT'


def test_replacement_withdraws_machine_candidates_and_preserves_calibrated_memory(writer):
    _, db = writer
    setup_book(db)
    for atom, status in [('candidate', 'candidate'), ('confirmed', 'confirmed'), ('rejected', 'rejected')]:
        db.execute("INSERT INTO memory_atoms(id,novel_id,entity_id,source,status,chapter_number,text_span) VALUES(?,'book','hero','chapter_extract',?,1,?)", (atom, status, atom))
    for identity, starred in [('machine', 0), ('starred', 1)]:
        db.execute("INSERT INTO triples(id,novel_id,subject,predicate,object,chapter_number,source_type,is_starred) VALUES(?,'book','a','holds','sword',1,'autopilot_extract',?)", (identity, starred))
    db.execute("INSERT INTO causal_edges(id,novel_id,source_event_summary,source_chapter,target_event_summary,is_resolved,resolved_chapter) VALUES('causal-old','book','promise',0,'payoff',1,1)")
    replace_chapter_generated_artifacts(db, 'book', 1, {'summary': 'A'})
    replace_chapter_generated_artifacts(db, 'book', 1, {'summary': 'B'})
    replace_chapter_generated_artifacts(db, 'book', 1, {})
    assert {r['id']: r['status'] for r in db.fetch_all('SELECT id,status FROM memory_atoms')} == {'candidate': 'superseded', 'confirmed': 'confirmed', 'rejected': 'rejected'}
    assert [r['id'] for r in db.fetch_all('SELECT id FROM triples')] == ['starred']
    assert db.fetch_one("SELECT is_resolved,resolved_chapter FROM causal_edges WHERE id='causal-old'") == {'is_resolved': 0, 'resolved_chapter': None}
    from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
    from infrastructure.persistence.database.sqlite_memory_repository import SqliteNarrativeMemoryRepository
    from domain.memory.entities import MemoryAtom
    SqliteKnowledgeRepository(db).save_triple('book', {'id': 'starred', 'subject': 'changed', 'predicate': 'lost', 'object': 'sword', 'chapter_number': 1, 'source_type': 'autopilot_extract'})
    assert db.fetch_one("SELECT subject,predicate,is_starred FROM triples WHERE id='starred'") == {'subject': 'a', 'predicate': 'holds', 'is_starred': 1}
    reextracted = SqliteNarrativeMemoryRepository(db).upsert_atom(MemoryAtom(
        id='other', novel_id='book', entity_id='hero', source='chapter_extract', status='candidate', chapter_number=1, text_span='confirmed'))
    assert reextracted.id == 'confirmed' and reextracted.status == 'confirmed'


@pytest.mark.asyncio
async def test_empty_manuscript_clears_derived_read_models_without_model_calls(writer):
    _, db = writer
    setup_book(db)
    indexing = SimpleNamespace(remove_chapter_summary=AsyncMock())
    await clear_chapter_generated_artifacts(db, indexing, 'book', 1)
    from application.core.services.chapter_version_service import content_hash
    indexing.remove_chapter_summary.assert_awaited_once_with('book', 1, [(0, content_hash('body'))])


@pytest.mark.asyncio
async def test_empty_chapter_does_not_report_completed_when_memory_clear_failed(writer, monkeypatch):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    _, db = writer
    setup_book(db)
    memory = SimpleNamespace(reconcile_from_chapter=AsyncMock(return_value={'errors': ['writer failed']}))
    monkeypatch.setattr('application.engine.services.memory_engine.MemoryEngine', lambda **kwargs: memory)
    pipeline = ChapterAftermathPipeline(None, None, None,
        chapter_repository=SimpleNamespace(db=db), bible_repository=object())
    result = await pipeline._run_after_chapter_saved('book', 1, '')
    assert result['derivation_status'] == 'unavailable'
    assert result['narrative_sync_ok'] is False
    assert result['stage_errors'] == ['memory_engine: writer failed']
    assert db.fetch_one("SELECT tension_score FROM chapters WHERE id='chapter'")['tension_score'] == -1


def test_stale_evolution_snapshot_preserves_human_patch_history(writer):
    from application.evolution.services.snapshot_service import EvolutionSnapshotService
    from infrastructure.persistence.database.sqlite_evolution_repository import SqliteEvolutionRepository
    from domain.evolution.models import ChapterEvolutionSnapshot, EvolutionAction
    _, db = writer
    setup_book(db)
    repo = SqliteEvolutionRepository(db)
    patches = [{'op': 'replace', 'path': '/characters/hero/status', 'value': 'ambiguous'}]
    repo.save(ChapterEvolutionSnapshot(snapshot_id='old', novel_id='book', branch_id='main', chapter_number=1,
                                      status='stale', human_override_patches=patches))
    extractor = SimpleNamespace(extract=lambda *args, **kwargs: [EvolutionAction(
        action_id='alive', type='SET_CHARACTER_STATUS', payload={'character_id': 'hero', 'status': 'alive'})])
    rebuilt = EvolutionSnapshotService(repo, extractor).build_after_chapter_saved('book', 1, 'new prose')
    assert rebuilt.human_override_patches == patches
    assert rebuilt.ending_state.characters['hero']['status'] == 'ambiguous'
