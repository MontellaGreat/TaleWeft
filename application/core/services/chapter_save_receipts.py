"""Durable chapter PUT identity; pending means unconfirmed, never saved."""
import hashlib
import json

from application.core.services.chapter_version_service import content_hash


class SaveReceiptConflict(ValueError):
    pass


class ChapterSaveReceipts:
    def __init__(self, db):
        self.db = db

    def begin(self, request_id, novel_id, chapter_number, payload):
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
        row = self.db.fetch_one("SELECT * FROM chapter_save_receipts WHERE request_id=?", (request_id,))
        created = False
        if row is None:
            cursor = self.db.execute(
                "INSERT INTO chapter_save_receipts(request_id,novel_id,chapter_number,payload_hash) "
                "VALUES(?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
                (request_id, novel_id, chapter_number, fingerprint))
            created = cursor.rowcount == 1
            row = self.db.fetch_one("SELECT * FROM chapter_save_receipts WHERE request_id=?", (request_id,))
        if (row is None or row['novel_id'] != novel_id or row['chapter_number'] != chapter_number
                or row['payload_hash'] != fingerprint):
            raise SaveReceiptConflict('保存请求标识已被其它内容使用')
        return created, dict(row)

    def fail(self, request_id, error_code):
        self.db.execute("UPDATE chapter_save_receipts SET status='failed',error_code=?,updated_at=CURRENT_TIMESTAMP "
                        "WHERE request_id=? AND status='pending'", (error_code, request_id))

    def get(self, request_id, novel_id, chapter_number):
        row = self.db.fetch_one("SELECT * FROM chapter_save_receipts WHERE request_id=? AND novel_id=? AND chapter_number=?",
                                (request_id, novel_id, chapter_number))
        if row is None:
            return None
        result = dict(row)
        # A durable registration alone does not prove a writer survived restart.
        if result['status'] == 'pending':
            result['status'] = 'unconfirmed'
        if result['status'] == 'committed':
            version = self.db.fetch_one("SELECT * FROM content_versions WHERE chapter_id=? AND revision=?",
                                       (row['chapter_id'], row['revision']))
            if (version is None or version['novel_id'] != novel_id or version['chapter_number'] != chapter_number
                    or content_hash(version['content']) != row['content_hash']):
                result['status'] = 'unavailable'
                result['error_code'] = 'receipt_version_mismatch'
            else:
                snapshot = dict(version)
                snapshot.update(id=row['chapter_id'], number=chapter_number,
                                word_count=len(version['content']), content_hash=row['content_hash'],
                                etag=f'"chapter:{row["chapter_id"]}:{row["revision"]}:{row["content_hash"]}"',
                                derived_status='deferred', derivation_job_id=row['derivation_job_id'],
                                save_request_id=request_id, updated_at=version['created_at'])
                result['chapter'] = snapshot
        result.pop('payload_hash', None)
        return result
