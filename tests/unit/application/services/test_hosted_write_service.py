"""Hosted generation uses the actual single writer and frozen chapter CAS."""
import sqlite3
import pytest
from unittest.mock import AsyncMock, Mock
from application.engine.services.hosted_write_service import HostedWriteService
from application.core.services.chapter_service import ChapterService
from application.core.services.chapter_version_service import ChapterVersionService, content_hash
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from application.ai_invocation.prose_execution_evidence import verify_durable_prose_receipts
from application.engine.rules.anti_ai_stream_guard import prepare_direct_prose_stream
from domain.ai.value_objects.prompt import Prompt
from tests.unit.application.engine.test_persistence_commit_ack import writer

@pytest.fixture
def hosted(writer):
    queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES('book','Test','book')")
    db.execute("INSERT INTO novels(id,title,slug) VALUES('other-book','Other','other-book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('ch','book',1,'First','original')")
    wf = Mock()
    wf.prose_db = db
    wf.suggest_outline = AsyncMock(return_value="outline")
    svc = HostedWriteService(wf, ChapterService(SqliteChapterRepository(db), SqliteNovelRepository(db)))
    return svc, wf, db, queue

def stream(wf, mutate=None, terminal=True, *, proof_mutate=None, terminal_content="generated"):
    async def generate(*args, **kwargs):
        novel_id, number = args[:2]
        receipt = {}
        class Provider:
            async def stream_generate(self, *_args):
                yield "generated"
        pieces = [piece async for piece in prepare_direct_prose_stream(
            Provider(), Prompt(system="写正文", user="继续本章"), None,
            context={"novel_id": novel_id, "chapter_number": number},
            db=wf.prose_db, evidence_sink=receipt,
        )]
        assert "".join(pieces) == "generated"
        prompt_receipt = receipt["prompt_receipt"]
        proof = verify_durable_prose_receipts(
            wf.prose_db, novel_id=novel_id, chapter_number=number,
            attempt_id=receipt["job_id"], content="generated",
            prompt_hash=prompt_receipt["prompt_hash"], require_stream=True,
            prompt_run_id=prompt_receipt["run_id"], stream_run_id=receipt["run_id"],
        )
        yield {"type": "chunk", "text": "generated"}
        if mutate: mutate()
        if proof_mutate: proof_mutate(proof)
        if terminal: yield {"type": "done", "content": terminal_content, "prose_execution_evidence": proof}
    wf.generate_chapter_stream = generate

@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
async def test_hosted_real_commit_receipt_and_atomic_outbox(hosted, number):
    svc, wf, db, queue = hosted
    stream(wf)
    events = [e async for e in svc.stream_hosted_write("book",number,number)]
    saved = next(e for e in events if e["type"] == "saved")
    assert saved["ok"] and saved["status"] == "committed"
    row = db.fetch_one("SELECT * FROM chapters WHERE novel_id='book' AND number=?",(number,))
    assert row["content"] == "generated" and saved["revision"] == row["revision"]
    assert saved["content_hash"] == content_hash(row["content"])
    job = db.fetch_one("SELECT * FROM chapter_derivation_jobs WHERE job_id=?",(saved["job_id"],))
    assert job["revision"] == row["revision"] and job["status"] == "pending"
    assert events[-1]["type"] == "session_done"

@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
async def test_edit_or_create_during_generation_never_overwrites(hosted, number):
    svc, wf, db, queue = hosted
    def edit():
        if number == 1: ChapterVersionService(db).save_if_revision("ch",0,"author edit")
        else: db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('other','book',2,'Second','author edit')")
    stream(wf,edit)
    events = [e async for e in svc.stream_hosted_write("book",number,number)]
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "session_done" for e in events)
    failed = next(e for e in events if e["type"] == "saved")
    assert failed["ok"] is False and failed["content"] == "generated"
    assert db.fetch_one("SELECT content FROM chapters WHERE novel_id='book' AND number=?",(number,))["content"] == "author edit"
    assert not db.fetch_one("SELECT job_id FROM chapter_derivation_jobs")

@pytest.mark.asyncio
async def test_generation_eof_is_failure_and_no_save(hosted):
    svc, wf, db, queue = hosted
    stream(wf,terminal=False)
    events = [e async for e in svc.stream_hosted_write("book",1,2)]
    assert events[-1]["type"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch'")["content"] == "original"
    assert not any(e["type"] == "saved" for e in events)
    assert wf.suggest_outline.await_count == 1

@pytest.mark.asyncio
async def test_commit_failure_preserves_draft_and_stops_session(hosted):
    svc, wf, db, queue = hosted
    def fail(payload): raise RuntimeError("injected writer failure")
    stream(wf, lambda: queue.register_handler("execute_sql_txn_batch", fail))
    events = [e async for e in svc.stream_hosted_write("book",1,2)]
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "session_done" for e in events)
    assert next(e for e in events if e["type"] == "saved")["content"] == "generated"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='ch'")["content"] == "original"

@pytest.mark.asyncio
async def test_outline_without_autosave_uses_llm(hosted):
    svc,wf,db,queue = hosted
    stream(wf)
    events = [e async for e in svc.stream_hosted_write("book",2,2,auto_save=False)]
    assert next(e for e in events if e["type"] == "outline")["text"] == "outline"
    assert events[-1]["type"] == "session_done"
    assert not db.fetch_one("SELECT id FROM chapters WHERE novel_id='book' AND number=2")


@pytest.mark.asyncio
@pytest.mark.parametrize('planned', [False, True])
async def test_new_chapter_structure_projection_is_atomic_and_preserves_plan(hosted, planned):
    svc, wf, db, queue = hosted
    db.execute("INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index,chapter_start,chapter_end) VALUES('act','book','act',1,'Act',0,1,3)")
    if planned:
        db.execute("INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index,outline) VALUES('planned','book','act','chapter',2,'Author title',1,'Author outline')")
    stream(wf)
    events = [e async for e in svc.stream_hosted_write('book',2,2)]
    assert events[-1]['type'] == 'session_done'
    nodes = db.fetch_all("SELECT * FROM story_nodes WHERE novel_id='book' AND node_type='chapter'")
    assert len(nodes) == 1 and nodes[0]['content'] == 'generated'
    assert nodes[0]['parent_id'] == 'act'
    if planned:
        assert nodes[0]['title'] == 'Author title' and nodes[0]['outline'] == 'Author outline'
    act = db.fetch_one("SELECT * FROM story_nodes WHERE id='act'")
    assert (act['chapter_start'],act['chapter_end'],act['chapter_count']) == (2,2,1)


@pytest.mark.asyncio
async def test_losing_new_chapter_cas_does_not_create_structure(hosted):
    svc, wf, db, queue = hosted
    db.execute("INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index) VALUES('act','book','act',1,'Act',0)")
    stream(wf, lambda: db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('author','book',2,'Second','author edit')"))
    events = [e async for e in svc.stream_hosted_write('book',2,2)]
    assert events[-1]['type'] == 'error'
    assert not db.fetch_one("SELECT id FROM story_nodes WHERE node_type='chapter'")


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize("change", [
    "missing_proof", "orphan_prompt", "orphan_stream", "wrong_attempt",
    "wrong_book", "wrong_chapter", "tampered_hash", "tampered_body",
])
async def test_autosave_refuses_missing_or_unbound_prose_receipts(hosted, number, change):
    svc, wf, db, _queue = hosted

    def corrupt(proof):
        if change == "missing_proof":
            proof.clear()
        elif change in {"orphan_prompt", "orphan_stream"}:
            table, key = ("anti_ai_prompt_runs", "prompt_run_id") if change == "orphan_prompt" else ("anti_ai_stream_runs", "stream_run_id")
            db.execute(f"DELETE FROM {table} WHERE run_id=?", (proof[key],))
        elif change == "wrong_attempt":
            proof["attempt_id"] = "different-generation"
        elif change == "wrong_book":
            db.execute("UPDATE anti_ai_stream_runs SET novel_id='other-book' WHERE run_id=?", (proof["stream_run_id"],))
        elif change == "wrong_chapter":
            db.execute("UPDATE anti_ai_stream_runs SET chapter_number=chapter_number+1 WHERE run_id=?", (proof["stream_run_id"],))
        elif change == "tampered_hash":
            db.execute("UPDATE anti_ai_stream_runs SET content_hash='tampered' WHERE run_id=?", (proof["stream_run_id"],))

    stream(wf, proof_mutate=corrupt, terminal_content="changed after scan" if change == "tampered_body" else "generated")
    events = [event async for event in svc.stream_hosted_write("book", number, number)]
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "session_done" or (event["type"] == "saved" and event.get("ok")) for event in events)
    failed_save = next(event for event in events if event["type"] == "saved")
    assert failed_save["ok"] is False and failed_save["content"] == ("changed after scan" if change == "tampered_body" else "generated")
    row = db.fetch_one("SELECT * FROM chapters WHERE novel_id='book' AND number=?", (number,))
    if number == 1:
        assert row["content"] == "original"
    else:
        assert row is None
    assert not db.fetch_one("SELECT job_id FROM chapter_derivation_jobs")


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize("table", ["anti_ai_prompt_runs", "anti_ai_stream_runs"])
@pytest.mark.parametrize("change", ["delete", "tamper"])
async def test_receipt_changes_after_verification_block_the_actual_writer(hosted, number, table, change):
    svc, wf, db, queue = hosted
    original = queue._handlers["execute_sql_txn_batch"]
    changes = []

    def before_write(payload):
        operations = payload.get("operations") or []
        if any("anti_ai_stream_runs" in op.get("sql", "") and
               (op.get("sql", "").startswith("UPDATE chapters") or op.get("sql", "").startswith("INSERT INTO chapters"))
               for op in operations):
            # The reader has validated and frozen the receipt. Change it only
            # when the save reaches the real single writer, before BEGIN.
            with sqlite3.connect(db.db_path) as independent:
                if change == "delete":
                    cursor = independent.execute(f"DELETE FROM {table} WHERE novel_id=? AND chapter_number=?", ("book", number))
                else:
                    cursor = independent.execute(f"UPDATE {table} SET error='changed while queued' WHERE novel_id=? AND chapter_number=?", ("book", number))
                assert cursor.rowcount == 1
            changes.append(True)
        return original(payload)

    queue.register_handler("execute_sql_txn_batch", before_write)
    stream(wf)
    events = [event async for event in svc.stream_hosted_write("book", number, number)]
    assert changes == [True]
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "session_done" or (event["type"] == "saved" and event.get("ok")) for event in events)
    row = db.fetch_one("SELECT * FROM chapters WHERE novel_id='book' AND number=?", (number,))
    if number == 1:
        assert row["content"] == "original"
    else:
        assert row is None
    assert not db.fetch_one("SELECT job_id FROM chapter_derivation_jobs")
    assert not db.fetch_one("SELECT id FROM story_nodes WHERE node_type='chapter'")
