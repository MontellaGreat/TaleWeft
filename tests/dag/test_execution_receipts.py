"""Adversarial checks for execution, prompt consumption and durable body binding."""
import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from application.engine.dag.chapter_services import DAGChapterServices, content_hash
from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import NodeConfig, NodeExecutionReceipt, NodePort, NodeResult, NodeStatus
from application.engine.dag.nodes.anti_ai_nodes import BehaviorProtocolNode, MidGenerationRefreshNode
from application.engine.dag.nodes.execution_nodes import WriterNode
from application.engine.dag.registry import NodeRegistry
from application.engine.rules.prose_prompt_hooks import ProsePromptHooks, generate_direct_prose
from application.core.taxonomy.market_selection import normalize_market_preferences
from domain.ai.value_objects.prompt import Prompt
from tests.dag.test_execution_integrity import node_factory, graph, edge
from tests.dag.test_production_runtime import writer, manager, Provider, CLEAN
from tests.unit.application.core.test_market_selection_integrity import custom_selection


@pytest.mark.asyncio
async def test_disabled_required_dependency_does_not_run_downstream_or_emit_executed(node_factory):
    source = node_factory("source", lambda *_: pytest.fail("disabled node executed"))
    source.enabled = False
    sink = node_factory("sink", lambda *_: pytest.fail("disabled dependency was dropped"))
    events = []
    result = await DAGEngine().run(graph([source, sink], [edge("source", "sink")]), {"on_node_event": events.append})
    assert result.node_results["source"].status == NodeStatus.DISABLED
    assert result.node_results["sink"].status == NodeStatus.BYPASSED
    assert len(events) == 2
    assert all(event["executed"] is False and event["execution_receipt"]["reason"] for event in events)


@pytest.mark.asyncio
async def test_preflight_failure_and_actual_failure_have_distinct_receipts(node_factory):
    preflight = node_factory("preflight", lambda *_: pytest.fail("required input absent"), inputs=[NodePort(name="content")])
    def crash(*_):
        raise RuntimeError("actual execution failed")
    actual = node_factory("actual", crash)
    result = await DAGEngine().run(graph([preflight, actual]), {"dag_run_id": "receipt-run"})
    before = result.node_results["preflight"].execution_receipt
    after = result.node_results["actual"].execution_receipt
    assert not before.executed and not before.executor_started and not before.started_at
    assert after.executed and after.executor_started and after.executor_completed and after.started_at
    assert after.run_id == "receipt-run" and after.node_id == "actual"
    assert after.input_hash and after.output_hash


@pytest.mark.asyncio
async def test_forged_incomplete_success_receipt_is_an_error(node_factory, monkeypatch):
    node = node_factory("source", lambda *_: NodeResult())
    def forged(*_):
        async def execute(_state):
            return NodeResult(execution_receipt=NodeExecutionReceipt(executed=False))
        return execute
    monkeypatch.setattr(NodeRegistry, "create_executor", forged)
    result = await DAGEngine().run(graph([node]), {})
    assert result.status == "error"
    assert "没有完整执行回执" in result.node_results["source"].error


@pytest.mark.asyncio
async def test_behavior_injects_real_cpms_rules_and_refresh_reports_its_actual_mode(monkeypatch):
    monkeypatch.setattr(BehaviorProtocolNode, "get_effective_prompt", lambda *_:
        {"system": "编排器角色描述", "user_template": "P1 真正的规则 {nervous_habits}\nR8 真正的替换", "source": "cpms"})
    result = await BehaviorProtocolNode().execute({"nervous_habits": "摸袖口"}, {})
    assert result.status == NodeStatus.SUCCESS
    assert "P1 真正的规则 摸袖口" in result.outputs["behavior_protocol"]
    assert "编排器角色描述" not in result.outputs["behavior_protocol"]
    refresh = await MidGenerationRefreshNode().execute({}, {})
    assert refresh.status == NodeStatus.WARNING
    assert refresh.outputs["token_interception"] is False
    assert refresh.outputs["refresh_mode"] == "prompt_pre_generation_only"
    assert "未执行 Token" in refresh.error


@pytest.mark.asyncio
async def test_writer_consumes_all_fragments_even_when_custom_cpms_has_no_slots(writer, monkeypatch):
    db, _queue = writer
    monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "自定义正文规则", "user": "请写正文"})
    seen = []
    class TextProvider:
        async def generate(self, prompt, _config):
            seen.append(prompt)
            return CLEAN
    fragments = {key: f"独立约束片段-{key}" for key in
        ("behavior_protocol", "character_state_lock", "allowlist_block", "refresh_block", "finale_block")}
    sources = {key: {"node_id": "upstream-" + key, "run_id": "fragment-run",
        "fragment_hash": hashlib.sha256(value.encode()).hexdigest(), "output_hash": "verified-output"}
        for key, value in fragments.items()}
    state = {"novel_id": "novel", "chapter_number": 1, "dag_run_id": "fragment-run", "_dag_injection_receipts": sources}
    result = await WriterNode().execute(fragments, {"novel_id": "novel", "shared_state": state,
        "services": SimpleNamespace(db=db), "llm_service": TextProvider()})
    assert result.status == NodeStatus.SUCCESS, result.error
    assert len(seen) == 1
    assert all(value in seen[0].system + seen[0].user for value in fragments.values())
    evidence = result.outputs["anti_ai_prompt_evidence"]
    assert evidence["status"] == "completed" and evidence["persistence_status"] == "committed"
    assert evidence["content_hash"] == content_hash(CLEAN)
    assert {item["variable"] for item in evidence["dag_injections"]} == set(fragments)
    assert all(item["applied"] and item["source"] == sources[item["variable"]] for item in evidence["dag_injections"])
    row = db.fetch_one("SELECT * FROM anti_ai_prompt_runs WHERE run_id=?", (evidence["run_id"],))
    assert json.loads(row["evidence_json"])["content_hash"] == content_hash(CLEAN)


@pytest.mark.asyncio
async def test_tampered_upstream_fragment_is_rejected_before_provider(writer, monkeypatch):
    db, _queue = writer
    monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "正文", "user": "正文"})
    class NeverProvider:
        async def generate(self, *_):
            pytest.fail("tampered fragment reached provider")
    result = await WriterNode().execute({"refresh_block": "已被篡改"}, {"novel_id": "novel",
        "shared_state": {"_dag_injection_receipts": {"refresh_block": {"fragment_hash": "wrong"}}},
        "services": SimpleNamespace(db=db), "llm_service": NeverProvider()})
    assert result.status == NodeStatus.ERROR and "片段回执" in result.error
    assert db.fetch_one("SELECT count(*) AS n FROM anti_ai_prompt_runs")["n"] == 0


@pytest.mark.asyncio
async def test_writer_without_upstream_fragments_consumes_real_cpms_fallback_with_no_slots(writer):
    db, _queue = writer
    seen = []
    class TextProvider:
        async def generate(self, prompt, _config):
            seen.append(prompt)
            return CLEAN
    result = await WriterNode(NodeConfig(prompt_template='作者自定义正文规则')).execute(
        {'nervous_habits': '阿青紧张时捻袖口'}, {'novel_id': 'novel',
        'shared_state': {'chapter_number': 1, 'dag_run_id': 'fallback-run'},
        'services': SimpleNamespace(db=db), 'llm_service': TextProvider()})
    assert result.status == NodeStatus.SUCCESS, result.error
    actual = seen[0].system + seen[0].user
    assert 'P1. 信息密度法则' in actual and 'R8.' in actual
    assert '阿青紧张时捻袖口' in actual
    assert '{physical_state}' not in actual and '{allowed_patterns}' not in actual
    evidence = result.outputs['anti_ai_prompt_evidence']
    injected = {item['variable']: item for item in evidence['dag_injections']}
    assert injected['behavior_protocol']['applied'] is True
    assert injected['behavior_protocol']['source'] == {'source': 'cpms_fallback'}
    assert evidence['content_hash'] == content_hash(CLEAN)
    row = db.fetch_one('SELECT * FROM anti_ai_prompt_runs WHERE run_id=?', (evidence['run_id'],))
    stored = json.loads(row['evidence_json'])
    assert row['status'] == 'completed' and stored['content_hash'] == content_hash(CLEAN)
    assert stored['dag_injections'] == evidence['dag_injections']


@pytest.mark.asyncio
async def test_cpms_fallback_user_rules_are_bound_to_actual_prompt_and_durable_receipt(writer, monkeypatch):
    from infrastructure.ai import prompt_registry
    from infrastructure.ai.prompt_keys import ANTI_AI_BEHAVIOR_PROTOCOL
    db, _queue = writer
    cpms_system = '本项目 CPMS 编排器角色'
    cpms_user = '本项目独有去味规则：压力动作必须写为{nervous_habits}，禁止抽象情绪总结。'
    class Registry:
        def get_system(self, key):
            return cpms_system if key == ANTI_AI_BEHAVIOR_PROTOCOL else ''
        def get_user_template(self, key):
            return cpms_user if key == ANTI_AI_BEHAVIOR_PROTOCOL else ''
    monkeypatch.setattr(prompt_registry, 'get_prompt_registry', Registry)
    seen = []
    class Provider:
        async def generate(self, prompt, _config):
            seen.append(prompt)
            return CLEAN
    result = await WriterNode(NodeConfig(prompt_template='没有注入槽的作者模板')).execute(
        {'nervous_habits': '捻袖口'}, {'novel_id': 'novel',
        'shared_state': {'chapter_number': 1, 'dag_run_id': 'project-cpms-run'},
        'services': SimpleNamespace(db=db), 'llm_service': Provider()})
    assert result.status == NodeStatus.SUCCESS, result.error
    expected = cpms_system + '\n\n' + cpms_user.replace('{nervous_habits}', '捻袖口')
    assert expected in seen[0].system
    evidence = result.outputs['anti_ai_prompt_evidence']
    row = db.fetch_one('SELECT evidence_json FROM anti_ai_prompt_runs WHERE run_id=?', (evidence['run_id'],))
    stored = json.loads(row['evidence_json'])
    item = next(item for item in stored['dag_injections'] if item['variable'] == 'behavior_protocol')
    assert item['fragment_hash'] == hashlib.sha256(expected.encode('utf-8')).hexdigest()
    assert item['source'] == {'source': 'cpms_fallback'}
    assert stored['content_hash'] == content_hash(CLEAN)


@pytest.mark.asyncio
async def test_empty_provider_response_has_failed_durable_receipt(writer):
    db, _queue = writer
    class EmptyProvider:
        async def generate(self, *_):
            return SimpleNamespace(content="  ")
    evidence = {}
    with pytest.raises(ValueError, match="empty"):
        await generate_direct_prose(EmptyProvider(), Prompt(system="规则", user="正文"), None,
            context={"novel_id": "novel", "chapter_number": 1}, db=db, evidence_sink=evidence)
    assert evidence["status"] == "failed" and "content_hash" not in evidence
    assert db.fetch_one("SELECT status FROM anti_ai_prompt_runs")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete", "body", "book", "fabricate"])
async def test_commit_rejects_deleted_forged_or_unbound_prompt_receipt(writer, tamper):
    db, _queue = writer
    class ChangingServices(DAGChapterServices):
        def commit_chapter(self, state):
            evidence = state["anti_ai_prompt_evidence"]
            if tamper == "delete":
                self.db.execute("DELETE FROM anti_ai_prompt_runs WHERE run_id=?", (evidence["run_id"],))
            elif tamper == "body":
                self.db.execute("UPDATE anti_ai_prompt_runs SET evidence_json=json_set(evidence_json,'$.content_hash','wrong')")
            elif tamper == "book":
                self.db.execute("UPDATE anti_ai_prompt_runs SET novel_id='another-book'")
            else:
                evidence["run_id"] = "self-reported-nonexistent-run"
            self.db.commit()
            return super().commit_chapter(state)
    runtime = manager(db, Provider(dirty=False), service_cls=ChangingServices)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert db.fetch_one("SELECT status FROM chapter_rewrite_candidates")["status"] != "committed"


@pytest.mark.asyncio
async def test_commit_sql_rechecks_prompt_receipt_after_precheck(writer):
    db, _queue = writer
    class RaceServices(DAGChapterServices):
        def _validate_prompt_evidence(self, evidence, content, **kwargs):
            super()._validate_prompt_evidence(evidence, content, **kwargs)
            if kwargs["state"].get("quality_approved"):
                self.db.execute("DELETE FROM anti_ai_prompt_runs WHERE run_id=?", (evidence["run_id"],))
                self.db.commit()
    runtime = manager(db, Provider(dirty=False), service_cls=RaceServices)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    assert (await runtime.status("novel"))["status"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete", "report", "after_precheck"])
async def test_dag_commit_requires_its_generation_full_scan_receipt(writer, tamper):
    db, queue = writer
    deleted_at_write = []
    if tamper == "after_precheck":
        original_handler = queue._handlers["execute_sql_txn_batch"]
        def before_write(payload):
            if any(op.get("sql", "").startswith("UPDATE chapters SET content=")
                   and "anti_ai_audit_runs" in op.get("sql", "")
                   for op in payload.get("operations", [])):
                with sqlite3.connect(db.db_path) as connection:
                    deleted_at_write.append(connection.execute("DELETE FROM anti_ai_audit_runs").rowcount)
            return original_handler(payload)
        queue.register_handler("execute_sql_txn_batch", before_write)
    class ChangingServices(DAGChapterServices):
        def commit_chapter(self, state):
            scan_id = state["anti_ai_prompt_evidence"]["anti_ai_scan"]["run_id"]
            if tamper == "delete":
                self.db.execute("DELETE FROM anti_ai_audit_runs WHERE run_id=?", (scan_id,))
            elif tamper == "report":
                self.db.execute("UPDATE anti_ai_audit_runs SET report_json=json_set(report_json,'$.job_id','another-attempt') WHERE run_id=?", (scan_id,))
            self.db.commit()
            return super().commit_chapter(state)
    runtime = manager(db, Provider(dirty=False), service_cls=ChangingServices)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    assert (await runtime.status("novel"))["status"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert db.fetch_one("SELECT status FROM chapter_rewrite_candidates")["status"] != "committed"
    if tamper == "after_precheck":
        assert deleted_at_write and deleted_at_write[0] > 0


@pytest.mark.asyncio
async def test_archive_failure_blocks_writer_commit(writer, monkeypatch):
    db, _queue = writer
    from application.engine.rules import prose_prompt_hooks
    original = prose_prompt_hooks.archive_prompt_use
    def failed_archive(*args, **kwargs):
        receipt = original(*args, **kwargs)
        receipt["persistence_status"] = "failed"
        return receipt
    monkeypatch.setattr(prose_prompt_hooks, "archive_prompt_use", failed_archive)
    runtime = manager(db, Provider(dirty=False))
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "error"
    assert "未持久化" in status["node_states"]["exec_writer"]["error"]
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert db.fetch_one("SELECT count(*) AS n FROM chapter_rewrite_candidates")["n"] == 0


def save_preferences(db, raw):
    db.execute("UPDATE novels SET generation_prefs_json=? WHERE id='novel'", (json.dumps(raw, ensure_ascii=False),))
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["draft", "snapshot"])
async def test_invalid_market_profile_blocks_dag_before_any_provider_call(writer, tamper):
    db, _queue = writer
    if tamper == "draft":
        raw = normalize_market_preferences({"audience_mode": "male", "canonical_genre_id": "major.xuanhuan",
            "canonical_theme_id": "theme.xuanhuan.dongfang"})
    else:
        raw = normalize_market_preferences(custom_selection())
        raw["market_profile_snapshot"]["profile"]["writing_style"] = "伪造快照"
    save_preferences(db, raw)
    provider = Provider(dirty=False)
    with pytest.raises(ValueError):
        await manager(db, provider).start("novel")
    assert provider.calls == []
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"


@pytest.mark.asyncio
async def test_final_five_dimensions_are_consumed_by_provider_and_bound_to_receipt(writer):
    db, _queue = writer
    raw = normalize_market_preferences(custom_selection())
    save_preferences(db, raw)
    provider = Provider(dirty=False)
    runtime = manager(db, provider)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "awaiting_review", status
    evidence = status["result"]["final_state"]["anti_ai_prompt_evidence"]
    assert evidence["market_profile_hash"] == raw["profile_snapshot_hash"]
    prose = [prompt for prompt, _config in provider.calls if "去味执行协议" in prompt.system]
    assert prose
    for value in raw["market_profile_snapshot"]["profile"].values():
        assert all(value in prompt.system + prompt.user for prompt in prose)


def test_frozen_market_profile_changes_and_missing_hash_are_rejected(writer):
    db, _queue = writer
    hooks = ProsePromptHooks(db)
    raw = normalize_market_preferences(custom_selection())
    save_preferences(db, raw)
    _prompt, evidence = hooks.compose(Prompt(system="正文规则", user="本章"), {"novel_id": "novel"})
    hooks.validate_current_market_profile(evidence, {"novel_id": "novel"})
    with pytest.raises(RuntimeError, match="市场画像已变化"):
        hooks.validate_current_market_profile({}, {"novel_id": "novel"})
    raw["reader_contract"] = "修改后的读者契约"
    save_preferences(db, normalize_market_preferences(raw))
    with pytest.raises(RuntimeError, match="市场画像已变化"):
        hooks.validate_current_market_profile(evidence, {"novel_id": "novel"})


@pytest.mark.asyncio
async def test_cancelled_executor_has_attempt_receipt_but_unscheduled_nodes_do_not(writer):
    db, _queue = writer
    entered = asyncio.Event()
    runtime = manager(db, Provider(delay=entered))
    ack = await runtime.start("novel")
    await asyncio.wait_for(entered.wait(), 5)
    await runtime.stop("novel", ack["dag_run_id"])
    status = await runtime.status("novel")
    started = status["result"]["node_results"]["exec_writer"]["execution_receipt"]
    waiting = status["result"]["node_results"]["exec_commit"]["execution_receipt"]
    assert started["executor_started"] and started["executed"] and not started["executor_completed"]
    assert "未完成" in started["reason"]
    assert not waiting["executed"] and not waiting["executor_started"]
    assert waiting["reason"]
