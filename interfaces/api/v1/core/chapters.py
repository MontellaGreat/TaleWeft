"""Chapter API 路由"""
import logging
import asyncio
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Header, Response, Query
from pydantic import BaseModel, Field

from application.core.services.chapter_service import ChapterService
from application.core.services.novel_service import NovelService
from application.core.dtos.chapter_dto import ChapterDTO
from application.core.dtos.novel_dto import NovelDTO
from application.audit.dtos.chapter_review_dto import ChapterReviewDTO
from application.audit.services.chapter_ai_review_service import (
    ChapterAIReviewContractError,
    ChapterAIReviewService,
)
from application.core.dtos.chapter_structure_dto import ChapterStructureDTO
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.manuscript.reindex_job import reindex_chapter_entity_mentions
from interfaces.api.v1.engine.checkpoint_routes import GuardrailCheckResponse
from interfaces.api.dependencies import (
    get_chapter_service,
    get_novel_service,
    get_chapter_aftermath_pipeline,
    get_chapter_repository,
    get_knowledge_service,
    get_chapter_ai_review_service,
)
from application.world.services.knowledge_service import KnowledgeService
from infrastructure.persistence.database.chapter_draft_repository import ChapterDraftRepository
from application.paths import get_db_path
from domain.shared.exceptions import EntityNotFoundError
logger = logging.getLogger(__name__)


def _get_draft_repo() -> ChapterDraftRepository:
    from infrastructure.persistence.database.connection import DatabaseConnection
    db_path = get_db_path()
    return ChapterDraftRepository(DatabaseConnection(db_path))


async def _run_chapter_aftermath(
    novel_id: str,
    chapter_number: int,
    content: str,
    pipeline: ChapterAftermathPipeline,
    chapter_micro_beats: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """与托管/守护进程同源的章后管线（叙事/向量、文风、KG；三元组与伏笔单次 LLM）。"""
    await pipeline.run_after_chapter_saved(
        novel_id,
        chapter_number,
        content,
        chapter_micro_beats=chapter_micro_beats,
    )


router = APIRouter(tags=["chapters"])


# Request Models
class ChapterMicroBeatPayload(BaseModel):
    """写作指挥器微观节拍（与 chapter_summaries.micro_beats JSON 一致）"""
    description: str = Field(..., min_length=1)
    target_words: int = Field(default=0, ge=0)
    focus: str = Field(default="pacing")
    location_id: str = Field(default="")
    active_action: str = Field(default="")
    emotion_gap: str = Field(default="")
    forbidden_drift: str = Field(default="")


class ChapterMicroBeatsRequest(BaseModel):
    micro_beats: List[ChapterMicroBeatPayload] = Field(default_factory=list)


class UpdateChapterContentRequest(BaseModel):
    """更新章节内容请求"""
    content: str = Field(..., min_length=0, max_length=100000, description="章节内容")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200, pattern=r".*\S.*", description="可选章节标题，与正文按同一版本保存")
    expected_revision: Optional[int] = Field(default=None, ge=0)
    expected_hash: Optional[str] = None
    save_request_id: Optional[str] = Field(default=None, min_length=16, max_length=96, pattern=r'^[A-Za-z0-9_-]+$')
    micro_beats: Optional[List[ChapterMicroBeatPayload]] = Field(
        None,
        description="可选：本章指挥器节拍快照；落库后侧栏「微观」以知识库为准",
    )


class SaveChapterReviewRequest(BaseModel):
    """保存章节审阅请求"""
    status: Literal["draft", "reviewed", "approved"] = Field(..., description="审阅状态")
    memo: str = Field(default="", description="审阅备注")


class ChapterReviewResponse(BaseModel):
    """章节审阅响应"""
    status: str
    memo: str
    created_at: str
    updated_at: str


class ChapterAIReviewRequest(BaseModel):
    """AI 审阅请求"""
    save: bool = Field(default=False, description="是否保存到章节审阅记录")


class ChapterAIReviewResponse(BaseModel):
    """AI 审阅响应"""
    ok: bool
    status: str
    memo: str
    saved: bool
    score: int
    suggestions: List[str] = Field(default_factory=list)


class ChapterStructureResponse(BaseModel):
    """章节结构响应"""
    word_count: int
    paragraph_count: int
    dialogue_ratio: float
    scene_count: int
    pacing: str


class CreateChapterRequest(BaseModel):
    """创建章节请求"""
    chapter_id: str = Field(..., description="章节 ID")
    number: int = Field(..., gt=0, description="章节编号")
    title: str = Field(..., min_length=1, max_length=200, description="章节标题")
    content: str = Field(..., min_length=1, description="章节内容")


class EnsureChapterRequest(BaseModel):
    """确保章节存在请求（可选 title，不传则用「第N章」）"""
    title: str = Field(default="", max_length=200, description="章节标题（可选）")


# Routes
@router.get("/{novel_id}/chapters", response_model=List[ChapterDTO])
async def list_chapters(
    novel_id: str,
    service: ChapterService = Depends(get_chapter_service)
):
    """列出小说的所有章节

    Args:
        novel_id: 小说 ID
        service: Chapter 服务

    Returns:
        章节 DTO 列表
    """
    return service.list_chapters_by_novel(novel_id)


@router.post("/{novel_id}/chapters", response_model=NovelDTO, status_code=201)
async def create_chapter(
    novel_id: str,
    request: CreateChapterRequest,
    novel_service: NovelService = Depends(get_novel_service)
):
    """创建章节

    Args:
        novel_id: 小说 ID
        request: 创建章节请求
        novel_service: Novel 服务

    Returns:
        更新后的小说 DTO
    """
    try:
        return novel_service.add_chapter(
            novel_id=novel_id,
            chapter_id=request.chapter_id,
            number=request.number,
            title=request.title,
            content=request.content
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{novel_id}/chapters/{chapter_number}", response_model=ChapterDTO)
async def get_chapter(
    novel_id: str,
    response: Response,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service)
):
    """获取章节详情

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        service: Chapter 服务

    Returns:
        章节 DTO

    Raises:
        HTTPException: 如果章节不存在
    """
    chapter = service.get_chapter_by_novel_and_number(novel_id, chapter_number)
    if chapter is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chapter not found: {novel_id}/chapter-{chapter_number}"
        )
    response.headers["ETag"] = chapter.etag
    return chapter


@router.get(
    "/{novel_id}/chapters/{chapter_number}/guardrail-snapshot",
    response_model=Optional[GuardrailCheckResponse],
)
@router.get(
    "/{novel_id}/chapters/{chapter_number}/guardrail-snapshot/",
    response_model=Optional[GuardrailCheckResponse],
    include_in_schema=False,
)
async def get_guardrail_snapshot(
    novel_id: str,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
):
    """最近一次保存后自动护栏（建议模式）的快照。

    尚无快照时返回 HTTP 200 + JSON ``null``（避免客户端轮询刷 404 日志）。
    快照在章节 PUT 保存并由章后管线写入 ``chapter_guardrail_snapshots`` 后可用。
    """
    from infrastructure.persistence.database.chapter_guardrail_snapshot_repository import (
        ChapterGuardrailSnapshotRepository,
    )
    from infrastructure.persistence.database.connection import get_database

    repo = ChapterGuardrailSnapshotRepository(get_database())
    snap = repo.get(novel_id, chapter_number)
    if not snap:
        return None
    return GuardrailCheckResponse.model_validate(snap)


@router.post("/{novel_id}/chapters/{chapter_number}/ensure", response_model=ChapterDTO)
async def ensure_chapter(
    novel_id: str,
    request: EnsureChapterRequest,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service)
):
    """确保章节在正文库中存在；若不存在则创建空白记录（不校验章节号连续性）。

    适用于结构树手动添加章节节点后、用户点击想直接开始写作的场景。
    """
    return service.ensure_chapter(novel_id, chapter_number, request.title)


@router.put("/{novel_id}/chapters/{chapter_number}/micro-beats")
async def upsert_chapter_micro_beats(
    novel_id: str,
    request: ChapterMicroBeatsRequest,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
):
    """将指挥器微观节拍写入 chapter_summaries（不触发章后叙事 LLM）。"""
    beats = [b.model_dump() for b in request.micro_beats]
    knowledge_service.patch_chapter_micro_beats(novel_id, chapter_number, beats)
    return {"ok": True, "chapter_number": chapter_number, "count": len(beats)}


@router.put("/{novel_id}/chapters/{chapter_number}", response_model=ChapterDTO)
async def update_chapter(
    novel_id: str,
    request: UpdateChapterContentRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service),
    pipeline: ChapterAftermathPipeline = Depends(get_chapter_aftermath_pipeline),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
):
    """更新章节内容，保存成功后后台执行统一章后管线（见 ChapterAftermathPipeline）。"""
    return await _save_versioned_chapter(
        novel_id, request, background_tasks, response, if_match, chapter_number,
        service, pipeline, knowledge_service,
    )


async def _save_versioned_chapter(
    novel_id, request, background_tasks, response, if_match, chapter_number,
    service, pipeline, knowledge_service, *, source="manual", restore_audit=None,
):
    from application.core.services.chapter_version_service import ChapterVersionService, RevisionConflictError
    from application.core.services.chapter_derivation_jobs import run_chapter_derivation_job
    from application.core.services.chapter_save_receipts import ChapterSaveReceipts, SaveReceiptConflict
    from application.engine.services.persistence_queue import PersistenceCommitTimeout, PersistenceCommitUnconfirmed
    versions = ChapterVersionService(service.chapter_repository.db)
    receipts = ChapterSaveReceipts(versions.db)
    request_id = request.save_request_id or uuid.uuid4().hex
    replayed = False
    beats = [b.model_dump() for b in request.micro_beats] if request.micro_beats else None
    job = {"job_id": str(uuid.uuid4()), "payload": {"content": request.content, "micro_beats": beats, "reconcile_following": True}}

    def _fail_receipt_sync(error_code):
        """Mark a failed receipt and commit it on the same worker connection.

        The direct-write compatibility mode gives each worker thread its own
        SQLite connection.  Calling ``fail`` from the async event-loop thread
        after a worker raised can therefore leave the update uncommitted (or
        race with the worker's open transaction).  Keeping the fail + commit
        pair together makes the receipt durable while preserving the queued
        writer behaviour: ``DatabaseConnection.commit`` is a no-op outside
        direct/writer threads and the queued ``execute`` already waits for its
        commit.
        """
        receipts.fail(request_id, error_code)
        versions.db.commit()

    def _save_sync():
        nonlocal replayed
        created, receipt = receipts.begin(request_id, novel_id, chapter_number, {
            'content': request.content, 'expected_revision': request.expected_revision,
            'expected_hash': request.expected_hash, 'if_match': if_match, 'beats': beats, 'source': source,
            'restore_audit': restore_audit, 'title': request.title,
        })
        # ``ChapterSaveReceipts.begin`` intentionally keeps its historical
        # standalone semantics and does not commit.  This save flow may then
        # leave ``_save_sync`` with a pending INSERT before it raises a
        # revision conflict.  Commit here, while still on the same worker
        # thread, so the outer async error handler can safely mark the receipt
        # failed from another thread without inheriting a SQLite write lock.
        versions.db.commit()
        if not created:
            result = receipts.get(request_id, novel_id, chapter_number)
            if result['status'] == 'committed':
                replayed = True
                return result['chapter'], result['chapter']
            raise HTTPException(503 if result['status'] == 'failed' else 202, detail={
                'code': 'chapter_save_' + result['status'], 'save_request_id': request_id,
                'message': '此保存请求尚未确认或已失败，请查询提交回执并保留本地正文',
            })
        existing = versions.db.fetch_one("SELECT id FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
        if not existing:
            raise LookupError("章节不存在")
        current = versions.read(existing["id"])
        expected = request.expected_revision
        if if_match:
            if if_match != current["etag"] or (expected is not None and expected != current["revision"]):
                raise RevisionConflictError(existing["id"], expected, current, request.content)
            expected = current["revision"]
        if expected is None:
            raise HTTPException(428, "保存需要 expected_revision 或 If-Match，请先刷新章节")
        saved = versions.save_if_revision(existing["id"], expected, request.content,
                                          expected_hash=request.expected_hash, source=source,
                                          changes={"title": request.title.strip()} if request.title is not None else None,
                                          pending_job=job, restore_audit=restore_audit,
                                          save_receipt={'request_id': request_id, 'payload_hash': receipt['payload_hash']})
        return saved, current
    try:
        saved, previous = await asyncio.to_thread(_save_sync)
    except RevisionConflictError as exc:
        await asyncio.to_thread(_fail_receipt_sync, 'chapter_revision_conflict')
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except SaveReceiptConflict as exc:
        raise HTTPException(409, detail={'code': 'save_request_payload_conflict', 'message': str(exc)}) from exc
    except LookupError as exc:
        await asyncio.to_thread(_fail_receipt_sync, 'chapter_not_found')
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException as exc:
        if exc.status_code == 428:
            await asyncio.to_thread(_fail_receipt_sync, 'precondition_required')
        raise
    except (PersistenceCommitTimeout, PersistenceCommitUnconfirmed) as exc:
        raise HTTPException(503, detail={'code': 'chapter_save_unconfirmed', 'save_request_id': request_id,
                                        'message': '保存提交超时，可能晚到提交；请查询此请求回执'}) from exc
    except Exception as exc:
        logger.exception("章节保存未确认")
        try:
            await asyncio.to_thread(_fail_receipt_sync, 'writer_failed')
        except Exception:
            logger.exception('保存失败回执未确认')
        raise HTTPException(status_code=503, detail={'code': 'chapter_save_unconfirmed', 'save_request_id': request_id,
                                                  'message': '章节保存未确认，请查询回执并保留本地正文'}) from exc
    chapter = ChapterDTO(
        id=previous["id"], novel_id=novel_id, number=chapter_number,
        title=saved["title"], content=saved["content"], word_count=len(saved["content"]), status=saved["status"],
        generation_hint=previous.get("generation_hint", ""), revision=saved["revision"],
        content_hash=saved["content_hash"], etag=saved["etag"],
        created_at=str(previous.get("created_at") or ""), updated_at=str(saved.get("updated_at") or saved.get("created_at") or ""),
        derived_status=saved["derived_status"], derivation_job_id=saved["derivation_job_id"],
        save_request_id=request_id,
    )
    response.headers["ETag"] = saved["etag"]

    if not replayed:
        background_tasks.add_task(
            run_chapter_derivation_job, versions.db, job["job_id"], pipeline, knowledge_service,
        )
    return chapter


@router.get('/{novel_id}/chapters/{chapter_number}/save-requests/{request_id}')
async def get_chapter_save_receipt(novel_id: str, chapter_number: int, request_id: str,
                                   service: ChapterService = Depends(get_chapter_service)):
    from application.core.services.chapter_save_receipts import ChapterSaveReceipts
    result = await asyncio.to_thread(ChapterSaveReceipts(service.chapter_repository.db).get,
                                     request_id, novel_id, chapter_number)
    if result is None:
        raise HTTPException(404, detail='未找到此章节的保存请求；不得据此推断未知请求已回滚')
    return result


@router.get("/{novel_id}/chapters/{chapter_number}/versions")
async def list_chapter_versions(
    novel_id: str, chapter_number: int, service: ChapterService = Depends(get_chapter_service),
    limit: int = Query(default=50, ge=1, le=200), before_revision: Optional[int] = Query(default=None, ge=0),
):
    from application.core.services.chapter_version_service import ChapterVersionService
    versions = ChapterVersionService(service.chapter_repository.db)
    row = await asyncio.to_thread(versions.db.fetch_one, "SELECT id FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
    if not row:
        raise HTTPException(404, "章节不存在")
    return await asyncio.to_thread(versions.list_versions, row["id"], limit, before_revision)


@router.get("/{novel_id}/chapters/{chapter_number}/versions/{revision}/diff")
async def diff_chapter_version(
    novel_id: str, chapter_number: int, revision: int,
    against_revision: Optional[int] = None, service: ChapterService = Depends(get_chapter_service),
):
    import difflib
    db = service.chapter_repository.db
    chapter = await asyncio.to_thread(db.fetch_one, "SELECT id,revision,content FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
    if not chapter:
        raise HTTPException(404, "章节不存在")
    target = await asyncio.to_thread(db.fetch_one, "SELECT content FROM content_versions WHERE chapter_id=? AND revision=?", (chapter["id"], revision))
    if not target:
        raise HTTPException(404, "历史版本不存在")
    comparison = chapter
    if against_revision is not None:
        comparison = await asyncio.to_thread(db.fetch_one, "SELECT revision,content FROM content_versions WHERE chapter_id=? AND revision=?", (chapter["id"], against_revision))
        if not comparison:
            raise HTTPException(404, "比较版本不存在")
    diff = "".join(difflib.unified_diff(
        str(target["content"]).splitlines(keepends=True), str(comparison["content"]).splitlines(keepends=True),
        fromfile=f"revision-{revision}", tofile=f"revision-{comparison['revision']}",
    ))
    return {"revision": revision, "against_revision": comparison["revision"], "diff": diff}


class RestoreChapterVersionRequest(BaseModel):
    expected_revision: Optional[int] = Field(default=None, ge=0)
    expected_hash: Optional[str] = None


@router.post("/{novel_id}/chapters/{chapter_number}/versions/{revision}/restore", response_model=ChapterDTO)
async def restore_chapter_version(
    novel_id: str, chapter_number: int, revision: int, request: RestoreChapterVersionRequest,
    background_tasks: BackgroundTasks, response: Response,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    service: ChapterService = Depends(get_chapter_service),
    pipeline: ChapterAftermathPipeline = Depends(get_chapter_aftermath_pipeline),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
):
    db = service.chapter_repository.db
    target = await asyncio.to_thread(db.fetch_one,
        "SELECT v.content,v.title FROM content_versions v JOIN chapters c ON c.id=v.chapter_id "
        "WHERE c.novel_id=? AND c.number=? AND v.revision=?", (novel_id, chapter_number, revision))
    if not target:
        raise HTTPException(404, "章节或历史版本不存在")
    return await _save_versioned_chapter(
        novel_id, UpdateChapterContentRequest(content=target["content"], title=target['title'] or None, expected_revision=request.expected_revision, expected_hash=request.expected_hash),
        background_tasks, response, if_match, chapter_number, service, pipeline, knowledge_service,
        source=f"restore:{revision}",
    )


@router.get("/{novel_id}/chapters/{chapter_number}/derivation-jobs")
async def list_chapter_derivation_jobs(novel_id: str, chapter_number: int, service: ChapterService = Depends(get_chapter_service)):
    return await asyncio.to_thread(service.chapter_repository.db.fetch_all,
        "SELECT job_id,revision,status,attempts,last_error,cascade_status,cascade_error,created_at,updated_at FROM chapter_derivation_jobs "
        "WHERE novel_id=? AND chapter_number=? ORDER BY created_at DESC,revision DESC LIMIT 50", (novel_id, chapter_number))


class ChapterDerivationActionRequest(BaseModel):
    action: Literal['retry_aftermath', 'rebuild_indexes']


@router.post('/{novel_id}/chapters/{chapter_number}/derivation-jobs', status_code=202)
async def request_chapter_derivation(
    novel_id: str, chapter_number: int, request: ChapterDerivationActionRequest,
    background_tasks: BackgroundTasks,
    service: ChapterService = Depends(get_chapter_service),
    pipeline: ChapterAftermathPipeline = Depends(get_chapter_aftermath_pipeline),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
):
    from application.core.services.chapter_derivation_jobs import run_chapter_derivation_job
    from application.core.services.chapter_version_service import content_hash
    import json
    db = service.chapter_repository.db

    def enqueue():
        row = db.fetch_one('SELECT id,revision,content FROM chapters WHERE novel_id=? AND number=?', (novel_id, chapter_number))
        if not row:
            raise EntityNotFoundError('章节不存在')
        existing = db.fetch_one('SELECT status,payload_json,cascade_status FROM chapter_derivation_jobs WHERE chapter_id=? AND revision=?',
                                (row['id'], row['revision']))
        if (request.action == 'rebuild_indexes' and existing
                and json.loads(existing['payload_json']).get('operation') != 'rebuild_indexes'
                and (existing['status'] in ('unavailable', 'superseded') or existing['cascade_status'] == 'unavailable')):
            raise ValueError('请先重跑章后关联分析；重建索引不能替代尚未完成的记忆与关联工作流')
        job_id = uuid.uuid4().hex
        payload = json.dumps({'content': row['content'] or '', 'operation': request.action,
                              'reconcile_following': request.action == 'retry_aftermath'}, ensure_ascii=False)
        with db.transaction() as conn:
            conn.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
                         "SELECT ?,id,novel_id,number,revision,? FROM chapters WHERE id=? AND revision=? AND content IS ? "
                         "ON CONFLICT(chapter_id,revision) DO UPDATE SET payload_json=excluded.payload_json,status='pending',last_error='',updated_at=CURRENT_TIMESTAMP "
                         "WHERE chapter_derivation_jobs.status NOT IN ('pending','running') AND "
                         "(?<>'rebuild_indexes' OR json_extract(chapter_derivation_jobs.payload_json,'$.operation')='rebuild_indexes' OR "
                         "(chapter_derivation_jobs.status='completed' AND chapter_derivation_jobs.cascade_status IN ('completed','not_requested')))",
                         (job_id, payload, row['id'], row['revision'], row['content'], request.action))
            cursor = conn.execute('SELECT job_id,revision,status,attempts,last_error,cascade_status,cascade_error,created_at,updated_at FROM chapter_derivation_jobs WHERE chapter_id=? AND revision=?', (row['id'], row['revision']))
        result = cursor.fetchone()
        if result is None:
            raise RuntimeError('章节已更新，请刷新后重新安排索引任务')
        return dict(result)

    try:
        job = await asyncio.to_thread(enqueue)
    except EntityNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(409, '章节任务未安排，请刷新状态后重试') from exc
    background_tasks.add_task(run_chapter_derivation_job, db, job['job_id'], pipeline, knowledge_service)
    return job


class UpdateChapterHintRequest(BaseModel):
    """更新章节生成约束请求"""
    generation_hint: str = Field(default="", max_length=2000, description="用户手写的本章生成约束文本")


@router.patch("/{novel_id}/chapters/{chapter_number}/hint", response_model=ChapterDTO)
async def update_chapter_hint(
    novel_id: str,
    request: UpdateChapterHintRequest,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service),
):
    """更新章节生成约束文本，直接注入 AI 上下文（不触发章后管线）。"""
    try:
        return service.update_chapter_generation_hint(novel_id, chapter_number, request.generation_hint)
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{novel_id}/chapters/{chapter_number}/review", response_model=ChapterReviewResponse)
async def get_chapter_review(
    novel_id: str,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service)
):
    """获取章节审阅

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        service: Chapter 服务

    Returns:
        章节审阅信息

    Raises:
        HTTPException: 如果章节不存在
    """
    try:
        review = service.get_chapter_review(novel_id, chapter_number)
        return ChapterReviewResponse(
            status=review.status,
            memo=review.memo,
            created_at=review.created_at.isoformat(),
            updated_at=review.updated_at.isoformat()
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{novel_id}/chapters/{chapter_number}/review", response_model=ChapterReviewResponse)
async def save_chapter_review(
    novel_id: str,
    request: SaveChapterReviewRequest,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service)
):
    """保存章节审阅

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        request: 审阅请求
        service: Chapter 服务

    Returns:
        保存后的审阅信息

    Raises:
        HTTPException: 如果章节不存在
    """
    try:
        review = service.save_chapter_review(
            novel_id,
            chapter_number,
            request.status,
            request.memo
        )
        return ChapterReviewResponse(
            status=review.status,
            memo=review.memo,
            created_at=review.created_at.isoformat(),
            updated_at=review.updated_at.isoformat()
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{novel_id}/chapters/{chapter_number}/review-ai")
async def ai_review_chapter(
    novel_id: str,
    request: ChapterAIReviewRequest = ChapterAIReviewRequest(),
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service),
    ai_review_service: ChapterAIReviewService = Depends(get_chapter_ai_review_service),
):
    """AI 审阅章节

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        service: Chapter 服务

    Returns:
        AI 审阅结果

    Raises:
        HTTPException: 如果章节不存在或内容为空
    """
    try:
        # 获取章节
        chapter = service.get_chapter_by_novel_and_number(novel_id, chapter_number)
        if chapter is None:
            raise HTTPException(status_code=404, detail=f"Chapter not found: {novel_id}/chapter-{chapter_number}")

        # 检查内容是否为空
        if not chapter.content or not chapter.content.strip():
            raise HTTPException(status_code=400, detail="Chapter content is empty")

        result = await ai_review_service.review(
            chapter_number=chapter_number,
            chapter_title=chapter.title,
            chapter_content=chapter.content,
            chapter_outline="",
            generation_hint=getattr(chapter, "generation_hint", "") or "",
        )
        saved = False
        if request.save:
            service.save_chapter_review(
                novel_id,
                chapter_number,
                result.status,
                result.memo,
            )
            saved = True
        return ChapterAIReviewResponse(
            ok=True,
            status=result.status,
            memo=result.memo,
            saved=saved,
            score=result.score,
            suggestions=result.suggestions,
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ChapterAIReviewContractError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/{novel_id}/chapters/{chapter_number}/structure", response_model=ChapterStructureResponse)
async def get_chapter_structure(
    novel_id: str,
    chapter_number: int = Path(..., gt=0, description="章节编号"),
    service: ChapterService = Depends(get_chapter_service)
):
    """获取章节结构分析

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        service: Chapter 服务

    Returns:
        章节结构分析

    Raises:
        HTTPException: 如果章节不存在
    """
    try:
        structure = service.get_chapter_structure(novel_id, chapter_number)
        return ChapterStructureResponse(
            word_count=structure.word_count,
            paragraph_count=structure.paragraph_count,
            dialogue_ratio=structure.dialogue_ratio,
            scene_count=structure.scene_count,
            pacing=structure.pacing
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── 章节历史草稿（重新生成版本管理） ───────────────────────────────────

class ChapterDraftResponse(BaseModel):
    id: str
    novel_id: str
    chapter_id: str
    chapter_number: int
    content: str
    outline: str
    source: str
    word_count: int
    created_at: str


class ChapterDraftDiffResponse(BaseModel):
    draft_id: str
    against_revision: Optional[int] = None
    against_draft_id: Optional[str] = None
    diff: str


class RestoreChapterDraftRequest(BaseModel):
    expected_revision: Optional[int] = Field(default=None, ge=0)
    expected_hash: Optional[str] = None


class SaveDraftRequest(BaseModel):
    """保存历史草稿请求（调用方在触发重新生成前快照当前内容）"""
    source: str = Field(
        default="pre_regen",
        description="快照来源：pre_regen=重新生成前 | manual_save=手动 | auto_gen=首次生成",
    )


@router.post(
    "/{novel_id}/chapters/{chapter_number}/drafts",
    response_model=ChapterDraftResponse,
    status_code=201,
)
async def save_chapter_draft(
    novel_id: str,
    chapter_number: int = Path(..., gt=0),
    request: SaveDraftRequest = SaveDraftRequest(),
    chapter_service: ChapterService = Depends(get_chapter_service),
    draft_repo: ChapterDraftRepository = Depends(_get_draft_repo),
):
    """将当前章节内容快照为历史草稿（在触发重新生成之前调用）。

    章节不存在或内容为空时返回 404 / 422。
    """
    chapter = chapter_service.get_chapter_by_novel_and_number(novel_id, chapter_number)
    if chapter is None:
        raise HTTPException(status_code=404, detail=f"章节 {chapter_number} 不存在")
    if not chapter.content or not chapter.content.strip():
        raise HTTPException(status_code=422, detail="章节内容为空，无需保存草稿")

    record = draft_repo.save_draft(
        novel_id=novel_id,
        chapter_id=chapter.id,
        chapter_number=chapter_number,
        content=chapter.content,
        outline=(chapter_service.chapter_repository.db.fetch_one("SELECT outline FROM chapters WHERE id=?", (chapter.id,)) or {}).get("outline") or "",
        source=request.source,
    )
    return ChapterDraftResponse(
        id=record.id,
        novel_id=record.novel_id,
        chapter_id=record.chapter_id,
        chapter_number=record.chapter_number,
        content=record.content,
        outline=record.outline,
        source=record.source,
        word_count=record.word_count,
        created_at=record.created_at,
    )


@router.get(
    "/{novel_id}/chapters/{chapter_number}/drafts",
    response_model=List[ChapterDraftResponse],
)
async def list_chapter_drafts(
    novel_id: str,
    chapter_number: int = Path(..., gt=0),
    draft_repo: ChapterDraftRepository = Depends(_get_draft_repo),
):
    """列出章节的历史草稿（最新在前，最多 10 条）。"""
    records = draft_repo.list_drafts(novel_id, chapter_number)
    return [
        ChapterDraftResponse(
            id=r.id,
            novel_id=r.novel_id,
            chapter_id=r.chapter_id,
            chapter_number=r.chapter_number,
            content=r.content,
            outline=r.outline,
            source=r.source,
            word_count=r.word_count,
            created_at=r.created_at,
        )
        for r in records
    ]


async def _load_owned_draft(
    novel_id: str,
    chapter_number: int,
    draft_id: str,
    draft_repo: ChapterDraftRepository,
):
    """Load a legacy draft only when it belongs to the requested chapter."""
    draft = await asyncio.to_thread(draft_repo.get_draft, draft_id)
    if draft is None or draft.novel_id != novel_id or draft.chapter_number != chapter_number:
        raise HTTPException(status_code=404, detail="历史草稿不存在")
    return draft


@router.get(
    "/{novel_id}/chapters/{chapter_number}/drafts/{draft_id}/diff",
    response_model=ChapterDraftDiffResponse,
)
async def diff_chapter_draft(
    novel_id: str,
    chapter_number: int,
    draft_id: str,
    against_revision: Optional[int] = Query(default=None, ge=0),
    against_draft_id: Optional[str] = Query(default=None),
    chapter_service: ChapterService = Depends(get_chapter_service),
    draft_repo: ChapterDraftRepository = Depends(_get_draft_repo),
):
    """Compare an old chapter_drafts snapshot with the current or another snapshot."""
    import difflib

    draft = await _load_owned_draft(novel_id, chapter_number, draft_id, draft_repo)
    db = chapter_service.chapter_repository.db
    chapter = await asyncio.to_thread(
        db.fetch_one,
        "SELECT id,content,revision FROM chapters WHERE novel_id=? AND number=?",
        (novel_id, chapter_number),
    )
    if not chapter or chapter["id"] != draft.chapter_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    if against_revision is not None and against_draft_id is not None:
        raise HTTPException(status_code=400, detail="只能选择一个比较目标")
    comparison = None
    if against_draft_id is not None:
        comparison = await _load_owned_draft(novel_id, chapter_number, against_draft_id, draft_repo)
        if comparison.chapter_id != draft.chapter_id:
            raise HTTPException(status_code=404, detail="比较草稿不存在")
        comparison_content = comparison.content
    else:
        if against_revision is not None:
            version = await asyncio.to_thread(
                db.fetch_one,
                "SELECT content FROM content_versions v JOIN chapters c ON c.id=v.chapter_id "
                "WHERE c.novel_id=? AND c.number=? AND v.revision=?",
                (novel_id, chapter_number, against_revision),
            )
            if not version:
                raise HTTPException(status_code=404, detail="比较版本不存在")
            comparison_content = version["content"]
        else:
            against_revision = int(chapter["revision"] or 0)
            comparison_content = chapter["content"]
    diff = "".join(difflib.unified_diff(
        str(draft.content).splitlines(keepends=True),
        str(comparison_content).splitlines(keepends=True),
        fromfile=f"draft-{draft.id}",
        tofile=(f"draft-{comparison.id}" if comparison else f"revision-{against_revision}"),
    ))
    return ChapterDraftDiffResponse(
        draft_id=draft.id,
        against_revision=against_revision,
        against_draft_id=comparison.id if comparison else None,
        diff=diff,
    )


@router.post(
    "/{novel_id}/chapters/{chapter_number}/drafts/{draft_id}/restore",
    response_model=ChapterDTO,
)
async def restore_chapter_draft(
    novel_id: str,
    chapter_number: int,
    draft_id: str,
    request: RestoreChapterDraftRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    chapter_service: ChapterService = Depends(get_chapter_service),
    draft_repo: ChapterDraftRepository = Depends(_get_draft_repo),
    pipeline: ChapterAftermathPipeline = Depends(get_chapter_aftermath_pipeline),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
):
    """Restore a legacy snapshot as a new immutable content_versions revision."""
    draft = await _load_owned_draft(novel_id, chapter_number, draft_id, draft_repo)
    db = chapter_service.chapter_repository.db
    chapter = await asyncio.to_thread(
        db.fetch_one,
        "SELECT id FROM chapters WHERE novel_id=? AND number=?",
        (novel_id, chapter_number),
    )
    if not chapter or chapter["id"] != draft.chapter_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    return await _save_versioned_chapter(
        novel_id,
        UpdateChapterContentRequest(
            content=draft.content,
            expected_revision=request.expected_revision,
            expected_hash=request.expected_hash,
        ),
        background_tasks,
        response,
        if_match,
        chapter_number,
        chapter_service,
        pipeline,
        knowledge_service,
        source=f"restore:draft:{draft.id}",
        restore_audit={"draft_id": draft.id},
    )
