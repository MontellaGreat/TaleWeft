"""Anti-AI API 端点 — 提供前端交互接口。

端点：
- POST /api/v1/anti-ai/scan — 扫描章节 AI 味
- GET /api/v1/anti-ai/categories — 获取分类信息
- GET /api/v1/anti-ai/rules — 获取规则列表
- POST /api/v1/anti-ai/allowlist — 更新白名单
- GET /api/v1/anti-ai/allowlist/scenes — 获取白名单场景
- GET /api/v1/anti-ai/stats — 获取系统统计
- GET /api/v1/anti-ai/audits/{novel_id} — 获取小说审计历史
- GET /api/v1/anti-ai/audits/{novel_id}/{chapter_number} — 获取章节审计详情
- GET /api/v1/anti-ai/trend/{novel_id} — 获取审计趋势
"""
from __future__ import annotations

import logging
from uuid import uuid4
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal

from application.audit.services.anti_ai_audit import get_anti_ai_auditor, AntiAIAuditReport, validate_completed_report
from application.engine.rules.allowlist_manager import get_allowlist_manager, AllowlistRule
from application.engine.rules.positive_framing_rules import POSITIVE_FRAMING_MAP
from infrastructure.ai.prompt_registry import get_prompt_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anti-ai", tags=["Anti-AI 防御"])


# ─── 请求/响应模型 ───

class ScanRequest(BaseModel):
    """扫描请求。"""
    content: str = Field(..., description="待扫描的章节正文")
    chapter_id: str = Field("", description="章节 ID")


class ScanResponse(BaseModel):
    """扫描响应。"""
    # Manual scans are deliberately ephemeral.  Keeping the execution state in
    # the response prevents the UI from presenting a successful scan as an
    # archived chapter audit.
    run_id: str = ""
    scan_status: Literal["completed", "failed", "unavailable"] = "completed"
    persistence_status: Literal["not_requested", "committed", "failed"] = "not_requested"
    source: Literal["manual"] = "manual"
    persisted: bool = False
    total_hits: int
    critical_hits: int
    warning_hits: int
    severity_score: float
    overall_assessment: str
    category_distribution: Dict[str, int]
    top_patterns: List[str]
    recommendations: List[str]
    improvement_suggestions: List[str]
    hits: List[Dict[str, Any]]
    rule_version: str = ""
    content_hash: str = ""


class CategoryInfo(BaseModel):
    """分类信息。"""
    key: str
    name: str
    icon: str
    description: str
    color: str
    sort_order: int
    prompt_count: int = 0


class RuleInfo(BaseModel):
    """规则信息。"""
    key: str
    anti_pattern: str
    positive_action: str
    category: str
    severity: str


class AllowlistUpdateRequest(BaseModel):
    """白名单更新请求。"""
    scene_type: str
    allowed_categories: List[str] = []
    allowed_patterns: List[str] = []
    max_density_per_1000: float = 1.0
    description: str = ""


class RewritePolicyRequest(BaseModel):
    mode: Literal["scan_only", "review", "auto"] = "scan_only"
    max_attempts: int = Field(2, ge=1, le=5)
    max_severity: float = Field(3.0, ge=0, le=100)
    stream_action: Literal["warn", "cancel"] = "warn"
    stream_critical_limit: int = Field(1, ge=1, le=100)
    scene_type: Literal["default","battle","suspense","horror","confession","revelation"] = "default"


class CandidateConfirmRequest(BaseModel):
    expected_candidate_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_base_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_base_revision: int = Field(..., ge=0)
    expected_run_epoch: int = Field(..., ge=0)


class CandidateRejectRequest(BaseModel):
    expected_candidate_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class CandidateRegenerateRequest(BaseModel):
    expected_base_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_base_revision: int = Field(..., ge=0)
    expected_run_epoch: int = Field(..., ge=0)


# ─── 辅助函数 ───

def _get_audit_repo():
    """获取 Anti-AI 审计仓储。"""
    try:
        from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository
        from infrastructure.persistence.database.connection import get_database
        db = get_database()
        return SqliteAntiAiAuditRepository(db)
    except Exception as e:
        logger.warning(f"获取审计仓储失败: {e}")
        return None


def _get_candidate_review(*, voice=False, llm=False):
    from application.audit.services.anti_ai_candidate_review import AntiAICandidateReview
    from infrastructure.persistence.database.connection import get_database
    from interfaces.api.dependencies import get_voice_drift_service, get_llm_service, llm_runtime_is_mock, get_chapter_aftermath_pipeline
    from application.engine.services.streaming_bus import streaming_bus
    provider = get_llm_service() if llm else None
    if llm and llm_runtime_is_mock(provider):
        raise HTTPException(status_code=503, detail="请先配置真实改写模型，模拟模型不能创建可提交候选")
    return AntiAICandidateReview(get_database(), llm=provider,
        voice_service=get_voice_drift_service() if voice else None,
        aftermath=get_chapter_aftermath_pipeline() if voice and not llm else None,
        on_event=lambda novel_id, name, data: streaming_bus.publish_audit_event(novel_id, name, data))


def _candidate_error(exc):
    from application.engine.services.chapter_rewrite_candidates import CandidateConflictError
    from application.audit.services.anti_ai_candidate_review import CandidateReviewError
    if isinstance(exc, CandidateConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail="小说、章节或候选不存在")
    if isinstance(exc, CandidateReviewError):
        return HTTPException(status_code=422, detail=str(exc))
    logger.exception("候选操作未确认完成")
    return HTTPException(status_code=503, detail="操作结果尚未确认，请刷新候选记录；已保存稿件会保留")


@router.get("/novels/{novel_id}/rewrite-policy")
def get_rewrite_policy(novel_id: str):
    try:
        service = _get_candidate_review()
        service._novel(novel_id)
        return service.candidates.get_policy(novel_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.put("/novels/{novel_id}/rewrite-policy")
def set_rewrite_policy(novel_id: str, request: RewritePolicyRequest):
    try:
        service = _get_candidate_review()
        service._novel(novel_id)
        return service.candidates.set_policy(novel_id, **request.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.get("/novels/{novel_id}/candidates")
def get_candidates(novel_id: str, chapter_number: Optional[int] = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)):
    try:
        return _get_candidate_review().list(novel_id, chapter_number, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.get("/novels/{novel_id}/candidates/{candidate_id}")
def get_candidate(novel_id: str, candidate_id: str):
    try:
        return _get_candidate_review().detail(novel_id, candidate_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.post("/novels/{novel_id}/candidates/{candidate_id}/confirm")
async def confirm_candidate(novel_id: str, candidate_id: str, request: CandidateConfirmRequest):
    try:
        return await _get_candidate_review(voice=True).confirm(novel_id, candidate_id, **request.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.post("/novels/{novel_id}/candidates/{candidate_id}/reject")
async def reject_candidate(novel_id: str, candidate_id: str, request: CandidateRejectRequest):
    try:
        return await _get_candidate_review().reject(novel_id, candidate_id, request.expected_candidate_hash)
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


@router.post("/novels/{novel_id}/chapters/{chapter_number}/rewrite-candidates")
async def regenerate_candidate(novel_id: str, chapter_number: int, request: CandidateRegenerateRequest):
    if chapter_number < 1:
        raise HTTPException(status_code=422, detail="章节序号必须为正整数")
    try:
        return await _get_candidate_review(voice=True, llm=True).regenerate(novel_id, chapter_number, **request.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        raise _candidate_error(exc) from exc


# ─── 端点 ───

@router.post("/scan", response_model=ScanResponse)
async def scan_chapter(request: ScanRequest):
    """扫描章节 AI 味。"""
    if not request.content.strip():
        raise HTTPException(status_code=422, detail="正文为空，未执行 Anti-AI 扫描")
    try:
        auditor = get_anti_ai_auditor()
        report = auditor.scan_chapter(request.chapter_id, request.content)
        validate_completed_report(report, request.content)
    except Exception as exc:
        # A manual scan has no persistence fallback.  Return an explicit
        # unavailable error instead of a synthetic zero-hit report.
        logger.warning("手工 Anti-AI 扫描不可用: %s", exc)
        raise HTTPException(status_code=503, detail=f"Anti-AI 扫描不可用: {exc}") from exc

    return ScanResponse(
        run_id=report.run_id or uuid4().hex,
        scan_status=report.scan_status,
        persistence_status="not_requested",
        source="manual",
        persisted=False,
        rule_version=report.rule_version,
        content_hash=report.content_hash,
        total_hits=report.metrics.total_hits,
        critical_hits=report.metrics.critical_hits,
        warning_hits=report.metrics.warning_hits,
        severity_score=report.metrics.severity_score,
        overall_assessment=report.metrics.overall_assessment,
        category_distribution=report.metrics.category_distribution,
        top_patterns=report.metrics.top_patterns,
        recommendations=report.recommendations,
        improvement_suggestions=report.improvement_suggestions,
        hits=[
            {
                "pattern": h.pattern,
                "text": h.text,
                "start": h.start,
                "end": h.end,
                "severity": h.severity,
                "category": h.category,
                "replacement_hint": h.replacement_hint,
            }
            for h in report.hits
        ],
    )


@router.get("/categories", response_model=List[CategoryInfo])
async def get_categories():
    """获取提示词分类信息（含 Anti-AI）。"""
    registry = get_prompt_registry()
    categories = registry.get_categories()

    result = []
    for cat in categories:
        key = cat.get("key", "")
        prompts_in_cat = registry.list_by_category(key)
        result.append(CategoryInfo(
            key=key,
            name=cat.get("name", key),
            icon=cat.get("icon", "📝"),
            description=cat.get("description", ""),
            color=cat.get("color", "#6b7280"),
            sort_order=cat.get("sort_order", 99),
            prompt_count=len(prompts_in_cat),
        ))

    return result


@router.get("/rules", response_model=List[RuleInfo])
async def get_rules():
    """获取正向行为映射规则列表。"""
    rules = []
    for key, mapping in POSITIVE_FRAMING_MAP.items():
        rules.append(RuleInfo(
            key=key,
            anti_pattern=key,  # 规则名本身就是禁止的模式描述
            positive_action=mapping.get("action", ""),
            category=mapping.get("condition", "").split("时")[0] if "时" in mapping.get("condition", "") else "通用",
            severity="warning",  # 默认 warning 级别
        ))
    return rules


@router.post("/allowlist")
async def update_allowlist(request: AllowlistUpdateRequest):
    """更新场景化白名单。"""
    mgr = get_allowlist_manager()
    rule = AllowlistRule(
        scene_type=request.scene_type,
        allowed_categories=set(request.allowed_categories),
        allowed_patterns=set(request.allowed_patterns),
        max_density_per_1000=request.max_density_per_1000,
        description=request.description,
    )
    mgr.add_custom_rule(rule)
    return {"status": "ok", "scene_type": request.scene_type}


@router.get("/allowlist/scenes")
async def get_allowlist_scenes():
    """获取所有白名单场景类型。"""
    mgr = get_allowlist_manager()
    scenes = mgr.list_scene_types()
    result = []
    for scene in scenes:
        rule = mgr.get_rule(scene)
        result.append({
            "scene_type": scene,
            "allowed_categories": list(rule.allowed_categories),
            "allowed_patterns": list(rule.allowed_patterns),
            "max_density_per_1000": rule.max_density_per_1000,
            "description": rule.description,
        })
    return result


def _probe_anti_ai_layers() -> Dict[str, Dict[str, Any]]:
    """Probe composition-root wiring without treating capability as execution.

    The dashboard previously hard-coded every layer as connected.  These
    probes only answer whether the concrete provider/parser/runtime hook can
    be resolved right now; ``observed`` is populated separately from durable
    audit evidence below.
    """
    from infrastructure.ai.prompt_keys import (
        ANTI_AI_ALLOWLIST_EXPLAIN,
        ANTI_AI_BEHAVIOR_PROTOCOL,
        ANTI_AI_CHAPTER_AUDIT,
        ANTI_AI_CHARACTER_STATE_LOCK,
        ANTI_AI_MID_GENERATION_REFRESH,
    )

    def prompt_hook_installed():
        return callable(__import__("application.ai_invocation.services", fromlist=["AttemptService"]).AttemptService.prose_prompt_hook_factory)

    checks = {
        "L1_positive_framing": prompt_hook_installed,
        "L2_protocol_rules": prompt_hook_installed,
        "L3_allowlist_scenes": prompt_hook_installed,
        "L4_state_vector": prompt_hook_installed,
        "L5_context_quota": prompt_hook_installed,
        "L6_token_guard": lambda: callable(__import__(
            "application.ai_invocation.services", fromlist=["AttemptService"]
        ).AttemptService.prose_stream_guard_factory),
        "L7_audit": lambda: get_anti_ai_auditor() is not None,
    }
    prompt_keys = {
        "L1_positive_framing": ANTI_AI_BEHAVIOR_PROTOCOL,
        "L2_protocol_rules": ANTI_AI_BEHAVIOR_PROTOCOL,
        "L3_allowlist_scenes": ANTI_AI_ALLOWLIST_EXPLAIN,
        "L4_state_vector": ANTI_AI_CHARACTER_STATE_LOCK,
        "L5_context_quota": ANTI_AI_MID_GENERATION_REFRESH,
        "L6_token_guard": ANTI_AI_MID_GENERATION_REFRESH,
        "L7_audit": ANTI_AI_CHAPTER_AUDIT,
    }
    states: Dict[str, Dict[str, Any]] = {}
    registry = get_prompt_registry()
    for key, check in checks.items():
        error = None
        implemented = True
        connected = False
        configured = False
        try:
            available = bool(check())
            # Inspect installed composition-root hooks. Actual provider use is
            # observed only from immutable prompt/stream/audit receipts.
            connected = available
        except Exception as exc:
            error = str(exc)
        try:
            configured = bool(registry.exists(prompt_keys[key]))
        except Exception as exc:
            error = error or str(exc)
        states[key] = {
            "implemented": implemented,
            "configured": configured,
            "connected": connected,
            "observed": False,
            "failed": bool(error),
            "status": "failed" if error else ("connected" if connected else ("configured" if configured else "implemented")),
        }
        if error:
            states[key]["error"] = error
        elif key not in {"L6_token_guard", "L7_audit"}:
            states[key]["connection_evidence"] = "provider_hook_installed" if connected else "provider_hook_not_reported"
    return states


@router.get("/stats")
async def get_stats(novel_id: Optional[str] = Query(None, description="按小说范围统计真实审计记录")):
    """获取 Anti-AI 系统统计。"""
    registry = get_prompt_registry()
    anti_ai_prompts = registry.list_by_category("anti-ai")
    from application.audit.services.cliche_scanner import ClicheScanner

    # These are capability facts, not claims that the current chapter ran.
    # ``observed`` is only true when a persisted audit run exists; a configured
    # rule/provider alone must never be rendered as an active execution.
    layer_definitions = _probe_anti_ai_layers()
    observed_count = 0
    failed_count = 0
    audit_store_available = False
    stream_store_available = False
    latest_run = None
    stream_count, stream_failed_count, latest_stream_run = 0, 0, None
    prompt_count, latest_prompt_run, prompt_layers = 0, None, {}
    from infrastructure.persistence.database.connection import get_database
    scope = " WHERE novel_id = ?" if novel_id else ""
    params = (novel_id,) if novel_id else ()
    try:
        repo = _get_audit_repo()
        if repo is not None:
            row = get_database().fetch_one(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN scan_status='failed' THEN 1 ELSE 0 END) AS failed "
                f"FROM anti_ai_audit_runs{scope}", params,
            )
            audit_store_available = True
            observed_count = int(row["total"] or 0) if row else 0
            failed_count = int(row["failed"] or 0) if row else 0
            latest_run = get_database().fetch_one(
                f"SELECT run_id,novel_id,chapter_number,scan_status,persistence_status,content_hash,rule_version,created_at "
                f"FROM anti_ai_audit_runs{scope} ORDER BY rowid DESC LIMIT 1", params,
            )
    except Exception:
        audit_store_available = False
    try:
        stream_row = get_database().fetch_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failed "
            f"FROM anti_ai_stream_runs{scope}", params,
        )
        stream_store_available = True
        stream_count = int(stream_row["total"] or 0) if stream_row else 0
        stream_failed_count = int(stream_row["failed"] or 0) if stream_row else 0
        latest_stream_run = get_database().fetch_one(
            f"SELECT run_id,job_id,status,scan_status,persistence_status,content_hash,rule_version "
            f"FROM anti_ai_stream_runs{scope} ORDER BY rowid DESC LIMIT 1", params,
        )
    except Exception:
        stream_store_available = False
    try:
        import json
        # Shared semantic reviews retain their real prompt/reply in this
        # history too. They do not observe generation-time L1-L5 hooks.
        prompt_scope = scope + (" AND " if scope else " WHERE ") + "COALESCE(json_extract(evidence_json,'$.kind'),'') != 'semantic_review'"
        row = get_database().fetch_one(f"SELECT COUNT(*) AS total FROM anti_ai_prompt_runs{prompt_scope}", params)
        prompt_count = int(row["total"] or 0) if row else 0
        latest = get_database().fetch_one(f"SELECT * FROM anti_ai_prompt_runs{prompt_scope} ORDER BY rowid DESC LIMIT 1", params)
        if latest:
            latest_prompt_run = dict(latest)
            latest_prompt_run["evidence"] = json.loads(latest_prompt_run.pop("evidence_json"))
            prompt_layers = latest_prompt_run["evidence"].get("layers", {})
    except Exception:
        for key in ("L1_positive_framing", "L2_protocol_rules", "L3_allowlist_scenes", "L4_state_vector", "L5_context_quota"):
            layer_definitions[key].update(connected=False, failed=True, error="去味提示词执行记录表不可用")
    l6 = layer_definitions["L6_token_guard"]
    l6["connected"] = bool(l6["connected"] and stream_store_available)
    if not stream_store_available:
        l6["failed"] = True
        l6["error"] = l6.get("error") or "流式扫描记录表不可用"
    l7 = layer_definitions["L7_audit"]
    l7["connected"] = bool(l7["connected"] and audit_store_available)
    l7["configured"] = bool(l7["configured"] and audit_store_available)
    if not audit_store_available:
        l7["failed"] = True
        l7["error"] = l7.get("error") or "审计记录表不可用"
    for key, state in layer_definitions.items():
        # Only durable audit attempts can prove that the layer ran.  Prompt
        # availability, parser imports and static wiring are not observations.
        state["observed"] = observed_count > 0 if key == "L7_audit" else stream_count > 0 if key == "L6_token_guard" else bool(prompt_layers.get(key, {}).get("applied"))
        if key in prompt_layers:
            state["latest_execution"] = prompt_layers[key]
            if latest_prompt_run["status"] == "failed":
                state["failed"] = True
        if key == "L6_token_guard" and latest_stream_run and (
            latest_stream_run["status"] in {"failed", "interrupted"}
            or latest_stream_run["persistence_status"] != "committed"
        ):
            state["failed"] = True
        if key == "L7_audit" and latest_run and (
            latest_run["scan_status"] != "completed" or latest_run["persistence_status"] != "committed"
        ):
            state["failed"] = True
        state["status"] = (
            "failed" if state["failed"] else
            "unavailable" if not state["connected"] else
            "unavailable" if key in prompt_layers and prompt_layers[key].get("status") == "unavailable" else
            "observed" if state["observed"] else
            "connected" if state["connected"] else
            "configured" if state["configured"] else
            "implemented" if state["implemented"] else "unavailable"
        )

    return {
        "total_prompts": registry.total_count,
        "anti_ai_prompts": len(anti_ai_prompts),
        "categories_count": len(registry.get_categories()),
        "cliche_patterns": len(ClicheScanner(use_enhanced=True).compiled_patterns),
        "layers": {
            "L1_positive_framing": len(POSITIVE_FRAMING_MAP),
            "L2_protocol_rules": 5,  # P1-P5
            "L3_allowlist_scenes": len(get_allowlist_manager().list_scene_types()),
            "L4_state_vector": layer_definitions["L4_state_vector"]["status"],
            "L5_context_quota": layer_definitions["L5_context_quota"]["status"],
            "L6_token_guard": layer_definitions["L6_token_guard"]["status"],
            "L7_audit": layer_definitions["L7_audit"]["status"],
        },
        "layer_states": layer_definitions,
        "observed_audit_runs": observed_count,
        "failed_audit_runs": failed_count,
        "latest_audit_run": dict(latest_run) if latest_run else None,
        "observed_stream_runs": stream_count,
        "failed_stream_runs": stream_failed_count,
        "latest_stream_run": dict(latest_stream_run) if latest_stream_run else None,
        "observed_prompt_runs": prompt_count,
        "latest_prompt_run": latest_prompt_run,
        "novel_id": novel_id,
        "status_scope": "capability",
    }


@router.get("/prompt-runs/{novel_id}")
async def get_prompt_runs(novel_id: str, limit: int = Query(50, ge=1, le=200)):
    from infrastructure.persistence.database.connection import get_database
    import json
    try:
        rows = get_database().fetch_all("SELECT * FROM anti_ai_prompt_runs WHERE novel_id=? ORDER BY rowid DESC LIMIT ?", (novel_id, limit))
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail="去味提示词执行记录暂不可用") from exc


@router.get("/stream-runs/{novel_id}")
async def get_stream_runs(novel_id: str, limit: int = Query(50, ge=1, le=200)):
    from infrastructure.persistence.database.connection import get_database
    import json
    try:
        rows = get_database().fetch_all("SELECT * FROM anti_ai_stream_runs WHERE novel_id=? ORDER BY rowid DESC LIMIT ?", (novel_id, limit))
        result = []
        for row in rows:
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json"))
            result.append(item)
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail="流式扫描记录暂不可用") from exc


@router.get("/runs/{novel_id}")
async def get_audit_runs(novel_id: str, limit: int = Query(50, ge=1, le=200)):
    """真实扫描尝试及正文/规则版本；失败记录不伪装为通过。"""
    repo = _get_audit_repo()
    if not repo:
        raise HTTPException(status_code=503, detail="审计仓储暂不可用")
    return repo.list_runs(novel_id, limit=limit)


@router.get("/runs/{novel_id}/{run_id}")
async def get_audit_run(novel_id: str, run_id: str):
    """按不可变运行标识恢复一次审计结果，兼容 SSE 断线后查询。"""
    repo = _get_audit_repo()
    if not repo:
        raise HTTPException(status_code=503, detail="审计仓储暂不可用")
    result = None
    try:
        result = repo.get_run(novel_id, run_id)
    except Exception:
        pass
    if result is None:
        from infrastructure.persistence.database.connection import get_database
        import json
        try:
            progress = get_database().fetch_one("SELECT * FROM anti_ai_scan_progress WHERE novel_id=? AND run_id=?", (novel_id,run_id))
            if progress:
                result = dict(progress)
                result["report"] = json.loads(result.pop("report_json"))
                result["source"] = "progress_receipt"
                return result
        except Exception as exc:
            raise HTTPException(status_code=503, detail="审计运行状态无法确认；记录存储暂不可用") from exc
        raise HTTPException(status_code=404, detail="未找到该次审计记录；不能确认已归档")
    return result


@router.get("/audit-progress/{novel_id}")
async def get_audit_progress(novel_id: str, limit: int = Query(20,ge=1,le=100)):
    from infrastructure.persistence.database.connection import get_database
    import json
    try:
        rows = get_database().fetch_all("SELECT * FROM anti_ai_scan_progress WHERE novel_id=? ORDER BY rowid DESC LIMIT ?",(novel_id,limit))
        return [{**dict(row), "report": json.loads(row["report_json"])} for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=503,detail="独立扫描进度暂不可用；无法确认本次审计终态") from exc


@router.get("/audits/{novel_id}")
async def get_audit_history(
    novel_id: str,
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
):
    """获取小说的 Anti-AI 审计历史。"""
    repo = _get_audit_repo()
    if not repo:
        raise HTTPException(status_code=503, detail="审计仓储暂不可用")
    return repo.list_by_novel(novel_id, limit=limit)


@router.get("/audits/{novel_id}/{chapter_number}")
async def get_chapter_audit(novel_id: str, chapter_number: int):
    """获取指定章节的 Anti-AI 审计详情。"""
    repo = _get_audit_repo()
    if not repo:
        raise HTTPException(status_code=503, detail="审计仓储暂不可用")
    result = repo.get_by_chapter(novel_id, chapter_number)
    if not result:
        raise HTTPException(status_code=404, detail="未找到该章节的审计结果")
    return result


@router.get("/trend/{novel_id}")
async def get_audit_trend(
    novel_id: str,
    last_n: int = Query(20, ge=1, le=100, description="最近 N 章的趋势数据"),
):
    """获取 Anti-AI 审计趋势数据。"""
    repo = _get_audit_repo()
    if not repo:
        raise HTTPException(status_code=503, detail="审计仓储暂不可用")
    return repo.get_trend_data(novel_id, last_n=last_n)
