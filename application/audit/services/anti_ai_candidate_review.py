"""Author review uses immutable candidates, fresh checks, writer CAS and final audit ACK."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from application.audit.services.anti_ai_rewrite_workflow import AntiAIRewriteWorkflow
from application.audit.services.anti_ai_metrics import get_anti_ai_metrics_service
from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates, CandidateConflictError, content_hash
from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository


class CandidateReviewError(RuntimeError):
    pass


def _safe_error_summary(exc, fallback):
    """Return a UI/audit-safe failure summary.

    Provider exceptions can contain request URLs, headers, or response bodies.
    Candidate audit records are durable and are shown to the author, so they
    must never retain the provider's raw message.  The exception class is
    enough for diagnostics; detailed text remains in protected application
    logs where the normal logging policy applies.
    """
    kind = type(exc).__name__ if exc is not None else "UnknownError"
    return f"{fallback}（{kind}）"


class AntiAICandidateReview:
    def __init__(self, db, *, llm=None, voice_service=None, auditor=None, validator=None, on_event=None, aftermath=None):
        self.db, self.llm, self.voice_service, self.on_event = db, llm, voice_service, on_event
        self.candidates = ChapterRewriteCandidates(db)
        self.auditor, self.validator = auditor, validator
        self.aftermath = aftermath

    def _novel(self, novel_id):
        novel = self.db.fetch_one("SELECT id,autopilot_status,current_stage,autopilot_run_epoch FROM novels WHERE id=?", (novel_id,))
        if novel is None:
            raise LookupError("小说不存在")
        return novel

    def detail(self, novel_id, candidate_id):
        candidate = self.candidates.get(candidate_id)
        if candidate["novel_id"] != novel_id:
            raise KeyError(candidate_id)
        novel = self._novel(novel_id)
        chapter = self.db.fetch_one("SELECT content,revision FROM chapters WHERE id=? AND novel_id=?", (candidate["chapter_id"], novel_id))
        committed = candidate["status"] == "committed"
        expected_hash = candidate["content_hash"] if committed else candidate["base_hash"]
        expected_revision = candidate["audit"].get("committed_revision") if committed else candidate["base_revision"]
        origin = candidate["audit"].get("execution_identity") or {}
        stale = chapter is None or content_hash(chapter.get("content") or "") != expected_hash or chapter["revision"] != expected_revision
        if not committed:
            stale = stale or bool(origin and origin.get("run_epoch") != novel["autopilot_run_epoch"])
            stale = stale or ("review_epoch" in candidate["audit"] and candidate["audit"]["review_epoch"] != novel["autopilot_run_epoch"])
        is_generating = novel["autopilot_status"] == "running" and novel["current_stage"] != "paused_for_review"
        return {**candidate, "stale": stale, "is_generating": is_generating,
                "current_revision": chapter["revision"] if chapter else None,
                "current_content_hash": content_hash(chapter.get("content") or "") if chapter else None,
                "run_epoch": novel["autopilot_run_epoch"], "novel_stage": novel["current_stage"],
                "can_confirm": not stale and not is_generating and candidate["status"] in {"audited", "pending_review"}}

    def list(self, novel_id, chapter_number=None, limit=50):
        self._novel(novel_id)
        return [self.detail(novel_id, item["candidate_id"]) for item in self.candidates.list_for_chapter(novel_id, chapter_number, limit)]

    async def _voice(self, novel_id, chapter_number, content):
        service = self.voice_service
        if service is None:
            return {"status": "unavailable", "baseline_available": False, "similarity_score": None, "error": "声线复验服务未配置"}
        try:
            if service.use_llm_mode:
                score = await service.score_chapter_async(novel_id, chapter_number, content, persist=False)
            else:
                score = await asyncio.to_thread(service.score_chapter, novel_id, chapter_number, content, persist=False)
            if not isinstance(score, dict) or score.get("similarity_score") is None:
                return {"status": "unavailable", "baseline_available": False, "similarity_score": None,
                        "error": "本书缺少有效声线基准，请在声线样本中建立基准后重试"}
            return {**score, "status": score.get("status", "completed"), "baseline_available": True}
        except Exception as exc:
            return {"status": "failed", "similarity_score": None,
                    "error": _safe_error_summary(exc, "声线复验不可用")}

    def _workflow(self, novel_id, chapter_number):
        return AntiAIRewriteWorkflow(self.db, self.llm, auditor=self.auditor, validator=self.validator,
            score_voice=lambda content: self._voice(novel_id, chapter_number, content))

    @staticmethod
    def _tokens(candidate, *, expected_candidate_hash, expected_base_hash, expected_base_revision, expected_run_epoch):
        if (candidate["content_hash"] != expected_candidate_hash or candidate["base_hash"] != expected_base_hash
                or candidate["base_revision"] != expected_base_revision or candidate["run_epoch"] != expected_run_epoch):
            raise CandidateConflictError("确认参数与展示的候选/原稿/运行版本不一致，请刷新候选详情")

    async def _event(self, novel_id, name, candidate, **extra):
        if self.on_event is not None:
            value = self.on_event(novel_id, name, {"candidate_id": candidate["candidate_id"],
                "chapter_number": candidate["chapter_number"], "run_id": candidate["run_id"],
                "content_hash": candidate["content_hash"], "revision": candidate["audit"].get("committed_revision"), **extra})
            if asyncio.iscoroutine(value):
                await value

    async def confirm(self, novel_id, candidate_id, **tokens):
        candidate = await asyncio.to_thread(self.detail, novel_id, candidate_id)
        self._tokens(candidate, **tokens)
        if candidate["stale"] or candidate["is_generating"]:
            if candidate["status"] != "committed" and candidate["stale"]:
                await asyncio.to_thread(self.candidates.mark_failed, candidate_id, "原稿或运行版本已变化，请重新生成或手工合并", "conflict")
            raise CandidateConflictError("当前正在生成，或候选依据的版本已过期；正文未覆盖")
        workflow = self._workflow(novel_id, candidate["chapter_number"])
        policy = await asyncio.to_thread(self.candidates.get_policy, novel_id)
        if candidate["status"] != "committed":
            if candidate["status"] not in {"audited", "pending_review"}:
                raise CandidateConflictError("只有已复验的待确认候选可以提交")
            baseline = await asyncio.to_thread(workflow.scan, candidate["chapter_id"], candidate["base_content"])
            voice = await self._voice(novel_id, candidate["chapter_number"], candidate["base_content"])
            candidate = await workflow.validate_candidate(candidate, max_severity=policy["max_severity"], baseline_report=baseline, baseline_voice=voice)
            if candidate["status"] != "audited":
                await self._event(novel_id, "audit_rewrite_review_failed", candidate, success=False, reasons=candidate["audit"].get("reasons"))
                raise CandidateReviewError("候选实时复验失败，正文保持原样；请检查复验结果后重新生成")
            commit = asyncio.create_task(asyncio.to_thread(self.candidates.commit_candidate, candidate_id,
                tokens["expected_candidate_hash"], execution_guard={"author_review": True, "run_epoch": tokens["expected_run_epoch"]}))
            try:
                candidate = await asyncio.shield(commit)
            except asyncio.CancelledError:
                while not commit.done():
                    try:
                        await asyncio.shield(commit)
                    except asyncio.CancelledError:
                        continue
                commit.result()
                raise
        final = await asyncio.to_thread(workflow.scan, candidate["chapter_id"], candidate["content"])
        if final.metrics.critical_hits or final.metrics.severity_score > policy["max_severity"]:
            raise CandidateReviewError("正文已提交，但最终规则扫描未通过；请检查候选记录")
        await asyncio.to_thread(self.candidates.assert_committed_version, candidate)
        final.run_id = uuid4().hex
        final.persistence_status = "committed"
        snapshot = get_anti_ai_metrics_service().compute_snapshot(chapter_id=candidate["chapter_id"],
            chapter_number=candidate["chapter_number"], content=candidate["content"], hits=final.hits)
        await asyncio.to_thread(SqliteAntiAiAuditRepository(self.db).save_report, novel_id, candidate["chapter_number"], final, snapshot)
        candidate = await asyncio.to_thread(self.candidates.record_final_audit, candidate_id, final)
        if self.aftermath is not None:
            try:
                result = await self.aftermath.run_after_chapter_saved(novel_id, candidate["chapter_number"], candidate["content"],
                    voice_result=candidate["audit"].get("voice"), expected_revision=candidate["audit"]["committed_revision"],
                    expected_content_hash=candidate["content_hash"])
            except Exception as exc:
                result = {"narrative_sync_ok": False, "derivation_status": "failed",
                          "error": _safe_error_summary(exc, "章后关联分析不可用")}
            candidate = await asyncio.to_thread(self.candidates.record_aftermath, candidate_id, result)
            if result.get("narrative_sync_ok") is not True or result.get("derivation_status") in {"failed", "superseded"}:
                raise CandidateReviewError("候选正文和规则审计已提交，但章后派生尚未完成；请查看结果并重试")
        await asyncio.to_thread(self.candidates.assert_committed_version, candidate)
        await self._event(novel_id, "audit_rewrite_committed", candidate, success=True, status="committed", requires_resume=True)
        return {"status": "committed", "candidate": await asyncio.to_thread(self.detail, novel_id, candidate_id), "requires_resume": True}

    async def reject(self, novel_id, candidate_id, expected_candidate_hash):
        candidate = await asyncio.to_thread(self.detail, novel_id, candidate_id)
        if candidate["content_hash"] != expected_candidate_hash or candidate["status"] == "committed":
            raise CandidateConflictError("候选已提交或展示版本不匹配，不能拒绝")
        if candidate["is_generating"]:
            raise CandidateConflictError("请先暂停生成再处理候选")
        candidate = await asyncio.to_thread(self.candidates.mark_failed, candidate_id, "作者拒绝候选；保留原稿与候选，可编辑原稿后重新生成", "rejected")
        await self._event(novel_id, "audit_rewrite_rejected", candidate, success=False, status="rejected", requires_resume=True)
        return await asyncio.to_thread(self.detail, novel_id, candidate_id)

    async def regenerate(self, novel_id, chapter_number, *, expected_base_hash, expected_base_revision, expected_run_epoch):
        novel = await asyncio.to_thread(self._novel, novel_id)
        if novel["autopilot_run_epoch"] != expected_run_epoch or novel["autopilot_status"] == "running" and novel["current_stage"] != "paused_for_review":
            raise CandidateConflictError("运行状态已变化，请刷新后在暂停状态下重新生成")
        if self.llm is None:
            raise CandidateReviewError("未配置真实改写模型")
        workflow = self._workflow(novel_id, chapter_number)
        policy = await asyncio.to_thread(self.candidates.get_policy, novel_id)
        def unchanged():
            current = self._novel(novel_id)
            return current["autopilot_run_epoch"] == expected_run_epoch and (current["autopilot_status"] != "running" or current["current_stage"] == "paused_for_review")
        result = await workflow.execute(novel_id, chapter_number, mode="review", force_rewrite=True,
            max_attempts=policy["max_attempts"], max_severity=policy["max_severity"], is_running=unchanged,
            expected_base_hash=expected_base_hash, expected_base_revision=expected_base_revision)
        if result.get("candidate") and result["status"] == "awaiting_confirmation":
            candidate = result["candidate"]
            candidate = await asyncio.to_thread(self.candidates.mark_audited, candidate["candidate_id"],
                {**candidate["audit"], "review_epoch": expected_run_epoch}, True)
            candidate = await asyncio.to_thread(self.candidates.mark_failed, candidate["candidate_id"], "候选已复验，等待作者确认", "pending_review")
            result["candidate"] = await asyncio.to_thread(self.detail, novel_id, candidate["candidate_id"])
        return result
