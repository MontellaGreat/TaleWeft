"""小说生命周期路由 — Phase 5 从 AutopilotDaemon 收拢到 engine/runtime"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from domain.novel.entities.novel import Novel, NovelStage, AutopilotStatus

from engine.runtime.act_planning_delegate import run_act_planning
from engine.runtime.audit_delegate import run_chapter_audit
from engine.runtime.macro_planning_delegate import run_macro_planning

logger = logging.getLogger(__name__)


def _is_novel_deleted(host: Any, novel: Novel) -> bool:
    """检查小说是否已被删除（用于区分 FK 失败 vs 真正的运行时错误）。"""
    try:
        status = host._read_autopilot_status_ephemeral(novel.novel_id)
        return status is None
    except Exception:
        return False


async def process_novel(host: Any, novel: Novel) -> None:
    """处理单个小说（全流程状态机路由）"""
    # Bind a fixed epoch before recovery can cause any database mutations.
    if not host._begin_novel_execution(novel.novel_id):
        logger.warning("[%s] 无有效执行租约，跳过本轮", novel.novel_id)
        return
    try:
        if not host._is_still_running(novel):
            return
        from application.engine.services import continuation_proposal_service as continuation
        continuation_waiting = continuation.active(novel.novel_id.value)
        if continuation_waiting and continuation_waiting["status"] == "awaiting_structure_review":
            # Durable author review precedes recovery heuristics and auto-approve.
            return
        try:
            from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
            from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace

            policy = AutopilotRecoveryPolicy(workspace=ChapterGenerationWorkspace())
            decision = policy.decide_on_daemon_tick(novel)
            policy.apply_transient_cleanup(decision)
            if decision.next_stage and decision.next_stage != novel.current_stage.value:
                try:
                    novel.current_stage = NovelStage(decision.next_stage)
                    host._update_shared_state(
                        novel.novel_id.value,
                        current_stage=decision.next_stage,
                        autopilot_recovery_reason=decision.reason,
                    )
                    host._save_novel_state(novel)
                    logger.info("[%s] 恢复策略修正阶段为 %s：%s", novel.novel_id, decision.next_stage, decision.reason)
                except ValueError:
                    logger.debug("[%s] 恢复策略返回未知阶段: %s", novel.novel_id, decision.next_stage)
            elif decision.reason:
                host._update_shared_state(
                    novel.novel_id.value,
                    autopilot_recovery_reason=decision.reason,
                )
            if decision.clear_pending_invocation:
                host._update_shared_state(
                    novel.novel_id.value,
                    active_invocation_session_id="",
                    active_invocation_operation="",
                    active_invocation_node_key="",
                    active_invocation_status="",
                    active_invocation_policy="",
                    has_active_invocation=False,
                    requires_ai_review=False,
                    autopilot_pause_reason="",
                )
        except Exception as recovery_error:
            logger.debug("[%s] 恢复策略执行失败（继续原流程）: %s", novel.novel_id, recovery_error)

        if not host._is_still_running(novel):
            logger.info("[%s] 用户已停止自动驾驶，跳过本轮", novel.novel_id)
            return

        stage_name = novel.current_stage.value
        operation_succeeded = True
        operation_neutral = False
        logger.debug("[%s] 当前阶段: %s", novel.novel_id, stage_name)

        if novel.current_stage in (NovelStage.PLANNING, NovelStage.MACRO_PLANNING):
            if novel.current_stage == NovelStage.PLANNING:
                logger.info("[%s] 旧版 planning 阶段归一为 macro_planning", novel.novel_id)
                novel.current_stage = NovelStage.MACRO_PLANNING
                try:
                    host._save_novel_state(novel)
                except Exception:
                    logger.debug("[%s] planning 阶段归一落库失败，将继续执行宏观规划", novel.novel_id, exc_info=True)
            logger.info("[%s] 开始宏观规划", novel.novel_id)
            await run_macro_planning(host, novel)
            if continuation_waiting:
                # prepare_structure owns the durable transition. An author can
                # confirm immediately after it returns; saving this in-memory
                # macro/paused object would overwrite the committed act stage.
                return
        elif novel.current_stage == NovelStage.ACT_PLANNING:
            logger.info("[%s] 开始幕级规划 (第 %s 幕)", novel.novel_id, novel.current_act + 1)
            await run_act_planning(host, novel)
        elif novel.current_stage == NovelStage.WRITING:
            logger.info("[%s] 开始写作 (第 %s 幕)", novel.novel_id, novel.current_act + 1)
            from engine.runtime.writing_delegate import run_writing, WritingOutcome

            writing_outcome = await run_writing(host, novel)
            # 写作委托必须显式返回结果。为了兼容第三方/旧测试替身，
            # None 视为“本轮无确认结果”，不能误报为成功。
            if isinstance(writing_outcome, WritingOutcome):
                operation_succeeded = writing_outcome.status == "success"
                operation_neutral = writing_outcome.status in {"paused", "interrupted", "neutral"}
                if writing_outcome.status == "failure":
                    logger.warning(
                        "[%s] 写作委托失败，交由生命周期统一累计熔断：%s",
                        novel.novel_id,
                        writing_outcome.error or "unknown",
                    )
            else:
                operation_succeeded = False
                operation_neutral = True
        elif novel.current_stage == NovelStage.AUDITING:
            logger.info("[%s] 开始审计", novel.novel_id)
            await run_chapter_audit(host, novel)
        elif novel.current_stage == NovelStage.PAUSED_FOR_REVIEW:
            from application.engine.services import continuation_proposal_service as continuation
            if continuation.active(novel.novel_id.value):
                logger.debug("[%s] 续写结构必须由作者确认，自动审批保持等待", novel.novel_id)
                return
            from application.ai_invocation.autopilot.review_gate import resume_block_reason_from_status
            from application.ai_invocation.autopilot.shared_state import read_autopilot_shared_state
            from application.engine.services.query_service import _hydrate_pending_invocation_from_db

            # Auto-approve applies to completed artifacts. An invocation that
            # failed or still needs acceptance must obey the same gate as the
            # resume API, otherwise each tick silently restarts planning.
            review_state = await asyncio.to_thread(_hydrate_pending_invocation_from_db, {
                **read_autopilot_shared_state(novel.novel_id.value),
                "novel_id": novel.novel_id.value,
                "current_stage": novel.current_stage.value,
                "autopilot_status": novel.autopilot_status.value,
            })
            if resume_block_reason_from_status(review_state):
                logger.debug("[%s] AI 请求尚未成功提交，自动审批保持等待", novel.novel_id)
                return
            if getattr(novel, "auto_approve_mode", False):
                logger.info("[%s] 全自动模式：跳过人工审阅", novel.novel_id)
                novel.current_stage = NovelStage.ACT_PLANNING
                host._save_novel_state(novel)
                return
            logger.debug("[%s] 等待人工审阅", novel.novel_id)
            return

        if not host._has_execution_ownership(novel.novel_id):
            return
        host._merge_autopilot_status_from_db(novel)
        if novel.autopilot_status == AutopilotStatus.RUNNING and operation_succeeded:
            if host.circuit_breaker:
                host.circuit_breaker.record_success()
            novel.consecutive_error_count = 0
        elif novel.autopilot_status == AutopilotStatus.RUNNING and not operation_neutral:
            if host.circuit_breaker:
                host.circuit_breaker.record_failure()
            novel.consecutive_error_count = (novel.consecutive_error_count or 0) + 1
            if novel.consecutive_error_count >= 3:
                logger.error(
                    "[%s] 写作连续失败 %s 次，挂起等待急救",
                    novel.novel_id,
                    novel.consecutive_error_count,
                )
                novel.autopilot_status = AutopilotStatus.ERROR
            else:
                logger.warning(
                    "[%s] 写作连续失败 %s/3 次",
                    novel.novel_id,
                    novel.consecutive_error_count,
                )
        else:
            logger.info("[%s] 本轮结束（用户已停止，不再计成功/重置熔断）", novel.novel_id)
        host._save_novel_state(novel)
        logger.debug("[%s] 状态已保存", novel.novel_id)

    except Exception as e:
        logger.error("[%s] 处理失败: %s", novel.novel_id, e, exc_info=True)

        if not host._has_execution_ownership(novel.novel_id):
            return

        if _is_novel_deleted(host, novel):
            logger.warning("[%s] 小说已被删除，放弃本轮处理（不累计错误）", novel.novel_id)
            return

        host._merge_autopilot_status_from_db(novel)
        if novel.autopilot_status != AutopilotStatus.RUNNING:
            logger.info("[%s] 处理异常但用户已停止，不累计熔断/失败次数", novel.novel_id)
            host._save_novel_state(novel)
            return

        if host.circuit_breaker:
            host.circuit_breaker.record_failure()
        novel.consecutive_error_count = (novel.consecutive_error_count or 0) + 1

        if novel.consecutive_error_count >= 3:
            logger.error("[%s] 连续失败 %s 次，挂起等待急救", novel.novel_id, novel.consecutive_error_count)
            novel.autopilot_status = AutopilotStatus.ERROR
        else:
            logger.warning("[%s] 连续失败 %s/3 次", novel.novel_id, novel.consecutive_error_count)
        host._save_novel_state(novel)
    finally:
        host._end_novel_execution(novel.novel_id)
