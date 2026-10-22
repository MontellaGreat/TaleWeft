"""Character extraction must resolve canonical identities idempotently."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from application.analyst.services.state_updater import StateUpdater
from domain.bible.entities.bible import Bible
from domain.bible.entities.character import Character
from domain.bible.value_objects.character_id import CharacterId
from domain.novel.value_objects.chapter_state import ChapterState
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


NOVEL = "character-identity-book"


def make_state(candidates):
    return ChapterState(candidates, [], [], [], [], [])


def make_character(character_id, name, description="author character bible"):
    return Character(CharacterId(character_id), name, description)


@pytest.fixture
def identity_store(writer):
    _queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)", (NOVEL, "identity test", NOVEL))
    db.execute("""INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index)
                 VALUES ('identity-chapter-1',?,'chapter',1,'Chapter 1',1)""", (NOVEL,))
    repository = SqliteBibleRepository(db)
    bible = Bible("identity-bible", NovelId(NOVEL))
    bible.add_character(make_character("canonical-lin", "Lin Che"))
    repository.save(bible)
    return db, repository


def test_existing_normalized_name_is_not_recreated_and_dynamic_state_survives(identity_store):
    db, repository = identity_store
    db.execute("""UPDATE unified_characters SET role='PROTAGONIST', faction_id='harbor-watch',
        emotional_arc_json=?, current_state_summary='wounded', last_updated_chapter=1,
        created_at='2025-01-02T03:04:05' WHERE id='canonical-lin'""",
        (json.dumps([{"chapter": 1, "emotion": "alert"}], ensure_ascii=False),))
    updater = StateUpdater(repository, Mock(), db_connection=db.get_connection())
    state = make_state([
        {"name": "  Lin   Che  ", "description": "model duplicate", "first_appearance": 1},
        {"name": " New Person ", "description": "new arrival", "first_appearance": 1},
        {"name": "New Person", "description": "same batch duplicate", "first_appearance": 1},
        {"name": "unknown character", "description": "placeholder", "first_appearance": 1},
    ])

    updater.update_from_chapter(NOVEL, 1, state)
    rows = db.fetch_all("""SELECT id,name,description,role,faction_id,emotional_arc_json,
        current_state_summary,last_updated_chapter,created_at
        FROM unified_characters WHERE novel_id=? ORDER BY name""", (NOVEL,))
    assert len(rows) == 2
    lin = next(row for row in rows if row["id"] == "canonical-lin")
    new_person = next(row for row in rows if row["name"] == "New Person")
    assert lin["description"] == "author character bible"
    assert lin["role"] == "PROTAGONIST"
    assert lin["faction_id"] == "harbor-watch"
    assert json.loads(lin["emotional_arc_json"]) == [{"chapter": 1, "emotion": "alert"}]
    assert lin["current_state_summary"] == "wounded"
    assert lin["last_updated_chapter"] == 1
    assert lin["created_at"] == "2025-01-02T03:04:05"
    assert {row["element_id"] for row in db.fetch_all(
        "SELECT element_id FROM chapter_elements WHERE chapter_id='identity-chapter-1' AND element_type='character'"
    )} == {"canonical-lin", new_person["id"]}

    updater.update_from_chapter(NOVEL, 1, state)
    assert db.fetch_one("SELECT COUNT(*) AS n FROM unified_characters WHERE novel_id=?", (NOVEL,))["n"] == 2
    assert db.fetch_one("SELECT COUNT(*) AS n FROM chapter_elements WHERE chapter_id='identity-chapter-1' AND element_type='character'")["n"] == 2


def test_ambiguous_same_name_keeps_both_identities_and_does_not_guess_chapter_link(writer):
    _queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)", (NOVEL, "identity test", NOVEL))
    db.execute("""INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index)
                 VALUES ('identity-chapter-1',?,'chapter',1,'Chapter 1',1)""", (NOVEL,))
    repository = SqliteBibleRepository(db)
    bible = Bible("identity-bible", NovelId(NOVEL))
    bible.add_character(make_character("lin-a", "Lin Che", "first identity"))
    bible.add_character(make_character("lin-b", "Lin Che", "second identity"))
    repository.save(bible)
    knowledge = Mock()
    updater = StateUpdater(repository, Mock(), knowledge_service=knowledge, db_connection=db.get_connection())

    updater.update_from_chapter(NOVEL, 1, make_state([
        {"name": " Lin Che ", "description": "ambiguous candidate", "first_appearance": 1},
    ]))

    rows = db.fetch_all("SELECT id,description FROM unified_characters WHERE novel_id=? ORDER BY id", (NOVEL,))
    assert [(row["id"], row["description"]) for row in rows] == [
        ("lin-a", "first identity"),
        ("lin-b", "second identity"),
    ]
    assert db.fetch_one("SELECT COUNT(*) AS n FROM chapter_elements WHERE chapter_id='identity-chapter-1'")["n"] == 0
    knowledge.upsert_fact.assert_not_called()

