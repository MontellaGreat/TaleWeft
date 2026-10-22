"""Bible saves must retain canonical character identity and chapter state."""
import json
from contextlib import contextmanager

import pytest

from domain.bible.entities.bible import Bible
from domain.bible.entities.character import Character
from domain.bible.entities.world_setting import WorldSetting
from domain.bible.value_objects.character_id import CharacterId
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


CID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
NEW_CID = 'b1b2c3d4-e5f6-7890-abcd-ef1234567890'


def make_character(cid=CID, name='林澈'):
    return Character(CharacterId(cid), name, '旧人物设定', relationships=[{
        'target': '秦鸢', 'relation': '朋友', 'description': '共同办案',
    }])


def make_bible(book='book', bid='bible', characters=None):
    bible = Bible(bid, NovelId(book))
    for character in characters if characters is not None else [make_character()]:
        bible.add_character(character)
    bible.add_world_setting(WorldSetting('world-' + book, '旧城', '旧世界设定', 'rule'))
    return bible


@pytest.fixture
def store(writer):
    _, db = writer
    for book in ('book', 'other'):
        db.execute('INSERT INTO novels(id,title,slug) VALUES (?,?,?)', (book, book, book))
    repository = SqliteBibleRepository(db)
    repository.save(make_bible())
    return db, repository


def chapter_state(db):
    db.execute('''UPDATE unified_characters SET role='PROTAGONIST',faction_id='faction-1',
        emotional_arc_json=?,current_state_summary='章后已负伤',last_updated_chapter=2,
        mental_state='ALERT',mental_state_reason='遭人追踪',active_wounds_json=?,
        created_at='2025-01-01T00:00:00' WHERE id=?''',
        (json.dumps([{'chapter': 2, 'emotion': '警觉'}]),
         json.dumps([{'trigger': '旧伤', 'response': '疼痛'}]), CID))


def row(db, cid=CID):
    return db.fetch_one('SELECT rowid AS identity_rowid,* FROM unified_characters WHERE id=?', (cid,))


def assert_preserved(before, after):
    for field in ('identity_rowid', 'role', 'faction_id', 'emotional_arc_json',
                  'current_state_summary', 'last_updated_chapter', 'created_at'):
        assert after[field] == before[field], field


def test_adding_character_keeps_existing_chapter_state_and_identity(store):
    db, repository = store
    chapter_state(db)
    before = row(db)
    bible = repository.get_by_novel_id(NovelId('book'))
    assert not hasattr(bible.characters[0], 'role')
    assert not hasattr(bible.characters[0], 'faction_id')
    bible.add_character(make_character(NEW_CID, '新人物'))
    repository.save(bible)
    after = row(db)
    assert_preserved(before, after)
    for field in ('mental_state', 'mental_state_reason'):
        assert after[field] == before[field]
    assert json.loads(after['active_wounds_json']) == json.loads(before['active_wounds_json'])
    assert len(repository.get_by_novel_id(NovelId('book')).characters) == 2
    assert row(db, NEW_CID)['last_updated_chapter'] == 0


def test_bible_owned_fields_remain_editable_without_resetting_dynamic_fields(store):
    db, repository = store
    chapter_state(db)
    before = row(db)
    bible = repository.get_by_novel_id(NovelId('book'))
    char = bible.characters[0]
    char.name, char.description = '林新名', '作者修改设定'
    char.public_profile, char.hidden_profile, char.reveal_chapter = '公开档案', '秘密档案', 8
    char.gender, char.age, char.appearance = '男', '29', '长发'
    char.personality, char.background = '沉稳', '旧城出身'
    char.core_motivation, char.inner_lack = '寻找真相', '缺少信任'
    char.verbal_tic, char.idle_behavior = '再想想', '转动铜铃'
    char.voice_profile = {'style': '简练', 'sentence_pattern': '短句', 'speech_tempo': '慢'}
    char.core_belief, char.moral_taboos = '守信', ['背叛同伴']
    char.active_wounds = [{'trigger': '作者修改', 'response': '沉默'}]
    char.mental_state, char.mental_state_reason = 'CALM', '作者调整'
    repository.save(bible)
    after = row(db)
    assert_preserved(before, after)
    for key, value in {'name': '林新名', 'description': '作者修改设定', 'public_profile': '公开档案',
        'hidden_profile': '秘密档案', 'reveal_chapter': 8, 'gender': '男', 'age': '29',
        'appearance': '长发', 'personality': '沉稳', 'background': '旧城出身',
        'core_motivation': '寻找真相', 'inner_lack': '缺少信任', 'verbal_tic': '再想想',
        'idle_behavior': '转动铜铃', 'voice_style': '简练', 'sentence_pattern': '短句',
        'speech_tempo': '慢', 'core_belief': '守信', 'mental_state': 'CALM',
        'mental_state_reason': '作者调整'}.items():
        assert after[key] == value
    assert json.loads(after['moral_taboos_json']) == ['背叛同伴']
    assert json.loads(after['active_wounds_json']) == char.active_wounds


def test_relationship_collection_is_replaced_without_accumulation(store):
    db, repository = store
    bible = repository.get_by_novel_id(NovelId('book'))
    for _ in range(3):
        repository.save(bible)
    assert db.fetch_one('SELECT COUNT(*) AS n FROM unified_character_relationships')['n'] == 1
    bible.characters[0].relationships = [{'target': '新伙伴', 'relation': '同盟', 'description': '新关系'}]
    repository.save(bible)
    assert repository.get_by_novel_id(NovelId('book')).characters[0].relationships == [
        {'target': '新伙伴', 'relation': '同盟', 'description': '新关系'}]
    bible.characters[0].relationships = []
    repository.save(bible)
    assert db.fetch_one('SELECT COUNT(*) AS n FROM unified_character_relationships')['n'] == 0


def test_full_aggregate_removes_only_absent_characters(store):
    db, repository = store
    bible = repository.get_by_novel_id(NovelId('book'))
    bible.add_character(make_character(NEW_CID, '待删除人物'))
    repository.save(bible)
    chapter_state(db)
    before = row(db)
    bible.remove_character(CharacterId(NEW_CID))
    repository.save(bible)
    assert_preserved(before, row(db))
    assert row(db, NEW_CID) is None
    assert db.fetch_one('SELECT id FROM unified_character_relationships WHERE character_id=?', (NEW_CID,)) is None


def test_empty_aggregate_clears_characters_of_this_novel_only(store):
    db, repository = store
    repository.save(make_bible('other', 'other-bible', [make_character(NEW_CID)]))
    repository.save(make_bible(characters=[]))
    assert row(db) is None and row(db, NEW_CID)['novel_id'] == 'other'


def test_delete_bible_still_removes_all_its_children(store):
    db, repository = store
    repository.save(make_bible('other', 'other-bible', [make_character(NEW_CID)]))
    repository.delete('bible')
    assert repository.get_by_id('bible') is None and row(db) is None
    assert db.fetch_one("SELECT id FROM bible_world_settings WHERE novel_id='book'") is None
    assert db.fetch_one('SELECT id FROM unified_character_relationships WHERE character_id=?', (CID,)) is None
    assert row(db, NEW_CID)['novel_id'] == 'other'


@pytest.mark.parametrize('kind', ['bible', 'character'])
def test_cross_novel_identity_collision_rejected_without_changing_either_novel(store, kind):
    db, repository = store
    chapter_state(db)
    before = row(db)
    replacement = make_bible('other', 'bible' if kind == 'bible' else 'other-bible',
                             [make_character(NEW_CID if kind == 'bible' else CID)])
    with pytest.raises(ValueError, match='belongs to another novel'):
        repository.save(replacement)
    assert_preserved(before, row(db))
    assert repository.get_by_id('bible').novel_id.value == 'book'
    assert repository.get_by_id('other-bible') is None
    assert db.fetch_one("SELECT COUNT(*) AS n FROM bible_world_settings WHERE novel_id='book'")['n'] == 1


@pytest.mark.parametrize('kind', ['bible', 'character'])
def test_writer_ownership_guard_closes_validation_race(store, monkeypatch, kind):
    db, repository = store
    chapter_state(db)
    before = row(db)
    original_transaction = db.transaction
    bible = make_bible('other', 'racing-bible', [make_character(NEW_CID)])

    @contextmanager
    def insert_conflict_before_batch():
        if kind == 'bible':
            db.execute("INSERT INTO bibles(id,novel_id) VALUES ('racing-bible','book')")
        else:
            db.execute('INSERT INTO unified_characters(id,novel_id,name) VALUES (?,?,?)',
                       (NEW_CID, 'book', '并发创建的人物'))
        with original_transaction() as conn:
            yield conn

    if kind == 'bible':
        # Free the per-novel Bible uniqueness slot for the concurrent insert.
        db.execute("DELETE FROM bibles WHERE id='bible'")
    monkeypatch.setattr(db, 'transaction', insert_conflict_before_batch)
    from application.engine.services.persistence_queue import PersistenceCommitError
    with pytest.raises(PersistenceCommitError, match='NOT NULL'):
        repository.save(bible)
    assert_preserved(before, row(db))
    assert db.fetch_one("SELECT id FROM bible_world_settings WHERE novel_id='other'") is None
    if kind == 'character':
        assert row(db, NEW_CID)['novel_id'] == 'book'
        assert repository.get_by_id('racing-bible') is None


def test_failed_aggregate_save_does_not_partially_clear_or_update_characters(store):
    db, repository = store
    chapter_state(db)
    before = row(db)
    repository.save(make_bible('other', 'other-bible', [make_character(NEW_CID)]))
    replacement = make_bible(characters=[])
    replacement.add_world_setting(WorldSetting('world-other', '冲突设定', '同ID属于另一书', 'rule'))
    from application.engine.services.persistence_queue import PersistenceCommitError
    with pytest.raises(PersistenceCommitError, match='UNIQUE'):
        repository.save(replacement)
    assert_preserved(before, row(db))
    assert repository.get_by_id('bible').characters[0].name == '林澈'
    assert db.fetch_one('SELECT COUNT(*) AS n FROM unified_character_relationships WHERE character_id=?', (CID,))['n'] == 1
    assert repository.get_by_id('bible').world_settings[0].name == '旧城'
