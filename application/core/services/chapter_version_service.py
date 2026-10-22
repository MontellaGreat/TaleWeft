"""Optimistic chapter saves with immutable versions and actual commit receipts."""
import difflib
import hashlib
import json
from uuid import uuid4


def content_hash(content):
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


class RevisionConflictError(ValueError):
    def __init__(self, chapter_id, expected_revision, current, proposed_content=None):
        self.chapter_id = chapter_id
        self.expected_revision = expected_revision
        self.current = current
        self.diff = "" if proposed_content is None else "".join(difflib.unified_diff(
            str((current or {}).get("content") or "").splitlines(keepends=True),
            str(proposed_content).splitlines(keepends=True), fromfile="server", tofile="local"))
        super().__init__("章节已被其他操作修改，请比较版本后再保存")

    def detail(self):
        return {"code": "chapter_revision_conflict", "message": str(self),
                "expected_revision": self.expected_revision, "current": self.current, "diff": self.diff}


class ChapterVersionService:
    def __init__(self, db=None):
        if db is None:
            from infrastructure.persistence.database.connection import get_database
            db = get_database()
        self.db = db

    def read(self, chapter_id):
        row = self.db.fetch_one("SELECT * FROM chapters WHERE id=?", (str(chapter_id),))
        return self._snapshot(row)

    @staticmethod
    def _snapshot(row):
        if row is None:
            return None
        result = dict(row)
        result["revision"] = int(result.get("revision") or 0)
        result["content_hash"] = content_hash(result.get("content"))
        result["etag"] = '"chapter:%s:%s:%s"' % (result["id"], result["revision"], result["content_hash"])
        return result

    def prepare_save(self, chapter_id, expected_revision, content, source="manual", *, expected_hash=None, changes=None):
        """Build one conditional statement usable by the staged writer batch."""
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        current = self.read(chapter_id)
        if current is None:
            raise LookupError("章节不存在")
        if current["revision"] != expected_revision or (expected_hash is not None and current["content_hash"] != expected_hash):
            raise RevisionConflictError(chapter_id, expected_revision, current, content)
        extra = dict(changes or {})
        if set(extra) - {"title", "outline", "status", "generation_hint"}:
            raise ValueError("unsupported chapter fields")
        fields = ["content=?", "content_hash=?", "version_source=?", "revision=revision+1", "word_count=?", "updated_at=CURRENT_TIMESTAMP"]
        params = [str(content), content_hash(content), str(source), len(str(content))]
        for key, value in extra.items():
            fields.append(key + "=?")
            params.append(value)
        # Comparing content too protects raw legacy writers before revision was migrated.
        params.extend([str(chapter_id), expected_revision, current.get("content")])
        return "UPDATE chapters SET " + ",".join(fields) + " WHERE id=? AND revision=? AND content IS ?", tuple(params)

    def save_if_revision(self, chapter_id, expected_revision, content, source="manual", *, expected_hash=None, changes=None, _writer=None, pending_job=None, restore_audit=None, save_receipt=None, _write_condition=None):
        sql, params = self.prepare_save(chapter_id, expected_revision, content, source,
                                        expected_hash=expected_hash, changes=changes)
        if _write_condition is not None:
            condition_sql, condition_params = _write_condition
            sql += " AND (" + condition_sql + ")"
            params += tuple(condition_params)
        if _writer is not None:
            if pending_job is not None or restore_audit is not None or save_receipt is not None:
                raise ValueError("pending jobs and restore audits require a managed transaction")
            # The caller owns transaction/ACK. Return the cursor and inspect it only
            # after exiting a staged batch; no premature reads of RETURNING results.
            return _writer.execute(sql, params)
        if save_receipt is not None:
            if (not isinstance(save_receipt, dict) or not save_receipt.get('request_id')
                    or not save_receipt.get('payload_hash')):
                raise ValueError('save receipt requires request identity and registered payload hash')
            # Check identity in the winning CAS itself. A missing, failed,
            # wrong-scope or changed registration cannot save the body while
            # leaving a zero-row receipt and a phantom derivation job.
            sql += """ AND EXISTS (SELECT 1 FROM chapter_save_receipts r
                WHERE r.request_id=? AND r.payload_hash=? AND r.status='pending'
                AND r.novel_id=chapters.novel_id AND r.chapter_number=chapters.number)"""
            params += (save_receipt['request_id'], save_receipt['payload_hash'])
        scope = self.read(chapter_id) if pending_job is not None else None
        with self.db.transaction() as conn:
            cursor = conn.execute(sql, params)
            if save_receipt is not None:
                # This receipt commits with exactly this winning CAS and version.
                conn.execute(
                    "UPDATE chapter_save_receipts SET status='committed',chapter_id=?,revision=?,content_hash=?,"
                    "derivation_job_id=?,updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='pending' AND changes()=1",
                    (str(chapter_id), expected_revision + 1, content_hash(content),
                     pending_job['job_id'] if pending_job else None, save_receipt['request_id']))
            if pending_job is not None:
                # changes() observes this transaction's CAS UPDATE, so a losing
                # writer cannot attach its sidecar payload to the winning save.
                conn.execute(
                    "INSERT INTO chapter_derivation_jobs "
                    "(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
                    "SELECT ?,id,novel_id,number,revision,? FROM chapters WHERE id=? AND changes()=1",
                    (pending_job["job_id"], json.dumps(pending_job["payload"], ensure_ascii=False), str(chapter_id)),
                )
            if restore_audit is not None:
                conn.execute(
                    "INSERT INTO chapter_draft_restore_audits "
                    "(audit_id,draft_id,chapter_id,novel_id,from_revision,to_revision,draft_content_hash,source) "
                    "SELECT ?,?,?,novel_id,?,?,?,? FROM chapters WHERE id=? AND changes()=1",
                    (
                        restore_audit.get("audit_id") or uuid4().hex,
                        str(restore_audit["draft_id"]), str(chapter_id),
                        int(expected_revision), int(expected_revision) + 1,
                        content_hash(content), str(source), str(chapter_id),
                    ),
                )
            if pending_job is not None:
                # Invalidate dependent read models in the same winning save.
                # The job identity gates every statement so losing CAS writers
                # cannot mark another writer's valid summaries stale.
                conn.execute("UPDATE chapter_summaries SET sync_status='stale' WHERE knowledge_id IN "
                             "(SELECT id FROM knowledge WHERE novel_id=(SELECT novel_id FROM chapters WHERE id=?)) "
                             "AND chapter_number >= (SELECT number FROM chapters WHERE id=?) "
                             "AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                             (str(chapter_id), str(chapter_id), pending_job['job_id']))
                conn.execute("UPDATE chapter_evolution_snapshots SET status='stale' WHERE novel_id=(SELECT novel_id FROM chapters WHERE id=?) "
                             "AND chapter_number >= (SELECT number FROM chapters WHERE id=?) "
                             "AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                             (str(chapter_id), str(chapter_id), pending_job['job_id']))
                conn.execute("UPDATE story_nodes SET title=(SELECT title FROM chapters WHERE id=?) WHERE novel_id=(SELECT novel_id FROM chapters WHERE id=?) "
                             "AND node_type='chapter' AND number=(SELECT number FROM chapters WHERE id=?) "
                             "AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                             (str(chapter_id), str(chapter_id), str(chapter_id), pending_job['job_id']))
                from application.engine.services.memory_engine import invalidate_memory_engine_from_chapter
                invalidate_memory_engine_from_chapter(
                    conn, scope['novel_id'], scope['number'], job_id=pending_job['job_id'])
            version_cursor = conn.execute("SELECT * FROM content_versions WHERE chapter_id=? AND revision=?", (str(chapter_id), expected_revision + 1))
        if cursor.rowcount != 1:
            raise RevisionConflictError(chapter_id, expected_revision, self.read(chapter_id), content)
        version = version_cursor.fetchone()
        if version is None:
            raise RuntimeError("章节版本提交回执缺失")
        snapshot = dict(version)
        snapshot["id"] = str(chapter_id)
        snapshot["content_hash"] = content_hash(snapshot["content"])
        snapshot["etag"] = '"chapter:%s:%s:%s"' % (chapter_id, snapshot["revision"], snapshot["content_hash"])
        if pending_job is not None:
            snapshot["derived_status"] = "deferred"
            snapshot["derivation_job_id"] = pending_job["job_id"]
        return snapshot

    def list_versions(self, chapter_id, limit=50, before_revision=None):
        condition = "chapter_id=?"
        params = [str(chapter_id)]
        if before_revision is not None:
            condition += " AND revision<?"
            params.append(int(before_revision))
        params.append(max(1, min(200, int(limit))))
        rows = self.db.fetch_all("SELECT * FROM content_versions WHERE " + condition + " ORDER BY revision DESC LIMIT ?", tuple(params))
        return [dict(row, content_hash=content_hash(row["content"])) for row in rows]
