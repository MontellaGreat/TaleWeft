"""The DAG and chapter audit share the production rule scanner."""
from __future__ import annotations

import time
import hashlib
import math
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
        from application.audit.services.anti_ai_audit import AntiAIAuditor
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
        return NodeResult(
            outputs={
                "scan_status": "completed",
                "scanner": "AntiAIAuditor",
                "input_hash": input_hash,
                "total_hits": report.metrics.total_hits,
                "severity_score": severity,
                "hits": [asdict(hit) for hit in report.hits],
                "overall_assessment": report.metrics.overall_assessment,
                "recommendations": report.recommendations,
                "improvement_suggestions": report.improvement_suggestions,
                "rule_version": report.rule_version,
                "content_hash": report.content_hash,
            },
            status=NodeStatus.WARNING if report.metrics.total_hits else NodeStatus.SUCCESS,
            metrics={"severity_score": severity, "total_hits": float(report.metrics.total_hits)},
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        # No fabricated 0-score / clean result when a scan did not run.
        result = _scan_failure(content, str(exc), scan_status="failed")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result
