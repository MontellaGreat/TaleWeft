"""Small, content-free execution receipts for the existing DAG runner."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from application.engine.dag.models import NodeExecutionReceipt, NodeStatus


def now():
    return datetime.now(timezone.utc).isoformat()


def data_hash(data):
    # Only stable JSON data enters the digest. Providers, callbacks and scoped
    # services have process-specific representations and are not input content.
    def clean(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()
                    if key not in {"services", "llm_service", "on_node_event", "_dag_node_receipts"}
                    and (item is None or isinstance(item, (str, int, float, bool, dict, list, tuple)))}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return None
    raw = json.dumps(clean(data), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def receipt_for(node_id, node_type, state, *, started_at=None, executed=False, completed=False,
                inputs=None, outputs=None, reason=None):
    return NodeExecutionReceipt(run_id=str(state.get("dag_run_id") or ""), node_id=node_id,
        node_type=node_type, executor_started=bool(started_at), executor_completed=completed,
        executed=executed, started_at=started_at, completed_at=now() if completed else None,
        input_hash=data_hash(inputs) if inputs is not None else "",
        output_hash=data_hash(outputs) if outputs is not None else "", reason=reason)


def unexecuted_result(result, node, state, reason):
    result.execution_receipt = receipt_for(node.id, node.type, state, completed=True,
        outputs=result.outputs, reason=reason)
    return result


def did_execute(result):
    return bool(result.execution_receipt and result.execution_receipt.executed
                and result.status not in {NodeStatus.BYPASSED, NodeStatus.DISABLED, NodeStatus.PENDING, NodeStatus.IDLE})
