"""Destructive changes use real SQLite writer ACK, durable snapshots and guarded undo."""
import json

import pytest

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW
from application.snapshot.services.data_change_journal import DataChangeJournal, DataChangeConflict
from application.audit.services.macro_merge_engine import MergeConflictException


@pytest.fixture
def journal(daemon_writer):
    db = daemon_writer[0]
    db.execute("UPDATE novels SET current_stage='paused_for_review' WHERE id='novel'")
    for node_id, parent, kind, number in [('part', None, 'part', 1), ('volume', 'part', 'volume', 1), ('act', 'volume', 'act', 1), ('node-chapter', 'act', 'chapter', 1)]:
        db.execute("INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index) VALUES (?,'novel',?,?,?,?,0)", (node_id, parent, kind, number, node_id))
    db.execute("INSERT INTO chapter_elements(id,chapter_id,element_type,element_id,relation_type) VALUES ('element','node-chapter','location','location','scene')")
    db.execute("INSERT INTO beat_sheets(id,chapter_id,data) VALUES ('beats','chapter','{\"scenes\":[1]}')")
    return DataChangeJournal(db)


def apply(journal, proposal, rollback=False):
    return journal.apply('novel', proposal['proposal_id'], expected_hash=proposal['after_hash'] if rollback else proposal['base_hash'],
        proposal_hash=proposal['proposal_hash'], run_epoch=proposal['run_epoch'], rollback=rollback)


def test_proposal_captures_authored_body_without_mutating_it(journal):
    proposal = journal.propose_delete('novel', 'act')
    assert proposal['status'] == 'pending' and proposal['snapshot_id']
    assert proposal['proposal']['affected_chapters'][0]['has_content'] is True
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW
    persisted = journal.db.fetch_one('SELECT snapshot_json FROM data_change_proposals WHERE proposal_id=?', (proposal['proposal_id'],))
    assert RAW in persisted['snapshot_json']


def test_save_receipts_do_not_change_snapshot_hash_or_get_rewound_by_undo(journal):
    # Simulate a concurrent pending/failed save receipt without touching the body.
    # It is command metadata, so a structure review must neither conflict on it
    # nor restore its old state during an otherwise valid protected undo.
    if not journal.db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_save_receipts'"):
        journal.db.execute("CREATE TABLE chapter_save_receipts (request_id TEXT PRIMARY KEY,novel_id TEXT,status TEXT)")
    columns = {row['name'] for row in journal.db.fetch_all("PRAGMA table_info(chapter_save_receipts)")}
    if 'chapter_number' in columns:
        journal.db.execute("""INSERT INTO chapter_save_receipts(request_id,novel_id,chapter_number,payload_hash,status,created_at,updated_at)
            VALUES ('save-metadata','novel',1,'hash','pending',1,1)""")
    else:
        journal.db.execute("INSERT INTO chapter_save_receipts VALUES ('save-metadata','novel','pending')")
    proposal = journal.propose_delete('novel', 'act')
    journal.db.execute("UPDATE chapter_save_receipts SET status='failed' WHERE request_id='save-metadata'")
    assert apply(journal, proposal)['status'] == 'applied'
    committed = journal.get('novel', proposal['proposal_id'])
    assert apply(journal, committed, True)['status'] == 'rolled_back'
    assert journal.db.fetch_one("SELECT status FROM chapter_save_receipts WHERE request_id='save-metadata'")['status'] == 'failed'


def test_confirm_and_guarded_undo_restore_body_structure_fk_children_and_history(journal):
    proposal = journal.propose_delete('novel', 'act')
    receipt = apply(journal, proposal)
    assert receipt['status'] == 'applied' and receipt['audit_id']
    assert journal.db.fetch_one("SELECT id FROM chapters WHERE id='chapter'") is None
    assert journal.db.fetch_one("SELECT id FROM chapter_elements WHERE id='element'") is None
    assert journal.db.fetch_one("SELECT id FROM beat_sheets WHERE id='beats'") is None
    committed = journal.get('novel', proposal['proposal_id'])
    restored = apply(journal, committed, True)
    assert restored['status'] == 'rolled_back'
    chapter = journal.db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'")
    assert chapter['content'] == RAW and chapter['revision'] > 0
    assert journal.db.fetch_one("SELECT id FROM chapter_elements WHERE id='element'")
    assert journal.db.fetch_one("SELECT id FROM beat_sheets WHERE id='beats'")
    assert not journal.db.fetch_all('PRAGMA foreign_key_check')
    assert journal.db.fetch_one("SELECT COUNT(*) AS count FROM content_versions WHERE chapter_id='chapter'")['count'] >= 1


def test_edit_after_preview_blocks_deletion_at_writer(journal):
    proposal = journal.propose_delete('novel', 'act')
    journal.db.execute("UPDATE chapters SET content='作者新稿' WHERE id='chapter'")
    with pytest.raises(DataChangeConflict):
        apply(journal, proposal)
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '作者新稿'
    assert journal.get('novel', proposal['proposal_id'])['status'] == 'pending'


def test_resume_after_preview_blocks_old_epoch(journal):
    proposal = journal.propose_delete('novel', 'act')
    journal.db.execute("UPDATE novels SET autopilot_run_epoch=2,current_stage='writing' WHERE id='novel'")
    with pytest.raises(DataChangeConflict):
        apply(journal, proposal)
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_edit_after_change_is_preserved_and_undo_fails_closed(journal):
    proposal = journal.propose_delete('novel', 'act')
    apply(journal, proposal)
    committed = journal.get('novel', proposal['proposal_id'])
    journal.db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('new','novel',2,'新章','作者新章')")
    with pytest.raises(DataChangeConflict):
        apply(journal, committed, True)
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='new'")['content'] == '作者新章'
    assert journal.db.fetch_one("SELECT id FROM chapters WHERE id='chapter'") is None


def test_proposal_scope_and_digest_are_required(journal):
    proposal = journal.propose_delete('novel', 'act')
    with pytest.raises(LookupError):
        journal.get('other', proposal['proposal_id'])
    with pytest.raises(DataChangeConflict):
        journal.apply('novel', proposal['proposal_id'], expected_hash=proposal['base_hash'], proposal_hash='0'*64, run_epoch=1)
    assert journal.db.fetch_one("SELECT id FROM chapters WHERE id='chapter'")


def test_expired_proposal_cannot_delete_body(journal):
    proposal = journal.create('novel', {'operation': 'delete_nodes', 'node_ids': ['act'], 'chapter_ids': ['chapter']}, ttl_seconds=-1)
    assert proposal['expired']
    with pytest.raises(DataChangeConflict):
        apply(journal, proposal)
    assert journal.db.fetch_one("SELECT id FROM chapters WHERE id='chapter'")


def test_macro_missing_authored_carrier_is_blocked(journal):
    with pytest.raises(MergeConflictException):
        journal.propose_macro('novel', [])
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_safe_macro_merge_uses_journal_and_can_be_undone(journal):
    nodes = journal.db.fetch_all("SELECT * FROM story_nodes WHERE novel_id='novel' AND node_type!='chapter'")
    nodes[0]['title'] = '修改后的结构标题'
    proposal = journal.propose_macro('novel', nodes)
    apply(journal, proposal)
    committed = journal.get('novel', proposal['proposal_id'])
    assert journal.db.fetch_one("SELECT title FROM story_nodes WHERE id='part'")['title'] == '修改后的结构标题'
    apply(journal, committed, True)
    assert journal.db.fetch_one("SELECT title FROM story_nodes WHERE id='part'")['title'] == 'part'
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_protection_snapshot_and_proposal_are_immutable(journal):
    proposal = journal.propose_delete('novel', 'act')
    with pytest.raises(Exception, match='immutable'):
        journal.db.execute("UPDATE data_change_proposals SET snapshot_json='{}' WHERE proposal_id=?", (proposal['proposal_id'],))


def test_snapshot_collection_rollback_is_recoverable_even_with_empty_pointer_list(journal):
    journal.db.execute("INSERT INTO novel_snapshots(id,novel_id,trigger_type,name,chapter_pointers,created_at) VALUES ('snapshot','novel','MANUAL','旧存档','[]',CURRENT_TIMESTAMP)")
    proposal = journal.propose_snapshot_rollback('novel', 'snapshot')
    assert proposal['proposal']['chapter_ids'] == ['chapter']
    apply(journal, proposal)
    apply(journal, journal.get('novel', proposal['proposal_id']), True)
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_cross_novel_fk_before_preview_blocks_cascading_deletion(journal):
    journal.db.execute("INSERT INTO novels(id,title,slug) VALUES ('other','另一部作品','other')")
    journal.db.execute("INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index) VALUES ('other-node','other','act','chapter',1,'跨作品关联',0)")
    with pytest.raises(DataChangeConflict, match='跨作品'):
        journal.propose_delete('novel', 'act')
    assert journal.db.fetch_one("SELECT id FROM story_nodes WHERE id='other-node'")
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


def test_cross_novel_fk_added_after_preview_is_checked_by_writer(journal):
    proposal = journal.propose_delete('novel', 'act')
    journal.db.execute("INSERT INTO novels(id,title,slug) VALUES ('other','另一部作品','other')")
    journal.db.execute("INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index) VALUES ('other-node','other','act','chapter',1,'跨作品关联',0)")
    with pytest.raises(DataChangeConflict, match='跨作品'):
        apply(journal, proposal)
    assert journal.db.fetch_one("SELECT id FROM story_nodes WHERE id='other-node'")
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW


@pytest.mark.asyncio
async def test_production_macro_confirmation_uses_protection_journal_and_preserves_body(daemon_writer):
    from types import SimpleNamespace
    from application.blueprint.services.continuous_planning_service import ContinuousPlanningService
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
    from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
    journal = DataChangeJournal(daemon_writer[0])
    journal.db.execute("UPDATE novels SET current_stage='paused_for_review' WHERE id='novel'")
    for node_id, parent, kind, title in [('part-novel-1', None, 'part', 'part'), ('volume-novel-1', 'part-novel-1', 'volume', 'volume'), ('act-novel-1', 'volume-novel-1', 'act', 'act'), ('node-chapter', 'act-novel-1', 'chapter', 'chapter')]:
        journal.db.execute("INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index) VALUES (?,'novel',?,?,1,?,0)", (node_id, parent, kind, title))
    service = ContinuousPlanningService(StoryNodeRepository(journal.db), ChapterElementRepository(journal.db.db_path), SimpleNamespace())
    structure = [{'number': 1, 'title': '安全的新部标题', 'volumes': [{'number': 1, 'title': 'volume', 'acts': [{'number': 1, 'title': 'act', 'suggested_chapter_count': 5}]}]}]
    result = await service.confirm_macro_plan_safe('novel', structure)
    assert result['success'] is True and result['status'] == 'applied' and result['snapshot_id'] and result['audit_id']
    assert journal.db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == RAW
    saved = journal.get('novel', result['proposal_id'])
    assert saved['proposal']['automatic_safe'] is True and saved['status'] == 'applied'
    apply(journal, saved, True)
    assert journal.db.fetch_one("SELECT title FROM story_nodes WHERE id='part-novel-1'")['title'] == 'part'
