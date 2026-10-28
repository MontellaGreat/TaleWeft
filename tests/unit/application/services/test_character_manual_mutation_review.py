"""Adversarial regressions for author edits; temporary SQLite + real writer only."""
import json
from contextlib import contextmanager

import pytest

from application.world.services.character_manual_mutation import CharacterEditConflict
from tests.unit.application.services.test_character_manual_mutation import book, snapshot, writer  # noqa: F401


def test_new_name_containing_old_name_is_applied_once(book):
    _, db, bible, edits = book
    edits.update('book', 'hero', {'name': '林舟云', 'description': '林舟云是新的正式名。'}, snapshot(bible))
    assert snapshot(bible)['name'] == '林舟云'
    assert snapshot(bible)['description'] == '林舟云是新的正式名。'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch1'")['content'] == '林舟云见到赵禾，林舟远也在。'


def test_all_structured_voice_fields_survive_save_and_readback(book):
    _, _db, bible, edits = book
    voice = {'style': '温和', 'sentence_pattern': '短句', 'speech_tempo': 'slow',
             'catchphrases': ['风会记得'], 'metaphors': ['浪潮'], 'custom_anchor': '只在亲友面前说笑'}
    edits.update('book', 'hero', {'voice_profile': voice}, snapshot(bible))
    assert snapshot(bible)['voice_profile'] == voice


def test_a_lease_acquired_after_snapshot_blocks_entire_mutation(book, monkeypatch):
    _, db, bible, edits = book
    before = snapshot(bible)
    transaction = db.transaction
    from application.engine.services.execution_lease import claim_execution_lease

    @contextmanager
    def raced():
        assert claim_execution_lease('book', 'autopilot', 'raced-lease', 120, db)
        with transaction() as connection:
            yield connection

    monkeypatch.setattr(db, 'transaction', raced)
    with pytest.raises(CharacterEditConflict):
        edits.update('book', 'hero', {'name': '沈砚'}, before)
    assert snapshot(bible)['name'] == before['name']
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch1'")['content'].startswith('林舟')
    assert db.fetch_all('SELECT * FROM character_manual_mutations') == []


def test_json_character_state_does_not_rewrite_identity_keys_or_values(book):
    _, db, bible, edits = book
    columns = {column['name'] for column in db.fetch_all('PRAGMA table_info(character_states)')}
    if not {'novel_id', 'character_id', 'base_traits'}.issubset(columns):
        pytest.skip('character state layout unavailable')
    before = json.dumps({'description': '林舟的旧身份', 'source_id': '林舟-source-identity'}, ensure_ascii=False)
    db.execute("INSERT INTO character_states(novel_id,character_id,base_traits) VALUES ('book','friend',?)", (before,))
    edits.update('book', 'hero', {'name': '沈砚'}, snapshot(bible))
    state = json.loads(db.fetch_one("SELECT base_traits FROM character_states WHERE novel_id='book' AND character_id='friend'")['base_traits'])
    assert state == {'description': '沈砚的旧身份', 'source_id': '林舟-source-identity'}


def test_deleted_character_projection_cannot_recreate_its_entity_or_legacy_memory(book):
    _, db, bible, edits = book
    from application.memory.services.character_projection_service import CharacterProjectionService
    from application.memory.services.narrative_memory_service import NarrativeMemoryService
    from infrastructure.persistence.database.sqlite_memory_repository import SqliteNarrativeMemoryRepository
    from infrastructure.persistence.database.sqlite_character_state_repository import SqliteCharacterStateRepository
    from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
    db.execute("INSERT INTO character_states(novel_id,character_id,current_state_summary) VALUES ('book','hero','林舟的遗留心理状态')")
    projection = CharacterProjectionService(memory_service=NarrativeMemoryService(SqliteNarrativeMemoryRepository(db)),
        unified_character_repository=SqliteUnifiedCharacterRepository(db), character_state_repository=SqliteCharacterStateRepository(db))
    assert projection.get_projection('book', 'hero')['name'] == '林舟'
    edits.delete('book', 'hero', snapshot(bible))
    with pytest.raises(LookupError):
        projection.get_projection('book', 'hero')
    assert db.fetch_one("SELECT lifecycle_status FROM narrative_entities WHERE novel_id='book' AND id='hero'")['lifecycle_status'] == 'deleted'
    assert db.fetch_one("SELECT 1 FROM memory_projections WHERE novel_id='book' AND entity_id='hero'") is None


def test_delete_invalidates_pending_analysis_that_was_prepared_for_the_old_roster(book):
    _, db, bible, edits = book
    result = edits.update('book', 'hero', {'personality': '更谨慎'}, snapshot(bible))
    assert result['derivation_jobs'] == 2
    edits.delete('book', 'hero', snapshot(bible))
    assert db.fetch_all("SELECT status FROM chapter_derivation_jobs WHERE novel_id='book'") == [
        {'status': 'superseded'}, {'status': 'superseded'}]


def test_deleted_name_is_neither_recommended_nor_applied_from_an_old_admission_plan(book):
    _, db, bible, edits = book
    from application.character.services.character_narrative_kernel import CharacterNarrativeKernel
    from application.character.dtos.character_narrative import ChapterCastPlan, NewCharacterCandidate
    from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
    kernel = CharacterNarrativeKernel(bible_service=bible, bible_repository=SqliteBibleRepository(db))
    old_plan = ChapterCastPlan(novel_id='book', chapter_number=3, new_character_candidates=[NewCharacterCandidate(
        name='林舟', evidence='林舟', narrative_function='explicit_scene_cast', recommendation='create_bible_character', confidence=0.8, reason='旧正文再次提及')])
    edits.delete('book', 'hero', snapshot(bible))
    candidates = kernel.detect_new_character_candidates('book', 3, '林舟回城', bible.get_bible_by_novel('book').characters,
        scene_director={'characters': ['林舟']})
    assert all(candidate.recommendation == 'ignore' for candidate in candidates if candidate.name == '林舟')
    kernel._apply_new_character_candidates(old_plan, 'unused-chapter-node')
    assert all(character.name != '林舟' for character in bible.get_bible_by_novel('book').characters)
    assert old_plan.slots == []


def test_delete_a_character_without_memory_yet_still_leaves_an_admission_tombstone(book):
    _, db, bible, edits = book
    assert db.fetch_one("SELECT id FROM narrative_entities WHERE id='long-name'") is None
    edits.delete('book', 'long-name', snapshot(bible, 'long-name'))
    assert db.fetch_one("SELECT canonical_name,lifecycle_status FROM narrative_entities WHERE novel_id='book' AND id='long-name'") == {
        'canonical_name': '林舟远', 'lifecycle_status': 'deleted'}


@pytest.mark.asyncio
async def test_deleted_character_read_models_return_404_without_reviving_memory(book):
    import httpx
    from fastapi import FastAPI
    from interfaces.api.v1.world import cast as routes
    from application.character.services.character_narrative_kernel import CharacterNarrativeKernel
    from application.memory.services.character_projection_service import CharacterProjectionService
    from application.memory.services.narrative_memory_service import NarrativeMemoryService
    from infrastructure.persistence.database.sqlite_memory_repository import SqliteNarrativeMemoryRepository
    from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
    _, db, bible, edits = book
    edits.delete('book', 'hero', snapshot(bible))
    kernel = CharacterNarrativeKernel(bible_service=bible)
    projection = CharacterProjectionService(memory_service=NarrativeMemoryService(SqliteNarrativeMemoryRepository(db)),
        unified_character_repository=SqliteUnifiedCharacterRepository(db))
    app = FastAPI(); app.include_router(routes.router, prefix='/api/v1')
    app.dependency_overrides[routes.get_character_narrative_kernel] = lambda: kernel
    app.dependency_overrides[routes.get_character_projection_service] = lambda: projection
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for suffix in ('projection', 'narrative-profile'):
            response = await client.get('/api/v1/novels/book/characters/hero/' + suffix)
            assert response.status_code == 404, response.text
    assert db.fetch_one("SELECT lifecycle_status FROM narrative_entities WHERE id='hero'")['lifecycle_status'] == 'deleted'
