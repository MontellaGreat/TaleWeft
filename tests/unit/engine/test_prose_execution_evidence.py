"""Reject contradictory durable receipts, including their serialized evidence."""
import json

import pytest

from application.ai_invocation.prose_execution_evidence import (
    ProseExecutionEvidenceError, verify_durable_prose_receipts,
)
from application.engine.rules.anti_ai_stream_guard import prepare_direct_prose_stream
from application.engine.rules.prose_prompt_hooks import generate_direct_prose
from domain.ai.value_objects.prompt import Prompt
from tests.unit.engine.test_prose_prompt_hooks import hook_db


BODY = "他把钥匙压在账本下面，关上窗。"


class Provider:
    async def generate(self, *_):
        return BODY

    async def stream_generate(self, *_):
        yield BODY[:5]
        yield BODY[5:]


async def receipts(db, streaming):
    evidence = {}
    context = {"novel_id": "one", "chapter_number": 1, "job_id": "attempt"}
    prompt = Prompt(system="写小说正文", user="阿青关窗")
    if streaming:
        pieces = [piece async for piece in prepare_direct_prose_stream(
            Provider(), prompt, None, context=context, db=db, evidence_sink=evidence)]
        assert "".join(pieces) == BODY
        prompt_receipt = evidence["prompt_receipt"]
    else:
        assert await generate_direct_prose(Provider(), prompt, None, context=context,
                                           db=db, evidence_sink=evidence) == BODY
        prompt_receipt = evidence
    return dict(novel_id="one", chapter_number=1, attempt_id="attempt", content=BODY,
        prompt_hash=prompt_receipt["prompt_hash"], require_stream=streaming,
        prompt_run_id=prompt_receipt["run_id"], stream_run_id=evidence["run_id"] if streaming else "",
        scan_run_id="" if streaming else evidence["anti_ai_scan"]["run_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_receipts_verify(hook_db, streaming):
    proof = verify_durable_prose_receipts(hook_db, **await receipts(hook_db, streaming))
    assert proof["verified"] and proof["attempt_id"] == "attempt"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("field,value", [
    ("run_id", "another-run"), ("job_id", "another-attempt"),
    ("status", "failed"), ("rule_version", "other-rules"),
    ("error", "archival failure"), ("persistence_status", "failed"),
])
async def test_prompt_json_contradictions_cannot_be_adopted(hook_db, streaming, field, value):
    args = await receipts(hook_db, streaming)
    row = hook_db.fetch_one("SELECT * FROM anti_ai_prompt_runs WHERE run_id=?", (args["prompt_run_id"],))
    evidence = json.loads(row["evidence_json"])
    evidence[field] = value
    hook_db.execute("UPDATE anti_ai_prompt_runs SET evidence_json=? WHERE run_id=?",
                    (json.dumps(evidence), args["prompt_run_id"]))
    hook_db.commit()
    with pytest.raises(ProseExecutionEvidenceError):
        verify_durable_prose_receipts(hook_db, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("run_id", "another-run"), ("job_id", "another-attempt"),
    ("status", "failed"), ("rule_version", "other-rules"),
    ("final_scan_status", "not_run"), ("persistence_status", "failed"),
    ("error", "scanner failed"), ("archive_error", "writer failed"),
])
async def test_stream_summary_contradictions_cannot_be_adopted(hook_db, field, value):
    args = await receipts(hook_db, True)
    row = hook_db.fetch_one("SELECT * FROM anti_ai_stream_runs WHERE run_id=?", (args["stream_run_id"],))
    summary = json.loads(row["summary_json"])
    summary[field] = value
    hook_db.execute("UPDATE anti_ai_stream_runs SET summary_json=? WHERE run_id=?",
                    (json.dumps(summary), args["stream_run_id"]))
    hook_db.commit()
    with pytest.raises(ProseExecutionEvidenceError):
        verify_durable_prose_receipts(hook_db, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("table,column", [
    ("anti_ai_audit_runs", "report_json"), ("anti_ai_stream_runs", "summary_json"),
])
@pytest.mark.parametrize("field,value", [("run_id", "another-run"), ("error", "scanner failed")])
async def test_scan_identity_and_error_are_checked(hook_db, table, column, field, value):
    streaming = table == "anti_ai_stream_runs"
    args = await receipts(hook_db, streaming)
    run_id = args["stream_run_id" if streaming else "scan_run_id"]
    row = hook_db.fetch_one(f"SELECT * FROM {table} WHERE run_id=?", (run_id,))
    report = json.loads(row[column])
    report[field] = value
    hook_db.execute(f"UPDATE {table} SET {column}=? WHERE run_id=?", (json.dumps(report), run_id))
    hook_db.commit()
    with pytest.raises(ProseExecutionEvidenceError):
        verify_durable_prose_receipts(hook_db, **args)
