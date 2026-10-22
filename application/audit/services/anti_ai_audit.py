"""Anti-AI 指标与审计服务 — Layer 7: 章后审计与指标追踪。

核心功能：
- 对生成的章节进行 35+ 模式 AI 味扫描
- 计算综合 AI 味评分
- 生成结构化审计报告
- 追踪章节间的 AI 味趋势
"""
from __future__ import annotations

import logging
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from application.audit.services.cliche_scanner import ClicheHit, ClicheScanner

logger = logging.getLogger(__name__)


@dataclass
class AntiAIMetrics:
    """Anti-AI 指标快照。"""
    chapter_id: str = ""
    total_hits: int = 0
    critical_hits: int = 0
    warning_hits: int = 0
    info_hits: int = 0
    severity_score: float = 0.0  # 0-100，越高AI味越重
    category_distribution: Dict[str, int] = field(default_factory=dict)
    top_patterns: List[str] = field(default_factory=list)
    overall_assessment: str = "未检测"  # 纯净/轻微/中等/严重


@dataclass
class AntiAIAuditReport:
    """Anti-AI 审计报告。"""
    chapter_id: str = ""
    metrics: AntiAIMetrics = field(default_factory=AntiAIMetrics)
    hits: List[ClicheHit] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    improvement_suggestions: List[str] = field(default_factory=list)
    scan_status: str = "completed"
    persistence_status: str = "not_requested"
    content_hash: str = ""
    rule_version: str = ""
    run_id: str = ""
    chapter_revision: Optional[int] = None
    job_id: str = ""
    run_epoch: Optional[int] = None
    error: Optional[str] = None
    scene_type: str = "default"
    exempted_hits: List[ClicheHit] = field(default_factory=list)


def validate_completed_report(report, content: str, *, rule_version: str = "") -> None:
    """Reject unavailable, stale or fabricated successful scan receipts."""
    if report is None or report.metrics is None:
        raise RuntimeError("Anti-AI 扫描缺少报告")
    if report.scan_status != "completed" or report.error:
        raise RuntimeError(report.error or "Anti-AI 扫描未完成")
    if report.content_hash != hashlib.sha256(content.encode("utf-8")).hexdigest():
        raise RuntimeError("Anti-AI 扫描正文哈希不匹配")
    if not report.rule_version or (rule_version and report.rule_version != rule_version):
        raise RuntimeError("Anti-AI 扫描规则版本无效")
    severity = report.metrics.severity_score
    counts = [report.metrics.total_hits, report.metrics.critical_hits, report.metrics.warning_hits, report.metrics.info_hits]
    if (isinstance(severity, bool) or not isinstance(severity, (int, float))
            or not math.isfinite(severity) or not 0 <= severity <= 100
            or any(type(count) is not int or count < 0 for count in counts)
            or sum(counts[1:]) != counts[0] or not isinstance(report.hits, list)
            or counts[0] != len(report.hits)):
        raise RuntimeError("Anti-AI 扫描指标无效")


class AntiAIAuditor:
    """Anti-AI 审计服务。"""

    SEVERITY_WEIGHTS = {"critical": 3.0, "warning": 1.0, "info": 0.3}
    ASSESSMENT_THRESHOLDS = [(5.0, "严重"), (3.0, "中等"), (1.0, "轻微"), (0.0, "纯净")]

    def __init__(self, *, rule_provider=None, scene_type="default"):
        self.rule_provider = rule_provider
        self.scene_type = scene_type

    def effective_rule_version(self, effective):
        if self.scene_type == "default":
            return effective['version']
        from application.engine.rules.allowlist_manager import get_allowlist_manager
        rule = get_allowlist_manager().get_rule(self.scene_type)
        return hashlib.sha256(json.dumps({'rules':effective['version'],'scene':self.scene_type,
            'categories':sorted(rule.allowed_categories),'patterns':sorted(rule.allowed_patterns),
            'max_density_per_1000':rule.max_density_per_1000},ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def scan_chapter(self, chapter_id: str, content: str) -> AntiAIAuditReport:
        """对章节进行 Anti-AI 审计。"""
        from application.audit.services.cliche_scanner import AI_CLICHE_PATTERNS_ENHANCED
        effective = self.rule_provider() if self.rule_provider else {
            'patterns': AI_CLICHE_PATTERNS_ENHANCED,
            'version': hashlib.sha256(json.dumps(AI_CLICHE_PATTERNS_ENHANCED, ensure_ascii=False).encode('utf-8')).hexdigest(),
        }
        scanner = ClicheScanner(use_enhanced=True, patterns=effective['patterns'])
        hits = scanner.scan_cliches(content)
        exempted = []
        if self.scene_type != "default":
            from application.engine.rules.allowlist_manager import get_allowlist_manager
            manager = get_allowlist_manager()
            rule = manager.get_rule(self.scene_type)
            allowance = max(0, int(len(content) / 1000 * rule.max_density_per_1000))
            counts, retained = {}, []
            for hit in hits:
                count = counts.get(hit.pattern,0)
                if manager.is_allowed(self.scene_type,hit.category,hit.pattern) and count < allowance:
                    exempted.append(hit)
                    counts[hit.pattern] = count+1
                else:
                    retained.append(hit)
            hits = retained
        metrics = self._calculate_metrics(chapter_id, hits, content)
        recommendations = self._generate_recommendations(metrics, hits)
        improvement_suggestions = self._generate_improvement_suggestions(hits)
        return AntiAIAuditReport(
            chapter_id=chapter_id,
            metrics=metrics,
            hits=hits,
            recommendations=recommendations,
            improvement_suggestions=improvement_suggestions,
            rule_version=self.effective_rule_version(effective),
            content_hash=hashlib.sha256(content.encode('utf-8')).hexdigest(),
            scene_type=self.scene_type, exempted_hits=exempted,
        )

    def _calculate_metrics(self, chapter_id: str, hits: List[ClicheHit], content: str) -> AntiAIMetrics:
        """计算 Anti-AI 指标。"""
        critical_count = sum(1 for h in hits if h.severity == "critical")
        warning_count = sum(1 for h in hits if h.severity == "warning")
        info_count = sum(1 for h in hits if h.severity == "info")

        char_count = max(len(content), 1)
        raw_score = sum(self.SEVERITY_WEIGHTS.get(h.severity, 0.5) for h in hits)
        normalized_score = min(100.0, (raw_score / char_count) * 1000 * 10)

        category_dist: Dict[str, int] = {}
        for h in hits:
            cat = h.category or "其他"
            category_dist[cat] = category_dist.get(cat, 0) + 1

        pattern_counts: Dict[str, int] = {}
        for h in hits:
            pattern_counts[h.pattern] = pattern_counts.get(h.pattern, 0) + 1
        top_patterns = sorted(pattern_counts, key=pattern_counts.get, reverse=True)[:5]

        assessment = "纯净"
        for threshold, label in self.ASSESSMENT_THRESHOLDS:
            if normalized_score >= threshold:
                assessment = label
                break

        return AntiAIMetrics(
            chapter_id=chapter_id, total_hits=len(hits),
            critical_hits=critical_count, warning_hits=warning_count, info_hits=info_count,
            severity_score=round(normalized_score, 1),
            category_distribution=category_dist, top_patterns=top_patterns,
            overall_assessment=assessment,
        )

    def _generate_recommendations(self, metrics: AntiAIMetrics, hits: List[ClicheHit]) -> List[str]:
        """生成修改建议。"""
        recs = []
        if metrics.critical_hits > 0:
            critical_patterns = [h.pattern for h in hits if h.severity == "critical"]
            unique = list(dict.fromkeys(critical_patterns))[:3]
            recs.append(f"紧急：发现 {metrics.critical_hits} 处严重AI味模式（{', '.join(unique)}），必须修改")
        if metrics.category_distribution.get("微表情", 0) > 3:
            recs.append("微表情过多：建议用完整姿态变化或对白本身替代")
        if metrics.category_distribution.get("比喻", 0) > 2:
            recs.append("比喻过多：建议用感官细节替代比喻")
        if metrics.category_distribution.get("声线", 0) > 2:
            recs.append("声线标签过多：建议用对白的标点和断句表现语气")
        if metrics.category_distribution.get("情绪", 0) > 2:
            recs.append("情绪标签过多：建议通过动作和环境暗示传递情绪")
        return recs

    def _generate_improvement_suggestions(self, hits: List[ClicheHit]) -> List[str]:
        """生成具体改进建议。"""
        suggestions = []
        seen_patterns = set()
        for h in hits:
            if h.pattern in seen_patterns:
                continue
            seen_patterns.add(h.pattern)
            if h.replacement_hint:
                suggestions.append(f"[{h.category}] {h.pattern} → {h.replacement_hint}")
            if len(suggestions) >= 5:
                break
        return suggestions


_auditor: Optional[AntiAIAuditor] = None

def get_anti_ai_auditor(db=None) -> AntiAIAuditor:
    if db is not None:
        from infrastructure.ai.quality_rule_store import QualityRuleStore
        return AntiAIAuditor(rule_provider=lambda: QualityRuleStore(db).effective())
    global _auditor
    if _auditor is None:
        from infrastructure.ai.quality_rule_store import QualityRuleStore
        _auditor = AntiAIAuditor(rule_provider=lambda: QualityRuleStore().effective())
    return _auditor
