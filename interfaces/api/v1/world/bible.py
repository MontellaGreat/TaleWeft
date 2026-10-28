"""Bible API 路由"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Union
import logging
import json
import asyncio
from dataclasses import asdict, is_dataclass

from application.world.services.bible_service import BibleService
from application.world.services.auto_bible_generator import AutoBibleGenerator
from application.world.services.auto_knowledge_generator import AutoKnowledgeGenerator
from application.world.dtos.bible_dto import BibleDTO
from application.ai_invocation.dtos import InvocationPolicy, InvocationRequest
from application.ai_invocation.gateway import AIInvocationGateway
from application.ai_invocation.services import AdoptionCommitService, AdoptionService, AttemptService, InvocationSessionService
from application.ai_invocation.spec_service import InvocationSpecService
from application.ai_invocation.variable_hub import VariableResolver, VariableWrite
from application.world.services.bible_setup_continuation import register_bible_setup_continuations
from application.world.services.bible_setup_invocation import (
    BibleSetupPromptAssembler,
)
from application.onboarding.setup_stage_definitions import get_onboarding_stage_definition
from application.core.v1_length_tiers import strip_v1_structure_black_box_hint
from interfaces.api.dependencies import (
    get_bible_service,
    get_auto_bible_generator,
    get_auto_knowledge_generator
)
from interfaces.api.urls import bible_generation_status_url
from domain.shared.exceptions import EntityNotFoundError
from application.world.bible_generation_state import (
    clear_bible_generation_state,
    get_bible_generation_state,
    record_bible_generation_failure,
)
from application.world.worldbuilding_schema import WORLDBUILDING_DIMENSION_DEFS
from interfaces.api.v1.world.bible_runtime_settings import get_bible_runtime_settings
from application.onboarding.setup_edit_guard import SetupEditConflict, assert_setup_editable

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/bible", tags=["bible"])


class ManualCharacterEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict
    expected_character: dict


class ManualCharacterDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_character: dict


async def _run_character_derivations(db, job_ids):
    """Lazily initialize analysis after the author edit has really committed."""
    if not job_ids:
        return
    from interfaces.api.dependencies import get_chapter_aftermath_pipeline, get_knowledge_service
    from application.core.services.chapter_derivation_jobs import run_chapter_derivation_job
    try:
        pipeline = await asyncio.to_thread(get_chapter_aftermath_pipeline)
        knowledge = await asyncio.to_thread(get_knowledge_service)
    except Exception:
        logger.exception("角色编辑后的关联分析初始化失败")
        for job_id in job_ids:
            await asyncio.to_thread(db.execute,
                "UPDATE chapter_derivation_jobs SET status='unavailable',last_error='角色已保存，关联分析服务初始化失败，请重跑关联分析' WHERE job_id=? AND status='pending'",(job_id,))
        await asyncio.to_thread(db.commit)
        return
    for job_id in job_ids:
        await run_chapter_derivation_job(db,job_id,pipeline,knowledge)


def _mutate_character(novel_id, character_id, expected, changes=None, *, delete=False, db=None):
    from application.world.services.character_manual_mutation import CharacterManualMutation, CharacterEditConflict
    from infrastructure.persistence.database.connection import get_database
    db = db or get_database()
    mutation = CharacterManualMutation(db)
    try:
        if not delete:
            # Validate requested values with the existing public field contract;
            # omission remains omission, explicit empty values stay editable.
            validated = CharacterData.model_validate({**expected, **changes, 'id':character_id})
            changes = {key:validated.model_dump(mode='json')[key] for key in changes}
        info = mutation.delete(novel_id,character_id,expected) if delete else mutation.update(novel_id,character_id,changes,expected)
        dto = mutation.bible.get_bible_by_novel(novel_id)
    except (CharacterEditConflict, SetupEditConflict) as exc:
        raise HTTPException(409,detail={'code':'CHARACTER_EDIT_CONFLICT','reason':str(exc)}) from exc
    except LookupError as exc:
        raise HTTPException(404,detail=str(exc)) from exc
    except (ValueError,KeyError,TypeError) as exc:
        raise HTTPException(422,detail=str(exc)) from exc
    # CastGraph is a TTL read cache. Clear cached instances rather than return
    # a stale name/roster until its normal expiry after the database commit.
    from interfaces.api.dependencies import get_cast_service
    get_cast_service.cache_clear()
    return {**asdict(dto),'character_mutation':info},db


@router.patch('/novels/{novel_id}/bible/characters/{character_id}')
async def edit_character_profile(novel_id:str,character_id:str,request:ManualCharacterEditRequest,background_tasks:BackgroundTasks):
    result,db = await asyncio.to_thread(_mutate_character,novel_id,character_id,request.expected_character,request.changes)
    background_tasks.add_task(_run_character_derivations,db,result['character_mutation'].get('derivation_job_ids',[]))
    return result


@router.delete('/novels/{novel_id}/bible/characters/{character_id}')
async def delete_character_profile(novel_id:str,character_id:str,request:ManualCharacterDeleteRequest):
    result,_ = await asyncio.to_thread(_mutate_character,novel_id,character_id,request.expected_character,delete=True)
    return result


def _guard_setup_edit(novel_id: str, stage: str) -> None:
    try:
        assert_setup_editable(novel_id, stage)
    except SetupEditConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "SETUP_AUDIT_INCOMPLETE", "reason": str(exc)}) from exc


def _guard_setup_generation(novel_id: str, stage: str) -> None:
    for target in (("worldbuilding", "characters", "locations") if stage == "all" else (stage,)):
        _guard_setup_edit(novel_id, target)


def _section_changed(requested: list, current: list, *, preserve_none: bool = False) -> bool:
    """Compare the effective PUT, including optional character preservation."""
    def plain(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        return dict(value) if isinstance(value, dict) else vars(value)

    if len(requested) != len(current):
        return True
    for incoming, existing in zip(requested, current):
        new, old = plain(incoming), plain(existing)
        # BibleService preserves optional character fields when they are None;
        # absent DTO-only fields therefore do not create a false edit conflict.
        if any(value != old.get(key) for key, value in new.items() if value is not None or not preserve_none):
            return True
    return False


def _validate_generation_stage(stage: str) -> None:
    if stage not in {"all", "worldbuilding", "characters", "locations"}:
        raise HTTPException(status_code=422, detail="unsupported_bible_generation_stage")


def _sync_bible_variable_hub(novel_id: str, dto: BibleDTO) -> None:
    try:
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
    except Exception:
        return

    context_key = f"novel_id:{novel_id}"
    repo = SqliteVariableHubRepository(get_database())
    characters = [
        {
            "id": str(getattr(item, "id", "") or ""),
            "name": str(getattr(item, "name", "") or ""),
            "description": str(getattr(item, "description", "") or ""),
            "gender": str(getattr(item, "gender", "") or ""),
            "age": str(getattr(item, "age", "") or ""),
            "appearance": str(getattr(item, "appearance", "") or ""),
            "personality": str(getattr(item, "personality", "") or ""),
            "background": str(getattr(item, "background", "") or ""),
            "core_motivation": str(getattr(item, "core_motivation", "") or ""),
            "inner_lack": str(getattr(item, "inner_lack", "") or ""),
            "relationships": list(getattr(item, "relationships", []) or []),
            "public_profile": str(getattr(item, "public_profile", "") or ""),
            "hidden_profile": str(getattr(item, "hidden_profile", "") or ""),
            "reveal_chapter": getattr(item, "reveal_chapter", None),
            "mental_state": str(getattr(item, "mental_state", "") or "NORMAL"),
            "mental_state_reason": str(getattr(item, "mental_state_reason", "") or ""),
            "verbal_tic": str(getattr(item, "verbal_tic", "") or ""),
            "idle_behavior": str(getattr(item, "idle_behavior", "") or ""),
            "core_belief": str(getattr(item, "core_belief", "") or ""),
            "moral_taboos": list(getattr(item, "moral_taboos", []) or []),
            "voice_profile": dict(getattr(item, "voice_profile", {}) or {}),
            "active_wounds": list(getattr(item, "active_wounds", []) or []),
        }
        for item in (dto.characters or [])
        if getattr(item, "name", "")
    ]
    protagonist = characters[0] if characters else None
    locations = [
        {
            "id": str(getattr(item, "id", "") or ""),
            "name": str(getattr(item, "name", "") or ""),
            "description": str(getattr(item, "description", "") or ""),
            "type": str(getattr(item, "location_type", "") or ""),
            "location_type": str(getattr(item, "location_type", "") or ""),
            "parent_id": getattr(item, "parent_id", None),
        }
        for item in (dto.locations or [])
        if getattr(item, "name", "")
    ]
    style = str(getattr(dto, "style", "") or "").strip()

    writes = [
        (
            "characters.list",
            characters,
            "list",
            "角色列表",
            "characters",
        ),
        (
            "characters.protagonist",
            protagonist,
            "object",
            "主角",
            "characters",
        ),
        (
            "locations.list",
            locations,
            "list",
            "地点列表",
            "locations",
        ),
        (
            "worldbuilding.style",
            style,
            "string",
            "文风公约",
            "setup",
        ),
    ]
    for key, value, value_type, display_name, stage in writes:
        if value in (None, "", [], {}):
            continue
        repo.set_value(
            VariableWrite(
                key=key,
                value=value,
                context_key=context_key,
                source_trace_id="bible_manual_sync",
                source_node_key="bible_api",
                lineage={"source": "bible_api_manual_sync"},
                value_type=value_type,
                display_name=display_name,
                scope="global",
                stage=stage,
            )
        )


# Request Models
class CreateBibleRequest(BaseModel):
    """创建 Bible 请求"""
    bible_id: str = Field(..., description="Bible ID")
    novel_id: str = Field(..., description="小说 ID")


class AddCharacterRequest(BaseModel):
    """添加人物请求"""
    character_id: str = Field(..., description="人物 ID")
    name: str = Field(..., description="人物名称")
    description: str = Field(..., description="人物描述")


class AddWorldSettingRequest(BaseModel):
    """添加世界设定请求"""
    setting_id: str = Field(..., description="设定 ID")
    name: str = Field(..., description="设定名称")
    description: str = Field(..., description="设定描述")
    setting_type: str = Field(..., description="设定类型")


class AddLocationRequest(BaseModel):
    """添加地点请求"""
    location_id: str = Field(..., description="地点 ID")
    name: str = Field(..., description="地点名称")
    description: str = Field(..., description="地点描述")
    location_type: str = Field(..., description="地点类型")
    parent_id: Optional[str] = Field(default=None, description="父地点 id，根为 null")


class AddTimelineNoteRequest(BaseModel):
    """添加时间线笔记请求"""
    note_id: str = Field(..., description="笔记 ID")
    event: str = Field(..., description="事件")
    time_point: str = Field(..., description="时间点")
    description: str = Field(..., description="描述")


class AddStyleNoteRequest(BaseModel):
    """添加风格笔记请求"""
    note_id: str = Field(..., description="笔记 ID")
    category: str = Field(..., description="类别")
    content: str = Field(..., description="内容")


class BibleCharacterRelationshipItem(BaseModel):
    """Bible 人物关系项（与 LLM 输出的 target/relation/description 对象一致）"""

    model_config = ConfigDict(extra="allow")

    target: Optional[str] = None
    relation: Optional[str] = None
    description: Optional[str] = None


class CharacterData(BaseModel):
    """人物数据"""
    id: str = Field(..., description="人物 ID")
    name: str = Field(..., description="人物名称")
    description: str = Field(..., description="人物描述")
    relationships: list[Union[str, BibleCharacterRelationshipItem]] = Field(
        default_factory=list,
        description="关系列表：字符串或结构化对象",
    )
    gender: Optional[str] = Field(default=None, description="性别/呈现；省略则保留库中旧值")
    age: Optional[str] = Field(default=None, description="年龄/年龄段；省略则保留库中旧值")
    appearance: Optional[str] = Field(default=None, description="外貌锚点；省略则保留库中旧值")
    personality: Optional[str] = Field(default=None, description="性格底色；省略则保留库中旧值")
    background: Optional[str] = Field(default=None, description="背景经历；省略则保留库中旧值")
    core_motivation: Optional[str] = Field(default=None, description="核心驱动力；省略则保留库中旧值")
    inner_lack: Optional[str] = Field(default=None, description="内在缺口；省略则保留库中旧值")
    mental_state: Optional[str] = Field(
        default=None,
        description="心理状态；省略则保留库中旧值（新角色默认 NORMAL）",
    )
    verbal_tic: Optional[str] = Field(default=None, description="口头禅；省略则保留库中旧值")
    idle_behavior: Optional[str] = Field(
        default=None,
        description="待机动作/小动作；省略则保留库中旧值",
    )
    mental_state_reason: Optional[str] = Field(default=None, description="心理状态成因；省略则保留库中旧值")
    public_profile: Optional[str] = Field(default=None, description="公开人设；省略则保留库中旧值")
    hidden_profile: Optional[str] = Field(default=None, description="隐藏身份；省略则保留库中旧值")
    reveal_chapter: Optional[int] = Field(default=None, description="揭示隐藏信息的章节号；省略则保留")
    core_belief: Optional[str] = Field(default=None, description="核心信念（价值选择）；省略则保留")
    moral_taboos: Optional[list[str]] = Field(default=None, description="绝对禁忌列表；省略则保留")
    voice_profile: Optional[dict] = Field(default=None, description="声线结构 JSON；省略则保留")
    active_wounds: Optional[list[dict]] = Field(default=None, description="创伤触发链；省略则保留")


class WorldSettingData(BaseModel):
    """世界设定数据"""
    id: str = Field(..., description="设定 ID")
    name: str = Field(..., description="设定名称")
    description: str = Field(..., description="设定描述")
    setting_type: str = Field(..., description="设定类型")


class LocationData(BaseModel):
    """地点数据"""
    id: str = Field(..., description="地点 ID")
    name: str = Field(..., description="地点名称")
    description: str = Field(..., description="地点描述")
    location_type: str = Field(..., description="地点类型")
    parent_id: Optional[str] = Field(default=None, description="父地点 id，根为 null")


class TimelineNoteData(BaseModel):
    """时间线笔记数据"""
    id: str = Field(..., description="笔记 ID")
    event: str = Field(..., description="事件")
    time_point: str = Field(..., description="时间点")
    description: str = Field(..., description="描述")


class StyleNoteData(BaseModel):
    """风格笔记数据"""
    id: str = Field(..., description="笔记 ID")
    category: str = Field(..., description="类别")
    content: str = Field(..., description="内容")


class BulkUpdateBibleRequest(BaseModel):
    """批量更新 Bible 请求"""
    characters: list[CharacterData] = Field(default_factory=list, description="人物列表")
    world_settings: list[WorldSettingData] = Field(default_factory=list, description="世界设定列表")
    locations: list[LocationData] = Field(default_factory=list, description="地点列表")
    timeline_notes: list[TimelineNoteData] = Field(default_factory=list, description="时间线笔记列表")
    style_notes: list[StyleNoteData] = Field(default_factory=list, description="风格笔记列表")


# Routes
@router.post("/novels/{novel_id}/generate", status_code=202)
async def generate_bible(
    novel_id: str,
    background_tasks: BackgroundTasks,
    stage: str = "all",  # all / worldbuilding / characters / locations
    bible_generator: AutoBibleGenerator = Depends(get_auto_bible_generator),
    knowledge_generator: AutoKnowledgeGenerator = Depends(get_auto_knowledge_generator)
):
    """手动触发 Bible 和 Knowledge 生成（异步）

    支持分阶段生成：
    - stage=all: 一次性生成所有内容（默认，向后兼容）
    - stage=worldbuilding: 只生成世界观（5维度）和文风公约
    - stage=characters: 基于已有世界观生成人物
    - stage=locations: 基于已有世界观和人物生成地点

    用户创建小说后，前端调用此接口开始生成 Bible。
    生成过程在后台进行，前端应轮询 /bible/novels/{novel_id}/bible/status 检查状态。

    Args:
        novel_id: 小说 ID
        stage: 生成阶段
        background_tasks: FastAPI 后台任务
        bible_generator: Bible 生成器
        knowledge_generator: Knowledge 生成器

    Returns:
        202 Accepted，表示生成任务已启动；分阶段引导流会改为返回 AI Invocation 审阅会话
    """
    _validate_generation_stage(stage)
    _guard_setup_generation(novel_id, stage)
    if stage in _BIBLE_SETUP_NODE_BY_STAGE:
        return await _create_bible_setup_invocation_response(
            novel_id=novel_id,
            stage=stage,
            bible_generator=bible_generator,
        )

    async def _generate_task():
        logger.info("Bible generation task started for %s, stage=%s", novel_id, stage)
        clear_bible_generation_state(novel_id)
        try:
            # 获取小说信息（需要 premise 和 target_chapters）
            from interfaces.api.dependencies import get_novel_service
            novel_service = get_novel_service()
            novel = novel_service.get_novel(novel_id)
            if not novel:
                logger.error(f"Novel not found: {novel_id}")
                record_bible_generation_failure(novel_id, stage, "小说不存在，无法生成 Bible")
                return

            # 使用 premise（故事梗概）生成 Bible，如果没有则使用 title
            premise = strip_v1_structure_black_box_hint(novel.premise if novel.premise else novel.title)

            # 生成 Bible（支持分阶段）
            _guard_setup_generation(novel_id, stage)
            bible_data = await bible_generator.generate_and_save(
                novel_id,
                premise,
                novel.target_chapters,
                stage=stage
            )

            # 构建 Bible 摘要供 Knowledge 生成使用
            chars = bible_data.get("characters", [])
            locs = bible_data.get("locations", [])
            char_desc = "、".join(f"{c['name']}（{c.get('role', '')}）" for c in chars[:5])
            loc_desc = "、".join(c['name'] for c in locs[:3])
            bible_summary = f"主要角色：{char_desc}。重要地点：{loc_desc}。文风：{bible_data.get('style', '')}。"

            # 生成初始 Knowledge
            await knowledge_generator.generate_and_save(
                novel_id,
                novel.title,
                bible_summary
            )
            logger.info(f"Bible and Knowledge generated successfully for {novel_id}")
            clear_bible_generation_state(novel_id)
        except Exception as e:
            import traceback
            logger.error("Bible generation task failed for %s: %s", novel_id, e)
            logger.error(traceback.format_exc())
            record_bible_generation_failure(novel_id, stage, str(e))

    background_tasks.add_task(_generate_task)

    return {
        "message": "Bible generation started",
        "novel_id": novel_id,
        "status_url": bible_generation_status_url(novel_id),
    }


# ---------------------------------------------------------------------------
# SSE 流式生成接口：逐步推送每个维度的生成进度和数据
# ---------------------------------------------------------------------------

def _sse_fmt(event: str, data: dict) -> str:
    """格式化单条 SSE 消息。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_BIBLE_SETUP_NODE_BY_STAGE = {
    stage: get_onboarding_stage_definition(stage).node_key
    for stage in ("worldbuilding", "characters", "locations")
}


def _write_variable_if_missing(variable_repo, *, key: str, value, context_key: str, value_type: str, display_name: str, stage: str) -> None:
    if value in (None, "", [], {}):
        return
    if variable_repo.get_value(key, context_key) is not None:
        return
    variable_repo.set_value(
        VariableWrite(
            key=key,
            value=value,
            context_key=context_key,
            source_trace_id="setup_guide_backfill",
            source_node_key="novel_setup_guide",
            lineage={"source": "novel_setup_guide_backfill"},
            value_type=value_type,
            display_name=display_name,
            scope="global",
            stage=stage,
        )
    )

def _backfill_bible_setup_variable_hub(*, variable_repo, novel_id: str, novel) -> None:
    context_key = f"novel_id:{novel_id}"
    _write_variable_if_missing(
        variable_repo,
        key="novel.title",
        value=str(getattr(novel, "title", "") or "").strip(),
        context_key=context_key,
        value_type="string",
        display_name="名称",
        stage="setup",
    )
    _write_variable_if_missing(
        variable_repo,
        key="novel.premise",
        value=strip_v1_structure_black_box_hint(
            str(getattr(novel, "premise", "") or getattr(novel, "title", "") or "").strip()
        ),
        context_key=context_key,
        value_type="string",
        display_name="设定",
        stage="setup",
    )
    _write_variable_if_missing(
        variable_repo,
        key="novel.target_chapters",
        value=int(getattr(novel, "target_chapters", 0) or 0),
        context_key=context_key,
        value_type="integer",
        display_name="章节数量",
        stage="setup",
    )
    _write_variable_if_missing(
        variable_repo,
        key="novel.target_words_per_chapter",
        value=int(getattr(novel, "target_words_per_chapter", 0) or 0),
        context_key=context_key,
        value_type="integer",
        display_name="每章字数",
        stage="setup",
    )
    for key, attr, label in (
        ("novel.genre_label", "locked_genre", "类型"),
        ("novel.world_preset", "locked_world_preset", "基调"),
        ("novel.story_structure", "locked_story_structure", "剧情结构"),
        ("novel.pacing_control", "locked_pacing_control", "节奏把控"),
        ("novel.writing_style", "locked_writing_style", "写作风格"),
        ("novel.special_requirements", "locked_special_requirements", "特殊要求"),
    ):
        _write_variable_if_missing(
            variable_repo,
            key=key,
            value=str(getattr(novel, attr, "") or "").strip(),
            context_key=context_key,
            value_type="string",
            display_name=label,
            stage="setup",
        )
    genre_label = str(getattr(novel, "locked_genre", "") or "").strip()
    if genre_label:
        parts = [part.strip() for part in genre_label.split("/") if part.strip()]
        _write_variable_if_missing(
            variable_repo,
            key="novel.genre_major",
            value=parts[0] if parts else "",
            context_key=context_key,
            value_type="string",
            display_name="大类",
            stage="setup",
        )
        _write_variable_if_missing(
            variable_repo,
            key="novel.genre_theme",
            value=" / ".join(parts[1:]) if len(parts) > 1 else "",
            context_key=context_key,
            value_type="string",
            display_name="主题",
            stage="setup",
        )


async def _create_bible_setup_invocation(
    *,
    novel_id: str,
    stage: str,
    novel,
    bible_generator: AutoBibleGenerator,
) -> dict:
    """Create an AI Invocation session for a setup-guide Bible stage."""
    if stage not in _BIBLE_SETUP_NODE_BY_STAGE:
        raise ValueError(f"unsupported bible setup invocation stage: {stage}")
    _guard_setup_generation(novel_id, stage)
    stage_definition = get_onboarding_stage_definition(stage)
    register_bible_setup_continuations()
    operation = stage_definition.operation
    node_key = stage_definition.node_key
    try:
        from interfaces.api.v1.engine.ai_invocation_routes import _repositories
        from infrastructure.persistence.database.connection import get_database

        stage_definition.contract_ensurer(get_database())
        repos = _repositories()
        _backfill_bible_setup_variable_hub(
            variable_repo=repos["variable_hub"],
            novel_id=novel_id,
            novel=novel,
        )
    except Exception:
        logger.exception("Failed to prepare Bible setup Variable Hub contract: stage=%s", stage)
        from interfaces.api.v1.engine.ai_invocation_routes import _repositories

        repos = _repositories()
    gateway = AIInvocationGateway(
        spec_service=InvocationSpecService(repos["spec"]),
        variable_resolver=VariableResolver(repos["variable_hub"]),
        prompt_assembler=BibleSetupPromptAssembler(),
        llm_service=bible_generator.llm_service,
        session_service=InvocationSessionService(),
        attempt_service=AttemptService(bible_generator.llm_service),
        adoption_service=AdoptionService(),
        commit_service=AdoptionCommitService(variable_hub_repository=repos["variable_hub"]),
    )
    result = await gateway.invoke(
        InvocationRequest(
            operation=operation,
            node_key=node_key,
            variables={},
            context={"novel_id": novel_id, "stage": stage},
            policy=InvocationPolicy.FULL_INTERACTIVE,
            metadata={
                "source": "novel_setup_guide",
                "novel_id": novel_id,
                "stage": stage,
            },
        )
    )
    from interfaces.api.v1.engine.ai_invocation_routes import (
        _attempt_payload,
        _commit_payload,
        _decision_payload,
        _next_action,
        _save_invocation_result,
        _session_payload,
    )

    _save_invocation_result(repos, result)
    return {
        "session": _session_payload(repos, result.session),
        "attempt": _attempt_payload(result.attempt),
        "decision": _decision_payload(result.decision),
        "commit": _commit_payload(result.commit),
        "next_action": _next_action(result.session.status),
    }


async def _create_bible_setup_invocation_response(
    *,
    novel_id: str,
    stage: str,
    bible_generator: AutoBibleGenerator,
) -> dict:
    from interfaces.api.dependencies import get_novel_service

    novel_service = get_novel_service()
    novel = novel_service.get_novel(novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="novel_not_found")
    payload = await _create_bible_setup_invocation(
        novel_id=novel_id,
        stage=stage,
        novel=novel,
        bible_generator=bible_generator,
    )
    return {
        "message": "Bible generation requires review",
        "novel_id": novel_id,
        "stage": stage,
        "session": payload.get("session"),
        "attempt": payload.get("attempt"),
        "decision": payload.get("decision"),
        "commit": payload.get("commit"),
        "next_action": payload.get("next_action", ""),
    }


async def _sse_bible_generator(
    novel_id: str,
    stage: str,
    bible_generator: AutoBibleGenerator,
    knowledge_generator: AutoKnowledgeGenerator,
):
    """SSE 生成器：逐步推送 Bible 生成进度和数据片段。"""
    from interfaces.api.dependencies import get_novel_service

    if stage not in {"all", "worldbuilding", "characters", "locations"}:
        yield _sse_fmt("error", {"message": "unsupported_bible_generation_stage"})
        return
    try:
        _guard_setup_generation(novel_id, stage)
    except HTTPException as exc:
        yield _sse_fmt("error", {"code": "SETUP_AUDIT_INCOMPLETE", "message": str(exc.detail)})
        return
    runtime_settings = get_bible_runtime_settings()

    # ── 起始 ──
    yield _sse_fmt("phase", {"phase": "init", "message": "正在准备生成环境..."})
    await asyncio.sleep(0)

    clear_bible_generation_state(novel_id)

    # 获取小说信息
    try:
        novel_service = get_novel_service()
        novel = novel_service.get_novel(novel_id)
        if not novel:
            yield _sse_fmt("error", {"message": "小说不存在，无法生成 Bible"})
            return
        premise = strip_v1_structure_black_box_hint(novel.premise if novel.premise else novel.title)
    except Exception as e:
        yield _sse_fmt("error", {"message": f"获取小说信息失败: {e}"})
        return

    if stage in _BIBLE_SETUP_NODE_BY_STAGE:
        try:
            payload = await _create_bible_setup_invocation(
                novel_id=novel_id,
                stage=stage,
                novel=novel,
                bible_generator=bible_generator,
            )
            session = payload.get("session") or {}
            session_id = str(session.get("id") or "")
            if session_id:
                yield _sse_fmt(
                    "data",
                    {
                        "type": "approval_required",
                        "session_id": session_id,
                        "status": session.get("status", ""),
                        "next_action": payload.get("next_action", ""),
                        "stage": stage,
                    },
                )
                return
            yield _sse_fmt("error", {"message": "AI 审阅会话创建失败"})
            return
        except Exception as e:
            logger.exception("Bible setup invocation stream failed for %s stage=%s", novel_id, stage)
            yield _sse_fmt("error", {"message": f"AI 审阅会话创建失败: {e}"})
            return

    # Validate before changing persistent data, including the legacy SSE path.
    try:
        from application.core.taxonomy.market_prompt import load_verified_market_profile
        from infrastructure.persistence.database.connection import get_database
        load_verified_market_profile(get_database(), novel_id)
    except (ValueError, TypeError) as exc:
        yield _sse_fmt("error", {"message": f"市场画像校验失败: {exc}"})
        return

    # 确保Bible记录存在
    try:
        _guard_setup_generation(novel_id, stage)
        existing_bible = bible_generator.bible_service.get_bible_by_novel(novel_id)
        if not existing_bible:
            bible_generator.bible_service.create_bible(f"{novel_id}-bible", novel_id)
    except Exception:
        try:
            _guard_setup_generation(novel_id, stage)
            bible_generator.bible_service.create_bible(f"{novel_id}-bible", novel_id)
        except Exception as e:
            yield _sse_fmt("error", {"message": f"创建 Bible 记录失败: {e}"})
            return

    try:
        if stage in ("all", "worldbuilding"):
            # ── 世界观生成（单次 LLM 流式，五维联动） ──
            yield _sse_fmt("phase", {"phase": "worldbuilding", "message": "AI 正在构建世界观（5维度框架）..."})
            await asyncio.sleep(0)

            # 1. 先生成文风公约（快速，独立调用）
            yield _sse_fmt("phase", {"phase": "worldbuilding_style", "message": "正在生成文风公约..."})
            await asyncio.sleep(0)
            try:
                style_chunks: list[str] = []
                async for item in bible_generator._stream_style(premise, novel.target_chapters, novel_id=novel_id):
                    if item.get("type") == "chunk":
                        chunk_text = item.get("text") or ""
                        if chunk_text:
                            style_chunks.append(chunk_text)
                            yield _sse_fmt("data", {
                                "type": "style_chunk",
                                "chunk": chunk_text,
                            })
                            await asyncio.sleep(0)
                    elif item.get("type") == "done":
                        done_style = item.get("style") or ""
                        if done_style:
                            style_chunks = [done_style]

                style_text = "".join(style_chunks).strip()
                if not style_text:
                    raise ValueError("文风公约生成返回空内容")
                if style_text:
                    yield _sse_fmt("data", {"type": "style", "content": style_text})
                    # 保存文风
                    try:
                        _guard_setup_edit(novel_id, "worldbuilding")
                        bible_generator.bible_service.add_style_note(
                            novel_id=novel_id,
                            note_id=f"{novel_id}-style-1",
                            category="文风公约",
                            content=style_text,
                        )
                    except Exception as exc:
                        raise RuntimeError(f"文风公约保存失败: {exc}") from exc
            except Exception as e:
                logger.error("Style generation failed: %s", e)
                raise RuntimeError(f"文风公约生成失败，已阻塞后续 Bible 流程: {e}") from e

            # 2. 单次 LLM 流式生成完整五维世界观（维度联动，增量解析各维）
            dim_labels = {
                "core_rules": "核心法则",
                "geography": "地理生态",
                "society": "社会结构",
                "culture": "历史文化",
                "daily_life": "沉浸感细节",
            }
            accumulated_wb: dict = {}
            saved_dim_snapshots: dict[str, dict] = {}
            announced_fields: set[tuple[str, str]] = set()

            yield _sse_fmt("phase", {
                "phase": "worldbuilding_streaming",
                "message": "AI 正在一次性构建五维联动世界观（流式输出）...",
            })
            await asyncio.sleep(0)

            try:
                async for item in bible_generator._stream_worldbuilding_full(
                    premise, novel.target_chapters, novel_id=novel_id,
                ):
                    if item["type"] == "chunk":
                        chunk_text = item.get("text") or ""
                        if chunk_text:
                            yield _sse_fmt("data", {
                                "type": "worldbuilding_chunk",
                                "chunk": chunk_text,
                            })
                        await asyncio.sleep(0)

                    elif item["type"] == "dimension_start":
                        dim_key = item.get("key")
                        if dim_key:
                            dim_label = dim_labels.get(dim_key, dim_key)
                            yield _sse_fmt("phase", {
                                "phase": f"worldbuilding_{dim_key}",
                                "message": f"{dim_label} 生成中...",
                            })
                            await asyncio.sleep(0)

                    elif item["type"] == "field":
                        dim_key = item.get("key")
                        field_key = item.get("field")
                        field_value = item.get("value")
                        if dim_key and field_key and field_value:
                            marker = (dim_key, field_key)
                            if marker not in announced_fields:
                                announced_fields.add(marker)
                                dim_label = dim_labels.get(dim_key, dim_key)
                                field_label = (
                                    WORLDBUILDING_DIMENSION_DEFS
                                    .get(dim_key, {})
                                    .get("fields", {})
                                    .get(field_key, field_key)
                                )
                                yield _sse_fmt("phase", {
                                    "phase": f"worldbuilding_{dim_key}",
                                    "message": f"{dim_label} · {field_label} 已解析",
                                })
                            accumulated_wb.setdefault(dim_key, {})[field_key] = field_value
                            logger.info(
                                "SSE worldbuilding field send: novel=%s dim=%s field=%s len=%s",
                                novel_id,
                                dim_key,
                                field_key,
                                len(str(field_value)),
                            )
                            yield _sse_fmt("data", {
                                "type": "worldbuilding_field",
                                "dimension": dim_key,
                                "field": field_key,
                                "value": field_value,
                            })
                            await asyncio.sleep(
                                runtime_settings.worldbuilding_field_emit_delay_seconds
                            )

                    elif item["type"] == "dimension":
                        dim_key = item["key"]
                        dim_data = item.get("content") or {}
                        if not dim_data:
                            continue
                        accumulated_wb.setdefault(dim_key, {}).update(dim_data)
                        dim_data = accumulated_wb[dim_key]
                        dim_label = dim_labels.get(dim_key, dim_key)

                        yield _sse_fmt("phase", {
                            "phase": f"worldbuilding_{dim_key}",
                            "message": f"{dim_label} 已就绪",
                        })
                        yield _sse_fmt("data", {
                            "type": "worldbuilding_dimension",
                            "dimension": dim_key,
                            "label": dim_label,
                            "content": dim_data,
                        })
                        logger.info(
                            "SSE worldbuilding dimension send: novel=%s dim=%s fields=%s",
                            novel_id,
                            dim_key,
                            sorted(dim_data.keys()),
                        )
                        if saved_dim_snapshots.get(dim_key) != dim_data:
                            try:
                                _guard_setup_edit(novel_id, "worldbuilding")
                                await bible_generator._save_worldbuilding(
                                    novel_id, {dim_key: dim_data},
                                )
                                saved_dim_snapshots[dim_key] = dict(dim_data)
                            except Exception as e:
                                raise RuntimeError(f"世界观维度 {dim_key} 保存失败: {e}") from e
                        await asyncio.sleep(
                            runtime_settings.worldbuilding_dimension_emit_delay_seconds
                        )

                    elif item["type"] == "done":
                        full = item.get("worldbuilding") or {}
                        for dk, dv in full.items():
                            if dv:
                                accumulated_wb.setdefault(dk, {}).update(dv)

            except Exception as e:
                logger.error("Failed to stream full worldbuilding: %s", e)
                raise RuntimeError(f"世界观生成未完成，已停止后续流程：{e}") from e

            missing_dimensions = set(WORLDBUILDING_DIMENSION_DEFS) - set(accumulated_wb)
            if missing_dimensions:
                raise ValueError(f"世界观生成未完成，缺少维度：{', '.join(sorted(missing_dimensions))}")

            # Field-level streaming can succeed even when the final dimension object
            # cannot be parsed as strict JSON (common when models emit raw newlines in
            # long strings). Persist every accumulated dimension here so the UI does not
            # show freshly generated fields that vanish after reload.
            for dim_key, dim_data in accumulated_wb.items():
                if not dim_data or saved_dim_snapshots.get(dim_key) == dim_data:
                    continue
                try:
                    _guard_setup_edit(novel_id, "worldbuilding")
                    await bible_generator._save_worldbuilding(
                        novel_id, {dim_key: dim_data},
                    )
                    saved_dim_snapshots[dim_key] = dict(dim_data)
                except Exception as e:
                    raise RuntimeError(f"世界观维度 {dim_key} 最终保存失败: {e}") from e

            yield _sse_fmt("phase", {"phase": "worldbuilding_done", "message": "世界观生成完成！"})

        if stage in ("all", "characters"):
            # ── 人物生成（流式 LLM） ──
            yield _sse_fmt("phase", {"phase": "characters", "message": "AI 正在生成主要角色..."})
            await asyncio.sleep(0)

            existing_worldbuilding = bible_generator._load_worldbuilding(novel_id)
            chars_payload = []
            character_ids = []
            used_char_ids = set()

            async for item in bible_generator._stream_generate_characters(
                premise, novel.target_chapters, existing_worldbuilding, novel_id=novel_id
            ):
                if item["type"] == "character":
                    char_data = item["content"]
                    chars_payload.append(char_data)
                    idx = item["index"]
                    yield _sse_fmt("phase", {"phase": f"character_{idx}", "message": f"正在生成角色：{char_data.get('name', '...')}..."})
                    yield _sse_fmt("data", {
                        "type": "character",
                        "index": idx,
                        "content": char_data,
                    })
                    # 即时落库
                    character_id = f"{novel_id}-char-{idx+1}"
                    if character_id in used_char_ids:
                        character_id = f"{novel_id}-char-{idx+1}-{len(used_char_ids)}"
                    used_char_ids.add(character_id)
                    try:
                        _guard_setup_edit(novel_id, "characters")
                        bible_generator.bible_service.add_character(
                            novel_id=novel_id,
                            character_id=character_id,
                            name=char_data["name"],
                            description=f"{char_data.get('role', '')} - {char_data.get('description', '')}",
                            relationships=char_data.get("relationships", []),
                            gender=char_data.get("gender") or "",
                            age=char_data.get("age") or "",
                            appearance=char_data.get("appearance") or "",
                            personality=char_data.get("personality") or char_data.get("flaw") or "",
                            background=char_data.get("background") or char_data.get("ghost") or "",
                            core_motivation=char_data.get("core_motivation") or char_data.get("want") or "",
                            inner_lack=char_data.get("inner_lack") or char_data.get("need") or "",
                            public_profile=char_data.get("public_profile") or "",
                            hidden_profile=char_data.get("hidden_profile") or "",
                            reveal_chapter=char_data.get("reveal_chapter"),
                            mental_state=char_data.get("mental_state") or "NORMAL",
                            mental_state_reason=char_data.get("mental_state_reason") or "",
                            verbal_tic=char_data.get("verbal_tic") or "",
                            idle_behavior=char_data.get("idle_behavior") or "",
                            core_belief=char_data.get("core_belief") or "",
                            moral_taboos=char_data.get("moral_taboos") or [],
                            voice_profile=char_data.get("voice_profile") or {},
                            active_wounds=char_data.get("active_wounds") or [],
                        )
                        character_ids.append((character_id, char_data))
                    except Exception as exc:
                        raise RuntimeError(f"人物 {char_data.get('name', character_id)} 保存失败: {exc}") from exc
                elif item["type"] == "chunk":
                    # 透传 LLM 原始 chunk（前端可用于打字效果）
                    yield _sse_fmt("data", {
                        "type": "character_chunk",
                        "chunk": item["text"],
                    })

            if not character_ids:
                raise ValueError("人物生成未得到任何已保存角色，已停止后续流程")
            # 生成人物关系三元组
            if bible_generator.triple_repository and character_ids:
                await bible_generator._generate_character_triples(novel_id, character_ids)

            yield _sse_fmt("phase", {"phase": "characters_done", "message": f"人物生成完成！共 {len(chars_payload)} 个角色"})

        if stage in ("all", "locations"):
            # ── 地点生成（流式 LLM） ──
            yield _sse_fmt("phase", {"phase": "locations", "message": "AI 正在生成地图系统..."})
            await asyncio.sleep(0)

            existing_worldbuilding = bible_generator._load_worldbuilding(novel_id)
            existing_characters = bible_generator._load_characters(novel_id)
            locs_payload = []
            location_ids = []

            async for item in bible_generator._stream_generate_locations(
                premise, novel.target_chapters, existing_worldbuilding, existing_characters, novel_id=novel_id
            ):
                if item["type"] == "location":
                    loc_data = item["content"]
                    locs_payload.append(loc_data)
                    idx = item["index"]
                    yield _sse_fmt("phase", {"phase": f"location_{idx}", "message": f"正在生成地点：{loc_data.get('name', '...')}..."})
                    yield _sse_fmt("data", {
                        "type": "location",
                        "index": idx,
                        "content": loc_data,
                    })
                elif item["type"] == "chunk":
                    yield _sse_fmt("data", {
                        "type": "location_chunk",
                        "chunk": item["text"],
                    })

            if not locs_payload:
                raise ValueError("地点生成未得到任何有效地点，已停止后续流程")
            # Resolve IDs and parents across the complete graph. Preparing each
            # item independently reuses loc-1 and drops parents not yet streamed.
            for pd in bible_generator._prepare_locations_for_save(novel_id, locs_payload):
                try:
                    _guard_setup_edit(novel_id, "locations")
                    bible_generator.bible_service.add_location(
                        novel_id=novel_id,
                        location_id=pd["location_id"],
                        name=pd["name"],
                        description=pd["description"],
                        location_type=pd["location_type"],
                        connections=pd.get("connections", []),
                        parent_id=pd.get("parent_id"),
                    )
                    location_ids.append((pd["location_id"], pd))
                except Exception as exc:
                    raise RuntimeError(f"地点 {pd['name']} 保存失败: {exc}") from exc
            if len(location_ids) != len(locs_payload):
                raise ValueError("地点生成结果未完整保存，已停止后续流程")
            # 生成地点关系三元组
            if bible_generator.triple_repository and location_ids:
                await bible_generator._generate_location_triples(novel_id, location_ids)

            yield _sse_fmt("phase", {"phase": "locations_done", "message": f"地图生成完成！共 {len(locs_payload)} 个地点"})

        # ── 知识库生成 ──
        yield _sse_fmt("phase", {"phase": "knowledge", "message": "正在构建知识库..."})
        await asyncio.sleep(0)

        bible = bible_generator.bible_service.get_bible_by_novel(novel_id)
        if bible:
            chars = bible.characters or []
            locs = bible.locations or []
            char_desc = "、".join(f"{c.name}" for c in chars[:5])
            loc_desc = "、".join(c.name for c in locs[:3])
            style_notes = bible.style_notes or []
            style_text = "；".join(n.content for n in style_notes if n.content)
            bible_summary = f"主要角色：{char_desc}。重要地点：{loc_desc}。文风：{style_text}。"
            await knowledge_generator.generate_and_save(novel_id, novel.title, bible_summary)

        clear_bible_generation_state(novel_id)
        yield _sse_fmt("done", {"message": "全部生成完成！", "novel_id": novel_id})

    except Exception as e:
        import traceback
        logger.error("SSE Bible generation failed for %s: %s", novel_id, e)
        logger.error(traceback.format_exc())
        record_bible_generation_failure(novel_id, stage, str(e))
        yield _sse_fmt("error", {"message": f"生成失败: {e}"})


@router.post("/novels/{novel_id}/generate-stream/")
@router.post("/novels/{novel_id}/generate-stream")
async def generate_bible_stream(
    novel_id: str,
    stage: str = "worldbuilding",
    bible_generator: AutoBibleGenerator = Depends(get_auto_bible_generator),
    knowledge_generator: AutoKnowledgeGenerator = Depends(get_auto_knowledge_generator),
):
    """SSE 流式 Bible 生成接口。

    世界观：单次 LLM 流式输出五维 JSON，后端增量解析后只推送
    worldbuilding_dimension / worldbuilding_field。人物/地点仍为流式 JSON 数组解析。

    事件类型：
    - phase: init / worldbuilding_streaming / worldbuilding_{dim} / characters / locations / *_done
    - data: style / worldbuilding_dimension / worldbuilding_field / character / location
    - done / error
    """
    _validate_generation_stage(stage)
    _guard_setup_generation(novel_id, stage)
    return StreamingResponse(
        _sse_bible_generator(novel_id, stage, bible_generator, knowledge_generator),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/novels/{novel_id}/bible", response_model=BibleDTO, status_code=201)
async def create_bible(
    novel_id: str,
    request: CreateBibleRequest,
    service: BibleService = Depends(get_bible_service)
):
    """为小说创建 Bible

    Args:
        novel_id: 小说 ID
        request: 创建 Bible 请求
        service: Bible 服务

    Returns:
        创建的 Bible DTO
    """
    # Reusing a Bible id replaces its child collections, so this endpoint is
    # also a canonical edit when a book already has setup material.
    for stage in ("worldbuilding", "characters", "locations"):
        _guard_setup_edit(novel_id, stage)
    return service.create_bible(request.bible_id, novel_id)


# 注意：必须先注册比 `/novels/{id}/bible` 更长的路径，避免与 `{novel_id}` 匹配歧义
@router.get("/novels/{novel_id}/bible/generation-feedback")
async def get_bible_generation_feedback(novel_id: str):
    """新书向导轮询用：最近一次 Bible 异步生成失败原因（成功或未失败时为 null）。"""
    state = get_bible_generation_state(novel_id)
    if not state:
        return {"novel_id": novel_id, "error": None, "stage": None, "at": None}
    return {
        "novel_id": novel_id,
        "error": state.get("error"),
        "stage": state.get("stage"),
        "at": state.get("at"),
    }


@router.get("/novels/{novel_id}/bible/status")
async def get_bible_status(
    novel_id: str,
    service: BibleService = Depends(get_bible_service)
):
    """检查 Bible 生成状态

    Args:
        novel_id: 小说 ID
        service: Bible 服务

    Returns:
        状态信息：{ "exists": bool, "ready": bool }
    """
    try:
        bible = service.get_bible_by_novel(novel_id)
        exists = bible is not None
        # 修改ready逻辑：只要有文风公约或世界观就算ready（支持分阶段生成）
        ready = exists and (len(bible.style_notes) > 0 or len(bible.world_settings) > 0 or len(bible.characters) > 0)

        return {
            "exists": exists,
            "ready": ready,
            "novel_id": novel_id
        }
    except Exception as e:
        logger.exception("get_bible_status failed for novel_id=%s", novel_id)
        raise HTTPException(status_code=500, detail=f"检查 Bible 状态失败: {e}") from e


@router.get("/novels/{novel_id}/bible", response_model=BibleDTO)
async def get_bible_by_novel(
    novel_id: str,
    service: BibleService = Depends(get_bible_service)
):
    """获取小说的 Bible（小说存在但尚无 Bible 时自动建空 Bible，避免工作台首屏 404）"""
    try:
        return service.ensure_bible_for_novel(novel_id)
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/novels/{novel_id}/bible/characters", response_model=list)
async def list_characters(
    novel_id: str,
    service: BibleService = Depends(get_bible_service)
):
    """列出 Bible 中的所有人物"""
    try:
        bible = service.ensure_bible_for_novel(novel_id)
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return bible.characters


@router.post("/novels/{novel_id}/bible/characters", response_model=BibleDTO)
async def add_character(
    novel_id: str,
    request: AddCharacterRequest,
    service: BibleService = Depends(get_bible_service)
):
    """添加人物到 Bible

    Args:
        novel_id: 小说 ID
        request: 添加人物请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在
    """
    _guard_setup_edit(novel_id, "characters")
    try:
        return service.add_character(
            novel_id=novel_id,
            character_id=request.character_id,
            name=request.name,
            description=request.description
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/novels/{novel_id}/bible/world-settings", response_model=BibleDTO)
async def add_world_setting(
    novel_id: str,
    request: AddWorldSettingRequest,
    service: BibleService = Depends(get_bible_service)
):
    """添加世界设定到 Bible

    Args:
        novel_id: 小说 ID
        request: 添加世界设定请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在
    """
    _guard_setup_edit(novel_id, "worldbuilding")
    try:
        return service.add_world_setting(
            novel_id=novel_id,
            setting_id=request.setting_id,
            name=request.name,
            description=request.description,
            setting_type=request.setting_type
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/novels/{novel_id}/bible/locations", response_model=BibleDTO)
async def add_location(
    novel_id: str,
    request: AddLocationRequest,
    service: BibleService = Depends(get_bible_service)
):
    """添加地点到 Bible

    Args:
        novel_id: 小说 ID
        request: 添加地点请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在
    """
    _guard_setup_edit(novel_id, "locations")
    try:
        return service.add_location(
            novel_id=novel_id,
            location_id=request.location_id,
            name=request.name,
            description=request.description,
            location_type=request.location_type,
            parent_id=request.parent_id,
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/novels/{novel_id}/bible/timeline-notes", response_model=BibleDTO)
async def add_timeline_note(
    novel_id: str,
    request: AddTimelineNoteRequest,
    service: BibleService = Depends(get_bible_service)
):
    """添加时间线笔记到 Bible

    Args:
        novel_id: 小说 ID
        request: 添加时间线笔记请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在
    """
    try:
        return service.add_timeline_note(
            novel_id=novel_id,
            note_id=request.note_id,
            event=request.event,
            time_point=request.time_point,
            description=request.description
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/novels/{novel_id}/bible/style-notes", response_model=BibleDTO)
async def add_style_note(
    novel_id: str,
    request: AddStyleNoteRequest,
    service: BibleService = Depends(get_bible_service)
):
    """添加风格笔记到 Bible

    Args:
        novel_id: 小说 ID
        request: 添加风格笔记请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在
    """
    _guard_setup_edit(novel_id, "worldbuilding")
    try:
        return service.add_style_note(
            novel_id=novel_id,
            note_id=request.note_id,
            category=request.category,
            content=request.content
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/novels/{novel_id}/bible", response_model=BibleDTO)
async def bulk_update_bible(
    novel_id: str,
    request: BulkUpdateBibleRequest,
    service: BibleService = Depends(get_bible_service)
):
    """批量更新 Bible 的所有数据

    Args:
        novel_id: 小说 ID
        request: 批量更新请求
        service: Bible 服务

    Returns:
        更新后的 Bible DTO

    Raises:
        HTTPException: 如果 Bible 不存在或参数无效
    """
    current = service.get_bible_by_novel(novel_id)
    for section, stage in (("characters", "characters"), ("world_settings", "worldbuilding"),
                           ("locations", "locations"), ("style_notes", "worldbuilding")):
        if _section_changed(getattr(request, section), getattr(current, section, []) if current else [],
                            preserve_none=section == "characters"):
            _guard_setup_edit(novel_id, stage)
    try:
        dto = service.update_bible(
            novel_id=novel_id,
            characters=request.characters,
            world_settings=request.world_settings,
            locations=request.locations,
            timeline_notes=request.timeline_notes,
            style_notes=request.style_notes
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        from application.engine.services.state_bootstrap import refresh_narrative_contract_in_shared_state
        refresh_narrative_contract_in_shared_state(novel_id)
    except Exception:
        pass
    _sync_bible_variable_hub(novel_id, dto)
    return dto
