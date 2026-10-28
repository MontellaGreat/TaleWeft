"""Use production scanner and runtime with isolated fixtures, never a paid model."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from application.audit.services.anti_ai_audit import AntiAIAuditor, validate_completed_report
from application.engine.dag.anti_ai_scan import scan_chapter
from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.anti_ai_nodes import ChapterAuditNode, FinaleEnhancementNode
from application.engine.dag.nodes.validation_nodes import AntiAINode
from tests.dag.test_production_runtime import writer, manager, Provider


def test_zero_effective_rules_is_explicit_nonapproval(monkeypatch):
    auditor = AntiAIAuditor(rule_provider=lambda: {"patterns": [], "version": "empty-rules"})
    body = "他把钥匙放回账本。"
    report = auditor.scan_chapter("chapter", body)
    assert report.scan_status == "coverage_empty"
    assert report.effective_rule_count == 0
    with pytest.raises(RuntimeError):
        validate_completed_report(report, body)
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: auditor)
    result = scan_chapter(body, {})
    assert result.status == NodeStatus.ERROR
    assert result.outputs["effective_rule_count"] == 0


@pytest.mark.parametrize("scene_type", ["battle", "horror"])
def test_scene_allowance_counter_cannot_replace_effective_rule_count(scene_type):
    from application.audit.services.cliche_scanner import AI_CLICHE_PATTERNS_ENHANCED
    body = "他的嘴角上扬，眼中闪过一丝笑意。"
    report = AntiAIAuditor(scene_type=scene_type).scan_chapter("chapter", body)
    assert report.effective_rule_count == len(AI_CLICHE_PATTERNS_ENHANCED)
    validate_completed_report(report, body)


@pytest.mark.parametrize("tail", ["一切才刚刚开始。", "有些事情永远改变了。", "那天晚上改变了他的命运。",
    "他知道自己再也回不去了。", "他感到一种陌生的情绪。", "某种说不清的惆怅涌上心头。"])
def test_tail_specialist_runs_on_normal_chapters_and_keeps_body_offsets(monkeypatch, tail):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    body = "门口还放着一双旧靴。\n\n他收起了钥匙。\n\n" + tail
    result = scan_chapter(body, {"shared_state": {"is_final_chapter": False}})
    assert result.status == NodeStatus.WARNING
    assert result.outputs["tail_scan"]["scan_status"] == "completed"
    assert result.outputs["tail_scan"]["input_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert result.outputs["effective_rule_count"] > 0
    tail_hits = [hit for hit in result.outputs["hits"] if hit["category"] == "尾段专项"]
    assert tail_hits
    assert all(body[hit["start"]:hit["end"]] == hit["text"] for hit in tail_hits)


def test_tail_specialist_does_not_scan_earlier_paragraphs(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    result = scan_chapter("一切才刚刚开始。\n\n他关门。\n\n他收钥匙。\n\n灯灭了。", {})
    assert result.outputs["tail_scan"]["total_hits"] == 0


@pytest.mark.parametrize("text_kind", ["setup", "instructions"])
def test_specialist_is_for_story_endings_not_setup_documents(monkeypatch, text_kind):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    result = scan_chapter("一切才刚刚开始。", {"text_kind": text_kind})
    assert result.status == NodeStatus.SUCCESS
    assert result.outputs["scan_mode"] == "local_rules"
    assert result.outputs["tail_scan"]["scan_status"] == "not_applicable"


def test_runtime_derives_current_plan_and_author_override(writer):
    db, _ = writer
    runtime = manager(db, Provider())
    db.execute("UPDATE novels SET target_chapters=1 WHERE id='novel'")
    db.commit()
    _, state = runtime._chapter("novel")
    assert state["is_final_chapter"] is True and state["final_chapter_source"] == "chapter_plan"
    db.execute("UPDATE novels SET target_chapters=2 WHERE id='novel'")
    db.commit()
    _, state = runtime._chapter("novel")
    assert state["is_final_chapter"] is False
    _, state = runtime._chapter("novel", is_final_chapter=True)
    assert state["is_final_chapter"] is True and state["final_chapter_source"] == "author_confirmation"
    _, state = runtime._chapter("novel", is_final_chapter=False)
    assert state["is_final_chapter"] is False


def test_default_graph_has_pregeneration_finale_slot():
    from application.engine.dag.models import get_default_dag
    dag = get_default_dag()
    finale = dag.get_node("anti_ai_finale")
    assert finale and finale.type == "anti_ai_finale"
    assert any(edge.source == finale.id and edge.target == "exec_writer" for edge in dag.edges)


def test_existing_graph_upgrade_preserves_custom_and_disabled_finale_config(writer):
    from application.engine.dag.models import get_default_dag
    db, _ = writer
    runtime = manager(db, Provider())
    old = get_default_dag()
    old.nodes = [node for node in old.nodes if node.type != "anti_ai_finale"]
    ids = {node.id for node in old.nodes}
    old.edges = [edge for edge in old.edges if edge.source in ids and edge.target in ids]
    # A legitimate custom graph may rename its writer instance.
    old.get_node("exec_writer").id = "custom_writer"
    for edge in old.edges:
        if edge.source == "exec_writer": edge.source = "custom_writer"
        if edge.target == "exec_writer": edge.target = "custom_writer"
    old.get_node("val_anti_ai").config.temperature = 0.23
    runtime.versions.save("novel", old)
    upgraded = runtime.load_dag("novel")
    finale = next(node for node in upgraded.nodes if node.type == "anti_ai_finale")
    assert any(edge.source == finale.id and edge.target == "custom_writer" for edge in upgraded.edges)
    assert upgraded.get_node("val_anti_ai").config.temperature == 0.23
    assert not runtime.validate_production(upgraded)
    assert runtime.load_dag("novel").fingerprint() == upgraded.fingerprint()
    finale.enabled = False
    finale.config.prompt_template = "作者自定义终章限制"
    runtime.versions.save("novel", upgraded)
    preserved = runtime.load_dag("novel")
    assert preserved.get_node(finale.id).enabled is False
    assert preserved.get_node(finale.id).config.prompt_template == "作者自定义终章限制"


@pytest.mark.asyncio
async def test_standard_final_chapter_actually_injects_then_scans_body(writer):
    db, _ = writer
    db.execute("UPDATE novels SET target_chapters=1 WHERE id='novel'")
    db.commit()
    provider = Provider(dirty=False)
    runtime = manager(db, provider)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "awaiting_review", status
    finale = status["node_states"]["anti_ai_finale"]
    assert finale["status"] == "success"
    assert finale["outputs"]["body_scanned"] is False
    assert finale["outputs"]["finale_block"]
    assert any(finale["outputs"]["finale_block"] in prompt.system or finale["outputs"]["finale_block"] in prompt.user
               for prompt, _ in provider.calls)
    assert status["node_states"]["val_anti_ai"]["outputs"]["tail_scan"]["scan_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize('disabled', [False, True])
async def test_optional_finale_does_not_skip_normal_chapter_writer(writer, disabled):
    db, _ = writer
    runtime = manager(db, Provider(dirty=False))
    if disabled:
        await runtime.configure('novel', 'anti_ai_finale', toggle=True)
    await runtime.start('novel')
    await runtime.tasks['novel']
    status = await runtime.status('novel')
    assert status['status'] == 'awaiting_review', status
    assert status['node_states']['exec_writer']['status'] == 'success'
    assert status['node_states']['anti_ai_finale']['status'] == ('disabled' if disabled else 'success')
    assert status['node_states']['val_anti_ai']['outputs']['semantic_anti_ai']['status'] == 'completed'


@pytest.mark.asyncio
async def test_enabled_finale_failure_still_blocks_writer(writer, monkeypatch):
    db, _ = writer
    db.execute("UPDATE novels SET target_chapters=1 WHERE id='novel'")
    db.commit()
    monkeypatch.setattr(FinaleEnhancementNode, 'get_effective_prompt', lambda *_: {'system': '', 'user_template': ''})
    runtime = manager(db, Provider(dirty=False))
    await runtime.start('novel')
    await runtime.tasks['novel']
    status = await runtime.status('novel')
    assert status['status'] == 'error'
    assert status['node_states']['anti_ai_finale']['status'] == 'error'
    assert status['node_states']['exec_writer']['status'] == 'bypassed'


@pytest.mark.asyncio
async def test_finale_injection_is_pregeneration_and_resolves_actual_variables(monkeypatch):
    monkeypatch.setattr(FinaleEnhancementNode, "get_effective_prompt", lambda *_: {
        "system": "审稿器的系统角色", "user_template": "结尾约束 {finale_text} / {expected_ending}", "source": "builtin"})
    result = await FinaleEnhancementNode().execute({"is_final_chapter": True, "expected_ending": "守住渡口"}, {})
    assert result.status == NodeStatus.SUCCESS
    assert result.outputs["finale_mode"] == "prompt_pre_generation_only"
    assert result.outputs["body_scanned"] is False
    assert "守住渡口" in result.outputs["finale_block"]
    assert "{" not in result.outputs["finale_block"]


@pytest.mark.asyncio
async def test_rule_clean_dag_node_must_still_call_semantic_model(monkeypatch):
    from application.audit.services import anti_ai_audit
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    body, calls = "那是灵魂深处的一次洗礼，也是人生漫长旅途上的启示。", []
    async def invoke(prompt):
        request = json.loads(prompt.user)
        calls.append(request)
        return {"review_version": request["review_version"], "input_hash": request["input_hash"], "passed": False,
            "checks": {key: True for key in request["required_checks"]},
            "coverage": [{"text_id": request["texts"][0]["text_id"]}],
            "issues": [{"path": "$", "evidence": "灵魂深处的一次洗礼", "reason": "空泛抒情。",
                "suggestion": "用已有动作收束，不新增事实。", "category": "vague_lyricism"}]}
    result = await AntiAINode().execute({"content": body}, {"semantic_anti_ai_invoke": invoke})
    assert calls and result.status == NodeStatus.WARNING
    assert result.outputs["total_hits"] == 0
    assert result.outputs["semantic_anti_ai"]["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", ["val_anti_ai", "anti_ai_audit"])
@pytest.mark.parametrize("timeout_override", [None, 10])
async def test_semantic_executor_has_model_budget_and_respects_explicit_timeout(monkeypatch, node_type, timeout_override):
    from application.audit.services import anti_ai_audit
    from application.engine.dag.models import NodeConfig
    from application.engine.dag.registry import NodeRegistry
    from application.engine.dag import registry
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    async def virtual_wait(operation, timeout):
        # Simulate a 90-second model response without sleeping in the suite.
        if timeout < 90:
            operation.close()
            raise TimeoutError()
        return await operation
    monkeypatch.setattr(registry.asyncio, "wait_for", virtual_wait)
    calls = []
    async def invoke(prompt):
        request = json.loads(prompt.user)
        calls.append(request)
        return {"review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"], "issues": [],
            "checks": {key: True for key in request["required_checks"]},
            "coverage": [{"text_id": item["text_id"]}
                         for item in request["texts"]]}
    executor = NodeRegistry.create_executor(node_type, "review", NodeConfig(timeout_seconds=timeout_override))
    result = await executor({"content": "他把钥匙放回账本。", "semantic_anti_ai_invoke": invoke})
    if timeout_override is None:
        assert result.status == NodeStatus.SUCCESS, result.error
        assert len(calls) == 1 and result.outputs["semantic_anti_ai"]["status"] == "completed"
    else:
        assert result.status == NodeStatus.ERROR and "10s" in result.error
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [AntiAINode, ChapterAuditNode])
async def test_story_review_works_without_gateway_native_json_mode(monkeypatch, node_type):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    calls = []
    class TextJSONProvider:
        async def generate(self, prompt, config):
            calls.append(config)
            if config.response_format is not None:
                raise RuntimeError("gateway rejects native JSON output mode")
            request = json.loads(prompt.user)
            assert "JSON" in prompt.system
            return json.dumps({"review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"], "issues": [],
                "checks": {key: True for key in request["required_checks"]},
                "coverage": [{"text_id": item["text_id"]}
                             for item in request["texts"]]})
    result = await node_type().execute({"content": "他把钥匙放回账本。"}, {"llm_service": TextJSONProvider()})
    assert len(calls) == 1
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["semantic_anti_ai"]["status"] == "completed"


@pytest.mark.asyncio
async def test_semantic_provider_missing_is_a_dag_error_even_after_real_rule_scan(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    result = await AntiAINode().execute({"content": "他把钥匙放回账本。"}, {})
    assert result.status == NodeStatus.ERROR
    assert result.outputs["scan_status"] == "completed"
    assert result.outputs["semantic_anti_ai"]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [AntiAINode, ChapterAuditNode])
async def test_story_nodes_cannot_bypass_tail_with_document_context(monkeypatch, node_type):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    async def invoke(prompt):
        request = json.loads(prompt.user)
        return {"review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"], "issues": [],
            "checks": {key: True for key in request["required_checks"]},
            "coverage": [{"text_id": item["text_id"]} for item in request["texts"]]}
    result = await node_type().execute({"content": "一切才刚刚开始。"}, {
        "text_kind": "instructions", "semantic_anti_ai_invoke": invoke})
    assert result.status == NodeStatus.WARNING
    assert result.outputs["tail_scan"]["total_hits"] > 0


@pytest.mark.asyncio
async def test_semantic_rejection_rewrites_then_reaudits_and_commits_exact_receipt(writer):
    from tests.dag.test_production_runtime import CLEAN
    db, _ = writer
    dirty = CLEAN + "那是灵魂深处的一次洗礼，也是漫长人生旅途上的启示。"
    class SemanticProvider(Provider):
        def __init__(self):
            super().__init__(dirty=False)
            self.reviewed = []

        async def generate(self, prompt, config):
            if '"review_version": "semantic-style-' in prompt.user:
                request = json.loads(prompt.user)
                text = request["texts"][0]["text"]
                self.reviewed.append(text)
                report = json.loads(await super().generate(prompt, config))
                if text == dirty:
                    report.update(passed=False, issues=[{"path": "$", "evidence": "灵魂深处的一次洗礼",
                        "reason": "抽象感悟取代可感知的动作。", "suggestion": "删去概括，保留钥匙与敲门的既有动作。",
                        "category": "vague_lyricism"}])
                return json.dumps(report, ensure_ascii=False)
            result = await super().generate(prompt, config)
            if "plot_tension" not in prompt.system and "plot_tension" not in prompt.user and "次文风定向修正" not in prompt.user:
                return dirty
            return result
    provider, events = SemanticProvider(), []
    runtime = manager(db, provider, events=events)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "awaiting_review", status
    assert provider.reviewed == [dirty, CLEAN]
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == CLEAN
    rewrite_prompts = [prompt.user for prompt, _ in provider.calls if "次文风定向修正" in prompt.user]
    assert rewrite_prompts and "抽象感悟取代可感知的动作" in rewrite_prompts[0]
    assert all('每个自然段最多出现一个中文句号' in prompt.system
               for prompt, _ in provider.calls if "次文风定向修正" in prompt.user)
    candidate = db.fetch_one("SELECT audit_json FROM chapter_rewrite_candidates WHERE status='committed'")
    audit = json.loads(candidate["audit_json"])
    assert audit["semantic_anti_ai"]["passed"] is True
    assert audit["semantic_anti_ai"]["input_hash"] == hashlib.sha256(CLEAN.encode()).hexdigest()
    assert audit["effective_rule_count"] > 0 and audit["tail_scan"]["total_hits"] == 0


@pytest.mark.asyncio
async def test_semantic_invalid_response_keeps_formal_body_untouched(writer):
    db, _ = writer
    class BrokenSemanticProvider(Provider):
        async def generate(self, prompt, config):
            if '"review_version": "semantic-style-' in prompt.user:
                return '{"passed":true,"issues":[]}'
            return await super().generate(prompt, config)
    runtime = manager(db, BrokenSemanticProvider(dirty=False))
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "error"
    assert status["node_states"]["val_anti_ai"]["outputs"]["semantic_anti_ai"]["status"] == "failed"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert db.fetch_one("SELECT count(*) AS n FROM chapter_rewrite_candidates WHERE status='committed'")["n"] == 0


@pytest.mark.parametrize("field", ["rule_version", "tail_hash", "start"])
def test_commit_rejects_stale_specialist_receipt(monkeypatch, field):
    from application.audit.services import anti_ai_audit
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview
    from application.engine.dag.chapter_services import DAGChapterServices
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: AntiAIAuditor())
    body = "旧靴留在门边。\n他关门。\n他收钥匙。\n灯灭了。"
    state = {"content": body, **scan_chapter(body, {}).outputs}
    request = json.loads(SemanticAntiAIReview.build_prompt(body, version='semantic-style-v3').user)
    state["semantic_anti_ai"] = SemanticAntiAIReview.validate_report({
        "review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"], "issues": [],
        "checks": {key: True for key in request["required_checks"]},
        "coverage": [{"text_id": item["text_id"]}
                     for item in request["texts"]]}, body)
    DAGChapterServices._validate_anti_ai_review(state)
    state["tail_scan"][field] = -1 if field == "start" else "stale"
    with pytest.raises(RuntimeError, match="尾段"):
        DAGChapterServices._validate_anti_ai_review(state)
