"""Durable proof that a prose provider call passed the Anti-AI control plane."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping


class ProseExecutionEvidenceError(ValueError):
    """A prose result cannot be adopted without its immutable receipts."""


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _row_evidence(row: Mapping[str, Any], *, column: str = "evidence_json") -> dict[str, Any]:
    try:
        value = json.loads(row.get(column) or "{}")
    except (TypeError, ValueError) as exc:
        raise ProseExecutionEvidenceError("去味执行回执内容不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ProseExecutionEvidenceError("去味执行回执内容不是有效对象")
    return value


def _receipt_row(db, table: str, *, run_id: str, job_id: str, novel_id: str,
                 chapter_number: Any, status: str) -> Mapping[str, Any]:
    row = db.fetch_one(
        f"SELECT * FROM {table} WHERE run_id=? AND job_id=? AND novel_id=? "
        "AND chapter_number IS ? AND status=?",
        (run_id, job_id, novel_id, chapter_number, status),
    )
    if not row:
        raise ProseExecutionEvidenceError(f"缺少耐久去味执行回执: {table}")
    return row


def archive_prose_scan_receipt(db, *, novel_id: str, chapter_number: Any,
                               attempt_id: str, content: str,
                               scene_type: str = "default") -> dict[str, Any]:
    """Run and durably bind the mandatory full-body scan for non-stream prose."""
    novel_id = str(novel_id or "").strip()
    try:
        chapter_number = int(chapter_number)
    except (TypeError, ValueError) as exc:
        raise ProseExecutionEvidenceError("非流式正文缺少有效章节号，无法执行去味扫描") from exc
    if not novel_id or chapter_number <= 0 or not attempt_id:
        raise ProseExecutionEvidenceError("非流式正文缺少小说、章节或执行任务 ID")
    if not isinstance(content, str) or not content.strip():
        raise ProseExecutionEvidenceError("正文为空，无法执行去味扫描")

    try:
        from application.audit.services.anti_ai_audit import AntiAIAuditor, validate_completed_report
        from infrastructure.ai.quality_rule_store import QualityRuleStore
        from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository

        effective = QualityRuleStore(db).effective()
        auditor = AntiAIAuditor(rule_provider=lambda: effective, scene_type=scene_type or "default")
        report = auditor.scan_chapter(f"ch-{chapter_number}", content)
        validate_completed_report(report, content, rule_version=auditor.effective_rule_version(effective))
        report.run_id = uuid.uuid4().hex
        report.job_id = attempt_id
        report.persistence_status = "committed"
        with db.transaction() as writer:
            SqliteAntiAiAuditRepository(db).record_run(novel_id, chapter_number, report, _writer=writer)
        return {
            "run_id": report.run_id,
            "job_id": attempt_id,
            "scan_status": report.scan_status,
            "persistence_status": "committed",
            "content_hash": report.content_hash,
            "rule_version": report.rule_version,
        }
    except ProseExecutionEvidenceError:
        raise
    except Exception as exc:
        raise ProseExecutionEvidenceError(f"非流式正文去味扫描或回执持久化失败: {exc}") from exc


def verify_durable_prose_receipts(
    db,
    *,
    novel_id: str,
    chapter_number: Any,
    attempt_id: str,
    content: str,
    prompt_hash: str,
    require_stream: bool,
    prompt_run_id: str = "",
    stream_run_id: str = "",
    scan_run_id: str = "",
) -> dict[str, Any]:
    """Verify receipts and their hashes against the exact adopted attempt."""
    if not attempt_id or not content or not content.strip():
        raise ProseExecutionEvidenceError("正文为空或缺少执行任务 ID")
    if not prompt_hash:
        raise ProseExecutionEvidenceError("缺少冻结提示词哈希")
    try:
        prompt = _receipt_row(
            db, "anti_ai_prompt_runs", run_id=prompt_run_id, job_id=attempt_id,
            novel_id=novel_id, chapter_number=chapter_number, status="completed",
        )
        prompt_evidence = _row_evidence(prompt)
        if (not prompt.get("rule_version")
                or any(prompt_evidence.get(key) != prompt.get(key)
                       for key in ("run_id", "job_id", "status", "rule_version"))
                or prompt_evidence.get("error")
                or prompt_evidence.get("persistence_status") not in {None, "committed"}):
            raise ProseExecutionEvidenceError("去味提示词回执身份、规则版本或终态不一致")
        if prompt["prompt_hash"] != prompt_hash or prompt_evidence.get("prompt_hash") != prompt_hash:
            raise ProseExecutionEvidenceError("去味提示词回执与冻结提示词不匹配")
        expected_body_hash = _content_hash(content)
        if prompt_evidence.get("content_hash") != expected_body_hash:
            raise ProseExecutionEvidenceError("去味提示词回执与正文不匹配")
        if prompt.get("error"):
            raise ProseExecutionEvidenceError("去味提示词回执标记为错误")
        if not require_stream:
            # The scan receipt is owned by the attempt/direct-prose call and
            # is persisted separately from the prompt receipt.  Some legacy
            # callers only persist the prompt evidence JSON, so the optional
            # copy embedded there cannot be the source of truth.  When it is
            # present, still reject a contradictory run id.
            if not scan_run_id:
                raise ProseExecutionEvidenceError("缺少与本次生成绑定的完整去味扫描回执")
            embedded_scan = dict(prompt_evidence.get("anti_ai_scan") or {})
            if embedded_scan and embedded_scan.get("run_id") not in {None, scan_run_id}:
                raise ProseExecutionEvidenceError("提示词回执中的去味扫描 ID 与正文回执不匹配")
            scan = db.fetch_one(
                "SELECT * FROM anti_ai_audit_runs WHERE run_id=? AND novel_id=? AND chapter_number=?",
                (scan_run_id, novel_id, chapter_number),
            )
            if (not scan or scan.get("scan_status") != "completed"
                    or scan.get("persistence_status") != "committed" or scan.get("error")):
                raise ProseExecutionEvidenceError("完整去味扫描回执未完成或未持久化")
            if scan.get("content_hash") != expected_body_hash or not scan.get("rule_version"):
                raise ProseExecutionEvidenceError("完整去味扫描回执与正文不匹配")
            scan_report = _row_evidence(scan, column="report_json")
            if (scan_report.get("run_id") != scan_run_id
                    or scan_report.get("job_id") != attempt_id
                    or scan_report.get("error")
                    or scan_report.get("scan_status") != "completed"
                    or scan_report.get("persistence_status") != "committed"
                    or scan_report.get("content_hash") != expected_body_hash
                    or scan_report.get("rule_version") != scan.get("rule_version")):
                raise ProseExecutionEvidenceError("完整去味扫描回执未绑定本次生成")
        if require_stream:
            stream = _receipt_row(
                db, "anti_ai_stream_runs", run_id=stream_run_id, job_id=attempt_id,
                novel_id=novel_id, chapter_number=chapter_number, status="completed",
            )
            if stream.get("persistence_status") != "committed" or stream.get("scan_status") != "completed":
                raise ProseExecutionEvidenceError("流式去味回执未完成持久化或最终扫描")
            stream_summary = _row_evidence(stream, column="summary_json")
            if (not stream.get("rule_version") or stream.get("error")
                    or any(stream_summary.get(key) != stream.get(key)
                           for key in ("run_id", "job_id", "status", "persistence_status", "rule_version"))
                    or stream_summary.get("final_scan_status") != "completed"
                    or stream_summary.get("error") or stream_summary.get("archive_error")):
                raise ProseExecutionEvidenceError("流式去味回执身份、规则版本或终态不一致")
            if stream.get("content_hash") != expected_body_hash or stream_summary.get("content_hash") != expected_body_hash:
                raise ProseExecutionEvidenceError("流式去味回执与正文不匹配")
            return {
                "verified": True,
                "attempt_id": attempt_id,
                "require_stream": True,
                "prompt_run_id": prompt["run_id"],
                "stream_run_id": stream["run_id"],
                "scan_run_id": "",
                "content_hash": expected_body_hash,
                "prompt_hash": prompt_hash,
            }
        return {
            "verified": True,
            "attempt_id": attempt_id,
            "require_stream": False,
            "prompt_run_id": prompt["run_id"],
            "stream_run_id": "",
            "scan_run_id": scan_run_id,
            "content_hash": expected_body_hash,
            "prompt_hash": prompt_hash,
        }
    except ProseExecutionEvidenceError:
        raise
    except Exception as exc:
        raise ProseExecutionEvidenceError(f"无法读取耐久去味执行回执: {exc}") from exc


def verify_attempt_prose_receipts(db, *, session, attempt) -> dict[str, Any]:
    evidence = dict((session.metadata or {}).get("anti_ai_prompt") or {})
    if not evidence:
        raise ProseExecutionEvidenceError("缺少去味提示词执行证据")
    accounting = dict(attempt.accounting or {})
    prompt_receipt = dict(accounting.get("anti_ai_prompt") or {})
    stream_receipt = dict(accounting.get("anti_ai_stream") or {})
    scan_receipt = dict(accounting.get("anti_ai_scan") or {})
    return verify_durable_prose_receipts(
        db,
        novel_id=str((session.context or {}).get("novel_id") or ""),
        chapter_number=(session.context or {}).get("chapter_number"),
        attempt_id=attempt.id,
        content=attempt.content,
        prompt_hash=str(attempt.prompt_snapshot.rendered_prompt_hash or evidence.get("prompt_hash") or ""),
        require_stream=bool(stream_receipt),
        prompt_run_id=str(prompt_receipt.get("run_id") or stream_receipt.get("prompt_receipt", {}).get("run_id") or ""),
        stream_run_id=str(stream_receipt.get("run_id") or ""),
        scan_run_id=str(scan_receipt.get("run_id") or ""),
    )


def verify_prose_result_receipts(db, *, novel_id: str, chapter_number: int,
                                 content: str, proof: Mapping[str, Any]) -> dict[str, Any]:
    """Re-check a direct workflow's final body immediately before saving it."""
    if not isinstance(proof, Mapping) or not proof.get("verified"):
        raise ProseExecutionEvidenceError("缺少最终正文的去味执行证据")
    return verify_durable_prose_receipts(
        db, novel_id=novel_id, chapter_number=chapter_number,
        attempt_id=str(proof.get("attempt_id") or ""), content=content,
        prompt_hash=str(proof.get("prompt_hash") or ""),
        require_stream=bool(proof.get("require_stream")),
        prompt_run_id=str(proof.get("prompt_run_id") or ""),
        stream_run_id=str(proof.get("stream_run_id") or ""),
        scan_run_id=str(proof.get("scan_run_id") or ""),
    )


def prose_receipt_write_fence(db, *, novel_id: str, chapter_number: int,
                               content: str, proof: Mapping[str, Any]) -> tuple[str, tuple]:
    """Bind receipt verification to a queued manuscript CAS in the writer.

    The reader's checks alone cannot stop a receipt being deleted while its
    save command waits in the queue. Compare the verified receipt snapshots
    inside the actual INSERT/UPDATE statement as well.
    """
    verified = verify_prose_result_receipts(
        db, novel_id=novel_id, chapter_number=chapter_number, content=content, proof=proof)
    receipt_ids = [("anti_ai_prompt_runs", verified["prompt_run_id"])]
    if verified["require_stream"]:
        receipt_ids.append(("anti_ai_stream_runs", verified["stream_run_id"]))
    else:
        receipt_ids.append(("anti_ai_audit_runs", verified["scan_run_id"]))
    clauses, params, snapshots = [], [], {}
    for table, run_id in receipt_ids:
        row = db.fetch_one(f"SELECT * FROM {table} WHERE run_id=?", (run_id,))
        if not row:
            raise ProseExecutionEvidenceError("保存前去味回执已删除")
        snapshots[table] = row
        # All columns belong to our own fixed receipt tables; values stay bound.
        clauses.append(f"EXISTS (SELECT 1 FROM {table} WHERE " +
                       " AND ".join(f'"{column}" IS ?' for column in row) + ")")
        params.extend(row.values())
    class SnapshotReader:
        def fetch_one(self, sql, query_params):
            for table, row in snapshots.items():
                if f"FROM {table} WHERE " in sql:
                    columns = ("run_id", "novel_id", "chapter_number") if table == "anti_ai_audit_runs" else (
                        "run_id", "job_id", "novel_id", "chapter_number", "status")
                    return row if tuple(row.get(column) for column in columns) == tuple(query_params) else None
            raise ProseExecutionEvidenceError("未知去味回执查询")

    # Verify precisely the snapshots embedded in the writer predicate. A row
    # changed between the initial read and snapshot capture cannot be accepted.
    verify_prose_result_receipts(
        SnapshotReader(), novel_id=novel_id, chapter_number=chapter_number, content=content, proof=proof)
    return " AND ".join(clauses), tuple(params)


def verify_decision_prose_receipts(db, *, session, decision, proof: Mapping[str, Any]) -> dict[str, Any]:
    """Re-check a persisted decision immediately before committing it."""
    if not proof.get("verified"):
        raise ProseExecutionEvidenceError("缺少采纳时验证的去味执行证据")
    return verify_durable_prose_receipts(
        db,
        novel_id=str((session.context or {}).get("novel_id") or ""),
        chapter_number=(session.context or {}).get("chapter_number"),
        attempt_id=decision.attempt_id,
        content=decision.accepted_content,
        prompt_hash=str(proof.get("prompt_hash") or ""),
        require_stream=bool(proof.get("require_stream")),
        prompt_run_id=str(proof.get("prompt_run_id") or ""),
        stream_run_id=str(proof.get("stream_run_id") or ""),
        scan_run_id=str(proof.get("scan_run_id") or ""),
    )
