"""Character recovery must wait for the real SQLite writer's guarded commit."""
from dataclasses import replace

from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
from application.world.dtos.bible_dto import CharacterDTO
from application.world.services.bible_service import BibleService
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


def test_character_voice_repair_waits_for_real_writer_and_preserves_other_fields(writer):
    queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','Synthetic','voice-writer')")
    repository = SqliteBibleRepository(db)
    service = BibleService(repository)
    bible = service.create_bible('bible', 'book')
    service.update_bible('book', [CharacterDTO('hero', 'Lin', 'A keeper.', [])],
        bible.world_settings, bible.locations, bible.timeline_notes, bible.style_notes)
    before = service.get_bible_by_novel('book').characters
    wanted = [replace(before[0], voice_profile={'style': 'Measured.', 'speech_tempo': 'Slow.'})]
    assert claim_execution_lease('book', 'setup', 'voice-repair', 120, db)
    try:
        repository.restore_missing_character_voices('book', before, wanted, run_id='voice-repair',
            validate_basis=lambda: None)
    finally:
        release_execution_lease('book', 'setup', 'voice-repair', db)
    assert service.get_bible_by_novel('book').characters == wanted
    assert queue.wait_until_idle(timeout=2)
    assert queue._stats['failed'] == 0
