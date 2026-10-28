"""The DAG and chapter audit share the production rule scanner."""
from __future__ import annotations

import time
import hashlib
import math
import json
import re
from dataclasses import asdict
from typing import Any, Dict

from application.engine.dag.models import NodeResult, NodeStatus


def _scan_failure(content: Any, message: str, *, scan_status: str) -> NodeResult:
    """Return explicit non-success evidence without manufacturing a clean scan."""
    outputs = {
        "scan_status": scan_status,
        "scanner": "AntiAIAuditor",
    }
    if isinstance(content, str):
        outputs["input_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return NodeResult(
        outputs=outputs,
        status=NodeStatus.ERROR,
        error=message,
    )


def scan_tail(content: str) -> dict:
    """Deterministic specialist receipt, also rechecked at the submit gate."""
    from application.engine.rules.mid_generation_refresh import get_finale_enhancer
    from application.audit.services.cliche_scanner import ClicheHit
    enhancer = get_finale_enhancer()
    paragraphs = [match for match in re.finditer(r"[^\r\n]+", content) if match.group().strip()]
    if not paragraphs:
        raise ValueError("尾段扫描需要非空正文")
    start = paragraphs[max(0, len(paragraphs) - 3)].start()
    tail = content[start:]
    hits = []
    for pattern, name, suggestion in enhancer.FINALE_AI_PATTERNS:
        for match in re.finditer(pattern, tail):
            hits.append(asdict(ClicheHit(pattern=f"尾段：{name}", text=match.group(),
                start=start + match.start(), end=start + match.end(),
                severity="warning", category="尾段专项", replacement_hint=suggestion)))
    version = hashlib.sha256(json.dumps(enhancer.FINALE_AI_PATTERNS,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"scan_status": "completed", "input_hash": hashlib.sha256(content.encode()).hexdigest(),
            "tail_hash": hashlib.sha256(tail.encode()).hexdigest(), "start": start,
            "effective_rule_count": len(enhancer.FINALE_AI_PATTERNS), "rule_version": version,
            "total_hits": len(hits), "hits": hits}


def scan_chapter(content: Any, context: Dict[str, Any]) -> NodeResult:
    start = time.monotonic()
    if not isinstance(content, str) or not content.strip():
        result = _scan_failure(content, "Anti-AI 审计需要非空章节正文", scan_status="skipped")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    input_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        from application.audit.services.anti_ai_audit import get_anti_ai_auditor
        chapter_id = str(context.get("shared_state", {}).get("chapter_id") or context.get("chapter_number", ""))
        services = context.get("services") or context.get("shared_state", {}).get("services")
        db = getattr(services, "db", None)
        auditor = get_anti_ai_auditor(db=db) if db is not None else get_anti_ai_auditor()
        from application.audit.services.anti_ai_audit import AntiAIAuditor, validate_completed_report
    except Exception as exc:
        result = _scan_failure(content, str(exc) or "Anti-AI scanner unavailable", scan_status="unavailable")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    if auditor is None or not callable(getattr(auditor, "scan_chapter", None)):
        result = _scan_failure(content, "Anti-AI scanner unavailable", scan_status="unavailable")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    try:
        if isinstance(auditor, AntiAIAuditor):
            scene_type = context.get("shared_state",{}).get("scene_type")
            novel_id = context.get("shared_state",{}).get("novel_id")
            if not scene_type and novel_id and db is not None:
                from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
                scene_type = ChapterRewriteCandidates(db).get_policy(novel_id).get("scene_type", "default")
            if scene_type and scene_type != "default":
                auditor = AntiAIAuditor(rule_provider=auditor.rule_provider,scene_type=scene_type)
        report = auditor.scan_chapter(chapter_id, content)
        if report is not None and report.scan_status == "coverage_empty":
            result = _scan_failure(content, report.error or "Anti-AI 有效规则覆盖为空", scan_status="coverage_empty")
            result.outputs.update(effective_rule_count=0, scan_mode="local_rules", rule_version=report.rule_version)
            result.duration_ms = int((time.monotonic() - start) * 1000)
            return result
        validate_completed_report(report, content)
        if report is None or report.metrics is None:
            raise RuntimeError("Anti-AI scanner returned no report")
        if report.scan_status != "completed" or report.error:
            raise RuntimeError(report.error or "Anti-AI scanner did not complete")
        if report.content_hash != input_hash:
            raise RuntimeError("Anti-AI report does not match the candidate body")
        severity = float(report.metrics.severity_score)
        if (not math.isfinite(severity) or not 0 <= severity <= 100
                or isinstance(report.metrics.severity_score, bool)
                or type(report.metrics.total_hits) is not int or report.metrics.total_hits < 0
                or not isinstance(report.hits, list) or report.metrics.total_hits != len(report.hits)):
            raise RuntimeError("Anti-AI scanner returned invalid metrics")
        # Every generated chapter has an ending, including non-final chapters.
        # Reuse the existing specialist patterns rather than silently omitting
        # their checks from the production path.
        from application.engine.rules.mid_generation_refresh import get_finale_enhancer
        from application.audit.services.cliche_scanner import ClicheHit
        enhancer = get_finale_enhancer()
        text_kind = context.get("text_kind", "story")
        if text_kind not in {"story", "setup", "instructions"}:
            raise ValueError("未知 Anti-AI 文本类型")
        tail_scan = scan_tail(content) if text_kind == "story" else {
            "scan_status": "not_applicable", "text_kind": text_kind}
        tail = content[tail_scan["start"]:] if text_kind == "story" else ""
        tail_hits = [ClicheHit(**hit) for hit in tail_scan.get("hits", [])]
        all_hits = list(report.hits) + tail_hits
        if tail_hits:
            severity = min(100.0, round(severity + len(tail_hits) / max(1, len(content)) * 10000, 1))
        return NodeResult(
            outputs={
                "scan_status": "completed",
                "scanner": "AntiAIAuditor",
                "input_hash": input_hash,
                "scan_mode": "local_rules_and_tail" if text_kind == "story" else "local_rules",
                "effective_rule_count": report.effective_rule_count,
                "total_hits": len(all_hits),
                "severity_score": severity,
                "hits": [asdict(hit) for hit in all_hits],
                "overall_assessment": "尾段需修订" if tail_hits else report.metrics.overall_assessment,
                "recommendations": report.recommendations,
                "improvement_suggestions": report.improvement_suggestions + ([enhancer.enhance_finale(tail)] if tail_hits else []),
                "rule_version": report.rule_version,
                "content_hash": report.content_hash,
                "tail_scan": tail_scan,
            },
            status=NodeStatus.WARNING if all_hits else NodeStatus.SUCCESS,
            metrics={"severity_score": severity, "total_hits": float(len(all_hits))},
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        # No fabricated 0-score / clean result when a scan did not run.
        result = _scan_failure(content, str(exc), scan_status="failed")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result


async def review_chapter(content: Any, context: Dict[str, Any], *, config=None) -> NodeResult:
    """Mandatory semantic review after the independent local rule/tail scan."""
    start = time.monotonic()
    # These two production chapter nodes always review story prose. A caller's
    # document-type context cannot turn off their required ending check.
    result = scan_chapter(content, {**context, "text_kind": "story"})
    if result.status == NodeStatus.ERROR:
        return result
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview
    state = context.get("shared_state") or {}
    services = context.get("services") or state.get("services")
    llm = context.get("llm_service") or state.get("llm_service") or getattr(services, "llm", None)
    invoke = context.get("semantic_anti_ai_invoke") or state.get("semantic_anti_ai_invoke")
    db = getattr(services, "db", None)
    try:
        report = await SemanticAntiAIReview.review(content, llm=llm, config=config, invoke=invoke, db=db,
            context={"novel_id": context.get("novel_id", state.get("novel_id", "")),
                     "chapter_number": context.get("chapter_number", state.get("chapter_number")),
                     "outline": state.get("outline", ""), "is_final_chapter": state.get("is_final_chapter", False)})
        result.outputs["semantic_anti_ai"] = report
        if not report["passed"]:
            result.status = NodeStatus.WARNING
    except Exception as exc:
        result.status = NodeStatus.ERROR
        result.error = str(exc) or "语义 Anti-AI 审稿失败"
        result.outputs["semantic_anti_ai"] = {"status": "failed", "passed": False,
            "input_hash": hashlib.sha256(content.encode()).hexdigest(), "error": result.error}
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result
