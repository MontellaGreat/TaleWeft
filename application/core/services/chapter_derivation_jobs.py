"""Revision-bound durable chapter aftermath work for HTTP saves and recovery."""
import asyncio
import json
import logging
import re
import weakref

logger = logging.getLogger(__name__)
_NOVEL_LOCKS = weakref.WeakKeyDictionary()


def _format_stage_errors(value):
    """Summarize failed stage names without persisting provider error bodies."""
    if isinstance(value, (list, tuple, set)):
        stages = []
        for item in value:
            stage = str(item).split(':', 1)[0].strip()
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', stage) and stage not in stages:
                stages.append(stage)
        if stages:
            return '; '.join(f'{stage}: failed' for stage in stages)
    return '章后分析阶段失败，详细错误已省略'


def _safe_error_summary(result):
    if result.get('stage_errors'):
        return _format_stage_errors(result['stage_errors'])
    for field in ('narrative_sync_error', 'error'):
        value = str(result.get(field) or '')
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_.]*)\s*:', value)
        if match:
            return f'{match.group(1)}: 上游详细错误已省略'
    return '章后管线未完成，详细错误已省略'


async def run_chapter_derivation_job(db, job_id, pipeline, knowledge_service):
    """Serialize chapter read-model changes per novel and replay the suffix."""
    scope = await asyncio.to_thread(db.fetch_one,
        'SELECT novel_id,chapter_number,payload_json,status,cascade_status FROM chapter_derivation_jobs WHERE job_id=?', (job_id,))
    if not scope:
        return None
    locks = _NOVEL_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault((str(db.db_path), scope['novel_id']), asyncio.Lock())
    async with lock:
        scope = await asyncio.to_thread(db.fetch_one,
            'SELECT novel_id,chapter_number,payload_json,status,cascade_status FROM chapter_derivation_jobs WHERE job_id=?', (job_id,))
        if not scope:
            return None
        if scope['status'] == 'running':
            return None
        if scope['status'] == 'completed' and scope['cascade_status'] in ('completed', 'not_requested'):
            return None
        try:
            payload = json.loads(scope['payload_json'])
        except (TypeError, ValueError) as exc:
            # A corrupt outbox row must not remain pending forever or bubble
            # out of the background task without a durable UI-visible status.
            message = f"派生任务载荷无效：{type(exc).__name__}"
            await asyncio.to_thread(
                db.execute,
                "UPDATE chapter_derivation_jobs SET status='unavailable',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status=?",
                (message, job_id, scope['status']),
            )
            await asyncio.to_thread(db.commit)
            return 'unavailable'
        if not isinstance(payload, dict):
            message = '派生任务载荷无效：顶层数据必须是对象'
            await asyncio.to_thread(
                db.execute,
                "UPDATE chapter_derivation_jobs SET status='unavailable',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status=?",
                (message, job_id, scope['status']),
            )
            await asyncio.to_thread(db.commit)
            return 'unavailable'
        cascade = bool(payload.get('reconcile_following'))
        if cascade:
            await asyncio.to_thread(_cascade_status, db, job_id, 'running')
        outcome = await _run_chapter_derivation_job(db, job_id, pipeline, knowledge_service)
        if outcome != 'completed' or not payload.get('reconcile_following'):
            if cascade:
                await asyncio.to_thread(_cascade_status, db, job_id, 'unavailable', '本章关联分析尚未完成')
            return outcome
        following = await asyncio.to_thread(_enqueue_following_chapters, db, scope['novel_id'], scope['chapter_number'])
        for next_job in following:
            next_outcome = await _run_chapter_derivation_job(db, next_job, pipeline, knowledge_service)
            if next_outcome != 'completed':
                await asyncio.to_thread(_cascade_status, db, job_id, 'unavailable', '后续章节关联分析尚未完成：' + next_job)
                return 'unavailable'
        await asyncio.to_thread(_cascade_status, db, job_id, 'completed')
        return outcome


def _cascade_status(db, job_id, status, error=''):
    db.execute('UPDATE chapter_derivation_jobs SET cascade_status=?,cascade_error=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?',
               (status, error, job_id))
    db.commit()


def _enqueue_following_chapters(db, novel_id, chapter_number):
    """Reanalyse existing prose in order; never regenerate an author's text."""
    from uuid import uuid4
    rows = db.fetch_all('SELECT id,revision,content FROM chapters WHERE novel_id=? AND number>? AND trim(content)<>\'\' ORDER BY number',
                        (novel_id, chapter_number))
    jobs = []
    for row in rows:
        with db.transaction() as conn:
            conn.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
                         "SELECT ?,id,novel_id,number,revision,? FROM chapters WHERE id=? AND revision=? AND content IS ? "
                         "ON CONFLICT(chapter_id,revision) DO UPDATE SET status='pending',payload_json=excluded.payload_json,last_error='',updated_at=CURRENT_TIMESTAMP "
                         "WHERE chapter_derivation_jobs.status<>'running'",
                         (uuid4().hex, json.dumps({'content': row['content'], 'operation': 'retry_aftermath'}, ensure_ascii=False),
                          row['id'], row['revision'], row['content']))
            cursor = conn.execute('SELECT job_id FROM chapter_derivation_jobs WHERE chapter_id=? AND revision=?', (row['id'], row['revision']))
        job = cursor.fetchone()
        if job:
            jobs.append(job['job_id'])
    return jobs


async def _run_chapter_derivation_job(db, job_id, pipeline, knowledge_service):
    def claim():
        cursor = db.execute(
            "UPDATE chapter_derivation_jobs SET status='running',attempts=attempts+1,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND (status IN ('pending','unavailable') OR "
            "(status='completed' AND cascade_status IN ('running','unavailable')))",
            (job_id,),
        )
        db.commit()
        if cursor.rowcount != 1:
            return None
        return db.fetch_one("SELECT * FROM chapter_derivation_jobs WHERE job_id=?", (job_id,))

    try:
        job = await asyncio.to_thread(claim)
    except Exception:
        logger.exception("章节派生任务尚未取得 writer 回执 job=%s", job_id)
        return "unavailable"
    if not job:
        return None

    def finish(status, error=""):
        db.execute(
            "UPDATE chapter_derivation_jobs SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (status, str(error)[:1000], job_id),
        )
        db.commit()

    def still_current():
        from application.core.services.chapter_version_service import content_hash
        row = db.fetch_one("SELECT revision,content FROM chapters WHERE id=?", (job["chapter_id"],))
        if row is None or row['revision'] != job['revision'] or content_hash(row['content']) != content_hash(payload['content']):
            return False
        for dependency in dependencies:
            current = db.fetch_one('SELECT revision,content FROM chapters WHERE id=?', (dependency['id'],))
            if current is None or current['revision'] != dependency['revision'] or content_hash(current['content']) != dependency['content_hash']:
                return False
        return True

    try:
        payload = json.loads(job["payload_json"])
        from application.core.services.chapter_version_service import content_hash
        dependencies = [{"id": row['id'], "revision": row['revision'], "content_hash": content_hash(row['content'])}
            for row in await asyncio.to_thread(db.fetch_all,
                'SELECT id,revision,content FROM chapters WHERE novel_id=? AND number<?', (job['novel_id'], job['chapter_number']))]
        blocking = await asyncio.to_thread(db.fetch_one,
            "SELECT j.chapter_number FROM chapter_derivation_jobs j JOIN chapters c ON c.id=j.chapter_id AND c.revision=j.revision "
            "WHERE j.novel_id=? AND j.chapter_number<? AND j.status IN ('pending','running','unavailable') ORDER BY j.chapter_number LIMIT 1",
            (job['novel_id'], job['chapter_number']))
        if blocking:
            await asyncio.to_thread(finish, 'unavailable', f"请先完成第 {blocking['chapter_number']} 章的关联分析")
            return 'unavailable'
        if not await asyncio.to_thread(still_current):
            await asyncio.to_thread(finish, "superseded")
            return "superseded"
        from application.core.services.chapter_version_service import content_hash
        from infrastructure.persistence.database.chapter_write_fence import chapter_write_fence, ChapterWriteSuperseded
        with chapter_write_fence(db.db_path, job["chapter_id"], job["revision"], content_hash(payload["content"]), dependencies=dependencies):
            beats = payload.get("micro_beats")
            if beats:
                patched = await asyncio.to_thread(knowledge_service.patch_chapter_micro_beats,
                    job["novel_id"], job["chapter_number"], beats,
                    expected_chapter_revision=job["revision"], expected_content_hash=content_hash(payload["content"]))
                if patched is False:
                    raise ChapterWriteSuperseded("micro-beat snapshot superseded")
            result = {}
            if payload.get('operation') == 'rebuild_indexes':
                indexing = getattr(pipeline, '_indexing', None)
                if indexing is None:
                    raise RuntimeError('章节向量索引服务不可用')
                summary = await asyncio.to_thread(db.fetch_one,
                    'SELECT s.summary FROM chapter_summaries s JOIN knowledge k ON k.id=s.knowledge_id WHERE k.novel_id=? AND s.chapter_number=?',
                    (job['novel_id'], job['chapter_number']))
                # Rebuild the existing summary read model; generation stays in
                # the aftermath retry action and never runs implicitly here.
                text = (summary or {}).get('summary') or payload['content']
                await indexing.index_chapter_summary(job['novel_id'], job['chapter_number'], text)
            else:
                result = await pipeline.run_after_chapter_saved(
                    job["novel_id"], job["chapter_number"], payload["content"], chapter_micro_beats=beats,
                )
            if isinstance(result, dict) and result.get("auxiliary_deferred") and hasattr(pipeline, "drain_auxiliary_stages"):
                await pipeline.drain_auxiliary_stages()
            if await asyncio.to_thread(still_current):
                from application.manuscript.reindex_job import reindex_chapter_entity_mentions
                await asyncio.to_thread(reindex_chapter_entity_mentions,
                                        job["novel_id"], job["chapter_number"], payload["content"], strict=True, database=db)
        if not await asyncio.to_thread(still_current):
            await asyncio.to_thread(finish, "superseded")
            return "superseded"
        if payload.get('operation') != 'rebuild_indexes':
            if not isinstance(result, dict):
                await asyncio.to_thread(finish, 'unavailable', '章后管线未返回有效结果')
                return 'unavailable'
            narrative_status = str(result.get("narrative_sync_status") or "").strip().lower()
            derivation_status = str(result.get("derivation_status") or "").strip().lower()
            failed_statuses = {"failed", "partial", "unavailable", "retryable", "superseded", "invalid", "blocked"}
            if narrative_status in failed_statuses or derivation_status in failed_statuses:
                await asyncio.to_thread(finish, "unavailable", _safe_error_summary(result))
                return "unavailable"
            if result.get("narrative_sync_ok") is False or not (
                result.get("narrative_sync_ok") is True
                or narrative_status in {"ok", "synced", "completed"}
            ):
                await asyncio.to_thread(finish, "unavailable", _safe_error_summary(result))
                return "unavailable"
            if result.get("stage_errors"):
                await asyncio.to_thread(finish, "unavailable", _format_stage_errors(result["stage_errors"]))
                return "unavailable"
        await asyncio.to_thread(finish, "completed")
        return "completed"
    except Exception as exc:
        logger.exception("章节派生任务失败 job=%s，将保留以供恢复", job_id)
        status = "superseded" if "superseded" in str(exc).lower() else "unavailable"
        try:
            await asyncio.to_thread(finish, status, f"{type(exc).__name__}: 章后任务失败，详情请查看本地日志")
        except Exception:
            logger.exception("派生任务失败状态未取得 writer 回执 job=%s，启动恢复将重试", job_id)
        return status


async def recover_chapter_derivation_jobs(db, pipeline, knowledge_service, *, startup=False, limit=50):
    """Replay pending jobs after the writer is ready; models remain injectable.

    Only startup resets interrupted running jobs.  Normal retry never steals a
    currently executing claim.  Older revisions are marked superseded.
    """
    if startup:
        await asyncio.to_thread(db.execute, "UPDATE chapter_derivation_jobs SET status='pending' WHERE status='running'")
        await asyncio.to_thread(db.execute, "UPDATE chapter_derivation_jobs SET status='pending' WHERE status='completed' AND cascade_status='running'")
        await asyncio.to_thread(db.commit)
    jobs = await asyncio.to_thread(
        db.fetch_all,
        "SELECT job_id FROM chapter_derivation_jobs WHERE status IN ('pending','unavailable') OR "
        "(status='completed' AND cascade_status='unavailable') ORDER BY created_at LIMIT ?",
        (max(1, min(500, limit)),),
    )
    outcomes = []
    for job in jobs:
        outcomes.append(await run_chapter_derivation_job(db, job["job_id"], pipeline, knowledge_service))
    return outcomes
