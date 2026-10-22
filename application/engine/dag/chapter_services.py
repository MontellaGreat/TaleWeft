"""Production DAG adapters to the application's existing chapter contracts.

Repositories/providers are injected as a single run scope. No adapter constructs
an abstract repository, resolves an unrelated global database, or writes a
candidate chapter before the commit gate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from application.engine.dag.models import NodeResult, NodeStatus
from application.engine.dag.registry import generation_text
from application.engine.dag.plan.outline_beat_planner import build_chapter_execution_plan_async
from application.engine.dag.plan.schema import ChapterExecutionPlan
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.novel_id import NovelId


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class DAGChapterServices:
    SUPPORTED = {"ctx_blueprint", "ctx_memory", "ctx_voice", "ctx_foreshadow", "ctx_debt", "exec_beat",
                 "planning_outline_partition", "val_style", "val_tension", "gw_retry", "exec_commit",
                 "val_narrative", "val_foreshadow", "val_kg_infer", "gw_review", "exec_writer"}

    def __init__(self, db, llm, *, aftermath=None, dag=None, validator=None):
        self.db, self.llm, self.aftermath, self.dag = db, llm, aftermath, dag
        from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
        from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
        from infrastructure.persistence.database.sqlite_narrative_debt_repository import SqliteNarrativeDebtRepository
        from infrastructure.persistence.database.sqlite_foreshadowing_repository import SqliteForeshadowingRepository
        from infrastructure.persistence.database.sqlite_voice_fingerprint_repository import SQLiteVoiceFingerprintRepository
        self.chapters, self.bibles = SqliteChapterRepository(db), SqliteBibleRepository(db)
        self.debts, self.foreshadows = SqliteNarrativeDebtRepository(db), SqliteForeshadowingRepository(db)
        self.fingerprints = SQLiteVoiceFingerprintRepository(db)
        from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
        self.candidates = ChapterRewriteCandidates(db)
        self.candidate_id = None
        from engine.runtime.policy_validator import PolicyValidator
        self.validator = validator or PolicyValidator()

    def handles(self, node_type):
        return node_type in self.SUPPORTED

    async def execute_node(self, node, inputs, context):
        kind, state = node.meta.node_type, context["shared_state"]
        novel_id, number = state["novel_id"], state["chapter_number"]
        if kind.startswith("ctx_") or kind in ("exec_writer", "planning_outline_partition"):
            from application.core.services.chapter_derivation_readiness import assert_previous_derivations_ready
            await asyncio.to_thread(assert_previous_derivations_ready, self.db, novel_id, number)
        if kind == "exec_writer":
            from application.engine.rules.prose_prompt_hooks import load_verified_market_profile
            snapshot, profile_block = await asyncio.to_thread(load_verified_market_profile, self.db, novel_id)
            state["market_profile_snapshot"], state["genre_profile_block"] = snapshot, profile_block
            if state.get("resume_content"):
                result = NodeResult(outputs={"content": state["resume_content"], "word_count": len(state["resume_content"]), "resumed_candidate": True,
                    "anti_ai_prompt_evidence": {"status": "reused_candidate", "applied": False,
                        "source_run_id": state.get("resumed_from", ""), "content_hash": content_hash(state["resume_content"]),
                        "original_prompt_evidence": state.get("resume_prompt_evidence"),
                        "reason": "恢复已有候选，本次没有调用正文生成模型；提交前重新执行完整审计"}})
            else:
                result = await node.execute(inputs, context)
            if result.status in (NodeStatus.SUCCESS, NodeStatus.WARNING):
                candidate = await asyncio.to_thread(self.candidates.save_candidate, run_id=state["dag_run_id"],
                    novel_id=novel_id, chapter_id=state["chapter_id"], chapter_number=number,
                    content=result.outputs["content"], kind="generated", attempt=0,
                    base_content=state["chapter_before"].get("content") or "", base_hash=content_hash(state["chapter_before"].get("content") or ""),
                    base_revision=state["chapter_before"].get("revision"))
                self.candidate_id = candidate["candidate_id"]
                result.outputs["candidate_id"] = self.candidate_id
            return result
        if kind.startswith("ctx_"):
            return await asyncio.to_thread(self._context, kind, novel_id, number, state)
        if kind == "planning_outline_partition":
            plan = await build_chapter_execution_plan_async(
                state["outline"], novel_id=novel_id, chapter_number=number,
                target_chapter_words=state["target_chapter_words"], llm_service=self.llm,
                use_llm=state.get("use_llm_planning", True),
            )
            if not plan.atoms:
                raise ValueError("章节计划为空")
            return NodeResult(outputs={"chapter_plan_json": plan.model_dump(mode="json")}, metrics={"atom_count": len(plan.atoms)})
        if kind == "exec_beat":
            from application.engine.services.beat_projection import (
                apply_scene_director_to_beats, beats_from_execution_plan, planned_micro_beats_from_beats,
            )
            plan = ChapterExecutionPlan.model_validate(state["chapter_plan_json"])
            beats = beats_from_execution_plan(plan, outline=state["outline"], target_chapter_words=state["target_chapter_words"],
                                              infer_focus=lambda _: "setup", build_expansion_hints=lambda *_: [])
            apply_scene_director_to_beats(beats, inputs.get("scene_analysis"))
            return NodeResult(outputs={"beats": planned_micro_beats_from_beats(beats)})
        if kind == "val_style":
            return await asyncio.to_thread(self._style, novel_id, state["content"], node)
        if kind == "val_tension":
            # Use the shared contract but require actual numeric scores. The legacy
            # scorer may turn parse failures into a neutral 50; a gate cannot do so.
            from application.ai.tension_scoring_contract import TensionScoringLlmPayload, tension_scoring_payload_to_domain
            from application.ai.llm_json_extract import parse_llm_json_to_dict
            resolved = node.resolve_prompt({"content": state["content"], "prev_tension": str(state.get("prev_tension", 50))})
            raw = generation_text(await self.llm.generate(Prompt(system=resolved["system"], user=resolved["user"]), self._config(node, 0.2)))
            data, errors = parse_llm_json_to_dict(raw)
            if data is None or any(not isinstance(data.get(k), (int, float)) or isinstance(data.get(k), bool)
                                   for k in ("plot_tension", "emotional_tension", "pacing_tension")):
                raise ValueError(f"张力评分未得到有效数值: {errors}")
            dims = tension_scoring_payload_to_domain(TensionScoringLlmPayload.model_validate(data))
            return NodeResult(outputs={"plot_tension": dims.plot_tension, "emotional_tension": dims.emotional_tension,
                                       "pacing_tension": dims.pacing_tension, "composite": dims.composite_score},
                              metrics={"tension_composite": dims.composite_score})
        if kind == "gw_retry":
            return await self._quality_gate(node, state)
        if kind == "exec_commit":
            if not state.get("quality_approved"):
                raise ValueError("章节尚未通过生成后的审计门控")
            # Shield ownership/commit against cancellation: a queued SQL mutation
            # may commit after a timeout. The owner waits/reconciles before release.
            mutation = asyncio.create_task(asyncio.to_thread(self.commit_chapter, state))
            try:
                result = await asyncio.shield(mutation)
            except asyncio.CancelledError:
                # Await the ACK before the task owner releases its lease. Keep
                # commit evidence observable even when wait_for cancels us.
                while True:
                    try:
                        result = await asyncio.shield(mutation)
                        break
                    except asyncio.CancelledError:
                        # Repeated stop/shutdown must not cancel the underlying
                        # thread ACK or permit a successor to acquire ownership.
                        continue
                state.update(result.outputs)
                self.commit_result = result
                if self.candidate_id:
                    await asyncio.to_thread(self.candidates.mark_committed, self.candidate_id, result.outputs["saved_content_hash"], result.outputs["saved_revision"])
                raise
            self.commit_result = result
            if self.candidate_id:
                await asyncio.to_thread(self.candidates.mark_committed, self.candidate_id, result.outputs["saved_content_hash"], result.outputs["saved_revision"])
            return result
        if kind == "val_narrative":
            if not state.get("chapter_committed"):
                raise ValueError("章后分析必须在章节提交确认后执行")
            pipeline = self.aftermath
            if pipeline is None:
                from application.engine.dag.aftermath_factory import build_scoped_chapter_aftermath
                pipeline = self.aftermath = await asyncio.to_thread(build_scoped_chapter_aftermath, self.db, self.llm)
            result = await pipeline.run_after_chapter_saved(novel_id, number, state["content"], chapter_micro_beats=state.get("beats"),
                expected_revision=state["saved_revision"], expected_content_hash=state["saved_content_hash"])
            if result.get("narrative_sync_ok") is not True:
                raise RuntimeError(result.get("narrative_sync_error") or "章节已保存，但叙事同步没有成功确认")
            if result.get("derivation_status") in {"failed", "superseded"}:
                raise RuntimeError(result.get("error") or "章节已保存，但派生结果失败或已过期")
            return NodeResult(outputs={"narrative_result": result, "summary": result.get("summary", ""),
                                       "events": [], "triples": [], "causal_edges": []})
        if kind == "val_foreshadow":
            registry = await asyncio.to_thread(self.foreshadows.get_by_novel_id, NovelId(novel_id))
            if registry is None:
                raise ValueError("小说不存在，无法验证伏笔")
            pending = len(registry.get_unresolved())
            total = len(registry.foreshadowings)
            return NodeResult(outputs={"recovered": total - pending, "pending": pending, "recovery_rate": (total - pending) * 100 / total if total else 0})
        if kind == "val_kg_infer":
            from application.world.services.knowledge_graph_service import KnowledgeGraphService
            from infrastructure.persistence.database.triple_repository import TripleRepository
            from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
            from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
            service = KnowledgeGraphService(TripleRepository(self.db), ChapterElementRepository(self.db.db_path), StoryNodeRepository(self.db))
            # KnowledgeGraphService takes a story structure node id, not a novel id.
            row = await asyncio.to_thread(self.db.fetch_one, "SELECT id FROM story_nodes WHERE novel_id=? AND node_type='chapter' AND number=? LIMIT 1", (novel_id, number))
            if not row:
                return NodeResult(status=NodeStatus.BYPASSED, outputs={"inferred_triples": []}, error="本章没有故事结构节点，未执行图谱推断")
            triples = await service.infer_from_chapter(row["id"])
            return NodeResult(outputs={"inferred_triples": [str(t.id) for t in triples]})
        if kind == "gw_review":
            return NodeResult(status=NodeStatus.WARNING, outputs={"approved": False, "review_required": True},
                              error="正文已提交为待审阅，等待用户审阅")
        raise ValueError(f"未实现生产适配器: {kind}")

    def _context(self, kind, novel_id, number, state):
        if kind == "ctx_blueprint":
            from application.world.services.narrative_contract_loader import load_merged_worldbuilding_slices
            from application.world.services.narrative_contract_text import build_ctx_blueprint_outputs
            from infrastructure.persistence.database.worldbuilding_repository import WorldbuildingRepository
            bible = self.bibles.get_by_novel_id(NovelId(novel_id))
            wb = WorldbuildingRepository(self.db.db_path).get_by_novel_id(novel_id)
            outputs = build_ctx_blueprint_outputs(bible=bible, worldbuilding=wb,
                                                  worldbuilding_slices=load_merged_worldbuilding_slices(bible=bible, worldbuilding=wb))
            return NodeResult(outputs=outputs, status=NodeStatus.SUCCESS if bible or wb else NodeStatus.WARNING)
        if kind == "ctx_memory":
            from application.engine.services.memory_engine import MemoryEngine
            from application.engine.services.context_assembler import ContextAssembler
            memory = MemoryEngine(self.llm, self.bibles, db_connection=self.db)
            assembler = ContextAssembler(chapter_repo=self.chapters, bible_repo=self.bibles, debt_repo=self.debts)
            fact = memory.build_fact_lock_section(novel_id, number)
            previous = assembler.build_previously_on(novel_id, number)
            return NodeResult(outputs={"fact_lock": fact, "entity_memory": memory.get_revealed_clues_section(novel_id),
                                       "context": "\n\n".join(filter(None, [previous, memory.get_completed_beats_section(novel_id)]))})
        if kind == "ctx_voice":
            from application.engine.services.style_constraint_builder import build_style_summary
            from application.engine.rules.character_state_vector import CharacterStateVectorManager
            fp = self.fingerprints.get_by_novel(novel_id)
            vectors = CharacterStateVectorManager()
            bible = self.bibles.get_by_novel_id(NovelId(novel_id))
            characters = getattr(bible, "characters", []) if bible else []
            for character in characters:
                raw = vars(character) if hasattr(character, "__dict__") else {}
                name = str(raw.get("name") or getattr(character, "name", ""))
                if name:
                    vectors.update_from_bible(name, raw)
            lock = "\n\n".join(vector.to_lock_text() for vector in vectors._vectors.values())
            return NodeResult(outputs={"voice_block": build_style_summary({"metrics": fp} if fp else None),
                                       "nervous_habits": "", "character_state_lock": lock},
                              status=NodeStatus.SUCCESS if fp or lock else NodeStatus.WARNING)
        if kind == "ctx_foreshadow":
            registry = self.foreshadows.get_by_novel_id(NovelId(novel_id))
            if registry is None:
                raise ValueError("小说不存在")
            due = registry.get_ready_to_resolve(number)
            return NodeResult(outputs={"foreshadowing_block": "\n".join(str(f.description) for f in due)})
        if kind == "ctx_debt":
            from application.engine.services.context_assembler import ContextAssembler
            # Repository's actual API is get_due_soon/get_overdue, not find_due.
            due = self.debts.get_due_soon(novel_id, number, window=3) + self.debts.get_overdue(novel_id)
            return NodeResult(outputs={"debt_due_block": "\n".join(dict.fromkeys(str(d.description) for d in due))})
        raise ValueError(kind)

    def _style(self, novel_id, content, node):
        from application.analyst.services.voice_drift_service import VoiceDriftService
        fp = self.fingerprints.get_by_novel(novel_id)
        if not fp or fp.get("sample_count", 0) < 10:
            return NodeResult(status=NodeStatus.WARNING, outputs={"drift_score": None, "drift_alert": False,
                                                                 "baseline_available": False})
        similarity = VoiceDriftService._cosine_similarity(VoiceDriftService._compute_metrics(content), fp)
        drift = 1 - similarity
        threshold = node._config.thresholds.get("drift_warning", 0.5)
        return NodeResult(outputs={"drift_score": drift, "drift_alert": drift > threshold, "baseline_available": True},
                          metrics={"drift_score": drift}, status=NodeStatus.WARNING if drift > threshold else NodeStatus.SUCCESS)

    @staticmethod
    def _config(node, default_temperature):
        return GenerationConfig(model=node._config.model_override or "", max_tokens=node._config.max_tokens or 4000,
                                temperature=node._config.temperature if node._config.temperature is not None else default_temperature)

    async def rewrite(self, state, node, attempt):
        from infrastructure.ai.prompt_keys import VOICE_REWRITE
        from application.engine.dag.registry import BaseNode
        # Reuse CPMS voice-rewrite plus exact rule hits as explicit edit targets.
        template = node.meta.model_copy(update={"cpms_node_key": VOICE_REWRITE})
        original_meta = node.meta
        node.meta = template
        try:
            resolved = node.resolve_prompt({"chapter_number": state["chapter_number"], "attempt": attempt,
                "similarity_score": state.get("drift_score", "无基准"), "threshold": node._config.thresholds.get("anti_ai_max_score", 0),
                "outline": state["outline"], "content": state["content"], "style_fingerprint": state.get("voice_block", ""),
                "anchor_block": state.get("character_state_lock", "")})
        finally:
            node.meta = original_meta
        targets = json.dumps(state.get("hits", []), ensure_ascii=False)
        user = resolved["user"] + "\n\n同时修订以下 Anti-AI 规则命中，保留事实与对白信息：\n" + targets
        from application.engine.rules.prose_prompt_hooks import generate_direct_prose
        evidence = {}
        content = generation_text(await generate_direct_prose(self.llm,Prompt(system=resolved["system"],user=user),self._config(node,0.4),db=self.db,
            context={"novel_id":state["novel_id"],"chapter_number":state["chapter_number"],"job_id":f"{state.get('dag_run_id','')}:{attempt}"}, evidence_sink=evidence))
        self._validate_prompt_evidence(evidence, content, state={**state, "attempt": attempt})
        state["anti_ai_prompt_evidence"] = evidence
        return content

    def _validate_prompt_evidence(self, evidence, content, *, state, reused_candidate=False):
        if (not isinstance(evidence, dict) or evidence.get("content_hash") != content_hash(content)
                or (evidence.get("status") == "reused_candidate" and not reused_candidate)
                or evidence.get("status") not in ({"completed", "reused_candidate"} if reused_candidate else {"completed"})):
            raise RuntimeError("去味提示词执行回执与当前正文不匹配，不能提交正文")
        if evidence["status"] == "completed" and (not evidence.get("prompt_hash") or not evidence.get("run_id")
                or evidence.get("persistence_status") != "committed"):
            raise RuntimeError("去味提示词执行回执没有完整持久化，不能提交正文")
        if evidence["status"] == "reused_candidate":
            if (evidence.get("applied") is not False or not state.get("resumed_candidate")
                    or not state.get("resumed_from") or evidence.get("source_run_id") != state["resumed_from"]):
                raise RuntimeError("恢复候选来源没有匹配确认，不能提交正文")
            from application.engine.rules.prose_prompt_hooks import ProsePromptHooks, load_verified_market_profile
            _snapshot, block = load_verified_market_profile(self.db, state["novel_id"])
            original = evidence.get("original_prompt_evidence")
            if block or original:
                ProsePromptHooks(self.db).validate_current_market_profile(original or {}, {"novel_id": state["novel_id"]})
                original_job = str((original or {}).get("job_id") or "")
                base_job, separator, suffix = original_job.rpartition(":")
                if not separator or not suffix.isdigit():
                    base_job, suffix = original_job, "0"
                self._validate_prompt_evidence(original, content,
                    state={**state, "dag_run_id": base_job, "attempt": int(suffix)})
            return
        # A caller's dictionary is not proof of a durable provider attempt.
        # Bind the real receipt to this book, chapter, run and candidate body.
        row = self.db.fetch_one("SELECT * FROM anti_ai_prompt_runs WHERE run_id=?", (evidence["run_id"],))
        expected_job = str(state["dag_run_id"])
        attempt = state.get("attempt", 0)
        if type(attempt) is not int or attempt < 0:
            raise RuntimeError("正文生成回执的尝试次数无效")
        if attempt:
            expected_job += f":{attempt}"
        try:
            stored = json.loads(row["evidence_json"]) if row else {}
        except (ValueError, TypeError):
            stored = {}
        if (not row or row["status"] != "completed" or row["novel_id"] != state["novel_id"]
                or row["chapter_number"] != state["chapter_number"] or row["job_id"] != expected_job
                or evidence.get("job_id") != expected_job or row["prompt_hash"] != evidence["prompt_hash"]
                or row["rule_version"] != evidence.get("rule_version") or not isinstance(stored, dict)
                or stored.get("content_hash") != content_hash(content) or stored.get("status") != "completed"
                or any(stored.get(key) != evidence.get(key) for key in ("run_id", "prompt_hash", "rule_version", "hook_version",
                    "layers", "dag_injections", "market_profile_hash"))):
            raise RuntimeError("数据库中的去味执行回执未绑定本轮正文，不能提交正文")
        from application.ai_invocation.prose_execution_evidence import verify_durable_prose_receipts
        verify_durable_prose_receipts(self.db, novel_id=state["novel_id"], chapter_number=state["chapter_number"],
            attempt_id=expected_job, content=content, prompt_hash=evidence["prompt_hash"], require_stream=False,
            prompt_run_id=evidence["run_id"], scan_run_id=(evidence.get("anti_ai_scan") or {}).get("run_id", ""))
        from application.engine.rules.prose_prompt_hooks import ProsePromptHooks
        ProsePromptHooks(self.db).validate_current_market_profile(evidence, {"novel_id": state["novel_id"]})

    async def _quality_gate(self, node, state):
        from application.engine.dag.engine import DAGEngine
        maximum = node._config.thresholds.get("anti_ai_max_score", 0)
        budget = min(5, max(0, node.get_max_retries()))
        latest = dict(state)
        for attempt in range(budget + 1):
            reasons = []
            quality = await asyncio.to_thread(self.validator.check, text=latest["content"], chapter_goal=state["outline"])
            latest["quality_report"] = quality.to_dict()
            if quality.status in {"unavailable", "skipped", "cancelled"}:
                raise RuntimeError(quality.error or "内容质量检查不可用，不能提交正文")
            if quality.passed is not True or quality.status != "passed":
                reasons.append("内容质量未通过")
            if latest.get("drift_alert"):
                reasons.append("文风偏离")
            if "severity_score" not in latest or "hits" not in latest:
                raise RuntimeError("去 AI 味审计没有执行结果，不能提交正文")
            expected_scan_hash = content_hash(latest["content"])
            self._validate_prompt_evidence(latest.get("anti_ai_prompt_evidence"), latest["content"],
                state=latest, reused_candidate=bool(latest.get("resumed_candidate") and attempt == 0))
            if latest.get("scan_status") != "completed":
                raise RuntimeError("去 AI 味扫描没有完成，不能提交正文")
            if latest.get("input_hash") != expected_scan_hash or latest.get("content_hash") != expected_scan_hash:
                raise RuntimeError("去 AI 味扫描回执与当前正文不匹配，不能提交正文")
            if not isinstance(latest.get("rule_version"), str) or not latest["rule_version"].strip():
                raise RuntimeError("去 AI 味扫描回执缺少有效规则版本，不能提交正文")
            import math
            score = latest["severity_score"]
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0 or not isinstance(latest["hits"], list):
                raise RuntimeError("去 AI 味审计结果无效，不能提交正文")
            if float(latest["severity_score"]) > maximum:
                reasons.append("Anti-AI 规则命中")
            if any(isinstance(hit, dict) and hit.get("severity") == "critical" for hit in latest["hits"]):
                reasons.append("Anti-AI 严重规则命中")
            if not reasons:
                if self.candidate_id:
                    await asyncio.to_thread(self.candidates.mark_audited, self.candidate_id,
                        {"content_hash": expected_scan_hash, **{key: latest.get(key) for key in ("scan_status", "input_hash", "rule_version", "severity_score", "total_hits", "hits", "drift_score", "baseline_available", "composite", "quality_report", "anti_ai_prompt_evidence")}}, True)
                return NodeResult(outputs={**{k: v for k, v in latest.items() if k not in {"services", "llm_service", "on_node_event"}},
                    "quality_approved": True, "retry_required": False, "attempts_used": attempt})
            if self.candidate_id:
                await asyncio.to_thread(self.candidates.mark_audited, self.candidate_id,
                    {"content_hash": expected_scan_hash, **{key: latest.get(key) for key in ("scan_status", "input_hash", "rule_version", "severity_score", "total_hits", "hits", "drift_score", "baseline_available", "composite", "quality_report", "anti_ai_prompt_evidence")}}, False)
            if attempt == budget:
                return NodeResult(status=NodeStatus.ERROR, error=f"重写达到上限 ({budget})，审计仍未通过：{'、'.join(reasons)}",
                                  outputs={"quality_approved": False, "retry_required": True, "retry_reasons": reasons, "attempts_used": attempt})
            latest["attempt"] = attempt + 1
            content = await self.rewrite(latest, node, attempt + 1)
            if content_hash(content) == content_hash(latest["content"]):
                return NodeResult(status=NodeStatus.ERROR, error="重写返回原正文，未应用任何修订",
                    outputs={"quality_approved": False, "retry_required": True, "attempts_used": attempt + 1})
            candidate = await asyncio.to_thread(self.candidates.save_candidate, run_id=state["dag_run_id"],
                novel_id=state["novel_id"], chapter_id=state["chapter_id"], chapter_number=state["chapter_number"],
                content=content, kind="rewrite", attempt=attempt + 1,
                base_hash=content_hash(state["chapter_before"].get("content") or ""), base_content=state["chapter_before"].get("content") or "",
                base_revision=state["chapter_before"].get("revision"))
            self.candidate_id = candidate["candidate_id"]
            latest["content"] = content
            latest["candidate_id"] = self.candidate_id
            latest["word_count"] = len(content)
            # Persist each candidate through the real event callback before a
            # subsequent audit or cancellation can discard it.
            gate_def = self.dag.get_node("gw_retry")
            await DAGEngine._emit(latest, gate_def, NodeResult(status=NodeStatus.RUNNING,
                outputs={"content": content, "candidate_kind": "rewrite", "attempts_used": attempt + 1,
                    "anti_ai_prompt_evidence": latest["anti_ai_prompt_evidence"]}))
            audits = [self.dag.get_node(kind) for kind in ("val_style", "val_tension", "val_anti_ai")]
            for definition in audits:
                if definition is None or not definition.enabled:
                    raise RuntimeError("必要审计节点缺失或被禁用")
                audit = await DAGEngine()._execute_node(definition, latest)
                if audit.status not in (NodeStatus.SUCCESS, NodeStatus.WARNING, NodeStatus.COMPLETED):
                    raise RuntimeError(f"重写后审计 {definition.id} 失败: {audit.error}")
                latest.update(audit.outputs)
        raise AssertionError("unreachable rewrite budget")

    def commit_chapter(self, state):
        content = state["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("不能提交空正文")
        before, now = state["chapter_before"], datetime.now(timezone.utc).isoformat()
        digest = content_hash(content)
        if not self.candidate_id:
            raise RuntimeError("没有经过持久化复验的候选，不能提交正文")
        candidate = self.candidates.get(self.candidate_id)
        quality = candidate["audit"].get("quality_report") or {}
        if (state.get("quality_approved") is not True or candidate["status"] != "audited"
                or candidate["run_id"] != state["dag_run_id"] or candidate["content"] != content
                or candidate["content_hash"] != digest or candidate["audit"].get("content_hash") != digest
                or candidate["base_revision"] != before["revision"]
                or quality.get("passed") is not True or quality.get("status") != "passed"):
            raise RuntimeError("提交正文不是本轮已通过复验的候选版本；原稿保持不变")
        evidence = state.get("anti_ai_prompt_evidence")
        if candidate["audit"].get("anti_ai_prompt_evidence") != evidence:
            raise RuntimeError("提交前去味执行回执发生变化，不能提交正文")
        self._validate_prompt_evidence(evidence, content, state=state,
            reused_candidate=bool(state.get("resumed_candidate") and not state.get("attempt")))
        reuse = evidence.get("status") == "reused_candidate"
        durable_evidence = evidence.get("original_prompt_evidence") if reuse else evidence
        receipt_condition, receipt_params = "1", ()
        if durable_evidence:
            from application.ai_invocation.prose_execution_evidence import prose_receipt_write_fence
            receipt_condition, receipt_params = prose_receipt_write_fence(self.db,
                novel_id=state["novel_id"], chapter_number=state["chapter_number"], content=content,
                proof={"verified": True, "attempt_id": durable_evidence["job_id"],
                    "prompt_hash": durable_evidence["prompt_hash"], "require_stream": False,
                    "prompt_run_id": durable_evidence["run_id"],
                    "scan_run_id": (durable_evidence.get("anti_ai_scan") or {}).get("run_id", "")})
        # Body commit and candidate receipt share the existing writer's single
        # transaction. The condition is evaluated after queueing, so rejection,
        # lease expiry and editor races cannot commit an unaudited version.
        with self.db.transaction() as writer:
            cursor = writer.execute("""UPDATE chapters SET content=?,status='reviewing',word_count=?,
            plot_tension=?,emotional_tension=?,pacing_tension=?,tension_score=?,updated_at=?
            WHERE id=? AND novel_id=? AND COALESCE(content,'')=? AND COALESCE(outline,'')=? AND status=?
                AND revision=? AND EXISTS (SELECT 1 FROM novel_execution_leases l
                    WHERE l.novel_id=chapters.novel_id AND l.owner='dag' AND l.job_id=?
                        AND l.expires_at>(julianday('now')-2440587.5)*86400.0)
                AND EXISTS (SELECT 1 FROM chapter_rewrite_candidates r
                    WHERE r.candidate_id=? AND r.chapter_id=chapters.id AND r.novel_id=chapters.novel_id
                        AND r.run_id=? AND r.status='audited' AND r.content=? AND r.content_hash=?
                        AND json_extract(r.audit_json,'$.content_hash')=?
                        AND json_extract(r.audit_json,'$.quality_report.passed')=1
                        AND json_extract(r.audit_json,'$.quality_report.status')='passed'
                        AND json_extract(r.audit_json,'$.anti_ai_prompt_evidence.content_hash')=?
                        AND ((?=1 AND json_extract(r.audit_json,'$.anti_ai_prompt_evidence.status')='reused_candidate')
                            OR EXISTS (SELECT 1 FROM anti_ai_prompt_runs p
                                WHERE p.run_id=? AND p.novel_id=chapters.novel_id AND p.chapter_number=chapters.number
                                    AND p.status='completed' AND p.job_id=? AND p.prompt_hash=?
                                    AND p.run_id=COALESCE(json_extract(r.audit_json,'$.anti_ai_prompt_evidence.original_prompt_evidence.run_id'),
                                        json_extract(r.audit_json,'$.anti_ai_prompt_evidence.run_id'))
                                    AND p.prompt_hash=COALESCE(json_extract(r.audit_json,'$.anti_ai_prompt_evidence.original_prompt_evidence.prompt_hash'),
                                        json_extract(r.audit_json,'$.anti_ai_prompt_evidence.prompt_hash'))
                                    AND json_extract(p.evidence_json,'$.status')='completed'
                                    AND json_extract(p.evidence_json,'$.content_hash')=?)))""" + " AND (" + receipt_condition + ")",
            (content, len(content), state.get("plot_tension", 50), state.get("emotional_tension", 50),
             state.get("pacing_tension", 50), state.get("composite", 50), now,
             before["id"], state["novel_id"], before.get("content") or "", before.get("outline") or "", before["status"],
             before["revision"], state["dag_run_id"], self.candidate_id, state["dag_run_id"], content, digest, digest,
             digest, int(reuse and not durable_evidence), (durable_evidence or {}).get("run_id", ""),
             (durable_evidence or {}).get("job_id", ""), (durable_evidence or {}).get("prompt_hash", ""), digest) + receipt_params)
            receipt = writer.execute("""UPDATE chapter_rewrite_candidates
                SET status='committed',committed_at=?,updated_at=?,
                    audit_json=json_set(audit_json,'$.committed_revision',
                        (SELECT revision FROM chapters WHERE id=chapter_rewrite_candidates.chapter_id))
                WHERE candidate_id=? AND status='audited' AND changes()=1""", (now, now, self.candidate_id))
        if cursor.rowcount != 1 or receipt.rowcount != 1:
            raise RuntimeError("章节已被编辑或状态改变，候选正文保留在 DAG 运行记录中")
        committed = self.candidates.get(self.candidate_id)
        row = self.db.fetch_one("SELECT content,revision FROM chapters WHERE id=?", (before["id"],))
        if row is None or row["content"] != content or row["revision"] != committed["audit"].get("committed_revision"):
            raise RuntimeError("章节提交后的正文回读不一致；候选正文已保留")
        return NodeResult(outputs={"chapter_committed": True, "saved_content_hash": content_hash(content),
                                   "saved_revision": row["revision"], "chapter_id": before["id"]}, metrics={"word_count": len(content)})
