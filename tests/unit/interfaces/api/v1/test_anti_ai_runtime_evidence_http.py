"""HTTP regressions for capability facts and real Anti-AI execution evidence."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.audit.services.anti_ai_audit import AntiAIAuditor
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces.api.v1 import anti_ai


@pytest.fixture
def audit_client(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "anti-ai-http.sqlite"))
    monkeypatch.setattr(connection, "get_database", lambda *args, **kwargs: db)
    monkeypatch.setattr(anti_ai, "get_anti_ai_auditor", lambda: AntiAIAuditor())
    registry = SimpleNamespace(
        total_count=7,
        list_by_category=lambda _category: [],
        get_categories=lambda: [],
        exists=lambda _key: True,
    )
    monkeypatch.setattr(anti_ai, "get_prompt_registry", lambda: registry)
    app = FastAPI()
    app.include_router(anti_ai.router, prefix="/api/v1")
    with TestClient(app) as client:
        yield client, db
    db.close_all(skip_checkpoint=True)


def test_manual_scan_is_ephemeral_and_identified(audit_client):
    client, db = audit_client
    response = client.post("/api/v1/anti-ai/scan", json={"content": "他的嘴角上扬，眼中闪过一丝笑意。"})
    assert response.status_code == 200
    result = response.json()
    assert result["total_hits"] > 0
    assert result["source"] == "manual" and result["persisted"] is False
    assert result["scan_status"] == "completed"
    assert result["persistence_status"] == "not_requested"
    assert len(result["run_id"]) == 32 and len(result["content_hash"]) == 64
    assert db.fetch_one("SELECT count(*) AS n FROM anti_ai_audit_runs")["n"] == 0


@pytest.mark.parametrize("content", ["", "   "])
def test_manual_empty_body_is_not_clean_success(audit_client, content):
    client, _db = audit_client
    response = client.post("/api/v1/anti-ai/scan", json={"content": content})
    assert response.status_code == 422
    assert "severity_score" not in response.json()


def test_manual_scanner_failure_is_unavailable(audit_client, monkeypatch):
    client, _db = audit_client
    monkeypatch.setattr(anti_ai, "get_anti_ai_auditor", lambda: (_ for _ in ()).throw(RuntimeError("scanner disconnected")))
    response = client.post("/api/v1/anti-ai/scan", json={"content": "正文"})
    assert response.status_code == 503
    assert "scanner disconnected" in response.json()["detail"]
    assert "severity_score" not in response.json()


def test_manual_default_completed_report_is_not_clean_success(audit_client, monkeypatch):
    from application.audit.services.anti_ai_audit import AntiAIAuditReport
    client, _db = audit_client
    monkeypatch.setattr(anti_ai, "get_anti_ai_auditor", lambda: SimpleNamespace(scan_chapter=lambda *_: AntiAIAuditReport()))
    response = client.post("/api/v1/anti-ai/scan", json={"content": "正文"})
    assert response.status_code == 503 and "severity_score" not in response.json()


def test_stats_only_observe_real_runs_in_requested_novel(audit_client):
    client, db = audit_client
    db.execute("""INSERT INTO anti_ai_audit_runs
        (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
        VALUES ('other-run','other',1,'hash','rules','completed','committed')""")
    db.commit()
    scoped = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert scoped["observed_audit_runs"] == 0
    assert not scoped["layer_states"]["L7_audit"]["observed"]
    assert all(not value["observed"] for value in scoped["layer_states"].values())
    global_stats = client.get("/api/v1/anti-ai/stats").json()
    assert global_stats["observed_audit_runs"] == 1
    assert global_stats["layer_states"]["L7_audit"]["observed"]
    assert all(not value["observed"] for key, value in global_stats["layer_states"].items() if key != "L7_audit")


def test_stats_scanner_disconnection_and_store_failure_are_not_connected(audit_client, monkeypatch):
    client, db = audit_client
    monkeypatch.setattr(anti_ai, "get_anti_ai_auditor", lambda: (_ for _ in ()).throw(RuntimeError("scanner disconnected")))
    stats = client.get("/api/v1/anti-ai/stats").json()
    state = stats["layer_states"]["L7_audit"]
    assert state["failed"] and not state["connected"] and not state["observed"]
    assert state["status"] == "failed"
    db.execute("DROP TABLE anti_ai_audit_runs")
    db.commit()
    stats = client.get("/api/v1/anti-ai/stats").json()
    state = stats["layer_states"]["L7_audit"]
    assert state["status"] == "failed" and not state["connected"]
    assert stats["observed_audit_runs"] == 0 and stats["failed_audit_runs"] == 0


def test_stats_failed_scan_stays_failed_even_with_an_older_success(audit_client):
    client, db = audit_client
    for run, status in [("first", "completed"), ("second", "failed")]:
        db.execute("""INSERT INTO anti_ai_audit_runs
            (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
            VALUES (?,'novel',1,'hash','rules',?,'committed')""", (run, status))
    db.commit()
    stats = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert stats["observed_audit_runs"] == 2 and stats["failed_audit_runs"] == 1
    assert stats["layer_states"]["L7_audit"]["status"] == "failed"


def test_stats_old_failure_count_does_not_override_latest_success(audit_client):
    client, db = audit_client
    for run, status in [("first", "failed"), ("second", "completed")]:
        db.execute("""INSERT INTO anti_ai_audit_runs
            (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
            VALUES (?,'novel',1,'hash','rules',?,'committed')""", (run, status))
    db.commit()
    stats = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert stats["failed_audit_runs"] == 1
    assert stats["layer_states"]["L7_audit"]["status"] == "observed"
    assert stats["latest_audit_run"]["run_id"] == "second"


def test_stream_capability_requires_installed_factory_and_durable_receipts(audit_client, monkeypatch):
    from application.ai_invocation.services import AttemptService
    client, db = audit_client
    initial = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert initial["layer_states"]["L6_token_guard"]["connected"]
    assert not initial["layer_states"]["L6_token_guard"]["observed"]
    for run, novel, status in [("other", "other", "failed"), ("interrupted", "novel", "interrupted"), ("cancelled", "novel", "cancelled")]:
        db.execute("""INSERT INTO anti_ai_stream_runs
            (run_id,novel_id,job_id,status,scan_status,content_hash,rule_version,summary_json)
            VALUES (?,?,?,?,'not_run','hash','rules','{}')""", (run, novel, f"job-{run}", status))
    db.commit()
    scoped = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert scoped["observed_stream_runs"] == 2 and scoped["failed_stream_runs"] == 1
    assert scoped["layer_states"]["L6_token_guard"]["observed"]
    assert scoped["latest_stream_run"]["status"] == "cancelled"
    assert scoped["layer_states"]["L6_token_guard"]["status"] == "observed"
    monkeypatch.setattr(AttemptService, "prose_stream_guard_factory", None)
    disabled = client.get("/api/v1/anti-ai/stats?novel_id=novel").json()
    assert not disabled["layer_states"]["L6_token_guard"]["connected"]
    assert disabled["layer_states"]["L6_token_guard"]["observed"]
    assert disabled["layer_states"]["L6_token_guard"]["status"] == "unavailable"
    rows = client.get("/api/v1/anti-ai/stream-runs/novel").json()
    assert [row["run_id"] for row in rows] == ["cancelled", "interrupted"]
    assert rows[0]["summary"] == {} and rows[0]["job_id"] == "job-cancelled"


def test_unavailable_stream_store_does_not_hide_working_chapter_audit(audit_client):
    client, db = audit_client
    db.execute("DROP TABLE anti_ai_stream_runs")
    db.commit()
    stats = client.get("/api/v1/anti-ai/stats").json()
    assert stats["layer_states"]["L6_token_guard"]["status"] == "failed"
    assert stats["layer_states"]["L7_audit"]["connected"]
    assert not stats["layer_states"]["L7_audit"]["failed"]
    assert client.get("/api/v1/anti-ai/stream-runs/novel").status_code == 503


def test_prompt_hooks_are_observed_only_after_real_provider_use(audit_client, monkeypatch):
    import asyncio
    from application.engine.rules.prose_prompt_hooks import generate_direct_prose
    from application.ai_invocation.services import AttemptService
    from domain.ai.value_objects.prompt import Prompt
    client, db = audit_client
    class Provider:
        async def generate(self,prompt,_config):
            assert 'P1.' in prompt.system and '场景化豁免白名单' in prompt.system
            return SimpleNamespace(content='正文')
    asyncio.run(generate_direct_prose(Provider(),Prompt(system='system',user='正文'),None,db=db,context={'novel_id':'novel','chapter_number':1,'job_id':'prompt-job'}))
    stats=client.get('/api/v1/anti-ai/stats?novel_id=novel').json()
    assert stats['observed_prompt_runs']==1
    assert stats['layer_states']['L1_positive_framing']['observed']
    assert stats['layer_states']['L2_protocol_rules']['observed']
    assert stats['layer_states']['L3_allowlist_scenes']['observed']
    assert stats['layer_states']['L5_context_quota']['observed']
    assert not stats['layer_states']['L4_state_vector']['observed']
    assert stats['layer_states']['L4_state_vector']['status']=='unavailable'
    rows=client.get('/api/v1/anti-ai/prompt-runs/novel').json()
    assert rows[0]['job_id']=='prompt-job' and 'system' not in rows[0]['evidence']
    assert client.get('/api/v1/anti-ai/prompt-runs/other').json()==[]
    monkeypatch.setattr(AttemptService,'prose_prompt_hook_factory',None)
    disabled = client.get('/api/v1/anti-ai/stats?novel_id=novel').json()['layer_states']['L1_positive_framing']
    assert not disabled['connected'] and disabled['observed']
    assert disabled['status'] == 'unavailable'


def test_scan_progress_reconnect_keeps_failed_archival_receipt_when_run_table_unavailable(audit_client):
    from engine.runtime.daemon_host import DaemonHostMixin
    client,db=audit_client
    db.execute('DROP TABLE anti_ai_audit_runs'); db.commit()
    report=DaemonHostMixin()._sync_anti_ai_audit('novel',1,'正文')
    rows=client.get('/api/v1/anti-ai/audit-progress/novel').json()
    assert rows[0]['stage']=='failed' and rows[0]['persistence_status']=='failed'
    recovered=client.get(f'/api/v1/anti-ai/runs/novel/{report.run_id}')
    assert recovered.status_code==200 and recovered.json()['source']=='progress_receipt'
    assert recovered.json()['persistence_status']=='failed'
    assert client.get(f'/api/v1/anti-ai/runs/other/{report.run_id}').status_code==404
    db.execute('DROP TABLE anti_ai_scan_progress'); db.commit()
    assert client.get('/api/v1/anti-ai/audit-progress/novel').status_code==503
    assert client.get(f'/api/v1/anti-ai/runs/novel/{report.run_id}').status_code==503


def test_run_reconnect_query_is_scoped_and_unknown_is_not_archived(audit_client):
    client, db = audit_client
    db.execute("""INSERT INTO anti_ai_audit_runs
        (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
        VALUES ('run','novel',1,'hash','rules','failed','committed')""")
    db.commit()
    response = client.get("/api/v1/anti-ai/runs/novel/run")
    assert response.status_code == 200 and response.json()["scan_status"] == "failed"
    assert client.get("/api/v1/anti-ai/runs/other/run").status_code == 404
    assert client.get("/api/v1/anti-ai/runs/novel/unknown").status_code == 404


def test_trend_is_last_n_chapters_and_exposes_failed_latest_attempt(audit_client):
    from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository
    client, db = audit_client
    repo = SqliteAntiAiAuditRepository(db)
    for chapter in range(1, 5):
        repo.upsert("novel", chapter, 1, 0, 1, 0, float(chapter), "轻微")
        db.execute("""INSERT INTO anti_ai_audit_runs
            (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
            VALUES (?,'novel',?,'summary-hash','rules','completed','committed')""", (f"run-{chapter}", chapter))
    db.execute("""INSERT INTO anti_ai_audit_runs
        (run_id,novel_id,chapter_number,content_hash,rule_version,scan_status,persistence_status)
        VALUES ('failed','novel',4,'newer-hash','rules','failed','committed')""")
    db.commit()
    result = client.get("/api/v1/anti-ai/trend/novel?last_n=2").json()
    assert [point["chapter_number"] for point in result] == [3, 4]
    assert result[1]["latest_scan_status"] == "failed"
    assert result[1]["content_hash"] == "summary-hash" and result[1]["latest_run_id"] == "failed"
