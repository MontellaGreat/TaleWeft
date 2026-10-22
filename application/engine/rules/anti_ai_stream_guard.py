"""Production prose-stream scanning with explicit warn/cancel policy.

The guard yields source fragments unchanged. An optional finalizer runs before
the full-body scan and binds receipts to the adopted text. Warnings are advisory by default;
authors may explicitly stop streams after a configured count of critical
matches.  The EOF scan is separate from window scanning and chapter audits
remain mandatory after the resulting manuscript is committed.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import inspect
import json
import logging
from uuid import uuid4

from application.audit.services.anti_ai_audit import AntiAIAuditor, validate_completed_report
from application.engine.rules.chunked_buffer_scanner import ChunkedBufferScanner
from application.engine.rules.stream_ac_scanner import StreamACScanner

logger = logging.getLogger(__name__)


class AntiAIStreamInterrupted(RuntimeError):
    """An explicitly configured threshold stopped a still-recoverable stream."""


class AntiAIStreamGuard:
    def __init__(self, *, novel_id="", chapter_number=None, job_id="", db=None,
                 action=None, critical_limit=None, on_event=None, chunk_size=200, prompt_evidence=None, scene_type=None, content_transform=None):
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.ai.quality_rule_store import QualityRuleStore
        self.db = db or get_database()
        self.novel_id, self.chapter_number, self.job_id = novel_id or "", chapter_number, job_id or ""
        self.run_id = uuid4().hex
        self.job_id = self.job_id or self.run_id
        self.prompt_evidence = prompt_evidence
        self.on_event = on_event
        self.content_transform = content_transform
        policy = {}
        if self.novel_id:
            from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
            policy = ChapterRewriteCandidates(self.db).get_policy(self.novel_id)
        self.action = action or policy.get("stream_action", "warn")
        self.critical_limit = critical_limit if critical_limit is not None else policy.get("stream_critical_limit", 1)
        if self.action not in {"warn", "cancel"} or type(self.critical_limit) is not int or not 1 <= self.critical_limit <= 100:
            raise ValueError("无效流式去味策略")
        effective = QualityRuleStore(self.db).effective()
        self.scene_type = scene_type or policy.get("scene_type", "default")
        self.auditor = AntiAIAuditor(rule_provider=lambda: effective, scene_type=self.scene_type)
        self.rule_version = self.auditor.effective_rule_version(effective)
        scanner = StreamACScanner()
        scanner.add_patterns_from_list(effective["patterns"])
        self.scanner = ChunkedBufferScanner(chunk_size=chunk_size, scanner=scanner)
        self._seen_matches = set()
        self.summary = {"run_id": self.run_id, "job_id": self.job_id, "action": self.action,
            "critical_limit": self.critical_limit, "chunks_scanned": 0, "incremental_hits": 0,
            "critical_hits": 0, "final_scan_status": "not_run", "status": "running", "persistence_status": "not_requested"}
        self.summary["scene_type"] = self.scene_type
        self.summary["incremental_cancel_deferred_for_scene"] = self.scene_type != "default"

    async def _event(self, name, **payload):
        data = {"run_id": self.run_id, "job_id": self.job_id, "novel_id": self.novel_id,
            "chapter_number": self.chapter_number, "rule_version": self.rule_version, **payload}
        try:
            if self.on_event:
                result = self.on_event(name, data)
                if inspect.isawaitable(result):
                    await result
            elif self.novel_id:
                from application.engine.services.streaming_bus import streaming_bus
                streaming_bus.publish_audit_event(self.novel_id, name, data)
        except Exception as exc:
            # SSE is advisory.  Its failure must not discard provider output;
            # the immutable terminal receipt remains available over HTTP.
            self.summary["event_delivery_errors"] = self.summary.get("event_delivery_errors", 0) + 1
            self.summary["last_event_delivery_error"] = str(exc)
            logger.warning("流式去味事件推送失败 run=%s event=%s: %s", self.run_id, name, exc)

    async def _window(self, result):
        if result is None:
            return
        hits = []
        for hit in result.scan_results:
            # A growing regex can match the same start in successive windows.
            # Count the rule occurrence once rather than triggering a false
            # cancellation threshold from repeated overlap scans.
            key = (hit.pattern_name, hit.position)
            if key not in self._seen_matches:
                self._seen_matches.add(key)
                hits.append(hit)
        self.summary["chunks_scanned"] += 1
        self.summary["incremental_hits"] += len(hits)
        self.summary["critical_hits"] += sum(hit.severity == "critical" for hit in hits)
        if hits:
            await self._event("anti_ai_stream_warning", hits=[asdict(hit) for hit in hits],
                chunk_index=result.chunk_index, critical_hits=self.summary["critical_hits"], action=self.action)
        if self.action == "cancel" and self.scene_type == "default" and self.summary["critical_hits"] >= self.critical_limit:
            raise AntiAIStreamInterrupted(f"流式去味达到显式停止阈值（严重命中 {self.summary['critical_hits']}/{self.critical_limit}）；已生成片段保留")

    async def _archive(self, status, content, error=None):
        self.summary.update(status=status, content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            rule_version=self.rule_version, error=error, persistence_status="committed")
        receipt_errors = []
        if self.prompt_evidence is not None:
            from application.engine.rules.prose_prompt_hooks import archive_prompt_use
            evidence = {**self.prompt_evidence, "content_hash": self.summary["content_hash"]}
            prompt_receipt = archive_prompt_use(self.db, evidence,
                context={"novel_id": self.novel_id, "chapter_number": self.chapter_number},
                job_id=self.job_id, status=status, error=error)
            self.summary["prompt_receipt"] = prompt_receipt
            if prompt_receipt.get("persistence_status") != "committed":
                self.summary["persistence_status"] = "failed"
                if status == "completed":
                    self.summary["status"] = "failed"
                receipt_errors.append("去味提示词执行回执未持久化，正文不可采纳")
                self.summary["error"] = "; ".join(filter(None, (error, receipt_errors[-1])))
        try:
            # No per-token writes: one immutable terminal receipt.  The same
            # transaction writer is used in daemon and API processes.
            with self.db.transaction() as writer:
                writer.execute("""INSERT INTO anti_ai_stream_runs
                    (run_id,novel_id,chapter_number,job_id,status,scan_status,persistence_status,
                     content_hash,rule_version,action,summary_json,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.run_id,self.novel_id,self.chapter_number,self.job_id,self.summary["status"],
                     self.summary["final_scan_status"],self.summary["persistence_status"],self.summary["content_hash"],self.rule_version,
                     self.action,json.dumps(self.summary,ensure_ascii=False),self.summary["error"]))
        except Exception as exc:
            self.summary["persistence_status"] = "failed"
            self.summary["archive_error"] = str(exc)
            receipt_errors.append("流式去味执行回执未持久化，正文不可采纳")
            logger.warning("流式去味回执未归档 run=%s: %s", self.run_id, exc)
        await self._event("anti_ai_stream_terminal", **self.summary)
        if self.summary["final_scan_status"] == "completed" and self.summary["persistence_status"] != "committed":
            if not receipt_errors:
                receipt_errors.append("去味最终扫描已完成，但扫描回执未提交，正文不可采纳")
        if receipt_errors:
            raise RuntimeError("；".join(receipt_errors))

    async def wrap(self, source):
        content_parts = []
        final_content = None
        status, error = "cancelled", None
        await self._event("anti_ai_stream_start", status="running", action=self.action,
            critical_limit=self.critical_limit)
        try:
            async for piece in source:
                if not piece:
                    continue
                content_parts.append(piece)
                # Preserve the source fragment before a configured interruption
                # so invocation recovery and author previews keep this text.
                yield piece
                await self._window(self.scanner.append_text(piece))
            await self._window(self.scanner.flush_remaining())
            content = "".join(content_parts)
            if self.content_transform is not None:
                self.summary["raw_content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                content = self.content_transform(content)
            if not isinstance(content, str):
                raise RuntimeError("流式正文清洗结果无效")
            if not content.strip():
                raise RuntimeError("流式正文为空，未完成最终扫描")
            final_content = content
            final = self.auditor.scan_chapter(str(self.chapter_number or ""), content)
            validate_completed_report(final, content, rule_version=self.rule_version)
            self.summary.update(final_scan_status="completed", final_total_hits=final.metrics.total_hits,
                final_critical_hits=final.metrics.critical_hits, final_severity_score=final.metrics.severity_score,
                exempted_hits=len(final.exempted_hits))
            await self._event("anti_ai_stream_final_scan", scan_status="completed", content_hash=final.content_hash,
                total_hits=final.metrics.total_hits, critical_hits=final.metrics.critical_hits)
            # The full scan covers long custom regexes and window boundary cases.
            if self.action == "cancel" and final.metrics.critical_hits >= self.critical_limit:
                raise AntiAIStreamInterrupted("流式最终扫描达到显式停止阈值；已生成片段保留")
            status = "completed"
        except AntiAIStreamInterrupted as exc:
            status, error = "interrupted", str(exc)
            raise
        except (GeneratorExit, asyncio.CancelledError):
            status, error = "cancelled", "流式生成已取消，最终扫描未确认"
            raise
        except Exception as exc:
            status, error = "failed", str(exc)
            if self.summary["final_scan_status"] == "not_run":
                self.summary["final_scan_status"] = "failed"
            raise
        finally:
            close_error = None
            completed_before_close = status == "completed"
            try:
                if hasattr(source, "aclose"):
                    await source.aclose()
            except (GeneratorExit, asyncio.CancelledError) as exc:
                close_error = exc
                status = "cancelled"
                self.summary["source_close_status"] = "cancelled"
                error = "; ".join(filter(None, (error, str(exc) or "流式提供者关闭已取消")))
            except Exception as exc:
                close_error = exc
                self.summary["source_close_status"] = "failed"
                error = "; ".join(filter(None,(error,str(exc))))
                if completed_before_close:
                    status = "failed"
            finally:
                await self._archive(status, final_content if final_content is not None else "".join(content_parts), error)
            if close_error and (completed_before_close or isinstance(close_error, (GeneratorExit, asyncio.CancelledError))):
                raise close_error


def is_prose_operation(operation: str) -> bool:
    """Apply to prose operations; planning, scripts and structured JSON are separate."""
    value = (operation or "").lower()
    return "prose" in value or value in {"chapter.generate", "autopilot.voice.rewrite", "chapter.voice.rewrite"}


def prepare_direct_prose_stream(provider, prompt, config, *, context, db=None, evidence_sink=None, content_transform=None):
    """Legacy prose paths share the same prompt and streaming safeguards."""
    from application.engine.rules.prose_prompt_hooks import ProsePromptHooks
    if not str(context.get("novel_id") or "").strip() or type(context.get("chapter_number")) is not int or context["chapter_number"] <= 0:
        raise ValueError("流式正文缺少有效小说或章节号，无法绑定去味回执")
    hooks = ProsePromptHooks(db)
    from application.core.services.chapter_derivation_readiness import assert_previous_derivations_ready
    assert_previous_derivations_ready(hooks.db, context["novel_id"], context["chapter_number"])
    composed, evidence = hooks.compose(prompt, context)
    guard = AntiAIStreamGuard(db=hooks.db, novel_id=str(context.get("novel_id") or ""),
        chapter_number=context.get("chapter_number"), job_id=str(context.get("job_id") or ""),
        prompt_evidence=evidence,scene_type=evidence["scene_type"],content_transform=content_transform)
    async def guarded():
        assert_previous_derivations_ready(hooks.db, context["novel_id"], context["chapter_number"])
        stream = guard.wrap(provider.stream_generate(composed, config))
        try:
            async for piece in stream:
                yield piece
        finally:
            try:
                await stream.aclose()
            finally:
                if evidence_sink is not None:
                    evidence_sink.clear()
                    evidence_sink.update(guard.summary)
    return guarded()
