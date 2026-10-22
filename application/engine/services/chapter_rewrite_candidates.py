"""Recoverable, immutable rewrite candidates and compare-and-swap approval."""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
from uuid import uuid4


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class CandidateConflictError(RuntimeError):
    pass


class ChapterRewriteCandidates:
    def __init__(self, db):
        self.db = db

    def save_candidate(self, *, run_id, novel_id, chapter_id, chapter_number, content,
                       kind="rewrite", attempt=1, base_hash=None, base_content=None, base_revision=None,
                       prose_evidence=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("候选正文不能为空")
        chapter = self.db.fetch_one("SELECT * FROM chapters WHERE id=? AND novel_id=?", (chapter_id, novel_id))
        if chapter is None:
            raise ValueError("章节不存在")
        if chapter.get("number") != chapter_number:
            raise CandidateConflictError("候选章节号与实际章节不一致")
        if base_content is None:
            base_content = chapter.get("content") or ""
        actual_base_hash = content_hash(base_content)
        if base_hash is not None and base_hash != actual_base_hash:
            raise ValueError("原稿快照与 hash 不一致")
        now = datetime.now(timezone.utc).isoformat()
        candidate_id, digest = str(uuid4()), content_hash(content)
        audit, receipt_clause, receipt_params = {}, "1", ()
        if prose_evidence is not None:
            from application.ai_invocation.prose_execution_evidence import (
                verify_prose_result_receipts, prose_receipt_write_fence)
            audit["prose_evidence"] = verify_prose_result_receipts(
                self.db, novel_id=novel_id, chapter_number=chapter_number,
                content=content, proof=prose_evidence)
            receipt_clause, receipt_params = prose_receipt_write_fence(
                self.db, novel_id=novel_id, chapter_number=chapter_number,
                content=content, proof=audit["prose_evidence"])
        self.db.execute("""INSERT INTO chapter_rewrite_candidates
            (candidate_id,run_id,novel_id,chapter_id,chapter_number,kind,attempt,
             base_hash,base_revision,base_content,content,content_hash,status,audit_json,created_at,updated_at)
            SELECT ?,?,?,?,?,?,?,?,?,?,?,?,'generated',?,?,? WHERE """ + receipt_clause + """
            AND EXISTS (SELECT 1 FROM chapters WHERE id=? AND novel_id=? AND number=?)
            ON CONFLICT(run_id,kind,attempt) DO NOTHING""",
            (candidate_id, run_id, novel_id, chapter_id, chapter_number, kind, attempt,
             actual_base_hash, base_revision if base_revision is not None else chapter.get("revision"),
             base_content, content, digest, json.dumps(audit, ensure_ascii=False), now, now)
            + receipt_params + (chapter_id, novel_id, chapter_number))
        self.db.commit()
        row = self.db.fetch_one("SELECT * FROM chapter_rewrite_candidates WHERE run_id=? AND kind=? AND attempt=?",
                                (run_id, kind, attempt))
        if (row is None or row["novel_id"] != novel_id or row["chapter_id"] != chapter_id
                or row["chapter_number"] != chapter_number or row["content_hash"] != digest
                or row["base_hash"] != actual_base_hash):
            raise CandidateConflictError("候选回执、章节身份或同一改写步骤已经变化；已有稿件不会被覆盖")
        saved = self._decode(row)
        if saved["audit"].get("prose_evidence") != audit.get("prose_evidence"):
            raise CandidateConflictError("同一改写步骤的去味回执绑定已经变化")
        return saved

    @staticmethod
    def _decode(row):
        result = dict(row)
        result["audit"] = json.loads(result.pop("audit_json"))
        # Both immutable snapshots are stored, so recovery/review always uses
        # the same diff even if the author edits the chapter afterwards.
        result["diff"] = "".join(difflib.unified_diff(
            result["base_content"].splitlines(keepends=True), result["content"].splitlines(keepends=True),
            fromfile="原稿", tofile="候选稿"))
        return result

    def get(self, candidate_id):
        row = self.db.fetch_one("SELECT * FROM chapter_rewrite_candidates WHERE candidate_id=?", (candidate_id,))
        if row is None:
            raise KeyError(candidate_id)
        return self._decode(row)

    def list_for_chapter(self, novel_id, chapter_number=None, limit=50):
        condition = "novel_id=?" + (" AND chapter_number=?" if chapter_number is not None else "")
        params = (novel_id, chapter_number, min(100, max(1, limit))) if chapter_number is not None else (novel_id, min(100, max(1, limit)))
        rows = self.db.fetch_all(f"SELECT * FROM chapter_rewrite_candidates WHERE {condition} ORDER BY rowid DESC LIMIT ?", params)
        return [self._decode(row) for row in rows]

    def diff(self, candidate_id):
        row = self.get(candidate_id)
        row["diff"] = "".join(difflib.unified_diff(
            row["base_content"].splitlines(keepends=True), row["content"].splitlines(keepends=True),
            fromfile="原稿", tofile="候选稿"))
        return row

    def mark_audited(self, candidate_id, audit, accepted):
        row = self.db.fetch_one("SELECT * FROM chapter_rewrite_candidates WHERE candidate_id=?", (candidate_id,))
        if row is None:
            raise KeyError(candidate_id)
        candidate = self._decode(row)
        if audit.get("content_hash") not in (None, candidate["content_hash"]):
            raise ValueError("复验对象不是该候选正文")
        audit = dict(audit)
        proof = self.verify_prose_evidence(candidate)
        if proof is not None:
            if audit.get("prose_evidence", proof) != proof:
                raise CandidateConflictError("复验不能替换候选的去味回执绑定")
            audit["prose_evidence"] = proof
        elif "prose_evidence" in audit:
            raise CandidateConflictError("复验不能注入未绑定的去味回执")
        receipt_clause, receipt_params = "", ()
        if proof is not None:
            from application.ai_invocation.prose_execution_evidence import prose_receipt_write_fence
            receipt_clause, receipt_params = prose_receipt_write_fence(
                self.db, novel_id=candidate["novel_id"], chapter_number=candidate["chapter_number"],
                content=candidate["content"], proof=proof)
            receipt_clause = " AND " + receipt_clause
        updated = self.db.execute("""UPDATE chapter_rewrite_candidates SET audit_json=?,status=?,updated_at=?
            WHERE candidate_id=? AND status IN ('generated','audited','pending_review','failed')
                AND content=? AND content_hash=? AND audit_json=?
                AND novel_id=? AND chapter_id=? AND chapter_number=?
                AND EXISTS (SELECT 1 FROM chapters c WHERE c.id=chapter_rewrite_candidates.chapter_id
                    AND c.novel_id=chapter_rewrite_candidates.novel_id AND c.number=?)""" + receipt_clause,
            (json.dumps(audit, ensure_ascii=False), "audited" if accepted else "rejected",
             datetime.now(timezone.utc).isoformat(), candidate_id, candidate["content"],
             candidate["content_hash"], row["audit_json"], candidate["novel_id"], candidate["chapter_id"],
             candidate["chapter_number"], candidate["chapter_number"]) + receipt_params)
        self.db.commit()
        saved = self.get(candidate_id)
        # Rejection/commit by another owner must remain terminal. A failed CAS
        # while still reviewable must never reuse the previous successful audit.
        if updated.rowcount != 1 and saved["status"] in {"generated", "audited", "pending_review", "failed"}:
            raise CandidateConflictError("候选或去味回执在复验归档期间已经变化")
        return saved

    def verify_prose_evidence(self, candidate):
        """Reload and verify a candidate's immutable generation receipt binding.

        Callback and legacy candidates still receive fresh full-body audits;
        only candidates saved with actual provider receipts use this proof.
        """
        stored = self.get(candidate["candidate_id"])
        if (stored["content"] != candidate["content"]
                or stored["content_hash"] != content_hash(candidate["content"])
                or stored["novel_id"] != candidate["novel_id"]
                or stored["chapter_id"] != candidate["chapter_id"]
                or stored["chapter_number"] != candidate["chapter_number"]):
            raise CandidateConflictError("候选正文或章节身份已经变化")
        chapter = self.db.fetch_one("SELECT novel_id,number FROM chapters WHERE id=?", (stored["chapter_id"],))
        if (chapter is None or chapter["novel_id"] != stored["novel_id"]
                or chapter["number"] != stored["chapter_number"]):
            raise CandidateConflictError("候选对应的实际章节身份已经变化")
        proof = stored["audit"].get("prose_evidence")
        if proof is None:
            if "prose_evidence" in candidate.get("audit", {}):
                raise CandidateConflictError("候选去味回执未持久绑定")
            return None
        from application.ai_invocation.prose_execution_evidence import verify_prose_result_receipts
        return verify_prose_result_receipts(
            self.db, novel_id=stored["novel_id"], chapter_number=stored["chapter_number"],
            content=stored["content"], proof=proof)

    def mark_failed(self, candidate_id, error, status="failed"):
        if status not in {"failed", "conflict", "cancelled", "pending_review", "rejected"}:
            raise ValueError("无效候选状态")
        self.db.execute("""UPDATE chapter_rewrite_candidates SET status=?,error=?,updated_at=?
            WHERE candidate_id=? AND status!='committed'""",
            (status, str(error), datetime.now(timezone.utc).isoformat(), candidate_id))
        self.db.commit()
        return self.get(candidate_id)

    def mark_committed(self, candidate_id, digest, expected_revision=None):
        candidate = self.get(candidate_id)
        if candidate["content_hash"] != digest:
            raise CandidateConflictError("提交 hash 与候选正文不一致")
        chapter = self.db.fetch_one("SELECT content,revision FROM chapters WHERE id=? AND novel_id=?", (candidate["chapter_id"], candidate["novel_id"]))
        if chapter is None or content_hash(chapter.get("content") or "") != digest:
            raise CandidateConflictError("候选正文没有匹配的章节提交")
        revision = chapter["revision"] if expected_revision is None else expected_revision
        if chapter["revision"] != revision:
            raise CandidateConflictError("候选提交确认后正文 revision 已变化")
        proof = self.verify_prose_evidence(candidate)
        receipt_clause, receipt_params = "", ()
        if proof is not None:
            from application.ai_invocation.prose_execution_evidence import prose_receipt_write_fence
            receipt_clause, receipt_params = prose_receipt_write_fence(
                self.db, novel_id=candidate["novel_id"], chapter_number=candidate["chapter_number"],
                content=candidate["content"], proof=proof)
            receipt_clause = " AND " + receipt_clause
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.db.execute("""UPDATE chapter_rewrite_candidates
            SET status='committed',committed_at=?,updated_at=?,audit_json=json_set(audit_json,'$.committed_revision',?)
            WHERE candidate_id=? AND status IN ('audited','committed')
                AND novel_id=? AND chapter_id=? AND chapter_number=? AND EXISTS (SELECT 1 FROM chapters c
                WHERE c.id=chapter_rewrite_candidates.chapter_id AND c.novel_id=chapter_rewrite_candidates.novel_id
                    AND c.content=? AND c.revision=? AND c.number=?)""" + receipt_clause,
            (now, now, revision, candidate_id, candidate["novel_id"], candidate["chapter_id"],
             candidate["chapter_number"], candidate["content"], revision,
             candidate["chapter_number"]) + receipt_params)
        self.db.commit()
        if cursor.rowcount != 1:
            raise CandidateConflictError("候选归档时正文或候选状态已变化")
        saved = self.get(candidate_id)
        self.assert_committed_version(saved)
        return saved

    def commit_candidate(self, candidate_id, expected_candidate_hash=None, *, execution_guard=None):
        row = self.db.fetch_one("SELECT * FROM chapter_rewrite_candidates WHERE candidate_id=?", (candidate_id,))
        if row is None:
            raise KeyError(candidate_id)
        candidate = self._decode(row)
        if expected_candidate_hash is not None and candidate["content_hash"] != expected_candidate_hash:
            raise CandidateConflictError("确认的候选版本已经变化")
        if candidate["status"] == "committed":
            self.assert_committed_version(candidate)
            return candidate
        if candidate["status"] not in {"audited", "pending_review"} or candidate["audit"].get("accepted") is False:
            raise CandidateConflictError("候选正文尚未通过复验")
        if candidate["audit"].get("content_hash") != candidate["content_hash"]:
            raise CandidateConflictError("候选复验未绑定最终正文 hash")
        proof = self.verify_prose_evidence(candidate)
        receipt_clause, receipt_params = "", ()
        if proof is not None:
            from application.ai_invocation.prose_execution_evidence import prose_receipt_write_fence
            receipt_clause, receipt_params = prose_receipt_write_fence(
                self.db, novel_id=candidate["novel_id"], chapter_number=candidate["chapter_number"],
                content=candidate["content"], proof=proof)
            receipt_clause = " AND " + receipt_clause
        # Optional revision protects ABA edits as well as body equality. The
        # chapter revision triggers own content version history for every writer.
        revision_clause = " AND revision=?" if candidate["base_revision"] is not None else ""
        params = (candidate["content"], len(candidate["content"]), candidate["chapter_id"],
                  candidate["novel_id"], candidate["chapter_number"], candidate["base_content"], candidate_id,
                  candidate["content"], candidate["content_hash"], candidate["base_content"],
                  candidate["base_hash"], candidate["base_revision"], row["audit_json"],
                  candidate["run_id"], candidate["kind"], candidate["attempt"])
        if candidate["base_revision"] is not None:
            params += (candidate["base_revision"],)
        guard_clause = ""
        if execution_guard is not None:
            if execution_guard.get("author_review"):
                epoch = execution_guard.get("run_epoch")
                if epoch is None:
                    raise CandidateConflictError("作者确认缺少运行版本")
                guard_clause = """ AND EXISTS (SELECT 1 FROM novels n WHERE n.id=chapters.novel_id
                    AND n.autopilot_run_epoch=? AND
                    (COALESCE(n.autopilot_status,'idle')!='running' OR n.current_stage='paused_for_review'))"""
                params += (int(epoch),)
                origin = candidate["audit"].get("execution_identity") or {}
                if origin:
                    if origin.get("run_epoch") != int(epoch) or not origin.get("job_id"):
                        raise CandidateConflictError("候选所属运行已经停止或恢复；请根据当前正文重新生成")
                    guard_clause += """ AND EXISTS (SELECT 1 FROM autopilot_commands c
                        WHERE c.novel_id=chapters.novel_id AND c.command_id=? AND c.run_epoch=?
                            AND c.command_type IN ('start','resume') AND c.state='completed')"""
                    params += (origin["job_id"], int(epoch))
            else:
            # Evaluate ownership and expiry on the writer connection, rather
            # than trusting a check made before the command waited in a queue.
                job_id = str(execution_guard.get("job_id") or "")
                run_epoch = execution_guard.get("run_epoch")
                executor_pid = execution_guard.get("executor_pid")
                if not job_id or run_epoch is None or executor_pid is None:
                    raise CandidateConflictError("自动改写缺少本轮执行身份，不能提交")
                guard_clause = """ AND EXISTS (SELECT 1 FROM novels n
                JOIN novel_execution_leases l ON l.novel_id=n.id
                JOIN autopilot_commands c ON c.command_id=l.job_id AND c.novel_id=n.id
                WHERE n.id=chapters.novel_id AND n.autopilot_status='running'
                    AND n.autopilot_run_epoch=? AND l.owner='autopilot' AND l.job_id=?
                    AND l.expires_at>(julianday('now')-2440587.5)*86400.0
                    AND c.run_epoch=n.autopilot_run_epoch AND c.executor_pid=?
                    AND c.command_type IN ('start','resume') AND c.state='completed')"""
                params += (int(run_epoch), job_id, int(executor_pid))
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as writer:
            mutation = writer.execute("""UPDATE chapters SET content=?,word_count=?,updated_at=datetime('now')
                WHERE id=? AND novel_id=? AND number=? AND COALESCE(content,'')=?
                AND EXISTS (SELECT 1 FROM chapter_rewrite_candidates r WHERE r.candidate_id=?
                    AND r.status IN ('audited','pending_review') AND r.novel_id=chapters.novel_id
                    AND r.chapter_id=chapters.id AND r.chapter_number=chapters.number
                    AND r.content=? AND r.content_hash=? AND r.base_content=? AND r.base_hash=?
                    AND r.base_revision IS ? AND r.audit_json=? AND r.run_id=? AND r.kind=? AND r.attempt=?)"""
                + revision_clause + guard_clause + receipt_clause, params + receipt_params)
            # changes() belongs to the same SQLite connection and previous UPDATE;
            # no Python rowcount is inspected before the queued transaction ACK.
            confirmation = writer.execute("""UPDATE chapter_rewrite_candidates
                SET status='committed',committed_at=?,updated_at=?,
                    audit_json=json_set(audit_json,'$.committed_revision',
                        (SELECT revision FROM chapters WHERE id=chapter_rewrite_candidates.chapter_id))
                WHERE candidate_id=? AND status IN ('audited','pending_review') AND changes()=1""", (now, now, candidate_id))
        if mutation.rowcount != 1 or confirmation.rowcount != 1:
            self.mark_failed(candidate_id, "原稿或运行身份已经变化；请重新生成候选或手工合并", "conflict")
            raise CandidateConflictError("原稿版本或执行租约冲突；作者正文未被覆盖，候选稿已保留")
        saved = self.get(candidate_id)
        self.assert_committed_version(saved)
        return saved

    def assert_committed_version(self, candidate):
        """A prior commit is evidence for one exact body/revision, not later edits."""
        self.verify_prose_evidence(candidate)
        chapter = self.db.fetch_one("SELECT content,revision,number FROM chapters WHERE id=? AND novel_id=?",
                                    (candidate["chapter_id"], candidate["novel_id"]))
        revision = candidate.get("audit", {}).get("committed_revision")
        if (chapter is None or content_hash(chapter.get("content") or "") != candidate["content_hash"]
                or chapter["number"] != candidate["chapter_number"]
                or revision is not None and chapter["revision"] != revision):
            raise CandidateConflictError("已提交候选对应的正文版本已变化，不能复用旧审计")
        return chapter

    def record_final_audit(self, candidate_id, report):
        """Persist final evidence only while the exact acknowledged chapter is current."""
        candidate = self.get(candidate_id)
        self.assert_committed_version(candidate)
        if report.content_hash != candidate["content_hash"] or report.persistence_status != "committed":
            raise CandidateConflictError("最终审计未匹配已提交正文或未归档")
        from dataclasses import asdict
        revision = candidate["audit"].get("committed_revision")
        result = self.db.execute("""UPDATE chapter_rewrite_candidates
            SET audit_json=json_set(audit_json,'$.final_audit',json(?)),error=NULL,updated_at=?
            WHERE candidate_id=? AND status='committed' AND novel_id=? AND chapter_id=? AND chapter_number=? AND EXISTS
                (SELECT 1 FROM chapters c WHERE c.id=chapter_rewrite_candidates.chapter_id
                    AND c.novel_id=chapter_rewrite_candidates.novel_id AND c.content=? AND c.revision=?
                    AND c.number=?)""",
            (json.dumps(asdict(report), ensure_ascii=False), datetime.now(timezone.utc).isoformat(),
             candidate_id, candidate["novel_id"], candidate["chapter_id"], candidate["chapter_number"],
             candidate["content"], revision, candidate["chapter_number"]))
        self.db.commit()
        if result.rowcount != 1:
            raise CandidateConflictError("最终审计归档期间作者正文已变化")
        saved = self.get(candidate_id)
        self.assert_committed_version(saved)
        return saved

    def record_aftermath(self, candidate_id, result):
        candidate = self.get(candidate_id)
        self.assert_committed_version(candidate)
        cursor = self.db.execute("""UPDATE chapter_rewrite_candidates
            SET audit_json=json_set(audit_json,'$.aftermath',json(?)),updated_at=?
            WHERE candidate_id=? AND status='committed' AND novel_id=? AND chapter_id=? AND chapter_number=?
                AND EXISTS (SELECT 1 FROM chapters c
                WHERE c.id=chapter_rewrite_candidates.chapter_id AND c.novel_id=chapter_rewrite_candidates.novel_id
                    AND c.content=? AND c.revision=? AND c.number=?)""",
            (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), candidate_id,
             candidate["novel_id"], candidate["chapter_id"], candidate["chapter_number"], candidate["content"],
             candidate["audit"]["committed_revision"], candidate["chapter_number"]))
        self.db.commit()
        if cursor.rowcount != 1:
            raise CandidateConflictError("候选章后处理期间正文已变化")
        return self.get(candidate_id)

    def get_policy(self, novel_id):
        row = self.db.fetch_one("SELECT * FROM anti_ai_rewrite_policies WHERE novel_id=?", (novel_id,))
        return dict(row) if row else {"novel_id": novel_id, "mode": "scan_only", "max_attempts": 2, "max_severity": 3.0,
            "stream_action": "warn", "stream_critical_limit": 1, "scene_type": "default"}

    def set_policy(self, novel_id, mode, max_attempts=2, max_severity=3.0, stream_action="warn", stream_critical_limit=1, scene_type="default"):
        if mode not in {"scan_only", "review", "auto"} or not 1 <= max_attempts <= 5 or not 0 <= max_severity <= 100:
            raise ValueError("无效自动去味策略")
        if stream_action not in {"warn", "cancel"} or type(stream_critical_limit) is not int or not 1 <= stream_critical_limit <= 100:
            raise ValueError("无效流式去味策略")
        from application.engine.rules.allowlist_manager import get_allowlist_manager
        if scene_type not in get_allowlist_manager().list_scene_types():
            raise ValueError("无效去味场景类型")
        self.db.execute("""INSERT INTO anti_ai_rewrite_policies
            (novel_id,mode,max_attempts,max_severity,updated_at,stream_action,stream_critical_limit,scene_type) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(novel_id) DO UPDATE SET mode=excluded.mode,max_attempts=excluded.max_attempts,
                max_severity=excluded.max_severity,updated_at=excluded.updated_at,
                stream_action=excluded.stream_action,stream_critical_limit=excluded.stream_critical_limit,scene_type=excluded.scene_type""",
            (novel_id, mode, max_attempts, max_severity, datetime.now(timezone.utc).isoformat(), stream_action, stream_critical_limit,scene_type))
        self.db.commit()
        return self.get_policy(novel_id)
