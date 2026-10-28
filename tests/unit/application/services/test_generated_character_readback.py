"""Generated character readback uses the production SQLite repository pair."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from application.ai_invocation.continuation import ContinuationContext
from application.ai_invocation.dtos import AdoptionDecision, InvocationPolicy, InvocationSession
from application.world.dtos.bible_dto import CharacterDTO
from application.world.services import bible_setup_continuation as continuation
from application.world.services.bible_service import BibleService
from application.world.services.generated_character_normalization import prepare_generated_characters
from infrastructure.persistence.database import connection as connection_module
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import _json_dumps
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository


@pytest.fixture
def character_store(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / 'character-readback.sqlite'))
    monkeypatch.setattr(connection_module, '_db_instance', db)
    monkeypatch.setattr(connection_module, '_db_instances_by_path', {str(Path(db.db_path).resolve()): db})
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','Synthetic book','synthetic-book')")
    service = BibleService(SqliteBibleRepository(db),
        unified_character_repository=SqliteUnifiedCharacterRepository(db))
    service.create_bible('bible', 'book')
    yield db, service
    db.close_all(skip_checkpoint=True)


def save_characters(service, characters):
    current = service.get_bible_by_novel('book')
    service.update_bible('book', characters, current.world_settings, current.locations,
        current.timeline_notes, current.style_notes)
    return service.get_bible_by_novel('book').characters


@pytest.mark.parametrize('profile,expected', [
    ({'style': 'Reserved', 'sentence_pattern': 'Short', 'speech_tempo': 'Slow'},
     {'style': 'Reserved', 'sentence_pattern': 'Short', 'speech_tempo': 'Slow'}),
    ({'voice_style': 'Reserved'}, {'style': 'Reserved'}),
    ({'speech_tempo': 120}, {'speech_tempo': '120'}),
    ({'style': {'z': 'last', 'a': 'first'}, 'sentence_pattern': ['short', {'z': 2, 'a': 1}]},
     {'style': '{"a": "first", "z": "last"}', 'sentence_pattern': '["short", {"a": 1, "z": 2}]'}),
])
def test_generated_voice_survives_both_sqlite_repositories(character_store, profile, expected):
    db, service = character_store
    _, characters = prepare_generated_characters('book', [{'name': 'Lin', 'voice_profile': profile}])
    assert characters[0].voice_profile == expected

    actual = save_characters(service, characters)[0]

    assert actual.voice_profile == expected
    row = db.fetch_one('SELECT voice_style,sentence_pattern,speech_tempo FROM unified_characters')
    assert tuple(row.values()) == tuple(expected.get(key, '') for key in ('style', 'sentence_pattern', 'speech_tempo'))
    assert asdict(actual) == asdict(characters[0])


@pytest.mark.parametrize('field', [
    'role', 'description', 'appearance', 'personality', 'flaw', 'background', 'ghost',
    'core_motivation', 'want', 'inner_lack', 'need', 'public_profile', 'hidden_profile',
    'mental_state', 'mental_state_reason', 'verbal_tic', 'idle_behavior', 'core_belief',
])
@pytest.mark.parametrize('value', [
    {'z': 'last', 'a': {'y': 'two', 'b': 'one'}},
    ['keep-list-order', {'z': 'last', 'a': {'y': 'two', 'b': 'one'}}],
])
def test_structured_character_text_matches_restored_commit_receipt(character_store, field, value):
    _, service = character_store
    source = {'name': 'Lin', field: deepcopy(value)}
    saved, characters = prepare_generated_characters('book', [source])
    actual = save_characters(service, characters)[0]

    # The real adoption repository sorts receipt keys before storing them.
    receipt = json.loads(_json_dumps({'continuation': {'characters': saved}}))
    _, restored = prepare_generated_characters('book', receipt['continuation']['characters'])

    assert asdict(actual) == asdict(restored[0])


def test_voice_edit_preserves_existing_chapter_state(character_store):
    db, service = character_store
    original = CharacterDTO('hero', 'Lin', 'A keeper.', [])
    save_characters(service, [original])
    db.execute("""UPDATE unified_characters SET role='PROTAGONIST',faction_id='guild',
        emotional_arc_json='[{"chapter":3}]',current_state_summary='Injured',
        last_updated_chapter=3,created_at='2026-01-01' WHERE id='hero'""")

    actual = save_characters(service, [replace(original, voice_profile={'style': 'Measured'})])[0]

    assert actual.voice_profile == {'style': 'Measured'}
    row = db.fetch_one("""SELECT role,faction_id,emotional_arc_json,current_state_summary,
        last_updated_chapter,created_at FROM unified_characters WHERE id='hero'""")
    assert json.loads(row.pop('emotional_arc_json')) == [{'chapter': 3}]
    assert tuple(row.values()) == ('PROTAGONIST', 'guild', 'Injured', 3, '2026-01-01')


@pytest.mark.parametrize('explicit_null', [False, True])
def test_generated_replacement_clears_previous_reveal_chapter(character_store, monkeypatch, explicit_null):
    _, service = character_store
    save_characters(service, [CharacterDTO('book-char-1', 'Lin', 'A keeper.', [], reveal_chapter=7)])
    raw = {'name': 'Lin', 'description': 'A keeper.'}
    if explicit_null:
        raw['reveal_chapter'] = None
    session = InvocationSession(id='session', operation='bible.setup.characters',
        node_key='bible-characters', policy=InvocationPolicy.FULL_INTERACTIVE, context={'novel_id': 'book'})
    decision = AdoptionDecision(id='decision', session_id='session', attempt_id='attempt',
        accepted_content=json.dumps({'characters': [raw]}))
    monkeypatch.setattr(continuation, '_get_services', lambda _: (service, None))
    monkeypatch.setattr(continuation, '_refresh_shared_state', lambda _: None)

    continuation.bible_characters_handler(ContinuationContext(session, decision))

    actual = service.get_bible_by_novel('book').characters[0]
    assert actual.reveal_chapter is None


def test_ordinary_character_edit_keeps_unspecified_reveal_chapter(character_store):
    _, service = character_store
    save_characters(service, [CharacterDTO('hero', 'Lin', 'A keeper.', [], reveal_chapter=7)])

    actual = save_characters(service, [CharacterDTO('hero', 'Lin', 'Edited description.', [])])[0]

    assert actual.reveal_chapter == 7
