"""托管连写：自动规划大纲 + 按章流式生成 + 可选落库，上下文由 ContextBuilder 维护。"""
from __future__ import annotations

import asyncio
import logging
import json
import uuid
from typing import Any, AsyncIterator, Dict, Optional, TYPE_CHECKING

from application.core.services.chapter_service import ChapterService
from application.core.services.novel_service import NovelService
from application.workflows.auto_novel_generation_workflow import AutoNovelGenerationWorkflow
if TYPE_CHECKING:
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline

logger = logging.getLogger(__name__)


class HostedWriteService:
    """多章连续托管写作（单连接 SSE 推送全程事件）。"""

    def __init__(
        self,
        workflow: AutoNovelGenerationWorkflow,
        chapter_service: ChapterService,
        novel_service: Optional[NovelService] = None,
        chapter_aftermath_pipeline: Optional["ChapterAftermathPipeline"] = None,
    ):
        self._workflow = workflow
        self._chapter = chapter_service
        self._novel = novel_service
        self._aftermath = chapter_aftermath_pipeline

    def _schedule_chapter_aftermath(self, db, job_id: str) -> None:
        """与 HTTP 保存同源：叙事/向量、文风、KG（不阻塞 SSE）；三元组与伏笔在叙事同步单次 LLM 中落库。"""
        if not self._aftermath:
            return

        async def _run() -> None:
            try:
                from application.core.services.chapter_derivation_jobs import run_chapter_derivation_job
                await run_chapter_derivation_job(db, job_id, self._aftermath, None)
            except Exception as e:
                logger.warning(
                    "托管章后管线失败 job=%s: %s", job_id, e
                )

        try:
            asyncio.create_task(_run())
        except Exception as e:
            logger.warning("托管章后管线未调度: %s", e)

    def _freeze_version(self, novel_id, chapter_number):
        from application.core.services.chapter_version_service import ChapterVersionService
        from infrastructure.persistence.database.connection import DatabaseConnection
        db = getattr(self._chapter.chapter_repository, "db", None)
        if not isinstance(db, DatabaseConnection):
            raise RuntimeError("托管保存需要可确认提交的版本仓储")
        if not db.fetch_one("SELECT id FROM novels WHERE id=?", (novel_id,)):
            raise LookupError("小说不存在")
        row = db.fetch_one("SELECT id FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
        if row:
            return db, ChapterVersionService(db).read(row["id"])
        history = db.fetch_one("SELECT COALESCE(MAX(revision),-1) AS revision FROM content_versions WHERE novel_id=? AND chapter_number=?", (novel_id, chapter_number))
        return db, {"absent": True, "history_revision": history["revision"]}

    def _save_frozen_version(self, db, novel_id, chapter_number, base, content, proof=None):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("生成正文为空，拒绝提交")
        from application.core.services.chapter_version_service import ChapterVersionService, RevisionConflictError, content_hash
        from application.ai_invocation.prose_execution_evidence import prose_receipt_write_fence
        condition, receipt_params = prose_receipt_write_fence(
            db, novel_id=novel_id, chapter_number=chapter_number, content=content, proof=proof)
        versions = ChapterVersionService(db)
        job = {"job_id": str(uuid.uuid4()), "payload": {"content": content, "micro_beats": None}}
        if not base.get("absent"):
            saved = versions.save_if_revision(base["id"], base["revision"], content,
                source="hosted", expected_hash=base["content_hash"], pending_job=job,
                _write_condition=(condition, receipt_params))
        else:
            chapter_id = f"chapter-{novel_id}-{chapter_number}"
            with db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO chapters(id,novel_id,number,title,content,word_count,status,version_source) "
                    "SELECT ?,?,?,?,?,?,'draft','hosted' WHERE NOT EXISTS(SELECT 1 FROM chapters WHERE novel_id=? AND number=?) "
                    "AND COALESCE((SELECT MAX(revision) FROM content_versions WHERE novel_id=? AND chapter_number=?),-1)=? "
                    "AND (" + condition + ")",
                    (chapter_id, novel_id, chapter_number, f"第{chapter_number}章", content, len(content),
                     novel_id, chapter_number, novel_id, chapter_number, base["history_revision"]) + receipt_params,
                )
                conn.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
                    "SELECT ?,id,novel_id,number,revision,? FROM chapters WHERE id=? AND changes()=1",
                    (job["job_id"], json.dumps(job["payload"], ensure_ascii=False), chapter_id))
                # Preserve add_chapter's structure projection in the same
                # successful CAS transaction. Never save the manuscript again
                # through add_chapter after this commit.
                conn.execute(
                    "INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,order_index,content,word_count,status) "
                    "SELECT c.id,c.novel_id,a.id,'chapter',c.number,c.title,"
                    "COALESCE((SELECT MAX(order_index)+1 FROM story_nodes WHERE novel_id=c.novel_id),0),"
                    "c.content,c.word_count,c.status FROM chapters c JOIN story_nodes a ON a.id=("
                    "SELECT id FROM story_nodes WHERE novel_id=c.novel_id AND node_type='act' "
                    "ORDER BY CASE WHEN ? BETWEEN chapter_start AND chapter_end THEN 0 ELSE 1 END,number DESC,id LIMIT 1) "
                    "WHERE c.id=? AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?) "
                    "AND NOT EXISTS(SELECT 1 FROM story_nodes WHERE novel_id=c.novel_id AND node_type='chapter' AND number=c.number)",
                    (chapter_number, chapter_id, job["job_id"]),
                )
                conn.execute(
                    "UPDATE story_nodes SET content=(SELECT content FROM chapters WHERE id=?),"
                    "word_count=(SELECT word_count FROM chapters WHERE id=?),updated_at=CURRENT_TIMESTAMP "
                    "WHERE novel_id=? AND node_type='chapter' AND number=? "
                    "AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                    (chapter_id, chapter_id, novel_id, chapter_number, job["job_id"]),
                )
                conn.execute(
                    "UPDATE story_nodes SET chapter_start=(SELECT MIN(number) FROM story_nodes c WHERE c.parent_id=story_nodes.id AND c.node_type='chapter'),"
                    "chapter_end=(SELECT MAX(number) FROM story_nodes c WHERE c.parent_id=story_nodes.id AND c.node_type='chapter'),"
                    "chapter_count=(SELECT COUNT(*) FROM story_nodes c WHERE c.parent_id=story_nodes.id AND c.node_type='chapter'),updated_at=CURRENT_TIMESTAMP "
                    "WHERE novel_id=? AND node_type='act' AND id IN(SELECT parent_id FROM story_nodes WHERE novel_id=? AND node_type='chapter' AND number=?) "
                    "AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                    (novel_id, novel_id, chapter_number, job["job_id"]),
                )
                conn.execute("UPDATE novels SET updated_at=CURRENT_TIMESTAMP WHERE id=? AND EXISTS(SELECT 1 FROM chapter_derivation_jobs WHERE job_id=?)",
                             (novel_id, job["job_id"]))
                receipt = conn.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,))
            if cursor.rowcount != 1:
                current = db.fetch_one("SELECT * FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
                raise RevisionConflictError(chapter_id, None, versions._snapshot(current), content)
            saved = versions._snapshot(receipt.fetchone())
            saved.update(derived_status="deferred", derivation_job_id=job["job_id"])
        if saved["content_hash"] != content_hash(content):
            raise RuntimeError("托管正文提交回执不匹配")
        return saved

    def _fallback_outline(self, novel_id: str, chapter_number: int) -> str:
        dto = self._chapter.get_chapter_by_novel_and_number(novel_id, chapter_number)
        title = dto.title if dto else f"第{chapter_number}章"
        return (
            f"【托管】{title}\n\n"
            "承接已有正文与设定，推进本章情节与人物；保持人称、时态与全书一致。"
        )

    async def stream_hosted_write(
        self,
        novel_id: str,
        from_chapter: int,
        to_chapter: int,
        auto_save: bool = True,
        auto_outline: bool = True,
    ) -> AsyncIterator[Dict[str, Any]]:
        """按章节区间连续生成；每章先大纲（LLM 或模板），再复用 generate_chapter_stream。

        事件在单章事件上增加 ``chapter``；并可能发出 ``session`` / ``chapter_start`` /
        ``outline`` / ``saved``。
        """
        logger.info(f"========================================")
        logger.info(f"开始托管连写: 小说={novel_id}, 章节范围={from_chapter}-{to_chapter}")
        logger.info(f"配置: auto_save={auto_save}, auto_outline={auto_outline}")
        logger.info(f"========================================")

        if from_chapter < 1 or to_chapter < 1 or to_chapter < from_chapter:
            logger.error(f"无效的章节范围: {from_chapter}-{to_chapter}")
            yield {"type": "error", "message": "invalid chapter range"}
            return

        total = to_chapter - from_chapter + 1
        logger.info(f"总计需要生成 {total} 个章节")

        yield {
            "type": "session",
            "novel_id": novel_id,
            "from_chapter": from_chapter,
            "to_chapter": to_chapter,
            "total": total,
        }

        for index, n in enumerate(range(from_chapter, to_chapter + 1), start=1):
            db = base = None
            if auto_save:
                try:
                    db, base = await asyncio.to_thread(self._freeze_version, novel_id, n)
                except Exception as exc:
                    yield {"type": "error", "chapter": n, "message": str(exc), "status": "failed"}
                    return
            logger.info(f"----------------------------------------")
            logger.info(f"开始处理章节 {n} ({index}/{total})")
            logger.info(f"----------------------------------------")

            yield {"type": "chapter_start", "chapter": n, "index": index, "total": total}

            if auto_outline:
                try:
                    logger.info(f"  → 使用 LLM 生成章节 {n} 的大纲")
                    outline = await self._workflow.suggest_outline(novel_id, n)
                    logger.info(f"  大纲生成成功: {len(outline)} 字符")
                except Exception as e:
                    logger.warning(f"  × 大纲生成失败: {e}, 使用默认模板")
                    outline = self._fallback_outline(novel_id, n)
            else:
                logger.info(f"  → 使用默认大纲模板")
                outline = self._fallback_outline(novel_id, n)

            yield {"type": "outline", "chapter": n, "text": outline}

            generated = False
            async for ev in self._workflow.generate_chapter_stream(novel_id, n, outline, enable_beats=True):
                merged: Dict[str, Any] = dict(ev)
                merged["chapter"] = n
                yield merged

                if ev.get("type") == "done" and auto_save:
                    if generated:
                        yield {"type": "error", "chapter": n, "message": "重复生成终态", "status": "failed"}
                        return
                    generated = True
                    content = ev.get("content") or ""
                    logger.info(f"  → 尝试保存章节 {n} ({len(content)} 字符)")
                    try:
                        saved = await asyncio.to_thread(self._save_frozen_version, db, novel_id, n, base, content,
                                                       ev.get("prose_execution_evidence"))
                        self._schedule_chapter_aftermath(db, saved["derivation_job_id"])
                        yield {"type": "saved", "chapter": n, "ok": True, "status": "committed",
                               "revision": saved["revision"], "content_hash": saved["content_hash"],
                               "derived_status": saved["derived_status"], "job_id": saved["derivation_job_id"]}
                    except Exception as ex:
                        logger.error(f"  × 保存章节 {n} 时发生异常: {type(ex).__name__}: {ex}")
                        yield {
                            "type": "saved",
                            "chapter": n,
                            "ok": False,
                            "message": str(ex),
                            "content": content,
                        }
                        yield {"type": "error", "chapter": n, "status": "failed", "message": str(ex)}
                        return

                if ev.get("type") == "error":
                    logger.error(f"  × 章节 {n} 生成失败，终止托管连写")
                    return
                if ev.get("type") == "done":
                    generated = True

            if not generated:
                yield {"type": "error", "chapter": n, "status": "failed", "message": "生成流中断，未收到终态"}
                return

        logger.info(f"========================================")
        logger.info(f"托管连写完成: 小说={novel_id}, 共生成 {total} 个章节")
        logger.info(f"========================================")
        yield {"type": "session_done", "novel_id": novel_id}
