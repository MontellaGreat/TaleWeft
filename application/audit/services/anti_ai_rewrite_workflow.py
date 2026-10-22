"""Scan, rewrite, revalidate and preserve a candidate before any chapter write."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import inspect
import math
from uuid import uuid4

from application.audit.services.anti_ai_audit import AntiAIAuditor, validate_completed_report
from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates, CandidateConflictError, content_hash
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.ai.llm_output_sanitize import strip_reasoning_artifacts
from engine.runtime.policy_validator import PolicyValidator


class AntiAIRewriteWorkflow:
    def __init__(self, db, llm=None, *, auditor=None, validator=None, score_voice=None):
        self.db, self.llm = db, llm
        self.candidates = ChapterRewriteCandidates(db)
        from infrastructure.ai.quality_rule_store import QualityRuleStore
        self.auditor = auditor or AntiAIAuditor(rule_provider=lambda: QualityRuleStore(self.db).effective())
        self.validator = validator or PolicyValidator()
        self.score_voice = score_voice

    def scan(self, chapter_id, content):
        if not content or not content.strip():
            raise ValueError("扫描正文不能为空")
        report = self.auditor.scan_chapter(chapter_id, content)
        validate_completed_report(report,content)
        return report

    async def validate_candidate(self, candidate, *, max_severity, baseline_report=None, baseline_voice=None):
        if content_hash(candidate["content"]) != candidate["content_hash"]:
            raise CandidateConflictError("候选正文与 hash 不一致，不能复验")
        # Use the persisted binding, never a caller-supplied evidence dictionary.
        durable_proof = await asyncio.to_thread(self.candidates.verify_prose_evidence, candidate)
        report = await asyncio.to_thread(self.scan, candidate["chapter_id"], candidate["content"])
        policy = await asyncio.to_thread(self.validator.check, text=candidate["content"])
        reasons = []
        if report.metrics.critical_hits or report.metrics.severity_score > max_severity:
            reasons.append("AI 味规则仍超出阈值")
        if (baseline_report and (baseline_report.metrics.critical_hits or baseline_report.metrics.severity_score > max_severity)
                and report.metrics.severity_score >= baseline_report.metrics.severity_score):
            reasons.append("规则评分没有改善")
        if not policy.passed or policy.status != "passed":
            reasons.append(policy.error or "内容质量复验未通过")
        voice = {"status": "skipped", "reason": "本次没有要求声线复验"}
        if self.score_voice is not None:
            voice = await self.score_voice(candidate["content"])
            similarity = voice.get("similarity_score") if isinstance(voice, dict) else None
            valid_similarity = (isinstance(similarity, (int, float)) and not isinstance(similarity, bool)
                                and math.isfinite(similarity) and 0 <= similarity <= 1)
            if (not isinstance(voice, dict) or voice.get("status") in {"failed", "unavailable", "skipped", "cancelled"}
                    or not valid_similarity or voice.get("drift_alert")):
                reasons.append("声线复验未通过或不可用")
            before = (baseline_voice or {}).get("similarity_score")
            if before is not None and (not valid_similarity or float(similarity) < float(before)):
                reasons.append("声线相似度没有保持")
        if candidate["content"].strip() == candidate["base_content"].strip():
            reasons.append("没有产生正文变化")
        audit = {"content_hash": candidate["content_hash"], "base_hash": candidate["base_hash"],
                 "base_revision": candidate["base_revision"], "run_id": candidate["run_id"], "scan": asdict(report),
                 "quality": policy.to_dict(), "voice": voice, "accepted": not reasons, "reasons": reasons}
        if durable_proof is not None:
            audit["prose_evidence"] = durable_proof
        if candidate.get("audit", {}).get("execution_identity"):
            audit["execution_identity"] = candidate["audit"]["execution_identity"]
        if "review_epoch" in candidate.get("audit", {}):
            audit["review_epoch"] = candidate["audit"]["review_epoch"]
        return await asyncio.to_thread(self.candidates.mark_audited, candidate["candidate_id"], audit, not reasons)

    async def propose(self, novel_id, chapter_number, *, max_attempts=2, max_severity=3.0,
                      run_id=None, rewrite=None, force_rewrite=False, baseline_voice=None, is_running=None, on_event=None,
                      expected_base_hash=None, expected_base_revision=None, on_candidate=None, candidate_kind="rewrite"):
        if not 1 <= max_attempts <= 5 or not 0 <= max_severity <= 100:
            raise ValueError("无效改写边界")
        run_id = run_id or uuid4().hex
        chapter = await asyncio.to_thread(self.db.fetch_one,
            "SELECT * FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
        if chapter is None:
            raise LookupError("章节不存在")
        original = chapter.get("content") or ""
        if isinstance(self.auditor,AntiAIAuditor):
            scene_type=self.candidates.get_policy(novel_id).get("scene_type","default")
            self.auditor=AntiAIAuditor(rule_provider=self.auditor.rule_provider,scene_type=scene_type)
        if (expected_base_hash is not None and content_hash(original) != expected_base_hash
                or expected_base_revision is not None and chapter.get("revision") != expected_base_revision):
            raise CandidateConflictError("原稿在改写前已被编辑，不能审计旧版本")
        if is_running is not None and not is_running():
            return {"run_id": run_id, "status": "cancelled", "candidate": None}
        original_report = await asyncio.to_thread(self.scan, chapter["id"], original)
        if not force_rewrite and not original_report.metrics.critical_hits and original_report.metrics.severity_score <= max_severity:
            return {"run_id": run_id, "status": "skipped", "reason": "扫描通过，无需改写", "scan": asdict(original_report), "candidate": None}
        current, latest = original, None
        try:
            for attempt in range(1, max_attempts + 1):
                if is_running is not None and not is_running():
                    if latest:
                        await asyncio.to_thread(self.candidates.mark_failed, latest["candidate_id"], "用户停止改写", "cancelled")
                    return {"run_id": run_id, "status": "cancelled", "candidate": latest}
                await self._emit(on_event, "rewrite_start", {"run_id": run_id, "attempt": attempt, "chapter_number": chapter_number})
                durable_proof = None
                if rewrite is not None:
                    rewritten = await rewrite(current, attempt)
                else:
                    if self.llm is None:
                        raise RuntimeError("没有配置改写 provider")
                    findings = json.dumps([asdict(hit) for hit in (await asyncio.to_thread(self.scan, chapter["id"], current)).hits], ensure_ascii=False)
                    prompt = Prompt(
                        system="你是小说编辑。依据规则命中修改叙述，保留全部人物、事实、因果、时间线、对白信息和章节情节。不要增加事件，不要解释修改，只输出完整正文。用具体动作与感官细节替代俗套表达。",
                        user=f"章节大纲：{chapter.get('outline') or ''}\n规则命中：{findings}\n原稿：\n{current}")
                    from application.engine.rules.prose_prompt_hooks import generate_direct_prose
                    from application.ai_invocation.prose_execution_evidence import verify_durable_prose_receipts
                    evidence = {}
                    result = await asyncio.wait_for(generate_direct_prose(self.llm,prompt,GenerationConfig(
                        temperature=0.35, max_tokens=max(2048, min(8192, int(len(original) * 1.5)))), db=self.db,
                        context={"novel_id":novel_id,"chapter_number":chapter_number,"job_id":f"{run_id}:{attempt}"},
                        evidence_sink=evidence,
                        content_transform=lambda body: strip_reasoning_artifacts(body.strip()).strip()), timeout=300)
                    rewritten = getattr(result, "content", result if isinstance(result, str) else None)
                    durable_proof = await asyncio.to_thread(verify_durable_prose_receipts,
                        self.db, novel_id=novel_id, chapter_number=chapter_number,
                        attempt_id=f"{run_id}:{attempt}", content=rewritten,
                        prompt_hash=evidence.get("prompt_hash", ""), require_stream=False,
                        prompt_run_id=evidence.get("run_id", ""),
                        scan_run_id=(evidence.get("anti_ai_scan") or {}).get("run_id", ""))
                if rewrite is not None:
                    rewritten = strip_reasoning_artifacts((rewritten or "").strip())
                if not rewritten:
                    raise RuntimeError("provider 未返回有效改写正文")
                if on_candidate:
                    value = on_candidate(rewritten, attempt, run_id)
                    if inspect.isawaitable(value):
                        await value
                latest = await asyncio.to_thread(self.candidates.save_candidate,
                    run_id=run_id, novel_id=novel_id, chapter_id=chapter["id"], chapter_number=chapter_number,
                    content=rewritten, kind=candidate_kind, attempt=attempt, base_content=original,
                    base_hash=content_hash(original), base_revision=chapter.get("revision"),
                    prose_evidence=durable_proof)
                if is_running is not None and not is_running():
                    latest = await asyncio.to_thread(self.candidates.mark_failed, latest["candidate_id"], "改写已停止或执行租约失效", "cancelled")
                    return {"run_id": run_id, "status": "cancelled", "candidate": latest}
                latest = await self.validate_candidate(latest, max_severity=max_severity,
                    baseline_report=original_report, baseline_voice=baseline_voice)
                await self._emit(on_event, "rewrite_audited", {"run_id": run_id, "candidate_id": latest["candidate_id"],
                    "status": latest["status"], "content_hash": latest["content_hash"], "attempt": attempt})
                if is_running is not None and not is_running():
                    latest = await asyncio.to_thread(self.candidates.mark_failed, latest["candidate_id"], "复验后运行已停止或执行租约失效", "cancelled")
                    return {"run_id": run_id, "status": "cancelled", "candidate": latest}
                if latest["status"] == "audited":
                    return {"run_id": run_id, "status": "awaiting_confirmation", "candidate": latest}
                current = rewritten
            return {"run_id": run_id, "status": "rejected", "candidate": latest, "error": "改写达到上限，未通过复验；原稿未改变"}
        except asyncio.CancelledError:
            if latest:
                await asyncio.shield(asyncio.to_thread(self.candidates.mark_failed, latest["candidate_id"], "改写已取消", "cancelled"))
            raise
        except Exception as exc:
            if latest:
                await asyncio.to_thread(self.candidates.mark_failed, latest["candidate_id"], str(exc))
            await self._emit(on_event, "rewrite_failed", {"run_id": run_id, "error": str(exc), "chapter_number": chapter_number})
            raise

    @staticmethod
    async def _emit(callback, event, payload):
        if callback is not None:
            value = callback(event, payload)
            if inspect.isawaitable(value):
                await value

    async def execute(self, novel_id, chapter_number, *, mode="scan_only", execution_guard=None,
                      is_running=None, on_event=None, **kwargs):
        """Apply explicit author policy; a successful return requires commit ACK and exact final audit."""
        if mode not in {"scan_only", "review", "auto"}:
            raise ValueError("无效自动去味策略")
        if mode == "scan_only":
            chapter = await asyncio.to_thread(self.db.fetch_one,
                "SELECT * FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
            if chapter is None:
                raise LookupError("章节不存在")
            if kwargs.get("expected_base_hash") is not None and content_hash(chapter.get("content") or "") != kwargs["expected_base_hash"]:
                raise CandidateConflictError("扫描对象已被作者编辑，不能复用旧正文")
            if kwargs.get("expected_base_revision") is not None and chapter.get("revision") != kwargs["expected_base_revision"]:
                raise CandidateConflictError("扫描正文 revision 已变化，不能复用旧审计")
            if is_running is not None and not is_running():
                return {"status": "cancelled", "candidate": None}
            report = await asyncio.to_thread(self.scan, chapter["id"], chapter.get("content") or "")
            await self._emit(on_event, "rewrite_skipped", {"chapter_number": chapter_number,
                "status": "scan_only", "reason": "作品策略为仅扫描", "content_hash": report.content_hash})
            return {"status": "scan_only", "reason": "作品策略为仅扫描", "scan": asdict(report), "candidate": None}
        result = await self.propose(novel_id, chapter_number, is_running=is_running, on_event=on_event, **kwargs)
        candidate = result.get("candidate")
        if result["status"] != "awaiting_confirmation":
            return result
        if execution_guard:
            candidate = await asyncio.to_thread(self.candidates.mark_audited, candidate["candidate_id"],
                {**candidate["audit"], "execution_identity": dict(execution_guard)}, True)
        if mode == "review":
            candidate = await asyncio.to_thread(self.candidates.mark_failed, candidate["candidate_id"], "候选已通过复验，等待作者确认", "pending_review")
            await self._emit(on_event, "rewrite_pending_review", {"run_id": result["run_id"],
                "candidate_id": candidate["candidate_id"], "content_hash": candidate["content_hash"], "status": "pending_review"})
            return {**result, "candidate": candidate}
        if is_running is not None and not is_running():
            candidate = await asyncio.to_thread(self.candidates.mark_failed, candidate["candidate_id"], "提交前运行已停止或执行租约失效", "cancelled")
            return {**result, "status": "cancelled", "candidate": candidate}
        # Cancellation must not abandon an in-flight writer ACK and release
        # ownership while SQLite can still commit the queued operation.
        mutation = asyncio.create_task(asyncio.to_thread(self.candidates.commit_candidate,
            candidate["candidate_id"], candidate["content_hash"], execution_guard=execution_guard))
        try:
            committed = await asyncio.shield(mutation)
        except asyncio.CancelledError:
            while not mutation.done():
                try:
                    await asyncio.shield(mutation)
                except asyncio.CancelledError:
                    continue
            # Retrieve exceptions and preserve committed/candidate evidence.
            mutation.result()
            raise
        except Exception as exc:
            # A timeout is an unknown outcome. Never undo a commit or discard
            # its candidate; the caller reports failure and recovery can read it.
            await self._emit(on_event, "rewrite_commit_failed", {"run_id": result["run_id"],
                "candidate_id": candidate["candidate_id"], "status": "failed", "error": str(exc)})
            raise
        if committed["status"] != "committed":
            raise RuntimeError("改写正文未确认提交")
        if is_running is not None and not is_running():
            raise RuntimeError("正文已提交，但运行已停止或执行租约失效；不能报告审计成功")
        final_report = await asyncio.to_thread(self.scan, committed["chapter_id"], committed["content"])
        await asyncio.to_thread(self.candidates.assert_committed_version, committed)
        if final_report.metrics.critical_hits or final_report.metrics.severity_score > kwargs.get("max_severity", 3.0):
            raise RuntimeError("已提交正文的最终 Anti-AI 审计未通过")
        if is_running is not None and not is_running():
            raise RuntimeError("最终审计后运行已停止或执行租约失效；不能报告审计成功")
        await self._emit(on_event, "rewrite_committed", {"run_id": result["run_id"],
            "candidate_id": committed["candidate_id"], "content_hash": committed["content_hash"],
            "revision": committed["audit"].get("committed_revision"), "status": "committed"})
        return {**result, "status": "committed", "candidate": committed, "final_scan": asdict(final_report)}
