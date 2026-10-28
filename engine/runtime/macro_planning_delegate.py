"""宏观规划委托 — Phase 5 从 AutopilotDaemon 迁入 engine/runtime"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from domain.novel.entities.novel import AutopilotStatus, Novel, NovelStage

logger = logging.getLogger(__name__)


def _read_shared_state(novel_id: str) -> dict[str, Any]:
    from application.ai_invocation.autopilot.shared_state import read_autopilot_shared_state

    return read_autopilot_shared_state(novel_id)


def _consume_pending_macro_plan(host: Any, *, novel_id: str, target_chapters: int) -> dict[str, Any] | None:
    shared = _read_shared_state(novel_id)
    pending_target = int(shared.get("autopilot_pending_macro_target_chapters") or 0)
    pending = shared.get("autopilot_pending_macro_plan")
    if pending_target != int(target_chapters or 0) or not isinstance(pending, dict):
        return None
    host._update_shared_state(
        novel_id,
        active_invocation_session_id="",
        active_invocation_operation="",
        active_invocation_node_key="",
        active_invocation_status="completed",
        active_invocation_policy="",
        has_active_invocation=False,
        requires_ai_review=False,
        autopilot_pause_reason="",
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · 写入结构树",
    )
    return dict(pending)


def _durable_recovered_macro_plan(host: Any, novel_id: str, target_chapters: int):
    """Reuse a manually committed result after an explicit managed restart."""
    from infrastructure.persistence.database.connection import DatabaseConnection
    db = getattr(getattr(host,'novel_repository',None),'db',None)
    if not isinstance(db,DatabaseConnection):
        return None
    session = db.fetch_one("""SELECT * FROM ai_invocation_sessions WHERE operation='autopilot.macro.plan'
        AND ((CASE WHEN json_valid(context_json) THEN json_extract(context_json,'$.novel_id') END)=?
          OR (CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json,'$.novel_id') END)=?)
        ORDER BY created_at DESC,rowid DESC LIMIT 1""",(novel_id,novel_id))
    if not session or session['status']!='completed':
        return None
    metadata=json.loads(session['metadata_json'] or '{}')
    if not metadata.get('manual_stop_recovery') or metadata.get('macro_plan_applied'):
        return None
    commit=db.fetch_one("""SELECT c.*,d.accepted_content,a.status AS attempt_status FROM ai_adoption_commits c
        JOIN ai_adoption_decisions d ON d.id=c.decision_id AND d.session_id=c.session_id
        JOIN ai_invocation_attempts a ON a.id=d.attempt_id AND a.session_id=c.session_id
        WHERE c.session_id=? AND c.status='succeeded' AND d.decision='accepted'
        ORDER BY c.created_at DESC,c.rowid DESC LIMIT 1""",(session['id'],))
    if not commit or commit['attempt_status']!='succeeded':
        raise ValueError('恢复的结构缺少成功调用和采纳提交证据，请检查 AI 请求记录')
    continuation=json.loads(commit['result_json']).get('continuation') or {}
    from application.ai_invocation.autopilot.continuations import _load_json_value
    accepted=_load_json_value(commit['accepted_content'],'恢复结构不是有效 JSON')
    parts=accepted if isinstance(accepted,list) else accepted.get('parts') or accepted.get('structure') if isinstance(accepted,dict) else None
    result=continuation.get('macro_plan')
    if (not continuation.get('deferred_apply') or not isinstance(result,dict)
            or not result.get('success') or result.get('structure')!=parts):
        raise ValueError('恢复的结构与已采纳结果不一致，未重新调用模型')
    if int(continuation.get('target_chapters') or 0)!=int(target_chapters):
        raise ValueError('目标章节数已变化，请先核对已恢复的结构，未重新调用模型')
    return {'db':db,'session_id':session['id'],'result':result}


def _mark_recovered_macro_applied(recovered):
    if not recovered:
        return
    db=recovered['db']
    db.execute("""UPDATE ai_invocation_sessions SET metadata_json=json_set(metadata_json,'$.macro_plan_applied',json('true')),
        updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='completed'""",(recovered['session_id'],))
    db.commit()


def _write_macro_variables(*, novel_id: str, variables: Mapping[str, Any], node_key: str) -> None:
    from application.ai_invocation.variable_hub import VariableWrite
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository

    repo = SqliteVariableHubRepository(get_database())
    context_key = f"novel_id:{novel_id}"
    integer_aliases = {
        "target_chapters",
        "rec_parts",
        "rec_volumes_per_part",
        "rec_acts_per_volume",
        "rec_chapters_per_act",
        "total_recommended_acts",
    }
    variable_keys = {
        "planning_depth": "novel.planning.macro.depth",
        "rec_parts": "novel.planning.macro.rec_parts",
        "rec_volumes_per_part": "novel.planning.macro.rec_volumes_per_part",
        "rec_acts_per_volume": "novel.planning.macro.rec_acts_per_volume",
        "rec_chapters_per_act": "novel.planning.macro.rec_chapters_per_act",
        "total_recommended_acts": "novel.planning.macro.total_recommended_acts",
    }
    for alias, value in dict(variables or {}).items():
        if alias not in variable_keys:
            continue
        repo.set_value(
            VariableWrite(
                key=variable_keys[alias],
                value=value,
                context_key=context_key,
                source_node_key=node_key,
                source_trace_id=node_key,
                scope="novel",
                stage="planning",
                display_name=alias,
                value_type="integer" if alias in integer_aliases else "string",
            )
        )


def _backfill_autopilot_setup_variables(novel_id: str) -> None:
    """Materialize persisted setup and Bible facts before resolving DAG inputs."""
    from application.ai_invocation.variable_backfill import VariableHubBackfillService
    from application.paths import get_db_path
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
    from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
    from infrastructure.persistence.database.worldbuilding_repository import WorldbuildingRepository
    from interfaces.api.dependencies import get_bible_repository

    result = VariableHubBackfillService(
        variable_hub_repository=SqliteVariableHubRepository(get_database()),
        novel_repository=SqliteNovelRepository(get_database()),
        bible_repository=get_bible_repository(),
        worldbuilding_repository=WorldbuildingRepository(get_db_path()),
    ).backfill_novel(novel_id)
    if result.errors:
        raise RuntimeError("托管创作设定回填失败: " + "; ".join(result.errors))


def _pause_for_invocation(host: Any, novel: Novel, outcome) -> None:
    session = outcome.payload.get("session") if isinstance(outcome.payload, Mapping) else None
    policy_value = getattr(getattr(session, "policy", ""), "value", "") or "AUTOPILOT_PAUSE"
    host._update_shared_state(
        novel.novel_id.value,
        active_invocation_session_id=outcome.session_id,
        active_invocation_operation=outcome.operation,
        active_invocation_node_key=outcome.node_key,
        active_invocation_status=outcome.status,
        active_invocation_policy=policy_value,
        has_active_invocation=True,
        requires_ai_review=True,
        autopilot_pause_reason=outcome.autopilot_pause_reason or "awaiting_ai_review",
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · AI 请求面板",
    )
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    host._flush_novel(novel)


async def _request_macro_invocation(host: Any, novel: Novel, *, target_chapters: int) -> Any:
    from application.ai_invocation.autopilot.factory import get_or_create_autopilot_orchestrator
    from application.ai_invocation.autopilot.intents import AutopilotInvocationIntent
    from application.ai_invocation.autopilot.policy import AutopilotInvocationPolicyResolver
    from application.ai_invocation.contracts import ensure_invocation_contract
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import PLANNING_QUICK_MACRO
    from infrastructure.persistence.database.connection import get_database

    # The HTTP invocation route backfills legacy/setup facts before resolving
    # variables. Managed writing enters the same contract directly from the
    # daemon, so it must perform the identical idempotent backfill here. Without
    # this, a saved Bible character is visible to the planning service but the
    # Variable Hub still reports the required `characters` input as missing.
    try:
        _backfill_autopilot_setup_variables(novel.novel_id.value)
    except Exception:
        logger.exception("[%s] 托管宏观规划变量回填失败", novel.novel_id.value)
        raise

    bible_context = host.planning_service._get_bible_context(novel.novel_id.value)
    variables = host.planning_service.build_quick_macro_variables(bible_context, target_chapters)
    runtime_variables = {
        alias: variables[alias]
        for alias in (
            "planning_depth",
            "rec_parts",
            "rec_volumes_per_part",
            "rec_acts_per_volume",
            "rec_chapters_per_act",
            "total_recommended_acts",
        )
        if alias in variables
    }
    ensure_invocation_contract("autopilot.macro.plan", PLANNING_QUICK_MACRO, get_database())
    _write_macro_variables(
        novel_id=novel.novel_id.value,
        variables=runtime_variables,
        node_key=PLANNING_QUICK_MACRO,
    )
    policy = AutopilotInvocationPolicyResolver().resolve(
        operation="autopilot.macro.plan",
        node_key=PLANNING_QUICK_MACRO,
        novel=novel,
        context={"novel_id": novel.novel_id.value, "target_chapters": target_chapters},
    )
    config = generation_config_from_profile("planning_macro")
    return await get_or_create_autopilot_orchestrator(host).request(
        AutopilotInvocationIntent(
            novel_id=novel.novel_id.value,
            stage="planning",
            operation="autopilot.macro.plan",
            node_key=PLANNING_QUICK_MACRO,
            context={"novel_id": novel.novel_id.value, "target_chapters": target_chapters},
            explicit_variables={},
            continuation_handler_key="autopilot_macro_plan",
            policy_hint=policy,
            metadata={"source": "macro_planning_delegate"},
            config=config,
        )
    )


async def run_macro_planning(host: Any, novel: Novel) -> None:
    """处理宏观规划（规划部/卷/幕）- 使用极速模式让 AI 自主推断结构"""
    if not host._is_still_running(novel):
        return

    host._update_shared_state(
        novel.novel_id.value,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划",
        macro_structure_ready=False,
    )

    target_chapters = novel.target_chapters or 30

    logger.info(
        "[%s] macro_planning start target_chapters=%s",
        novel.novel_id.value,
        target_chapters,
    )

    recovered = await asyncio.to_thread(_durable_recovered_macro_plan,host,novel.novel_id.value,target_chapters)
    result = recovered['result'] if recovered else _consume_pending_macro_plan(
        host,
        novel_id=novel.novel_id.value,
        target_chapters=target_chapters,
    )
    if result is None:
        shared_state = _read_shared_state(novel.novel_id.value)
        if shared_state.get("active_invocation_session_id") and shared_state.get("has_active_invocation"):
            logger.info(
                "[%s] 宏观规划已有待处理 invocation session=%s，等待面板处理",
                novel.novel_id.value,
                shared_state.get("active_invocation_session_id"),
            )
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            novel.autopilot_status = AutopilotStatus.RUNNING
            host._update_shared_state(
                novel.novel_id.value,
                macro_structure_ready=False,
                writing_substep="macro_planning",
                writing_substep_label="宏观规划 · AI 请求面板",
            )
            host._flush_novel(novel)
            return
        outcome = await _request_macro_invocation(host, novel, target_chapters=target_chapters)
        if outcome.status in {
            "awaiting_pre_call_review",
            "awaiting_acceptance",
            "awaiting_commit",
            "blocked",
            "failed",
            "cancelled",
        }:
            _pause_for_invocation(host, novel, outcome)
            return
        commit = outcome.payload.get("commit") if isinstance(outcome.payload, Mapping) else None
        commit_result = getattr(commit, "result", None) if commit is not None else None
        continuation = commit_result.get("continuation") if isinstance(commit_result, Mapping) else None
        result = dict((continuation or {}).get("macro_plan") or {})

    ok = bool(result.get("success"))
    n_parts = len(result.get("structure") or []) if isinstance(result.get("structure"), list) else -1
    logger.info(
        "[%s] macro_planning generate_macro_plan returned success=%s parts=%s",
        novel.novel_id.value,
        ok,
        n_parts,
    )

    if not host._is_still_running(novel):
        logger.info("[%s] 宏观规划 LLM 返回后检测到停止，不再落库", novel.novel_id)
        return

    from application.engine.services import continuation_proposal_service as continuation
    proposal = continuation.active(novel.novel_id.value)
    if proposal:
        if not ok:
            raise ValueError("续写宏观规划未成功，未生成可确认结构")
        await asyncio.to_thread(continuation.prepare_structure, proposal["proposal_id"],
            novel.novel_id.value, result.get("structure"), planning_service=host.planning_service)
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        host._update_shared_state(novel.novel_id.value, macro_structure_ready=False,
            requires_structure_review=True, continuation_proposal_id=proposal["proposal_id"],
            writing_substep="macro_planning", writing_substep_label="续写宏观规划 · 等待作者确认结构预览")
        return

    await host.planning_service.apply_macro_plan_from_llm_result(
        result,
        novel_id=novel.novel_id.value,
        target_chapters=target_chapters,
        allow_minimal_placeholder_on_empty=False,
    )
    await asyncio.to_thread(_mark_recovered_macro_applied,recovered)
    host._update_shared_state(
        novel.novel_id.value,
        autopilot_pending_macro_plan=None,
        autopilot_pending_macro_target_chapters=None,
        macro_structure_ready=True,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · 结构已生成",
    )

    if getattr(novel, "auto_approve_mode", False):
        novel.current_stage = NovelStage.ACT_PLANNING
        host._flush_novel(novel)
        host._sync_storylines_to_shared_memory(novel.novel_id.value)
        logger.info("[%s] 全自动模式：宏观规划完成，直接进入幕级规划", novel.novel_id)
    else:
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        host._flush_novel(novel)
        host._sync_storylines_to_shared_memory(novel.novel_id.value)
        logger.info("[%s] 宏观规划完成，进入审阅等待", novel.novel_id)
