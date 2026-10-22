"""Provider rewrites bind the cleaned body to durable receipts through the writer."""
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from application.ai_invocation.prose_execution_evidence import ProseExecutionEvidenceError
from application.audit.services.anti_ai_rewrite_workflow import AntiAIRewriteWorkflow
from application.engine.services.chapter_rewrite_candidates import (
    CandidateConflictError, ChapterRewriteCandidates, content_hash)
from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW, CLEAN


class Provider:
    async def generate(self, prompt, config):
        return SimpleNamespace(content=f"<think>内部推理</think>\n{CLEAN}\n")


async def provider_candidate(db, **kwargs):
    result = await AntiAIRewriteWorkflow(db, Provider()).propose(
        "novel", 1, run_id="provider-rewrite", force_rewrite=True,
        max_attempts=1, **kwargs)
    assert result["status"] == "awaiting_confirmation"
    return result["candidate"]


def alter_receipt(conn, proof, mutation):
    if mutation == "delete_prompt":
        conn.execute("DELETE FROM anti_ai_prompt_runs WHERE run_id=?", (proof["prompt_run_id"],))
    elif mutation == "delete_scan":
        conn.execute("DELETE FROM anti_ai_audit_runs WHERE run_id=?", (proof["scan_run_id"],))
    elif mutation == "prompt_hash":
        conn.execute("UPDATE anti_ai_prompt_runs SET prompt_hash='tampered' WHERE run_id=?", (proof["prompt_run_id"],))
    elif mutation == "scan_hash":
        conn.execute("UPDATE anti_ai_audit_runs SET content_hash='tampered' WHERE run_id=?", (proof["scan_run_id"],))
    elif mutation == "scan_job":
        conn.execute("UPDATE anti_ai_audit_runs SET report_json=json_set(report_json,'$.job_id','other') WHERE run_id=?", (proof["scan_run_id"],))
    elif mutation == "scan_status":
        conn.execute("UPDATE anti_ai_audit_runs SET scan_status='failed' WHERE run_id=?", (proof["scan_run_id"],))
    elif mutation == "prompt_timestamp":
        conn.execute("UPDATE anti_ai_prompt_runs SET created_at='changed after enqueue' WHERE run_id=?", (proof["prompt_run_id"],))
    elif mutation == "scan_report":
        conn.execute("UPDATE anti_ai_audit_runs SET report_json=json_set(report_json,'$.tampered',1) WHERE run_id=?", (proof["scan_run_id"],))
    else:
        raise AssertionError(mutation)


@pytest.mark.asyncio
async def test_provider_candidate_binds_cleaned_full_body_and_preserves_proof_on_review(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    proof = candidate["audit"]["prose_evidence"]
    assert candidate["content"] == CLEAN and proof["content_hash"] == content_hash(CLEAN)
    assert proof["attempt_id"] == "provider-rewrite:1" and proof["require_stream"] is False
    assert candidate["audit"]["scan"]["content_hash"] == proof["content_hash"]
    with sqlite3.connect(db.db_path) as independent:
        saved_audit = json.loads(independent.execute(
            "SELECT audit_json FROM chapter_rewrite_candidates WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchone()[0])
        assert saved_audit["prose_evidence"] == proof
    reviewed = await AntiAIRewriteWorkflow(db).validate_candidate(candidate, max_severity=3)
    assert reviewed["audit"]["prose_evidence"] == proof and reviewed["status"] == "audited"
    committed = ChapterRewriteCandidates(db).commit_candidate(candidate["candidate_id"], candidate["content_hash"])
    assert committed["status"] == "committed" and committed["audit"]["prose_evidence"] == proof
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {
        "content": CLEAN, "revision": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete_prompt", "delete_scan", "prompt_hash", "scan_hash", "scan_job", "scan_status"])
async def test_deleted_or_tampered_receipt_blocks_revalidation_and_commit(daemon_writer, mutation):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    alter_receipt(db, candidate["audit"]["prose_evidence"], mutation)
    db.commit()
    with pytest.raises(ProseExecutionEvidenceError):
        await AntiAIRewriteWorkflow(db).validate_candidate(candidate, max_severity=3)
    with pytest.raises(ProseExecutionEvidenceError):
        ChapterRewriteCandidates(db).commit_candidate(candidate["candidate_id"], candidate["content_hash"])
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW
    assert ChapterRewriteCandidates(db).get(candidate["candidate_id"])["content"] == CLEAN


@pytest.mark.asyncio
async def test_receipt_deleted_between_generation_and_candidate_save_is_not_accepted(daemon_writer):
    db = daemon_writer[0]

    async def delete_scan(body, attempt, run_id):
        db.execute("DELETE FROM anti_ai_audit_runs")
        db.commit()

    with pytest.raises(ProseExecutionEvidenceError):
        await provider_candidate(db, on_candidate=delete_scan)
    assert ChapterRewriteCandidates(db).list_for_chapter("novel") == []
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_plain_hook_dict_without_db_receipts_is_not_durable_proof(daemon_writer, monkeypatch):
    from application.engine.rules import prose_prompt_hooks

    async def fake_hook(*args, evidence_sink, **kwargs):
        evidence_sink.update(run_id="fake-prompt", prompt_hash="fake-hash",
            anti_ai_scan={"run_id": "fake-scan"}, status="completed", persistence_status="committed")
        return SimpleNamespace(content=CLEAN)

    monkeypatch.setattr(prose_prompt_hooks, "generate_direct_prose", fake_hook)
    with pytest.raises(ProseExecutionEvidenceError, match="回执"):
        await provider_candidate(daemon_writer[0])
    assert ChapterRewriteCandidates(daemon_writer[0]).list_for_chapter("novel") == []


@pytest.mark.asyncio
async def test_bound_proof_cannot_be_removed_or_replaced_and_missing_input_is_preserved(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    store = ChapterRewriteCandidates(db)
    proof = candidate["audit"]["prose_evidence"]
    audit = {key: value for key, value in candidate["audit"].items() if key != "prose_evidence"}
    assert store.mark_audited(candidate["candidate_id"], audit, True)["audit"]["prose_evidence"] == proof
    with pytest.raises(CandidateConflictError, match="替换"):
        store.mark_audited(candidate["candidate_id"], {**audit, "prose_evidence": {"verified": True}}, True)
    for sql in [
        "UPDATE chapter_rewrite_candidates SET audit_json='{}' WHERE candidate_id=?",
        "UPDATE chapter_rewrite_candidates SET audit_json=json_remove(audit_json,'$.prose_evidence') WHERE candidate_id=?",
        "UPDATE chapter_rewrite_candidates SET audit_json=json_set(audit_json,'$.prose_evidence.scan_run_id','other') WHERE candidate_id=?",
    ]:
        with sqlite3.connect(db.db_path) as independent:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                independent.execute(sql, (candidate["candidate_id"],))
    assert store.get(candidate["candidate_id"])["audit"]["prose_evidence"] == proof


@pytest.mark.asyncio
async def test_callback_candidate_uses_explicit_full_body_audit_and_rejects_fake_proof(daemon_writer):
    db = daemon_writer[0]

    async def callback(body, attempt):
        return f"<think>内部推理</think>{CLEAN}"

    result = await AntiAIRewriteWorkflow(db).propose("novel", 1, rewrite=callback,
        force_rewrite=True, max_attempts=1)
    candidate = result["candidate"]
    assert candidate["content"] == CLEAN
    assert candidate["audit"]["scan"]["scan_status"] == "completed"
    assert candidate["audit"]["scan"]["content_hash"] == content_hash(CLEAN)
    assert "prose_evidence" not in candidate["audit"]
    with pytest.raises(CandidateConflictError, match="注入"):
        ChapterRewriteCandidates(db).mark_audited(candidate["candidate_id"],
            {**candidate["audit"], "prose_evidence": {"verified": True}}, True)
    assert ChapterRewriteCandidates(db).commit_candidate(candidate["candidate_id"])["status"] == "committed"


@pytest.mark.asyncio
async def test_candidate_final_body_hash_is_reverified_even_when_db_hash_was_changed(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    db.execute("UPDATE chapter_rewrite_candidates SET content=?,content_hash=? WHERE candidate_id=?",
        ("篡改后的正文", content_hash("篡改后的正文"), candidate["candidate_id"]))
    db.commit()
    altered = ChapterRewriteCandidates(db).get(candidate["candidate_id"])
    with pytest.raises(ProseExecutionEvidenceError, match="正文"):
        await AntiAIRewriteWorkflow(db).validate_candidate(altered, max_severity=3)
    with pytest.raises(CandidateConflictError, match="hash"):
        ChapterRewriteCandidates(db).commit_candidate(candidate["candidate_id"])
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete_prompt", "delete_scan", "prompt_timestamp", "scan_report", "candidate_audit", "candidate_body", "chapter_number", "chapter_and_candidate_number"])
async def test_queued_commit_rechecks_receipt_and_candidate_snapshots_at_writer(daemon_writer, mutation):
    db, pq, *_ = daemon_writer
    candidate = await provider_candidate(db)
    entered, release = threading.Event(), threading.Event()
    actual = pq._handlers["execute_sql_txn_batch"]

    def delayed(payload):
        if any("UPDATE chapters SET content" in operation.get("sql", "")
               for operation in payload.get("operations", [])):
            entered.set()
            assert release.wait(5)
        return actual(payload)

    pq._handlers["execute_sql_txn_batch"] = delayed
    task = asyncio.create_task(asyncio.to_thread(ChapterRewriteCandidates(db).commit_candidate,
        candidate["candidate_id"], candidate["content_hash"]))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with sqlite3.connect(db.db_path) as independent:
            if mutation == "candidate_audit":
                independent.execute("UPDATE chapter_rewrite_candidates SET audit_json=json_set(audit_json,'$.accepted',0) WHERE candidate_id=?", (candidate["candidate_id"],))
            elif mutation == "candidate_body":
                independent.execute("UPDATE chapter_rewrite_candidates SET content=?,content_hash=? WHERE candidate_id=?", ("篡改后的正文", content_hash("篡改后的正文"), candidate["candidate_id"]))
            elif mutation in {"chapter_number", "chapter_and_candidate_number"}:
                independent.execute("UPDATE chapters SET number=2 WHERE id='chapter'")
                if mutation == "chapter_and_candidate_number":
                    independent.execute("UPDATE chapter_rewrite_candidates SET chapter_number=2 WHERE candidate_id=?", (candidate["candidate_id"],))
            else:
                alter_receipt(independent, candidate["audit"]["prose_evidence"], mutation)
        release.set()
        with pytest.raises(CandidateConflictError, match="冲突"):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {
        "content": RAW, "revision": 0}
    assert ChapterRewriteCandidates(db).get(candidate["candidate_id"])["status"] == "conflict"


@pytest.mark.asyncio
async def test_committed_idempotent_result_cannot_reuse_deleted_receipt(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    store = ChapterRewriteCandidates(db)
    store.commit_candidate(candidate["candidate_id"])
    alter_receipt(db, candidate["audit"]["prose_evidence"], "delete_scan")
    db.commit()
    with pytest.raises(ProseExecutionEvidenceError):
        store.commit_candidate(candidate["candidate_id"])
    assert db.fetch_one("SELECT revision FROM chapters WHERE id='chapter'")["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["save", "review"])
@pytest.mark.parametrize("mutation", ["delete_scan", "chapter_number"])
async def test_queued_candidate_save_or_review_never_accepts_changed_evidence(daemon_writer, stage, mutation):
    db, pq, *_ = daemon_writer
    candidate = await provider_candidate(db) if stage == "review" else None
    entered, release = threading.Event(), threading.Event()
    actual = pq._handlers["execute_sql"]
    prefix = "INSERT INTO chapter_rewrite_candidates" if stage == "save" else (
        "UPDATE chapter_rewrite_candidates SET audit_json=")

    def delayed(payload):
        if payload.get("sql", "").startswith(prefix):
            entered.set()
            assert release.wait(5)
        return actual(payload)

    pq._handlers["execute_sql"] = delayed
    task = asyncio.create_task(provider_candidate(db) if stage == "save" else
        AntiAIRewriteWorkflow(db).validate_candidate(candidate, max_severity=3))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with sqlite3.connect(db.db_path) as independent:
            if mutation == "delete_scan":
                independent.execute("DELETE FROM anti_ai_audit_runs")
            else:
                independent.execute("UPDATE chapters SET number=2 WHERE id='chapter'")
        release.set()
        with pytest.raises(CandidateConflictError, match="回执"):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {
        "content": RAW, "revision": 0}
    saved = ChapterRewriteCandidates(db).list_for_chapter("novel")
    assert saved == [] if stage == "save" else saved[0]["content"] == CLEAN


@pytest.mark.asyncio
async def test_renumbering_saved_candidate_blocks_validation_and_manuscript_commit(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    db.execute("UPDATE chapters SET number=2 WHERE id='chapter'")
    db.commit()
    with pytest.raises(CandidateConflictError, match="章节身份"):
        await AntiAIRewriteWorkflow(db).validate_candidate(candidate, max_severity=3)
    with pytest.raises(CandidateConflictError, match="章节身份"):
        ChapterRewriteCandidates(db).commit_candidate(candidate["candidate_id"])
    assert db.fetch_one("SELECT content,revision,number FROM chapters WHERE id='chapter'") == {
        "content": RAW, "revision": 0, "number": 2}


@pytest.mark.asyncio
async def test_renumbered_commit_cannot_reuse_idempotent_ack_or_final_audit(daemon_writer):
    db = daemon_writer[0]
    candidate = await provider_candidate(db)
    store = ChapterRewriteCandidates(db)
    committed = store.commit_candidate(candidate["candidate_id"])
    db.execute("UPDATE chapters SET number=2 WHERE id='chapter'")
    db.commit()
    for operation in [
        lambda: store.commit_candidate(candidate["candidate_id"]),
        lambda: store.assert_committed_version(committed),
        lambda: store.mark_committed(candidate["candidate_id"], candidate["content_hash"]),
        lambda: store.record_final_audit(candidate["candidate_id"], None),
        lambda: store.record_aftermath(candidate["candidate_id"], {"narrative_sync_ok": True}),
    ]:
        with pytest.raises(CandidateConflictError, match="章节身份"):
            operation()
    assert db.fetch_one("SELECT content,revision,number FROM chapters WHERE id='chapter'") == {
        "content": CLEAN, "revision": 1, "number": 2}
    saved = store.get(candidate["candidate_id"])
    assert "final_audit" not in saved["audit"] and "aftermath" not in saved["audit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["commit_ack", "final_audit", "aftermath"])
async def test_queued_final_archive_fixes_actual_and_candidate_chapter_identity(daemon_writer, stage):
    db, pq, *_ = daemon_writer
    candidate = await provider_candidate(db)
    store = ChapterRewriteCandidates(db)
    store.commit_candidate(candidate["candidate_id"])
    report = AntiAIRewriteWorkflow(db).scan("chapter", CLEAN)
    report.persistence_status = "committed"
    if stage == "commit_ack":
        operation = lambda: store.mark_committed(candidate["candidate_id"], candidate["content_hash"])
    elif stage == "final_audit":
        operation = lambda: store.record_final_audit(candidate["candidate_id"], report)
    else:
        operation = lambda: store.record_aftermath(candidate["candidate_id"], {"narrative_sync_ok": True})
    entered, release = threading.Event(), threading.Event()
    actual = pq._handlers["execute_sql"]

    def delayed(payload):
        if payload.get("sql", "").startswith("UPDATE chapter_rewrite_candidates"):
            entered.set()
            assert release.wait(5)
        return actual(payload)

    pq._handlers["execute_sql"] = delayed
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with sqlite3.connect(db.db_path) as independent:
            independent.execute("UPDATE chapters SET number=2 WHERE id='chapter'")
            independent.execute("UPDATE chapter_rewrite_candidates SET chapter_number=2 WHERE candidate_id=?",
                (candidate["candidate_id"],))
        release.set()
        with pytest.raises(CandidateConflictError, match="变化"):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert db.fetch_one("SELECT content,revision,number FROM chapters WHERE id='chapter'") == {
        "content": CLEAN, "revision": 1, "number": 2}
    saved = store.get(candidate["candidate_id"])
    assert "final_audit" not in saved["audit"] and "aftermath" not in saved["audit"]
