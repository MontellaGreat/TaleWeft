from types import SimpleNamespace

import pytest

from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.anti_ai_nodes import ChapterAuditNode
from application.engine.dag.nodes.validation_nodes import AntiAINode
from application.engine.dag.nodes.execution_nodes import WriterNode


@pytest.mark.asyncio
@pytest.mark.parametrize("node_name", ["BehaviorProtocolNode", "AllowlistNode", "CharacterStateLockNode", "MidGenerationRefreshNode", "FinaleEnhancementNode"])
async def test_missing_injection_prompt_is_error(node_name, monkeypatch):
    from application.engine.dag.nodes import anti_ai_nodes
    node_type = getattr(anti_ai_nodes, node_name)
    monkeypatch.setattr(node_type, "get_effective_prompt", lambda *_: {"system": "", "user_template": "", "source": "none"})
    result = await node_type().execute({"is_final_chapter": True}, {})
    assert result.status == NodeStatus.ERROR
    assert "CPMS" in result.error


@pytest.mark.asyncio
async def test_non_final_chapter_records_finale_bypass():
    from application.engine.dag.nodes.anti_ai_nodes import FinaleEnhancementNode
    result = await FinaleEnhancementNode().execute({"is_final_chapter": False}, {})
    assert result.status == NodeStatus.BYPASSED


@pytest.mark.asyncio
@pytest.mark.parametrize("node_name,module_name,method", [
    ("StyleNode", "voice_drift_service", "analyze"),
    ("TensionNode", "tension_scoring_service", "score"),
])
async def test_unavailable_validation_dependency_is_error(node_name, module_name, method, monkeypatch):
    import sys
    from application.engine.dag.nodes import validation_nodes
    async def unavailable(*_):
        raise RuntimeError("audit dependency unavailable")
    service = type("Service", (), {method: unavailable})
    module = SimpleNamespace(**{("VoiceDriftService" if method == "analyze" else "TensionScoringService"): service})
    monkeypatch.setitem(sys.modules, "application.analyst.services." + module_name, module)
    result = await getattr(validation_nodes, node_name)().execute({"content": "正文"}, {})
    assert result.status == NodeStatus.ERROR
    assert "audit dependency unavailable" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [AntiAINode, ChapterAuditNode])
async def test_actual_scanner_reports_real_hits(node_type):
    result = await node_type().execute({"content": "他的嘴角上扬，眼中闪过一丝笑意。"}, {"chapter_number": 1})
    assert result.status == NodeStatus.WARNING
    assert result.outputs["total_hits"] > 0
    assert result.outputs["severity_score"] > 0
    assert result.outputs["hits"]
    assert isinstance(result.outputs["hits"][0], dict)


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [AntiAINode, ChapterAuditNode])
@pytest.mark.parametrize("content", [None, "", "   "])
async def test_blank_content_is_error_not_clean_scan(node_type, content):
    result = await node_type().execute({"content": content}, {})
    assert result.status == NodeStatus.ERROR
    assert result.error
    assert "severity_score" not in result.outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [AntiAINode, ChapterAuditNode])
async def test_unavailable_scanner_never_reports_zero_success(node_type, monkeypatch):
    from application.audit.services import anti_ai_audit
    def unavailable():
        raise RuntimeError("scanner unavailable")
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", unavailable)
    result = await node_type().execute({"content": "有效正文"}, {})
    assert result.status == NodeStatus.ERROR
    assert result.error == "scanner unavailable"
    assert "severity_score" not in result.outputs


@pytest.mark.asyncio
async def test_writer_uses_injected_configured_provider_content(monkeypatch, tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection
    db = DatabaseConnection(str(tmp_path / "writer.sqlite"))
    monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "system", "user": "user"})
    class Provider:
        async def generate(self, *_):
            return SimpleNamespace(content="实际章节正文")
    result = await WriterNode().execute({}, {
        "llm_service": Provider(),
        "shared_state": {
            "novel_id": "writer-test-novel",
            "chapter_number": 1,
            "services": SimpleNamespace(db=db),
        },
    })
    try:
        assert result.status == NodeStatus.SUCCESS
        assert result.outputs["content"] == "实际章节正文"
    finally:
        db.close_all(skip_checkpoint=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_writer_empty_response_or_provider_failure_is_error(monkeypatch, failure):
    monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "system", "user": "user"})
    class Provider:
        async def generate(self, *_):
            if failure:
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(content="")
    result = await WriterNode().execute({}, {"llm_service": Provider()})
    assert result.status == NodeStatus.ERROR
    assert result.error
    assert not result.outputs.get("content")
