"""章节保存后的统一管线：叙事落库、向量检索、文风、图谱推断与后台抽取。

供 HTTP 保存、托管连写、自动驾驶审计复用，避免：
- 索引用正文截断 vs 叙事层用 LLM 总结 两套逻辑；
- 文风既入队 VOICE_ANALYSIS 又同步 score_chapter 重复计算。

关键路径只保留下一章/审计立即需要的产物：
1. 章间桥段资产；
2. 分章叙事同步 + 张力评分 + 向量/伏笔/三元组/因果边/人物状态/债务；
3. 文风评分（若调用方已在同一正文上评分，则复用结果）。

其余护栏快照、治理提交、演进/世界线快照、道具同步、汇流点检查等
作为串行辅助阶段后台执行，避免写密集尾部工作阻塞下一章。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

from domain.ai.services.llm_service import LLMService

if TYPE_CHECKING:
    from application.world.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


async def _timed_aftermath_stage(
    stage: str,
    novel_id: str,
    chapter_number: int,
    work: Callable[[], Awaitable[Any]],
) -> Any:
    started = time.perf_counter()
    try:
        return await work()
    finally:
        elapsed = time.perf_counter() - started
        logger.info(
            "章后阶段完成 stage=%s novel=%s ch=%s elapsed=%.2fs",
            stage,
            novel_id,
            chapter_number,
            elapsed,
        )


class _SerializedAuxiliaryQueue:
    """Run non-critical chapter aftermath side effects off the critical path.

    Jobs are chained per event loop instead of launched concurrently. This keeps
    SQLite write-heavy auxiliary work from blocking the next chapter while also
    avoiding a thundering herd of background writes.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tail: asyncio.Task[Any] | None = None

    def enqueue(
        self,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> None:
        loop = asyncio.get_running_loop()
        previous = self._tail if self._loop is loop and self._tail and not self._tail.done() else None
        self._loop = loop
        self._tail = loop.create_task(
            self._run_after(previous, stage, novel_id, chapter_number, work)
        )

    async def drain(self) -> None:
        tail = self._tail
        if tail and not tail.done():
            await tail

    async def _run_after(
        self,
        previous: asyncio.Task[Any] | None,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> None:
        if previous is not None:
            try:
                await previous
            except Exception:
                # Previous jobs log their own failure; keep the chain alive.
                pass
        try:
            await _timed_aftermath_stage(stage, novel_id, chapter_number, work)
        except Exception as e:
            logger.warning(
                "章后辅助阶段失败 stage=%s novel=%s ch=%s: %s",
                stage,
                novel_id,
                chapter_number,
                e,
            )


_AUXILIARY_QUEUE = _SerializedAuxiliaryQueue()


async def infer_kg_from_chapter(novel_id: str, chapter_number: int, database: Any = None) -> None:
    """结构树章节节点 → 知识图谱增量推断（与 HTTP 原 _try_infer_kg_chapter 一致）。"""
    try:
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
        from infrastructure.persistence.database.triple_repository import TripleRepository
        from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
        from application.world.services.knowledge_graph_service import KnowledgeGraphService

        db = database if database is not None else get_database()
        db_path = db.db_path
        kr = SqliteKnowledgeRepository(db)
        story_node_id = kr.find_story_node_id_for_chapter_number(novel_id, chapter_number)
        if not story_node_id:
            logger.debug("KG 推断跳过：章节 %d 无故事节点 novel=%s", chapter_number, novel_id)
            return

        kg_service = KnowledgeGraphService(
            TripleRepository(db),
            ChapterElementRepository(db_path),
            StoryNodeRepository(db_path),
        )
        triples = await kg_service.infer_from_chapter(story_node_id)
        logger.debug("KG 推断完成 novel=%s ch=%d 新三元组=%d", novel_id, chapter_number, len(triples))
    except Exception as e:
        logger.warning("KG 推断失败 novel=%s ch=%d: %s", novel_id, chapter_number, e)
        raise


class ChapterAftermathPipeline:
    """章节保存后分析与落库的统一入口。

    V8 Feed-forward 升级：集成因果边提取、人物状态突变评估、叙事债务更新。
    """

    def __init__(
        self,
        knowledge_service: "KnowledgeService",
        chapter_indexing_service: Any,
        llm_service: LLMService,
        voice_drift_service: Any = None,
        triple_repository: Any = None,
        foreshadowing_repository: Any = None,
        storyline_repository: Any = None,
        chapter_repository: Any = None,
        plot_arc_repository: Any = None,
        narrative_event_repository: Any = None,
        # V8 Feed-forward: 新增仓储
        causal_edge_repository: Any = None,
        character_state_repository: Any = None,
        debt_repository: Any = None,
        bible_repository: Any = None,
        unified_checkpoint_service: Any = None,
        prop_lifecycle_syncer: Any = None,
        evolution_snapshot_service: Any = None,
        character_narrative_kernel: Any = None,
    ) -> None:
        self._knowledge = knowledge_service
        self._indexing = chapter_indexing_service
        self._llm = llm_service
        self._voice = voice_drift_service
        self._triple_repository = triple_repository
        self._foreshadowing_repository = foreshadowing_repository
        self._storyline_repository = storyline_repository
        self._chapter_repository = chapter_repository
        self._plot_arc_repository = plot_arc_repository
        self._narrative_event_repository = narrative_event_repository
        # V8 Feed-forward: 因果图谱 / 人物状态机 / 叙事债务
        self._causal_edge_repository = causal_edge_repository
        self._character_state_repository = character_state_repository
        self._debt_repository = debt_repository
        self._bible_repository = bible_repository
        self._unified_checkpoint = unified_checkpoint_service
        self._prop_syncer = prop_lifecycle_syncer
        self._evolution_snapshot_service = evolution_snapshot_service
        self._character_kernel = character_narrative_kernel

    def _database(self):
        """Resolve the project database captured by the injected repositories."""
        from infrastructure.persistence.database.connection import DatabaseConnection, get_database

        for repository in (self._chapter_repository, getattr(self._knowledge, "knowledge_repository", None)):
            db = getattr(repository, "db", None)
            if isinstance(db, DatabaseConnection):
                return db
        return get_database()

    async def run_after_chapter_saved(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        chapter_micro_beats: Optional[List[Dict[str, Any]]] = None,
        voice_result: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        expected_content_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bind every derived write, including background stages, to one body."""
        from infrastructure.persistence.database.connection import DatabaseConnection
        from infrastructure.persistence.database.chapter_write_fence import chapter_write_fence, current_chapter_write_fence
        from application.core.services.chapter_version_service import content_hash

        db = getattr(self._chapter_repository, "db", None)
        if not isinstance(db, DatabaseConnection):
            return await self._run_after_chapter_saved(novel_id, chapter_number, content, chapter_micro_beats, voice_result)
        current = await asyncio.to_thread(db.fetch_one,
            "SELECT id,revision,content FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
        digest = content_hash(content)
        existing = current_chapter_write_fence()
        revision = expected_revision if expected_revision is not None else (existing["revision"] if existing else current["revision"] if current else None)
        expected_hash = expected_content_hash or (existing["content_hash"] if existing else digest)
        if not current or current["revision"] != revision or content_hash(current["content"]) != expected_hash or digest != expected_hash:
            return {"narrative_sync_ok": False, "narrative_sync_status": "superseded", "derivation_status": "superseded"}
        if existing and (existing["chapter_id"] != current["id"] or existing["content_hash"] != digest):
            return {"narrative_sync_ok": False, "narrative_sync_status": "superseded", "derivation_status": "superseded"}
        dependencies = existing.get('dependencies', []) if existing else [
            {"id": row['id'], "revision": row['revision'], "content_hash": content_hash(row['content'])}
            for row in await asyncio.to_thread(db.fetch_all,
                'SELECT id,revision,content FROM chapters WHERE novel_id=? AND number<?', (novel_id, chapter_number))
        ]
        with chapter_write_fence(db.db_path, current["id"], revision, expected_hash, dependencies=dependencies):
            result = await self._run_after_chapter_saved(novel_id, chapter_number, content, chapter_micro_beats, voice_result)
        latest = await asyncio.to_thread(db.fetch_one, "SELECT revision,content FROM chapters WHERE id=?", (current["id"],))
        if not latest or latest["revision"] != revision or content_hash(latest["content"]) != expected_hash:
            result.update(narrative_sync_ok=False, narrative_sync_status="superseded", derivation_status="superseded")
        return result

    async def _run_after_chapter_saved(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        chapter_micro_beats: Optional[List[Dict[str, Any]]] = None,
        voice_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """保存正文后执行完整管线。返回文风结果供托管/审计门控使用。

        三元组与伏笔、故事线、张力、对话、因果边、人物状态、债务
        已在 narrative_sync 单次 LLM 中落库。
        """
        out: Dict[str, Any] = {
            "stage_errors": [],
            "drift_alert": False,
            "similarity_score": None,
            "narrative_sync_ok": False,
            "vector_stored": False,
            "foreshadow_stored": False,
            "triples_extracted": False,
            "causal_edges_stored": False,
            "character_mutations_stored": False,
            "debt_updated": False,
            "bridge_extracted": False,
            "guardrail_passed": None,
            "guardrail_score": None,
            "evolution_snapshot_ok": False,
            "evolution_snapshot_id": None,
            "character_reconcile_ok": False,
            "character_reconcile": None,
        }

        from application.world.services.chapter_projection_reconciliation import rollback_chapter_projections
        await asyncio.to_thread(rollback_chapter_projections, self._database(), novel_id, chapter_number)
        from application.world.services.chapter_artifact_reconciliation import withdraw_chapter_suffix_evidence
        await asyncio.to_thread(withdraw_chapter_suffix_evidence, self._database(), novel_id, chapter_number)

        # The legacy memory engine is a separate read model consumed by later
        # planning nodes; rebuilding narrative atoms alone does not update it.
        if self._bible_repository is not None:
            try:
                from application.engine.services.memory_engine import MemoryEngine
                memory = MemoryEngine(llm_service=self._llm, bible_repository=self._bible_repository,
                                      db_connection=self._database())
                result = await memory.reconcile_from_chapter(novel_id, chapter_number, content)
                out['memory_engine'] = result
                if result.get('errors'):
                    out['stage_errors'].extend('memory_engine: ' + str(error) for error in result['errors'])
            except Exception as error:
                out['stage_errors'].append('memory_engine: ' + type(error).__name__)

        if not content or not str(content).strip():
            from application.world.services.chapter_artifact_reconciliation import clear_chapter_generated_artifacts
            try:
                await clear_chapter_generated_artifacts(self._database(), self._indexing, novel_id, chapter_number)
                if out['stage_errors']:
                    out.update(narrative_sync_status='failed', derivation_status='unavailable')
                else:
                    out.update(narrative_sync_ok=True, narrative_sync_status="synced", derivation_status="completed")
            except Exception as error:
                out.setdefault("stage_errors", []).append("_run_after_chapter_saved: " + type(error).__name__)
                out.update(narrative_sync_status="failed", narrative_sync_error=f"清空章节关联内容失败: {type(error).__name__}")
            return out

        # 0) 章间衔接锚点。放在统一章后管线里，确保 HTTP 保存、托管连写、
        # 自动驾驶最终都会产出同一种前章桥段资产。
        try:
            await _timed_aftermath_stage(
                "bridge_extract",
                novel_id,
                chapter_number,
                lambda: self._extract_chapter_bridge(novel_id, chapter_number, content),
            )
            out["bridge_extracted"] = True
        except Exception as e:
            out.setdefault("stage_errors", []).append("_run_after_chapter_saved: " + type(e).__name__)
            logger.warning("章节桥段提取失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 1) 叙事 + 向量 + 故事线 + 张力 + 对话 + 因果边 + 人物状态 + 债务
        try:
            from application.world.services.chapter_narrative_sync import (
                sync_chapter_narrative_after_save,
            )

            async def _sync_narrative() -> Dict[str, Any]:
                return await sync_chapter_narrative_after_save(
                    novel_id,
                    chapter_number,
                    content,
                    self._knowledge,
                    self._indexing,
                    self._llm,
                    triple_repository=self._triple_repository,
                    foreshadowing_repo=self._foreshadowing_repository,
                    storyline_repository=self._storyline_repository,
                    chapter_repository=self._chapter_repository,
                    plot_arc_repository=self._plot_arc_repository,
                    narrative_event_repository=self._narrative_event_repository,
                    causal_edge_repository=self._causal_edge_repository,
                    character_state_repository=self._character_state_repository,
                    debt_repository=self._debt_repository,
                    bible_repository=self._bible_repository,
                    chapter_micro_beats=chapter_micro_beats,
                    database=self._database(),
                )

            sync_flags = await _timed_aftermath_stage(
                "narrative_sync",
                novel_id,
                chapter_number,
                _sync_narrative,
            )
            out["narrative_sync_ok"] = sync_flags.get("narrative_sync_ok") is True
            out["narrative_sync_status"] = sync_flags.get("sync_status", "unavailable")
            out["stage_errors"].extend(sync_flags.get("stage_errors") or [])
            for key in ("evolution_actions", "evolution_ignored", "evolution_errors", "evolution_state_coverage"):
                if key in sync_flags:
                    out[key] = sync_flags[key]
            if sync_flags.get("sync_error"):
                out["narrative_sync_error"] = sync_flags["sync_error"]
            out["vector_stored"] = bool(sync_flags.get("vector_stored"))
            out["foreshadow_stored"] = bool(sync_flags.get("foreshadow_stored"))
            out["triples_extracted"] = bool(sync_flags.get("triples_extracted"))
            out["causal_edges_stored"] = bool(sync_flags.get("causal_edges_stored"))
            out["character_mutations_stored"] = bool(sync_flags.get("character_mutations_stored"))
            out["debt_updated"] = bool(sync_flags.get("debt_updated"))
            # 传递多维张力评分（0-100），供审计流程替代旧式 _score_tension
            out["tension_composite"] = sync_flags.get("tension_composite")
        except Exception as e:
            out.setdefault("stage_errors", []).append("_run_after_chapter_saved: " + type(e).__name__)
            out["narrative_sync_status"] = "failed"
            out["narrative_sync_error"] = f"{type(e).__name__}: {e}"
            logger.warning(
                "叙事同步/向量失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # 1b) 角色叙事内核对账：cast plan vs 正文，自动投影状态与风险。
        try:
            if self._character_kernel:
                reconcile = self._character_kernel.reconcile_after_chapter(
                    novel_id,
                    chapter_number,
                    content,
                    None,
                )
                out["character_reconcile_ok"] = bool(reconcile.get("checked"))
                out["character_reconcile"] = reconcile
        except Exception as e:
            out.setdefault("stage_errors", []).append("_run_after_chapter_saved: " + type(e).__name__)
            logger.warning(
                "角色叙事对账失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # 2) 文风（落库 chapter_style_scores）
        # 支持 LLM 模式（异步）和统计模式（同步）
        if voice_result is not None:
            self._apply_voice_result(out, voice_result)
            out["voice_reused"] = True
            logger.debug(
                "复用文风评分 novel=%s ch=%s mode=%s drift=%s",
                novel_id,
                chapter_number,
                out.get("voice_mode"),
                out["drift_alert"],
            )
        elif self._voice:
            try:
                async def _score_voice() -> Dict[str, Any]:
                    if getattr(self._voice, "use_llm_mode", False):
                        return await self._voice.score_chapter_async(
                            novel_id=novel_id,
                            chapter_number=chapter_number,
                            content=content,
                        )
                    return self._voice.score_chapter(
                        novel_id=novel_id,
                        chapter_number=chapter_number,
                        content=content,
                    )

                vr = await _timed_aftermath_stage(
                    "voice_drift",
                    novel_id,
                    chapter_number,
                    _score_voice,
                )
                self._apply_voice_result(out, vr)
                out["voice_reused"] = False
                logger.debug(
                    "文风评分完成 novel=%s ch=%s mode=%s drift=%s",
                    novel_id,
                    chapter_number,
                    out.get("voice_mode"),
                    out["drift_alert"],
                )
            except Exception as e:
                out.setdefault("stage_errors", []).append("_run_after_chapter_saved: " + type(e).__name__)
                logger.warning("文风评分失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # Keep the exact result object as the auxiliary evidence envelope.  A
        # copied dict makes failures raised by a deferred stage invisible to
        # the derivation job after it drains the queue, which can incorrectly
        # acknowledge a partially completed chapter as fully completed.
        out["auxiliary_deferred"] = True
        self._schedule_auxiliary_stages(novel_id, chapter_number, content, out)

        return out

    @staticmethod
    def _apply_voice_result(out: Dict[str, Any], result: Dict[str, Any]) -> None:
        out["drift_alert"] = bool(result.get("drift_alert", False))
        out["similarity_score"] = result.get("similarity_score")
        out["voice_mode"] = result.get("mode", "statistics")

    async def drain_auxiliary_stages(self) -> None:
        """Wait for queued auxiliary aftermath work.

        Intended for tests and controlled shutdown paths; normal callers should
        not await this on the writing critical path.
        """
        await _AUXILIARY_QUEUE.drain()

    def _schedule_auxiliary_stages(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
    ) -> None:
        async def _job() -> None:
            try:
                await self._run_auxiliary_stages(novel_id, chapter_number, content, evidence)
            except Exception as error:
                evidence.setdefault("stage_errors", []).append(f"auxiliary: {type(error).__name__}")
                raise

        _AUXILIARY_QUEUE.enqueue(
            "auxiliary_after_chapter",
            novel_id,
            chapter_number,
            _job,
        )

    async def _run_auxiliary_stages(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
    ) -> None:
        # 3) 结构树 KG 推断
        db = self._database()
        try:
            await infer_kg_from_chapter(novel_id, chapter_number, database=db)
        except Exception as error:
            evidence.setdefault('stage_errors', []).append('knowledge_graph: ' + type(error).__name__)

        # 4) 质量护栏（建议模式）+ 快照落库 + 溯源（与手动 POST /guardrail/check 同源）
        try:
            import uuid
            from datetime import datetime, timezone

            from application.engine.services.guardrail_execution import run_guardrail_advise_sync
            from engine.core.ports.ports import TraceRecord
            from engine.infrastructure.persistence.trace_store import SqliteTraceStore
            from infrastructure.persistence.database.chapter_guardrail_snapshot_repository import (
                ChapterGuardrailSnapshotRepository,
            )

            t0 = time.perf_counter()
            dto = await asyncio.to_thread(
                run_guardrail_advise_sync,
                novel_id,
                content,
                f"第{chapter_number}章（保存后自动）",
                database=db,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            evidence["guardrail_passed"] = bool(dto.get("passed"))
            evidence["guardrail_score"] = dto.get("overall_score")

            repo = ChapterGuardrailSnapshotRepository(db)
            await asyncio.to_thread(repo.upsert, novel_id, chapter_number, dto)

            vsummary: list[str] = []
            for v in dto.get("violations") or []:
                if isinstance(v, dict) and v.get("description"):
                    vsummary.append(str(v["description"])[:120])
                if len(vsummary) >= 20:
                    break

            store = SqliteTraceStore(db)
            trace = TraceRecord(
                trace_id=str(uuid.uuid4()),
                node_type="guardrail",
                operation="chapter_after_save",
                input_summary=f"{novel_id} ch{chapter_number} len={len(content)}"[:200],
                output_summary=(
                    f"passed={dto.get('passed')} score={dto.get('overall_score')} "
                    f"viol={len(dto.get('violations') or [])}"
                )[:200],
                score=float(dto.get("overall_score") or 0.0),
                violations=vsummary,
                duration_ms=elapsed_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
                novel_id=novel_id,
            )
            await store.record(trace)
        except Exception as e:
            evidence.setdefault("stage_errors", []).append("guardrail: " + type(e).__name__)
            logger.warning("自动护栏/溯源失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 5) 叙事治理层：结构治理报告与严重问题暂停闸门（质量护栏之外的整书治理）
        try:
            from application.governance.service import NarrativeGovernanceService
            from infrastructure.persistence.database.sqlite_governance_repository import (
                SqliteGovernanceRepository,
            )
            from infrastructure.persistence.database.sqlite_storyline_repository import (
                SqliteStorylineRepository,
            )
            from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository

            governance = NarrativeGovernanceService(
                SqliteGovernanceRepository(db),
                SqliteNovelRepository(db),
                SqliteStorylineRepository(db),
                db,
            )
            report = await asyncio.to_thread(
                governance.commit_chapter,
                novel_id,
                chapter_number,
                content,
                dict(evidence),
            )
            evidence["governance_report"] = report.to_dict()
            evidence["governance_severity"] = report.severity
            evidence["governance_should_pause"] = report.should_pause_autopilot
        except Exception as e:
            evidence.setdefault("stage_errors", []).append("governance: " + type(e).__name__)
            logger.warning("叙事治理评估失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 6) 故事演进硬状态快照 — 只消费 evidence，不把 read model 当真源
        try:
            if self._evolution_snapshot_service:
                snapshot = await asyncio.to_thread(
                    self._evolution_snapshot_service.build_after_chapter_saved,
                    novel_id,
                    chapter_number,
                    content,
                    "main",
                    dict(evidence),
                )
                evidence["evolution_snapshot_ok"] = snapshot.status == "active"
                evidence["evolution_snapshot_id"] = snapshot.snapshot_id
                if snapshot.status != 'active':
                    evidence.setdefault('stage_errors', []).append('evolution_snapshot: ' + snapshot.status)
                logger.debug(
                    "[Evolution] snapshot novel=%s ch=%s id=%s status=%s",
                    novel_id,
                    chapter_number,
                    snapshot.snapshot_id,
                    snapshot.status,
                )
        except Exception as e:
            evidence.setdefault("stage_errors", []).append("evolution_snapshot: " + type(e).__name__)
            logger.warning("[Evolution] 快照创建失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 7) 世界线快照 — 章节完成后自动打 CHAPTER checkpoint
        try:
            if self._unified_checkpoint:
                cp_id = await asyncio.to_thread(
                    self._unified_checkpoint.create_checkpoint,
                    novel_id,
                    "CHAPTER",
                    f"第{chapter_number}章自动快照",
                    None,
                    "main",
                    None,
                    {"chapter": chapter_number},
                )
                evidence["worldline_checkpoint_id"] = cp_id
                logger.debug("[Worldline] CHAPTER checkpoint novel=%s ch=%s id=%s", novel_id, chapter_number, cp_id)
        except Exception as e:
            evidence.setdefault("stage_errors", []).append("worldline_checkpoint: " + type(e).__name__)
            logger.warning("[Worldline] 自动 checkpoint 失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 8) 道具生命周期同步 — 事件提取、状态机转换、知识库三元组
        try:
            if self._prop_syncer:
                sync_result = await self._prop_syncer.sync(novel_id, chapter_number, content)
                evidence["prop_sync"] = sync_result
                evidence['stage_errors'].extend(sync_result.get('errors') or [])
                logger.debug(
                    "[PropSync] 完成 novel=%s ch=%s result=%s",
                    novel_id,
                    chapter_number,
                    sync_result,
                )
        except Exception as e:
            evidence.setdefault("stage_errors", []).append("prop_sync: " + type(e).__name__)
            logger.warning(
                "[PropSync] 失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # ── 汇流点到达检查 ──
        try:
            from infrastructure.persistence.database.sqlite_confluence_point_repository import SqliteConfluencePointRepository
            _confluence_repo = SqliteConfluencePointRepository(db)
            _hit_cps = [
                cp for cp in _confluence_repo.get_by_novel_id(novel_id)
                if cp.target_chapter == chapter_number and not cp.resolved
            ]
            if _hit_cps:
                for _cp in _hit_cps:
                    logger.info(
                        "[汇流点] 第%d章完成，汇流点 %s (source=%s → target=%s) 建议标记为 resolved",
                        chapter_number,
                        _cp.id,
                        _cp.source_storyline_id,
                        _cp.target_storyline_id,
                    )
        except Exception as _cp_err:
            evidence.setdefault("stage_errors", []).append("confluence_check: " + type(_cp_err).__name__)
            logger.warning("汇流点检查失败（非致命）: %s", _cp_err)

    async def _extract_chapter_bridge(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> None:
        """统一章后桥段提取。

        这是保存后管线的衔接端口，而不是某条写作路径的私有后处理。
        ChapterBridgeService 的写入是 upsert，重复调用保持幂等。
        """
        from application.engine.services.chapter_bridge_service import ChapterBridgeService

        database = self._database()
        svc = ChapterBridgeService(
            llm_service=self._llm,
            db_path=str(database.db_path),
        )
        await svc.extract_bridge(novel_id, chapter_number, content, strict=True)
