"""Regression tests for previously silent skips / fabricated success.

All execution dependencies are injected; no database, LLM or live process runs.
"""
import asyncio
from types import SimpleNamespace

import pytest

from application.engine.dag.engine import DAGEngine, DAGExecutionError
from application.engine.dag.models import (
    DAGDefinition, EdgeCondition, EdgeDefinition, NodeCategory, NodeDefinition,
    NodeMeta, NodePort, NodeResult, NodeStatus, PortDataType, get_default_dag,
)
from application.engine.dag.registry import BaseNode, NodeRegistry, generation_text
from application.engine.dag.validator import DAGValidator


@pytest.fixture
def node_factory(monkeypatch):
    def register(node_id, behavior, *, inputs=None, outputs=None):
        node_type = "integrity_" + node_id
        class FakeNode(BaseNode):
            meta = NodeMeta(node_type=node_type, display_name=node_id, category=NodeCategory.EXECUTION,
                            input_ports=inputs or [], output_ports=outputs or [])
            async def execute(self, data, context):
                value = behavior(data, context)
                return await value if asyncio.iscoroutine(value) else value
            def validate_inputs(self, data):
                return True
        monkeypatch.setitem(NodeRegistry._registry, node_type, FakeNode)
        monkeypatch.setitem(NodeRegistry._meta_registry, node_type, FakeNode.meta)
        return NodeDefinition(id=node_id, type=node_type)
    return register


def graph(nodes, edges=()):
    return DAGDefinition(id="integrity", name="integrity", nodes=nodes, edges=list(edges))


def edge(source, target, condition=EdgeCondition.ALWAYS, **kwargs):
    return EdgeDefinition(id=f"edge_{source}_{target}", source=source, target=target, condition=condition, **kwargs)


def test_default_is_valid_and_every_enabled_node_is_scheduled():
    dag = get_default_dag()
    assert DAGValidator().validate(dag).errors == []
    layers = DAGEngine()._topological_layers(dag)
    assert {n.id for layer in layers for n in layer} == {n.id for n in dag.nodes if n.enabled}
    assert not any(e.source == "gw_retry" and e.target == "exec_writer" for e in dag.edges)


@pytest.mark.asyncio
async def test_result_metadata_failure_and_blocked_dependency(node_factory):
    source = node_factory("source", lambda *_: NodeResult(outputs={"content": "unsafe"}, status=NodeStatus.ERROR,
                                                          error="scanner unavailable", metrics={"attempts": 1}, duration_ms=42))
    sink = node_factory("sink", lambda *_: pytest.fail("Failed dependency must not execute"))
    result = await DAGEngine().run(graph([source, sink], [edge("source", "sink")]), {"novel_id": "n"})
    assert result.status == "error" and result.error_count == 1
    assert result.node_results["source"].error == "scanner unavailable"
    assert result.node_results["source"].metrics == {"attempts": 1}
    assert result.node_results["source"].duration_ms == 42
    assert result.node_results["sink"].status == NodeStatus.BYPASSED


@pytest.mark.asyncio
async def test_condition_uses_source_result_and_records_bypass(node_factory):
    source = node_factory("source", lambda *_: NodeResult(outputs={"drift_alert": True}, status=NodeStatus.WARNING))
    alert = node_factory("alert", lambda *_: NodeResult(metrics={"called": 1}))
    clean = node_factory("clean", lambda *_: pytest.fail("False branch must not execute"))
    result = await DAGEngine().run(graph([source, alert, clean], [
        edge("source", "alert", EdgeCondition.ON_DRIFT_ALERT), edge("source", "clean", EdgeCondition.ON_NO_DRIFT),
    ]), {"drift_alert": False})
    assert result.node_results["alert"].status == NodeStatus.SUCCESS
    assert result.node_results["clean"].status == NodeStatus.BYPASSED


@pytest.mark.asyncio
async def test_missing_condition_output_cannot_reuse_stale_global_flag(node_factory):
    source = node_factory("source", lambda *_: NodeResult())
    sink = node_factory("sink", lambda *_: pytest.fail("Stale condition must not execute"))
    result = await DAGEngine().run(graph([source, sink], [edge("source", "sink", EdgeCondition.ON_NO_DRIFT)]), {"drift_alert": False})
    assert result.status == "error"
    assert "未提供 drift_alert" in result.node_results["sink"].error


@pytest.mark.asyncio
async def test_source_error_can_route_on_error_but_run_remains_failed(node_factory):
    source = node_factory("source", lambda *_: NodeResult(status=NodeStatus.ERROR, error="boom"))
    recovery = node_factory("recovery", lambda *_: NodeResult())
    result = await DAGEngine().run(graph([source, recovery], [edge("source", "recovery", EdgeCondition.ON_ERROR)]), {})
    assert result.status == "error"
    assert result.node_results["recovery"].status == NodeStatus.SUCCESS


@pytest.mark.asyncio
async def test_data_edge_maps_named_ports(node_factory):
    source = node_factory("source", lambda *_: NodeResult(outputs={"message": "fresh"}),
                          outputs=[NodePort(name="message")])
    sink = node_factory("sink", lambda data, _: NodeResult(outputs={"observed": data["content"]}),
                        inputs=[NodePort(name="content")])
    result = await DAGEngine().run(graph([source, sink], [edge("source", "sink", source_port="message", target_port="content")]), {"content": "stale"})
    assert result.node_results["sink"].outputs["observed"] == "fresh"


@pytest.mark.asyncio
async def test_missing_required_input_fails_before_execute(node_factory):
    node = node_factory("source", lambda *_: pytest.fail("Missing required input"), inputs=[NodePort(name="content")])
    result = await DAGEngine().run(graph([node]), {})
    assert result.status == "error"
    assert "content" in result.node_results["source"].error


@pytest.mark.asyncio
async def test_cycle_and_unknown_type_never_complete(node_factory):
    a = node_factory("source", lambda *_: pytest.fail("Invalid graph must not execute"))
    b = node_factory("sink", lambda *_: NodeResult())
    result = await DAGEngine().run(graph([a, b], [edge("source", "sink"), edge("sink", "source")]), {})
    assert result.status == "error"
    a.type = "unregistered_integrity_node"
    result = await DAGEngine().run(graph([a]), {})
    assert result.status == "error"
    assert "未注册" in result.node_results["source"].error


@pytest.mark.asyncio
async def test_timeout_is_node_error_and_cancels_work(node_factory, monkeypatch):
    stopped = asyncio.Event()
    async def slow(*_):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
    monkeypatch.setattr(BaseNode, "get_timeout", lambda _: 0.01)
    result = await DAGEngine().run(graph([node_factory("source", slow)]), {})
    assert result.status == "error"
    assert "超时" in result.node_results["source"].error
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_task_cancellation_is_not_success_or_swallowed(node_factory):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def slow(*_):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
    task = asyncio.create_task(DAGEngine().run(graph([node_factory("source", slow)]), {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_retry_request_is_terminal_error():
    from application.engine.dag.nodes.gateway_nodes import RetryNode
    result = await RetryNode().execute({"content": "正文"}, {"shared_state": {"drift_alert": True, "severity_score": 0, "hits": []}})
    assert result.status == NodeStatus.ERROR
    assert result.outputs["attempts_used"] == 0
    assert "缺少章节运行服务" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "drift", "scan", "circuit"])
async def test_default_paths_never_fake_retry_or_scanner_success(monkeypatch, failure):
    dag = get_default_dag()
    def fake_executor(node_type, node_id, config):
        async def execute(state):
            if node_type == "val_anti_ai" and failure == "scan":
                return NodeResult(status=NodeStatus.ERROR, error="scanner unavailable")
            if node_type == "gw_retry":
                from application.engine.dag.nodes.gateway_nodes import RetryNode
                return await RetryNode(config).execute({"content": state["content"]}, {"shared_state": state})
            if node_type == "gw_halt":
                from application.engine.dag.nodes.gateway_nodes import HaltNode
                return await HaltNode(config).execute({}, {})
            outputs = {"content": "正文", "drift_alert": failure == "drift", "breaker_status": "open" if failure == "circuit" else "closed",
                       "severity_score": 0, "hits": []}
            return NodeResult(outputs=outputs)
        return execute
    monkeypatch.setattr(NodeRegistry, "create_executor", fake_executor)
    result = await DAGEngine().run(dag, {"novel_id": "n"})
    assert set(result.node_results) == {n.id for n in dag.nodes}
    assert result.status == ("completed" if failure is None else "error")
    if failure in ("drift", "circuit"):
        assert result.node_results["gw_retry"].status == NodeStatus.ERROR
    if failure == "scan":
        assert result.node_results["val_anti_ai"].error == "scanner unavailable"
        assert result.node_results["val_narrative"].status == NodeStatus.BYPASSED


def test_generation_uses_content_value_not_object_repr():
    assert generation_text(SimpleNamespace(content="真实正文")) == "真实正文"
    with pytest.raises(ValueError):
        generation_text(SimpleNamespace(content="  "))
    with pytest.raises(ValueError):
        generation_text(object())


@pytest.mark.asyncio
async def test_breaker_keeps_prior_runtime_errors_and_never_completes(monkeypatch):
    dag = get_default_dag()
    def fake_executor(node_type, node_id, config):
        async def execute(state):
            if node_type == "gw_circuit":
                from application.engine.dag.nodes.gateway_nodes import CircuitNode
                return await CircuitNode(config).execute({"error_count": state["error_count"]}, {})
            if node_type == "gw_retry":
                from application.engine.dag.nodes.gateway_nodes import RetryNode
                return await RetryNode(config).execute({"content": state["content"]}, {"shared_state": state})
            if node_type == "gw_halt":
                from application.engine.dag.nodes.gateway_nodes import HaltNode
                return await HaltNode(config).execute({}, {})
            return NodeResult(outputs={"content": "正文", "drift_alert": False, "severity_score": 0, "hits": []})
        return execute
    monkeypatch.setattr(NodeRegistry, "create_executor", fake_executor)
    result = await DAGEngine().run(dag, {"novel_id": "n", "error_count": 3})
    assert result.status == "error"
    assert result.node_results["gw_circuit"].outputs["breaker_status"] == "open"
    assert result.node_results["val_narrative"].status == NodeStatus.BYPASSED
    assert result.node_results["exec_commit"].status == NodeStatus.BYPASSED
    assert "熔断" in result.node_results["gw_halt"].error


def test_prompt_metadata_is_instance_local():
    from application.engine.dag.nodes.ext_supplement_nodes import SummaryNode
    first, second = SummaryNode(), SummaryNode()
    original = second.meta.cpms_node_key
    first.meta.cpms_node_key = "temporary"
    assert second.meta.cpms_node_key == original
