"""Execution 节点 — 执行与生成（4 个节点）

- exec_planning: 规划引擎
- exec_writer: 剧情引擎
- exec_beat: 节拍放大器
- exec_scene: 场景导演
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from application.engine.dag.models import (
    CPMSInjectionPoint,
    NodeCategory,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeStatus,
    PortDataType,
    PromptMode,
)
from application.engine.dag.registry import BaseNode, NodeRegistry
from infrastructure.ai.prompt_keys import (
    ANTI_AI_ALLOWLIST_EXPLAIN,
    ANTI_AI_BEHAVIOR_PROTOCOL,
    ANTI_AI_CHARACTER_STATE_LOCK,
    AUTOPILOT_STREAM_BEAT as _WORKFLOW_BEAT_NODE_KEY,
    CHAPTER_GENERATION_MAIN as _WORKFLOW_CHAPTER_GEN_NODE_KEY,
    MACRO_PLANNING,
    SCENE_DIRECTOR,
)

from application.workflows.prose_discipline import build_prose_discipline_block
from application.engine.services.beat_projection import planned_micro_beats_from_beats

logger = logging.getLogger(__name__)


@NodeRegistry.register("exec_commit")
class CommitChapterNode(BaseNode):
    """Only the scoped production adapter may mutate chapter content."""
    meta = NodeMeta(node_type="exec_commit", display_name="章节提交确认", category=NodeCategory.EXECUTION,
        input_ports=[NodePort(name="content", data_type=PortDataType.TEXT, required=True)],
        output_ports=[NodePort(name="chapter_committed", data_type=PortDataType.BOOLEAN),
                      NodePort(name="saved_content_hash", data_type=PortDataType.TEXT)],
        can_disable=False, is_configurable=False, default_timeout_seconds=120,
        description="审计通过后 CAS 提交正文，等待写库确认并核对正文哈希")

    async def execute(self, inputs, context):
        return NodeResult(status=NodeStatus.ERROR, error="章节提交需要运行器注入持久化服务")

    def validate_inputs(self, inputs):
        return bool(inputs.get("content"))


def _dag_context_builder():
    """尽力获取与 API 相同的 ContextBuilder（节拍放大依赖完整依赖链）。"""
    try:
        from interfaces.api.dependencies import get_context_builder

        return get_context_builder()
    except Exception as e:
        logger.warning("DAG exec_beat: ContextBuilder 不可用: %s", e)
        return None


# ─── exec_planning: 规划引擎 ───


@NodeRegistry.register("exec_planning")
class PlanningNode(BaseNode):
    """规划引擎 — PlanningService.generate_macro_plan"""

    meta = NodeMeta(
        node_type="exec_planning",
        display_name="规划引擎",
        category=NodeCategory.EXECUTION,
        icon="",
        color="#3b82f6",
        input_ports=[
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=True),
            NodePort(name="target_chapters", data_type=PortDataType.SCORE, required=False),
        ],
        output_ports=[
            NodePort(name="macro_plan", data_type=PortDataType.TEXT),
            NodePort(name="act_plan", data_type=PortDataType.TEXT),
        ],
        prompt_variables=["novel_id", "target_chapters"],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=120,
        cpms_node_key=MACRO_PLANNING,
        description="PlanningService.generate_macro_plan",
        default_edges=["exec_beat"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        novel_id = inputs.get("novel_id") or context.get("novel_id", "")

        try:
            macro_plan = ""
            act_plan = ""

            try:
                from application.blueprint.services.continuous_planning_service import ContinuousPlanningService
                from infrastructure.persistence.database.connection import get_database
                db = get_database()
                svc = ContinuousPlanningService(db)
                result = await svc.generate_macro_plan(novel_id)
                if result:
                    macro_plan = getattr(result, "plan_text", "") or str(result)
            except Exception as e:
                raise RuntimeError(f"PlanningService 调用失败: {e}") from e

            return NodeResult(
                outputs={"macro_plan": macro_plan, "act_plan": act_plan},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── exec_writer: 剧情引擎 ───

@NodeRegistry.register("exec_writer")
class WriterNode(BaseNode):
    """剧情引擎 — AutoNovelGenerationWorkflow.generate_chapter_stream

    优化点：
    1. 复用 CPMS 提示词注册表（chapter-generation-main），与主工作流保持一致
    2. 分节拍生成时使用 autopilot-stream-beat 模板，避免冗余系统指令
    3. 无节拍时使用精简版 system prompt，减少 token 开销
    """

    meta = NodeMeta(
        node_type="exec_writer",
        display_name="剧情引擎",
        category=NodeCategory.EXECUTION,
        icon="",
        color="#ef4444",
        input_ports=[
            NodePort(name="context", data_type=PortDataType.TEXT, required=False),
            NodePort(name="outline", data_type=PortDataType.TEXT, required=False),
            NodePort(name="voice_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="beats", data_type=PortDataType.LIST, required=False),
            NodePort(name="foreshadowing_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="debt_due_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="fact_lock", data_type=PortDataType.TEXT, required=False),
            # Anti-AI 子注入点变量槽
            NodePort(name="behavior_protocol", data_type=PortDataType.TEXT, required=False),
            NodePort(name="character_state_lock", data_type=PortDataType.TEXT, required=False),
            NodePort(name="allowlist_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="refresh_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="finale_block", data_type=PortDataType.TEXT, required=False),
            NodePort(name="nervous_habits", data_type=PortDataType.TEXT, required=False),
        ],
        output_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT),
            NodePort(name="word_count", data_type=PortDataType.SCORE),
        ],
        prompt_variables=["context", "outline", "voice_block", "fact_lock", "foreshadowing_block", "behavior_protocol", "character_state_lock", "allowlist_block", "refresh_block", "finale_block", "nervous_habits"],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=300,
        cpms_node_key=_WORKFLOW_CHAPTER_GEN_NODE_KEY,
        # CPMS 子提示词自动注入（Anti-AI 层）
        cpms_sub_keys=[
            CPMSInjectionPoint(cpms_node_key=ANTI_AI_BEHAVIOR_PROTOCOL, target_variable="behavior_protocol", description="Anti-AI 行为协议 P1-P5+R1-R8"),
            CPMSInjectionPoint(cpms_node_key=ANTI_AI_CHARACTER_STATE_LOCK, target_variable="character_state_lock", description="角色状态锁 L4"),
            CPMSInjectionPoint(cpms_node_key=ANTI_AI_ALLOWLIST_EXPLAIN, target_variable="allowlist_block", description="场景化白名单 L3"),
        ],
        prompt_mode=PromptMode.CPMS_FIRST,
        description="AutoNovelGenerationWorkflow — Anti-AI 协议化章节生成；全托管时由 ChapterConductor（铺陈/收束/着陆）控节拍，不做事后硬截断",
        default_edges=["val_style", "val_tension", "val_anti_ai"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            content = ""
            word_count = 0
            novel_id = context.get("novel_id", "")

            # 收集上下文变量
            beats = inputs.get("beats", []) or []
            variables = {
                "context": inputs.get("context", ""),
                "outline": inputs.get("outline", ""),
                "voice_block": inputs.get("voice_block", ""),
                "fact_lock": inputs.get("fact_lock", ""),
                "foreshadowing_block": inputs.get("foreshadowing_block", ""),
                "debt_due_block": inputs.get("debt_due_block", ""),
                "planning_section": "",
                "genre_profile_block": context.get("shared_state", {}).get("genre_profile_block", ""),
                "world_rules": context.get("shared_state", {}).get("world_rules", ""),
                # Anti-AI 子注入变量（优先使用上游传来的，缺失时由 cpms_sub_keys 自动拉取）
                "behavior_protocol": inputs.get("behavior_protocol", ""),
                "character_state_lock": inputs.get("character_state_lock", ""),
                "allowlist_block": inputs.get("allowlist_block", ""),
                "refresh_block": inputs.get("refresh_block", ""),
                "finale_block": inputs.get("finale_block", ""),
                "nervous_habits": inputs.get("nervous_habits", ""),
                "beat_extra": "",
                "beat_section": "",
                "prose_discipline": build_prose_discipline_block(
                    beat_mode=bool(beats),
                    beat_target_words=None,
                ),
            }
            variables["context"] = "\n\n".join(filter(None, [variables["world_rules"], variables["context"], variables["fact_lock"], variables["foreshadowing_block"], variables["debt_due_block"]]))
            if beats:
                import json
                variables["beat_section"] = "本章节拍计划（按顺序完成）：\n" + json.dumps(beats, ensure_ascii=False)

            # 使用 resolve_prompt 统一获取提示词（自动走 CPMS → Config → Meta + 子注入）
            resolved = self.resolve_prompt(variables)
            for key, fragment in resolved.get("injections", {}).items():
                if not variables.get(key):
                    variables[key] = fragment

            # Custom CPMS templates may omit injection slots. Always send each
            # available upstream fragment and record its exact digest; a node
            # that only returned text cannot claim the writer consumed it.
            import hashlib
            injection_evidence = []
            for key in ("behavior_protocol", "character_state_lock", "allowlist_block", "refresh_block", "finale_block"):
                fragment = variables.get(key)
                if not isinstance(fragment, str) or not fragment.strip():
                    continue
                fragment_hash = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
                source = context.get("shared_state", {}).get("_dag_injection_receipts", {}).get(key)
                if source and source.get("fragment_hash") != fragment_hash:
                    raise RuntimeError(f"Anti-AI 上游片段回执与 {key} 不匹配")
                if fragment not in resolved["system"] and fragment not in resolved["user"]:
                    resolved["system"] += f"\n\n【DAG 去味注入：{key}】\n{fragment}"
                injection_evidence.append({"variable": key, "fragment_hash": fragment_hash,
                    "source": source or {"source": "cpms_fallback" if key in resolved.get("injections", {}) else "writer_input"},
                    "applied": True, "mode": "prompt_pre_generation"})
            # The final project profile applies even to an edited CPMS template.
            profile_block = variables.get("genre_profile_block", "")
            if profile_block and profile_block not in resolved["system"] and profile_block not in resolved["user"]:
                resolved["system"] += "\n\n" + profile_block
            beat_section = variables.get("beat_section", "")
            if beat_section and beat_section not in resolved["system"] and beat_section not in resolved["user"]:
                resolved["user"] += "\n\n" + beat_section

            # 调用 LLM 生成
            try:
                from application.engine.dag.registry import resolve_llm_service, generation_text
                from domain.ai.value_objects.prompt import Prompt
                from domain.ai.services.llm_service import GenerationConfig

                llm = resolve_llm_service(context)
                system_prompt = resolved["system"]
                user_prompt = resolved["user"] or "请开始写作"

                prompt = Prompt(system=system_prompt, user=user_prompt)

                # 根据是否有节拍，调整生成参数
                config = GenerationConfig()
                if beats and len(beats) > 0:
                    config = GenerationConfig(
                        max_tokens=2000,
                        temperature=0.85,
                    )
                else:
                    config = GenerationConfig(
                        max_tokens=4000,
                        temperature=0.80,
                    )

                # 应用用户配置覆盖
                if self._config:
                    if self._config.model_override:
                        config.model = self._config.model_override
                    if self._config.temperature is not None:
                        config.temperature = self._config.temperature
                    if self._config.max_tokens is not None:
                        config.max_tokens = self._config.max_tokens

                from application.engine.rules.prose_prompt_hooks import generate_direct_prose
                services = context.get("services") or context.get("shared_state", {}).get("services")
                state = context.get("shared_state", {})
                prompt_evidence = {}
                result = await generate_direct_prose(llm, prompt, config, db=getattr(services, "db", None),
                    context={"novel_id": novel_id or state.get("novel_id", ""), "chapter_number": state.get("chapter_number"),
                        "scene_type": state.get("scene_type"), "job_id": state.get("dag_run_id", ""),
                        "dag_injections": injection_evidence}, evidence_sink=prompt_evidence)
                content = generation_text(result)
                if (prompt_evidence.get("status") != "completed"
                        or prompt_evidence.get("content_hash") != hashlib.sha256(content.encode("utf-8")).hexdigest()
                        or not prompt_evidence.get("prompt_hash")):
                    raise RuntimeError("正文生成缺少匹配的去味提示词执行回执")
                if getattr(services, "db", None) is not None and prompt_evidence.get("persistence_status") != "committed":
                    raise RuntimeError("去味提示词执行回执未持久化，正文不能进入提交流程")
                word_count = len(content)
            except Exception as e:
                raise RuntimeError(f"LLM 调用失败: {e}") from e

            return NodeResult(
                outputs={"content": content, "word_count": word_count, "anti_ai_prompt_evidence": prompt_evidence},
                status=NodeStatus.SUCCESS,
                metrics={"word_count": float(word_count)},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── exec_beat: 节拍放大器 ───


@NodeRegistry.register("exec_beat")
class BeatNode(BaseNode):
    """节拍放大器 — 始终先获得 ``ChapterExecutionPlan``，再由 ContextBuilder 投影为 runtime beats。"""

    meta = NodeMeta(
        node_type="exec_beat",
        display_name="节拍放大器",
        category=NodeCategory.EXECUTION,
        icon="",
        color="#14b8a6",
        input_ports=[
            NodePort(name="outline", data_type=PortDataType.TEXT, required=True),
            NodePort(name="scene_analysis", data_type=PortDataType.JSON, required=False,
                     default=None, description="来自场景导演的场记与 ATG；参与节拍地点及生成约束"),
            NodePort(
                name="chapter_plan_json",
                data_type=PortDataType.JSON,
                required=False,
                default=None,
                description="来自 planning_outline_partition（plan_outline）的标准 ChapterExecutionPlan JSON",
            ),
            NodePort(
                name="target_chapter_words",
                data_type=PortDataType.SCORE,
                required=False,
                default=2500,
            ),
            NodePort(
                name="beat_sheet_json",
                data_type=PortDataType.JSON,
                required=False,
                default=None,
                description="可选 BeatSheet JSON；仅作为 ChapterExecutionPlan 构建输入，不直接投影为 runtime beats",
            ),
        ],
        output_ports=[
            NodePort(name="beats", data_type=PortDataType.LIST),
        ],
        prompt_variables=["outline"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        cpms_node_key=_WORKFLOW_BEAT_NODE_KEY,
        description="承接 plan_outline 的 chapter_plan_json；缺失时构建 ChapterExecutionPlan，再统一投影为 beats",
        default_edges=["exec_writer"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time

        start = time.time()

        try:
            beats_out: List[Dict[str, Any]] = []
            outline = str(inputs.get("outline", "") or "")
            from application.engine.services.beat_projection import (
                apply_scene_director_to_beats, scene_director_for_beats,
            )
            scene_director = scene_director_for_beats(inputs.get("scene_analysis"))

            chap_raw = context.get("chapter_number") or inputs.get("chapter_number") or 1
            try:
                chap = int(chap_raw)
            except (TypeError, ValueError):
                chap = 1
            tw_raw = inputs.get("target_chapter_words") or context.get("target_chapter_words") or 2500
            try:
                tw = int(tw_raw)
            except (TypeError, ValueError):
                tw = 2500
            nid = context.get("novel_id") if isinstance(context, dict) else None

            sheet = inputs.get("beat_sheet_json")
            beat_sheet_json: Optional[Dict[str, Any]] = None
            if isinstance(sheet, dict):
                beat_sheet_json = sheet if sheet else None
            elif isinstance(sheet, str) and sheet.strip():
                try:
                    import json

                    beat_sheet_json = json.loads(sheet)
                except json.JSONDecodeError:
                    beat_sheet_json = None
                    logger.warning("exec_beat: beat_sheet_json 解析失败，已忽略")

            raw_plan = inputs.get("chapter_plan_json")
            chapter_plan = None
            if isinstance(raw_plan, dict) and raw_plan.get("atoms"):
                try:
                    from application.engine.dag.plan.schema import ChapterExecutionPlan

                    chapter_plan = ChapterExecutionPlan.model_validate(raw_plan)
                except Exception as e:
                    logger.warning("exec_beat: chapter_plan_json 无效，将重新规划: %s", e)

            try:
                from application.engine.dag.plan.outline_beat_planner import (
                    build_chapter_execution_plan_async,
                    build_chapter_execution_plan_sync,
                )

                builder = _dag_context_builder()
                if builder:
                    if not (chapter_plan is not None and bool(chapter_plan.atoms)):
                        try:
                            chapter_plan = await build_chapter_execution_plan_async(
                                outline,
                                target_chapter_words=tw,
                                novel_id=str(nid) if nid else None,
                                chapter_number=chap,
                                beat_sheet_json=beat_sheet_json,
                                use_llm=True,
                            )
                        except Exception as plan_err:
                            logger.warning("章前执行计划（exec_beat）异步构建失败，转同步 ChapterExecutionPlan：%s", plan_err)
                            chapter_plan = build_chapter_execution_plan_sync(
                                outline,
                                target_chapter_words=tw,
                                novel_id=str(nid) if nid else None,
                                chapter_number=chap,
                                beat_sheet_json=beat_sheet_json,
                                decomposition_label="dag_exec_beat_sync_fallback",
                            )

                    beats_raw = builder.magnify_outline_to_beats(
                        chap,
                        outline,
                        target_chapter_words=tw,
                        chapter_execution_plan=chapter_plan,
                        beat_sheet=None,
                        scene_director=scene_director,
                    )
                    apply_scene_director_to_beats(beats_raw, scene_director)
                    beats_out = planned_micro_beats_from_beats(beats_raw)
                elif outline:
                    raise RuntimeError("exec_beat: ContextBuilder 不可用，无法从 ChapterExecutionPlan 投影 beats")
            except Exception as e:
                raise RuntimeError(f"ContextBuilder.magnify_outline_to_beats 调用失败: {e}") from e

            return NodeResult(
                outputs={"beats": beats_out},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"beats": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── exec_scene: 场景导演 ───


@NodeRegistry.register("exec_scene")
class SceneNode(BaseNode):
    """场景导演 — SceneDirectorService"""

    meta = NodeMeta(
        node_type="exec_scene",
        display_name="场景导演",
        category=NodeCategory.EXECUTION,
        icon="",
        color="#a855f7",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=False),
            NodePort(name="outline", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="scene_analysis", data_type=PortDataType.JSON),
        ],
        prompt_variables=["outline"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        cpms_node_key=SCENE_DIRECTOR,
        description="SceneDirectorService 场景分析",
        default_edges=["exec_beat"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            from application.engine.services.scene_director_service import SceneDirectorService
            from application.engine.services.beat_projection import scene_director_for_beats
            from application.engine.dag.registry import resolve_llm_service

            if not self.validate_inputs(inputs):
                raise ValueError("场景导演需要非空的 outline")
            state = context.get("shared_state") or {}
            chapter_number = context.get("chapter_number", state.get("chapter_number"))
            if isinstance(chapter_number, str) and chapter_number.strip().isdigit():
                chapter_number = int(chapter_number.strip())
            if isinstance(chapter_number, bool) or not isinstance(chapter_number, int) or chapter_number < 1:
                raise ValueError("场景导演需要有效的 chapter_number（正整数）")

            svc = context.get("scene_director_service") or state.get("scene_director_service")
            if svc is None:
                svc = SceneDirectorService(llm_service=resolve_llm_service(context))
            result = await svc.analyze(chapter_number=chapter_number, outline=inputs["outline"].strip())
            # The tolerant parser returns defaults on invalid JSON. Empty lists,
            # whitespace strings and empty ATG objects are equally inconclusive.
            result = scene_director_for_beats(result)
            if result is None:
                raise ValueError("场景导演未返回有效分析，不能继续节拍生成")
            scene_analysis = result.model_dump(mode="json")

            return NodeResult(
                outputs={"scene_analysis": scene_analysis},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"scene_analysis": {}}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=f"SceneDirectorService 调用失败: {e}")

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        outline = inputs.get("outline")
        return isinstance(outline, str) and bool(outline.strip())
