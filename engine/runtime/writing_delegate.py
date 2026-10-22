"""StoryPipeline 写作阶段委托

环境变量 PLOTPILOT_USE_STORY_PIPELINE:
  - 未设置（默认）/ writing / 1 / true: StoryPipeline 写作
  - full / all / engine: StoryPipeline 写作（与 writing 等价）
  - off / legacy / false / 0: legacy 节拍写作（紧急回退）

生产入口统一为 EngineDaemon（Phase 9）。
"""
from __future__ import annotations

import logging
import time
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

from infrastructure.engine.story_pipeline_environment import (
    STORY_PIPELINE_MODE_ENV,
    StoryPipelineEnvironmentSettings,
)

logger = logging.getLogger(__name__)

PipelineMode = Literal["off", "writing", "full"]


@dataclass(frozen=True)
class WritingOutcome:
    """明确的写作生命周期结果。

    生命周期只允许 ``success`` 清零连续失败计数；``failure`` 由唯一的
    生命周期入口累计一次。其余状态表示本轮没有形成业务成功，也不应误
    报成功或失败。
    """

    status: Literal["success", "failure", "paused", "interrupted", "neutral"]
    error: Optional[str] = None
    derived_status: str = "not_requested"


def _legacy_outcome(before_chapters: int, novel: Any) -> WritingOutcome:
    """从旧委托的状态投影出最小可靠结果。

    旧委托历史上以 ``None`` 表示所有早退。只有明确进入审计或章节计数
    增长才算正文已提交；停止、审阅、规划和未完成草稿均保持中性。
    """
    from domain.novel.entities.novel import NovelStage

    stage = getattr(novel, "current_stage", None)
    try:
        after_chapters = int(getattr(novel, "current_auto_chapters", 0) or 0)
    except (TypeError, ValueError):
        after_chapters = before_chapters
    if stage in {NovelStage.AUDITING, NovelStage.COMPLETED} or after_chapters > before_chapters:
        return WritingOutcome("success")
    if stage == NovelStage.PAUSED_FOR_REVIEW:
        return WritingOutcome("paused")
    return WritingOutcome("neutral")


def get_story_pipeline_mode() -> PipelineMode:
    """解析引擎内核模式（未设置时默认 writing，Phase 4）"""
    settings = StoryPipelineEnvironmentSettings.from_env()
    if settings.is_unknown:
        logger.warning(
            "未知 %s=%r，使用默认 writing；回退 legacy 请设 off",
            STORY_PIPELINE_MODE_ENV,
            settings.raw_mode,
        )
    return settings.mode


def story_pipeline_mode_was_unset() -> bool:
    return StoryPipelineEnvironmentSettings.from_env().is_unset


def is_story_pipeline_writing_enabled() -> bool:
    """写作阶段是否走新内核（4a 或 4b）"""
    return get_story_pipeline_mode() in ("writing", "full")


def _build_runner(daemon: Any):
    from engine.runtime.runner import StoryPipelineRunner

    return StoryPipelineRunner(
        novel_repository=daemon.novel_repository,
        llm_service=daemon.llm_service,
        context_builder=daemon.context_builder,
        background_task_service=daemon.background_task_service,
        planning_service=daemon.planning_service,
        story_node_repo=daemon.story_node_repo,
        chapter_repository=daemon.chapter_repository,
        poll_interval=daemon.poll_interval,
        voice_drift_service=daemon.voice_drift_service,
        circuit_breaker=daemon.circuit_breaker,
        chapter_workflow=daemon.chapter_workflow,
        aftermath_pipeline=daemon.aftermath_pipeline,
        volume_summary_service=daemon.volume_summary_service,
        foreshadowing_repository=daemon.foreshadowing_repository,
        knowledge_service=daemon.knowledge_service,
    )


async def run_writing(host: Any, novel: Any) -> WritingOutcome:
    """写作阶段统一入口 — 按 host 配置或环境变量选择新/旧管线"""
    if getattr(host, "use_story_pipeline_for_writing", False):
        return await run_story_pipeline_writing(host, novel)
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    before_chapters = int(getattr(novel, "current_auto_chapters", 0) or 0)
    await run_legacy_writing(host, novel)
    return _legacy_outcome(before_chapters, novel)


async def run_story_pipeline_writing(daemon: Any, novel: Any) -> WritingOutcome:
    """执行单章写作（新管线），并同步 novel 状态到 daemon 模型"""
    from domain.novel.entities.novel import NovelStage
    from engine.pipelines.registry import get_pipeline_registry

    novel_id = novel.novel_id.value if hasattr(novel.novel_id, "value") else str(novel.novel_id)
    runner = _build_runner(daemon)
    target_words = int(getattr(novel, "target_words_per_chapter", None) or runner.DEFAULT_TARGET_WORDS)
    genre = (getattr(novel, "genre", "") or "").strip().lower()

    logger.info("[%s] StoryPipeline 写作模式 genre=%s", novel_id, genre or "(default)")

    def _writing_sink(substep: str, label: str, extra: Dict[str, Any]) -> None:
        merged = dict(extra)
        nw = merged.get("story_pipeline_wave_index")
        try:
            import sys

            shared = sys.modules.get("__shared_state")
            if shared is not None and nw is not None:
                nk = f"novel:{novel_id}"
                prev = dict(shared.get(nk, {}))
                pw = prev.get("story_pipeline_wave_index")
                if pw != nw:
                    merged["story_pipeline_wave_entered_at"] = time.time()
                elif "story_pipeline_wave_entered_at" not in merged and prev.get("story_pipeline_wave_entered_at"):
                    merged["story_pipeline_wave_entered_at"] = prev["story_pipeline_wave_entered_at"]
                # 新的一章开篇：清零事件轨迹
                if substep == "chapter_found" and int(nw) == 1:
                    merged["story_pipeline_events"] = [
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    ]
                else:
                    ev = list(prev.get("story_pipeline_events") or [])
                    ev.append(
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    )
                    merged["story_pipeline_events"] = ev[-32:]
            elif nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())
        except Exception:
            if nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())

        daemon._update_shared_state(
            novel_id,
            writing_substep=substep,
            writing_substep_label=label,
            **merged,
        )

    ctx = runner._make_context(
        novel_id=novel_id,
        target_word_count=target_words,
        phase=runner._get_novel_phase(novel),
        auto_approve_mode=getattr(novel, "auto_approve_mode", False),
        genre=getattr(novel, "genre", ""),
        era=getattr(novel, "era", "ancient"),
    )
    ctx.writing_progress_sink = _writing_sink

    pipeline = get_pipeline_registry().create_pipeline(genre)
    result = await pipeline.run_chapter(ctx)

    # Publish the same result for success, rejection, review and interruption.
    # A committed body can succeed while its checks/derived work remain pending.
    daemon._update_shared_state(
        novel_id,
        pipeline_status=result.status,
        pipeline_passed=result.passed,
        pipeline_success=result.success,
        pipeline_complete=True,
        pipeline_step_status=dict(result.step_status),
        pipeline_step_results=dict(result.step_results),
        pipeline_error=result.error or "",
        validation_status=result.validation_status,
        derived_status=result.derived_status,
        derived_error=result.derived_error or "",
    )

    if not result.success and result.error == "awaiting_ai_review":
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 等待 AI Invocation 审阅", novel_id)
        return WritingOutcome("paused", result.error)

    if not result.success and result.error == "interrupted":
        try:
            from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
            from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace

            policy = AutopilotRecoveryPolicy(workspace=ChapterGenerationWorkspace())
            decision = policy.decide_on_daemon_tick(novel)
            policy.apply_transient_cleanup(decision)
        except Exception:
            pass
        daemon._update_shared_state(
            novel_id,
            writing_substep="interrupted",
            writing_substep_label="正文生成已中断，等待重试",
            current_stage="writing",
        )
        if daemon._is_still_running(novel):
            novel.current_stage = NovelStage.WRITING
            daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 正文生成中断，未提交正式章节", novel_id)
        return WritingOutcome("interrupted", result.error)

    if result.success:
        chapter_num = result.chapter_number or ctx.chapter_number
        if getattr(result, "audit_snapshot", None):
            pending = getattr(daemon, "_pending_story_pipeline_aftermath", None)
            if pending is not None:
                content = result.content or ""
                pending[(novel_id, chapter_num)] = {
                    **dict(result.audit_snapshot),
                    "chapter_number": chapter_num,
                    "tension_composite": result.tension,
                    "derived_status": getattr(result, "derived_status", "not_requested"),
                    "derived_error": getattr(result, "derived_error", None),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "source": "story_pipeline",
                    "reused": False,
                }
        novel.current_auto_chapters = (novel.current_auto_chapters or 0) + 1
        novel.current_chapter_in_act = (novel.current_chapter_in_act or 0) + 1
        novel.current_beat_index = 0
        novel.beats_completed = False
        if result.tension:
            novel.last_chapter_tension = result.tension
        novel.current_stage = NovelStage.AUDITING

        daemon._update_shared_state(
            novel_id,
            writing_substep="pipeline_done",
            writing_substep_label="审计准备",
            current_chapter_number=chapter_num,
            last_chapter_tension=result.tension,
            audit_aftermath_reused=False,
            audit_aftermath_rebuilt=False,
        )
        daemon._flush_novel(novel)
        logger.info(
            "[%s] StoryPipeline 完成：第%s章 %s字 张力%s",
            novel_id,
            chapter_num,
            result.word_count,
            result.tension,
        )
        derived_status = str(getattr(result, "derived_status", "not_requested") or "not_requested")
        derived_error = getattr(result, "derived_error", None)
        if derived_status in {"deferred", "retryable", "failed"}:
            daemon._update_shared_state(
                novel_id,
                derived_status=derived_status,
                derived_error=derived_error or "",
                derived_retryable=derived_status in {"retryable", "failed"},
            )
            logger.warning(
                "[%s] 正文已提交，但章后派生状态=%s%s",
                novel_id,
                derived_status,
                f": {derived_error}" if derived_error else "",
            )
        # A body may be committed while quality/derived work is unavailable or
        # deferred. Keep the body receipt and advance the novel, but do not let
        # lifecycle accounting reset consecutive failures as a full pass.
        outcome_status = "success" if result.passed else "neutral"
        return WritingOutcome(outcome_status, error=derived_error, derived_status=derived_status)

    error = result.error or "unknown"
    if "所有章节已写完" in error:
        logger.info("[%s] StoryPipeline：当前幕章节已全部写完", novel_id)
        if await daemon._current_act_fully_written(novel):
            novel.current_act = (novel.current_act or 0) + 1
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        else:
            # Pipeline 没找到下一章，但当前幕又没有达到“已全部完成”的条件。
            # 这通常表示章节规划节点缺失或刚被并发清理，不应跳到下一幕；
            # 回到本幕规划，让引擎补齐当前幕章节。
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        daemon._flush_novel(novel)
        return WritingOutcome("neutral")

    logger.error("[%s] StoryPipeline 写作失败: %s", novel_id, error)
    return WritingOutcome("failure", error)
