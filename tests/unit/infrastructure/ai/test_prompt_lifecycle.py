"""Exercise Plaza mutations against the same persisted state used by runtime."""
from pathlib import Path
import sqlite3

import pytest

from infrastructure.ai.prompt_manager import PromptManager
from infrastructure.ai.prompt_registry import PromptRegistry


class PromptDb:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        root = Path(__file__).resolve().parents[4]
        self.conn.executescript((root / "infrastructure/persistence/database/schema.sql").read_text(encoding="utf-8"))
        self.conn.executescript((root / "infrastructure/persistence/database/migrations/add_ai_invocation_variable_hub.sql").read_text(encoding="utf-8"))

    def get_connection(self):
        return self.conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


@pytest.fixture
def manager(tmp_path):
    db = PromptDb(tmp_path / "prompts.sqlite")
    mgr = PromptManager(db)
    assert mgr.ensure_seeded()
    yield mgr
    db.conn.close()


def custom(manager, key="custom-test"):
    return manager.create_node(manager.list_templates()[0].id, key, "Test", "original", "{content}")


def test_runtime_observes_saved_and_rolled_back_version_without_waiting_for_cache(manager):
    node = custom(manager)
    registry = PromptRegistry(prompt_manager=manager, cache_ttl_seconds=300)
    assert registry.get_system(node.node_key) == "original"
    manager.update_node(node.id, system_prompt="edited")
    assert registry.get_system(node.node_key) == "edited"
    manager.rollback_node(node.id, node.active_version_id)
    assert registry.get_system(node.node_key) == "original"


def test_delete_removes_custom_versions_and_cached_runtime_copy_persistently(manager):
    node = custom(manager)
    registry = PromptRegistry(prompt_manager=manager, cache_ttl_seconds=300)
    assert registry.get_system(node.node_key) == "original"
    assert manager.delete_node(node.id)
    assert registry.get_node(node.node_key) is None
    reopened = PromptManager(manager._db)
    assert reopened.get_node(node.node_key) is None
    assert reopened.get_node_versions(node.id) == []


def test_manager_protects_builtin_prompts(manager):
    node = manager.get_node("chapter-generation-main")
    with pytest.raises(ValueError, match="内置"):
        manager.delete_node(node.id)
    assert manager.get_node(node.node_key) is not None


@pytest.mark.parametrize("sql", [
    "INSERT INTO prompt_workflows (id, name) VALUES ('wf', 'Workflow'); INSERT INTO prompt_bindings (id, workflow_id, node_key, enabled) VALUES ('binding', 'wf', 'custom-test', 0)",
    "INSERT INTO invocation_specs (id, operation, node_key) VALUES ('spec', 'test', 'custom-test')",
    "INSERT INTO cpms_variable_binding_sets (id, node_key, direction) VALUES ('set', 'custom-test', 'input')",
    "INSERT INTO prompt_asset_link_sets (id, node_key) VALUES ('assets', 'custom-test')",
    "INSERT INTO ai_invocation_sessions (id, operation, node_key, policy, status) VALUES ('session', 'test', 'custom-test', 'DIRECT', 'RUNNING')",
])
def test_delete_preserves_referenced_custom_prompt_and_reports_conflict(manager, sql):
    node = custom(manager)
    manager._db.conn.executescript(sql)
    with pytest.raises(ValueError, match="引用"):
        manager.delete_node(node.id)
    assert manager.get_node(node.node_key).active_version_id == node.active_version_id
    assert len(manager.get_node_versions(node.id)) == 1


def test_rollback_cannot_activate_a_version_from_another_node(manager):
    first = custom(manager, "custom-first")
    second = custom(manager, "custom-second")
    assert manager.rollback_node(first.id, second.active_version_id) is None
    assert len(manager.get_node_versions(first.id)) == 1


def test_plaza_http_create_update_delete_refresh_and_reference_conflict(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from infrastructure.ai import prompt_registry
    from interfaces.api.v1.workbench import llm_control
    from application.engine.narrative_projection import linkage_kernel

    registry = PromptRegistry(prompt_manager=manager)
    monkeypatch.setattr(llm_control, "get_prompt_manager", lambda: manager)
    monkeypatch.setattr(prompt_registry, "_registry_instance", registry)
    monkeypatch.setattr(linkage_kernel, "linkage_bundle", lambda: {"nodes": [], "registry_cpms_by_type": {}})
    llm_control._invalidate_plaza_cache()
    app = FastAPI()
    app.include_router(llm_control.router)
    with TestClient(app) as client:
        created = client.post("/llm-control/prompts/nodes", json={"name": "HTTP Test", "node_key": "custom-http", "system": "v1", "user_template": "test"})
        assert created.status_code == 200
        node = created.json()["node"]
        assert registry.get_system(node["node_key"]) == "v1"
        updated = client.put("/llm-control/prompts/custom-http", json={"system": "v2"})
        assert updated.status_code == 200
        assert registry.get_system(node["node_key"]) == "v2"
        detail = client.get("/llm-control/prompts/custom-http").json()
        assert detail["can_delete"] is True
        assert detail["runtime_usage"]["status"] == "unbound"
        assert client.delete(f"/llm-control/prompts/nodes/{node['id']}").status_code == 200
        assert client.get("/llm-control/prompts/custom-http").status_code == 404
        plaza = client.get("/llm-control/prompts/plaza-init").json()
        assert all(item["node_key"] != "custom-http" for group in plaza["nodes_by_category"].values() for item in group)

        referenced = custom(manager)
        manager._db.conn.execute("INSERT INTO invocation_specs (id, operation, node_key) VALUES ('spec', 'test', ?)", (referenced.node_key,))
        manager._db.conn.commit()
        blocked = client.delete(f"/llm-control/prompts/nodes/{referenced.id}")
        assert blocked.status_code == 409
        assert "引用" in blocked.json()["detail"]
        detail = client.get(f"/llm-control/prompts/{referenced.node_key}").json()
        assert detail["can_delete"] is False
        assert detail["deletion_references"][0]["kind"] == "invocation_specs"
        builtin = manager.get_node("chapter-generation-main")
        assert client.delete(f"/llm-control/prompts/nodes/{builtin.id}").status_code == 403
    llm_control._invalidate_plaza_cache()
