"""Gateway 节点 — 网关与熔断（4 个节点）

- gw_circuit: 熔断保护
- gw_review: 审阅网关
- gw_condition: 条件路由
- gw_retry: 重试网关
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from application.engine.dag.models import (
    NodeCategory,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeStatus,
    PortDataType,
)
from application.engine.dag.registry import BaseNode, NodeRegistry
from infrastructure.ai.prompt_keys import (
    CIRCUIT_BREAKER,
    CONDITION_GATEWAY,
    RETRY_GATEWAY,
    REVIEW_GATEWAY,
)

logger = logging.getLogger(__name__)


@NodeRegistry.register("gw_halt")
class HaltNode(BaseNode):
    meta = NodeMeta(node_type="gw_halt", display_name="熔断终止", category=NodeCategory.GATEWAY,
        can_disable=False, is_configurable=False, description="熔断分支明确终止并保留候选正文")

    async def execute(self, inputs, context):
        return NodeResult(status=NodeStatus.ERROR, error="熔断器已打开，章节没有提交")

    def validate_inputs(self, inputs):
        return True


# ─── gw_circuit: 熔断保护 ───


@NodeRegistry.register("gw_circuit")
class CircuitNode(BaseNode):
    """熔断保护 — CircuitBreaker"""

    meta = NodeMeta(
        node_type="gw_circuit",
        display_name="熔断保护",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#ef4444",
        input_ports=[
            NodePort(name="error_count", data_type=PortDataType.SCORE, required=False, default=0),
            NodePort(name="max_errors", data_type=PortDataType.SCORE, required=False, default=3),
        ],
        output_ports=[
            NodePort(name="breaker_status", data_type=PortDataType.TEXT),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=False,
        default_timeout_seconds=5,
        cpms_node_key=CIRCUIT_BREAKER,
        description="CircuitBreaker 熔断保护网关",
        default_edges=["val_narrative"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            error_count = inputs.get("error_count", 0)
            thresholds = self._config.thresholds if self._config else {}
            max_errors = thresholds.get("max_errors", inputs.get("max_errors", 3))

            breaker_status = "open" if error_count >= max_errors else "closed"

            return NodeResult(
                outputs={"breaker_status": breaker_status},
                status=NodeStatus.WARNING if breaker_status == "open" else NodeStatus.SUCCESS,
                metrics={"error_count": float(error_count)},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"breaker_status": "open"}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── gw_review: 审阅网关 ───


@NodeRegistry.register("gw_review")
class ReviewNode(BaseNode):
    """审阅网关 — PAUSED_FOR_REVIEW 状态"""

    meta = NodeMeta(
        node_type="gw_review",
        display_name="⏸️ 审阅网关",
        category=NodeCategory.GATEWAY,
        icon="⏸️",
        color="#f59e0b",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=False),
            NodePort(name="metrics", data_type=PortDataType.JSON, required=False),
        ],
        output_ports=[
            NodePort(name="approved", data_type=PortDataType.BOOLEAN),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=10,
        cpms_node_key=REVIEW_GATEWAY,
        description="PAUSED_FOR_REVIEW 审阅网关",
        default_edges=[],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            # 检查是否自动审批模式
            approved = True  # 默认自动审批

            # 如果有关键指标异常，不自动审批
            metrics = inputs.get("metrics", {})
            if isinstance(metrics, dict):
                if metrics.get("drift_alert", False):
                    approved = False
                if metrics.get("breaker_status") == "open":
                    approved = False

            return NodeResult(
                outputs={"approved": approved},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"approved": False}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── gw_condition: 条件路由 ───


@NodeRegistry.register("gw_condition")
class ConditionNode(BaseNode):
    """条件路由 — 根据输入条件决定走哪条分支"""

    meta = NodeMeta(
        node_type="gw_condition",
        display_name="条件路由",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#3b82f6",
        input_ports=[
            NodePort(name="input", data_type=PortDataType.JSON, required=True),
        ],
        output_ports=[
            NodePort(name="output_true", data_type=PortDataType.JSON),
            NodePort(name="output_false", data_type=PortDataType.JSON),
        ],
        prompt_variables=[],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=5,
        cpms_node_key=CONDITION_GATEWAY,
        description="条件路由网关",
        default_edges=[],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            input_data = inputs.get("input", {})

            # 简单条件判断：检查是否有异常标志
            condition_met = True
            if isinstance(input_data, dict):
                condition_met = not (
                    input_data.get("drift_alert", False) or
                    input_data.get("breaker_status") == "open" or
                    input_data.get("error")
                )

            return NodeResult(
                outputs={
                    "output_true": input_data if condition_met else None,
                    "output_false": input_data if not condition_met else None,
                },
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"output_true": None, "output_false": None}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "input" in inputs


# ─── gw_retry: 重试网关 ───


@NodeRegistry.register("gw_retry")
class RetryNode(BaseNode):
    """重试网关 — 文风检查失败时触发重写"""

    meta = NodeMeta(
        node_type="gw_retry",
        display_name="重写网关",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#8b5cf6",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
            NodePort(name="max_attempts", data_type=PortDataType.SCORE, required=False, default=2),
        ],
        output_ports=[
            NodePort(name="output", data_type=PortDataType.JSON),
            NodePort(name="attempts_used", data_type=PortDataType.SCORE),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=600,
        cpms_node_key=RETRY_GATEWAY,
        description="真实去 AI 味/文风重写，按预算循环重新审计；失败保留候选正文",
        default_edges=[],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        state = context.get("shared_state", {})
        if state.get("breaker_status") == "open":
            return NodeResult(status=NodeStatus.ERROR, error="熔断器已打开，正文不能提交")
        import math
        score, hits = state.get("severity_score"), state.get("hits")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0 or not isinstance(hits, list):
            return NodeResult(status=NodeStatus.ERROR, error="Anti-AI 审计没有有效结果，正文不能提交")
        maximum = self._config.thresholds.get("anti_ai_max_score", 0) if self._config else 0
        if not state.get("drift_alert") and score <= maximum:
            return NodeResult(outputs={"content": inputs["content"], "quality_approved": True,
                "retry_required": False, "attempts_used": 0}, metrics={"retry_count": 0.0})
        return NodeResult(
            outputs={"attempts_used": 0, "quality_approved": False}, status=NodeStatus.ERROR,
            error="审计要求重写，但缺少章节运行服务，正文不能提交",
            metrics={"retry_count": 0.0},
        )

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return isinstance(inputs.get("content"), str) and bool(inputs["content"].strip())
