"""SQLite Anti-AI 审计结果仓储 — 持久化章节 AI 味检测与审计结果。

核心功能：
- 保存每章的 Anti-AI 审计报告
- 按小说查询审计历史
- 获取章节审计结果
- 获取多章节趋势数据
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class SqliteAntiAiAuditRepository:
    """Anti-AI 审计结果仓储。"""

    def __init__(self, db_connection):
        self.db = db_connection

    def save_report(self, novel_id: str, chapter_number: int, report, snapshot=None) -> None:
        """The chapter summary and immutable scan attempt share one commit ACK."""
        with self.db.transaction() as writer:
            if report.scan_status == "completed":
                self.upsert(
                    novel_id=novel_id, chapter_number=chapter_number,
                    total_hits=report.metrics.total_hits,
                    critical_hits=report.metrics.critical_hits,
                    warning_hits=report.metrics.warning_hits,
                    info_hits=report.metrics.info_hits,
                    severity_score=report.metrics.severity_score,
                    overall_assessment=report.metrics.overall_assessment,
                    hit_density=snapshot.hit_density,
                    critical_density=snapshot.critical_density,
                    category_distribution=report.metrics.category_distribution,
                    top_patterns=report.metrics.top_patterns,
                    recommendations=report.recommendations,
                    improvement_suggestions=report.improvement_suggestions,
                    hits_detail=[vars(hit) for hit in report.hits],
                    _writer=writer,
                )
            self.record_run(novel_id, chapter_number, report, _writer=writer)

    def record_run(self, novel_id: str, chapter_number: int, report, *, _writer=None) -> None:
        from dataclasses import asdict

        (_writer or self.db).execute(
            """INSERT INTO anti_ai_audit_runs
            (run_id, novel_id, chapter_number, content_hash, rule_version,
             scan_status, persistence_status, error, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report.run_id, novel_id, chapter_number, report.content_hash, report.rule_version,
             report.scan_status, report.persistence_status, report.error,
             json.dumps(asdict(report), ensure_ascii=False)),
        )

    def list_runs(self, novel_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT rowid AS sequence, * FROM anti_ai_audit_runs WHERE novel_id = ? "
            "ORDER BY rowid DESC LIMIT ?", (novel_id, limit),
        )
        results = []
        for row in rows:
            result = dict(row)
            result["report"] = json.loads(result.pop("report_json"))
            results.append(result)
        return results

    def get_run(self, novel_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetch_one(
            "SELECT rowid AS sequence, * FROM anti_ai_audit_runs WHERE novel_id = ? AND run_id = ?",
            (novel_id, run_id),
        )
        if not row:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("report_json"))
        return result

    def upsert(
        self,
        novel_id: str,
        chapter_number: int,
        total_hits: int,
        critical_hits: int,
        warning_hits: int,
        info_hits: int,
        severity_score: float,
        overall_assessment: str,
        hit_density: float = 0.0,
        critical_density: float = 0.0,
        category_distribution: Optional[Dict[str, int]] = None,
        top_patterns: Optional[List[str]] = None,
        recommendations: Optional[List[str]] = None,
        improvement_suggestions: Optional[List[str]] = None,
        hits_detail: Optional[List[Dict[str, Any]]] = None,
        *, _writer=None,
    ) -> str:
        """保存或更新章节审计结果。"""
        existing = self.get_by_chapter(novel_id, chapter_number)
        cat_dist_json = json.dumps(category_distribution or {}, ensure_ascii=False)
        top_pat_json = json.dumps(top_patterns or [], ensure_ascii=False)
        recs_json = json.dumps(recommendations or [], ensure_ascii=False)
        sug_json = json.dumps(improvement_suggestions or [], ensure_ascii=False)
        hits_json = json.dumps(hits_detail or [], ensure_ascii=False)

        if existing:
            (_writer or self.db).execute(
                """
                UPDATE anti_ai_audits
                SET total_hits = ?, critical_hits = ?, warning_hits = ?, info_hits = ?,
                    severity_score = ?, overall_assessment = ?,
                    hit_density = ?, critical_density = ?,
                    category_distribution = ?, top_patterns = ?,
                    recommendations = ?, improvement_suggestions = ?,
                    hits_detail = ?, created_at = datetime('now')
                WHERE novel_id = ? AND chapter_number = ?
                """,
                (
                    total_hits, critical_hits, warning_hits, info_hits,
                    severity_score, overall_assessment,
                    hit_density, critical_density,
                    cat_dist_json, top_pat_json,
                    recs_json, sug_json,
                    hits_json,
                    novel_id, chapter_number,
                ),
            )
            return existing["audit_id"]

        audit_id = str(uuid4())
        (_writer or self.db).execute(
            """
            INSERT INTO anti_ai_audits
            (audit_id, novel_id, chapter_number, total_hits, critical_hits, warning_hits,
             info_hits, severity_score, overall_assessment, hit_density, critical_density,
             category_distribution, top_patterns, recommendations,
             improvement_suggestions, hits_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, novel_id, chapter_number, total_hits, critical_hits, warning_hits,
                info_hits, severity_score, overall_assessment, hit_density, critical_density,
                cat_dist_json, top_pat_json, recs_json, sug_json, hits_json,
            ),
        )
        return audit_id

    def get_by_chapter(
        self, novel_id: str, chapter_number: int
    ) -> Optional[Dict[str, Any]]:
        """获取指定章节的审计结果。"""
        row = self.db.fetch_one(
            """
            SELECT audit_id, novel_id, chapter_number, total_hits, critical_hits,
                   warning_hits, info_hits, severity_score, overall_assessment,
                   hit_density, critical_density, category_distribution, top_patterns,
                   recommendations, improvement_suggestions, hits_detail, created_at
            FROM anti_ai_audits
            WHERE novel_id = ? AND chapter_number = ?
            """,
            (novel_id, chapter_number),
        )
        if not row:
            return None
        result = dict(row)
        # 解析 JSON 字段
        for key in ("category_distribution", "top_patterns", "recommendations",
                     "improvement_suggestions", "hits_detail"):
            if result.get(key) and isinstance(result[key], str):
                try:
                    result[key] = json.loads(result[key])
                except (json.JSONDecodeError, TypeError):
                    result[key] = [] if key != "category_distribution" else {}
        return result

    def list_by_novel(
        self, novel_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """列出小说的所有审计结果（按章节号排序）。"""
        rows = self.db.fetch_all(
            """
            SELECT audit_id, novel_id, chapter_number, total_hits, critical_hits,
                   warning_hits, info_hits, severity_score, overall_assessment,
                   hit_density, critical_density, category_distribution, top_patterns,
                   recommendations, improvement_suggestions, hits_detail, created_at
            FROM anti_ai_audits
            WHERE novel_id = ?
            ORDER BY chapter_number ASC
            LIMIT ?
            """,
            (novel_id, limit),
        )
        results = []
        for row in rows:
            r = dict(row)
            for key in ("category_distribution", "top_patterns", "recommendations",
                         "improvement_suggestions", "hits_detail"):
                if r.get(key) and isinstance(r[key], str):
                    try:
                        r[key] = json.loads(r[key])
                    except (json.JSONDecodeError, TypeError):
                        r[key] = [] if key != "category_distribution" else {}
            results.append(r)
        return results

    def get_trend_data(
        self, novel_id: str, last_n: int = 20
    ) -> List[Dict[str, Any]]:
        """获取最近 N 章的审计趋势数据。"""
        rows = self.db.fetch_all(
            """
            SELECT a.chapter_number, a.severity_score, a.hit_density, a.overall_assessment,
                   a.critical_hits, a.total_hits,
                   s.run_id, s.content_hash, s.rule_version,
                   r.run_id AS latest_run_id,
                   r.scan_status AS latest_scan_status,
                   r.persistence_status AS latest_persistence_status
            FROM anti_ai_audits a
            LEFT JOIN anti_ai_audit_runs r ON r.rowid = (
                SELECT r2.rowid FROM anti_ai_audit_runs r2
                WHERE r2.novel_id = a.novel_id AND r2.chapter_number = a.chapter_number
                ORDER BY r2.rowid DESC LIMIT 1
            )
            LEFT JOIN anti_ai_audit_runs s ON s.rowid = (
                SELECT s2.rowid FROM anti_ai_audit_runs s2
                WHERE s2.novel_id = a.novel_id AND s2.chapter_number = a.chapter_number
                    AND s2.scan_status = 'completed' AND s2.persistence_status = 'committed'
                ORDER BY s2.rowid DESC LIMIT 1
            )
            WHERE a.novel_id = ?
            ORDER BY a.chapter_number DESC
            LIMIT ?
            """,
            (novel_id, last_n),
        )
        # Select the last N chapters, then return ascending points for the UI.
        # A summary may outlive a failed newer attempt; callers receive that
        # failure explicitly and cannot call its older score a fresh success.
        results = []
        for row in reversed(rows or []):
            result = dict(row)
            result["legacy"] = result["run_id"] is None
            results.append(result)
        return results

    def delete_by_novel(self, novel_id: str) -> int:
        """删除小说的所有审计数据。"""
        self.db.execute(
            "DELETE FROM anti_ai_audits WHERE novel_id = ?",
            (novel_id,),
        )
        return 0
