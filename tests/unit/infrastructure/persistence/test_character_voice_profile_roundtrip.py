"""Structured author voice data survives every canonical character writer."""
from dataclasses import replace
import json

from application.world.dtos.bible_dto import CharacterDTO
from application.world.services.generated_character_normalization import prepare_generated_characters
from domain.character.entities.character import Character
from domain.character.value_objects.character_id import CharacterId
from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
from tests.unit.application.services.test_generated_character_readback import character_store, save_characters  # noqa: F401


VOICE = {'style': '温和', 'sentence_pattern': '短句', 'speech_tempo': 'slow',
         'catchphrases': ['风会记得'], 'metaphors': ['浪潮'], 'custom_anchor': {'family': '说笑'}}


def test_bible_save_and_unrelated_edit_preserve_full_voice_profile(character_store):
    db, service = character_store
    original = CharacterDTO('hero', 'Lin', 'A keeper.', [], voice_profile=VOICE)
    saved = save_characters(service, [original])[0]
    assert saved.voice_profile == VOICE
    assert json.loads(db.fetch_one("SELECT voice_profile_json FROM unified_characters WHERE id='hero'")['voice_profile_json']) == VOICE
    saved = save_characters(service, [replace(saved, background='New background')])[0]
    assert saved.voice_profile == VOICE
    assert saved.background == 'New background'


def test_dynamic_state_writer_retains_full_voice_and_legacy_scalar_changes(character_store):
    db, service = character_store
    save_characters(service, [CharacterDTO('hero', 'Lin', 'A keeper.', [], voice_profile=VOICE)])
    repository = SqliteUnifiedCharacterRepository(db)
    dynamic = repository.get(CharacterId('hero'))
    assert dynamic.voice_profile == VOICE
    dynamic.update_state(3, mental_state='ANXIOUS', summary='受伤')
    dynamic.voice_style = '紧张'
    repository.save(dynamic)
    assert service.get_bible_by_novel('book').characters[0].voice_profile == {**VOICE, 'style': '紧张'}
    assert repository.get(CharacterId('hero')).current_state_summary == '受伤'
    # Existing producers construct an aggregate without the new optional field.
    legacy = Character(id=CharacterId('hero'), novel_id='book', name='Lin', voice_style='冷静')
    repository.save(legacy)
    voice = service.get_bible_by_novel('book').characters[0].voice_profile
    assert voice['catchphrases'] == VOICE['catchphrases']
    assert voice['style'] == '冷静'


def test_explicit_empty_voice_clears_stored_extras_and_legacy_projection(character_store):
    db, service = character_store
    saved = save_characters(service, [CharacterDTO('hero', 'Lin', 'A keeper.', [], voice_profile=VOICE)])[0]
    saved = save_characters(service, [replace(saved, voice_profile={})])[0]
    assert saved.voice_profile == {}
    assert db.fetch_one("SELECT voice_profile_json,voice_style,sentence_pattern,speech_tempo FROM unified_characters WHERE id='hero'") == {
        'voice_profile_json': '{}', 'voice_style': '', 'sentence_pattern': '', 'speech_tempo': ''}


def test_upsert_omitted_voice_retains_extensions_but_explicit_empty_clears_them(character_store):
    _db, service = character_store
    save_characters(service, [CharacterDTO('hero', 'Lin', 'A keeper.', [], voice_profile=VOICE)])
    saved = service.upsert_character('book', 'hero', 'Lin', 'New biography').characters[0]
    assert saved.voice_profile == VOICE
    saved = service.upsert_character('book', 'hero', 'Lin', 'New biography', voice_profile={}).characters[0]
    assert saved.voice_profile == {}


def test_legacy_voice_columns_and_generated_v13_normalization_remain_compatible(character_store):
    db, service = character_store
    save_characters(service, [CharacterDTO('hero', 'Lin', 'A keeper.', [])])
    db.execute("UPDATE unified_characters SET voice_profile_json=NULL,voice_style='Legacy',speech_tempo='Slow' WHERE id='hero'")
    assert service.get_bible_by_novel('book').characters[0].voice_profile == {'style': 'Legacy', 'speech_tempo': 'Slow'}
    _, generated = prepare_generated_characters('book', [{'name': 'Lin', 'voice_profile': {**VOICE, 'voice_style': 'Legacy'}}])
    assert generated[0].voice_profile == {key: VOICE[key] for key in ('style', 'sentence_pattern', 'speech_tempo')}
