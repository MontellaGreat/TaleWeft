from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.v1.engine.dag import dag_routes
from application.engine.dag.runtime import DAGExecutionManager
from infrastructure.persistence.database.connection import DatabaseConnection


def test_run_and_stop_reject_missing_novel(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / 'dag.db'))
    monkeypatch.setattr(dag_routes, '_execution_manager', DAGExecutionManager(db, None))
    app = FastAPI()
    app.include_router(dag_routes.router)
    with TestClient(app) as client:
        for operation in ("run", "stop"):
            response = client.post(f"/dag/isolated-test/{operation}", json={})
            assert response.status_code == 404
    db.close_all()


def test_capabilities_report_connected_production_runner():
    capability = dag_routes._execution_capabilities()
    assert not capability["can_checkpoint"]
    assert capability["can_run"] and capability['can_stop'] and capability['can_rewrite']


def test_node_settings_reject_missing_novel_without_updating_cache(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / 'dag.db'))
    monkeypatch.setattr(dag_routes, '_execution_manager', DAGExecutionManager(db, None))
    app = FastAPI()
    app.include_router(dag_routes.router)
    node = dag_routes._get_dag_for_novel("isolated-config").get_node("val_anti_ai")
    before = node.model_dump()
    with TestClient(app) as client:
        response = client.post("/dag/isolated-config/nodes/val_anti_ai/toggle")
        assert response.status_code == 404
        response = client.put("/dag/isolated-config/nodes/val_anti_ai", json={"temperature": 0.3})
        assert response.status_code == 404
    assert node.model_dump() == before
    db.close_all()
