import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.engine.services.query_service import _merge_runtime_fields_from_raw
from interfaces.api.v1.engine import autopilot_routes as routes


def snapshot(status):
    return {
        "pipeline_status": status, "pipeline_passed": False,
        "pipeline_success": status != "failed", "pipeline_complete": True,
        "pipeline_step_status": {"run_post_commit": status},
        "pipeline_step_results": {"run_post_commit": {
            "status": status, "passed": False, "skip": False, "message": "store unavailable"}},
        "pipeline_error": "rejected" if status == "failed" else "",
        "validation_status": "passed", "derived_status": "retryable" if status == "unavailable" else status,
    }


@pytest.mark.parametrize("status", ["failed", "unavailable", "deferred"])
def test_status_and_query_service_preserve_result_evidence(status):
    raw = snapshot(status)
    raw["current_stage"] = "writing"
    api = routes._build_status_pure_memory("result-test", raw)
    query = _merge_runtime_fields_from_raw({"novel_id": "result-test", "current_stage": "writing"}, raw)
    for response in (api, query):
        assert response["pipeline_status"] == status
        assert response["pipeline_passed"] is False
        assert response["pipeline_step_results"] == raw["pipeline_step_results"]
        assert response["pipeline_step_status"] == raw["pipeline_step_status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,autopilot,event_type", [
    ("unavailable", "running", "progress"), ("deferred", "running", "progress"),
    ("failed", "error", "autopilot_complete"),
])
async def test_real_sse_generator_exposes_degraded_and_rejected_results(monkeypatch, status, autopilot, event_type):
    raw = snapshot(status)
    raw["_cached_current_chapter_number"] = 1
    novel = SimpleNamespace(
        current_stage=SimpleNamespace(value="writing"), autopilot_status=SimpleNamespace(value=autopilot),
        current_beat_index=0, current_act=0, target_chapters=1, consecutive_error_count=0,
    )
    monkeypatch.setattr(routes, "get_novel_repository", lambda: object())
    monkeypatch.setattr(routes, "get_chapter_repository", lambda: object())
    monkeypatch.setattr(routes, "install_autopilot_log_ring_handler", lambda: None)
    monkeypatch.setattr(routes, "_log_stream_boot_meta_sync", lambda *_: {})
    monkeypatch.setattr(routes, "_log_stream_replay_sync", lambda *_: ([], 0))
    monkeypatch.setattr(routes, "_log_stream_file_cursor_init_sync", lambda *_: 0)
    monkeypatch.setattr(routes, "_log_stream_io_tick_sync", lambda *_: (novel, None, [], 0, [], []))
    monkeypatch.setattr(routes, "_get_shared_state_for_novel_cached", lambda *_: raw)
    monkeypatch.setattr(routes, "_is_client_disconnected", AsyncMock(return_value=False))
    response = await routes.autopilot_log_stream("result-test", after_seq=0)
    try:
        async for frame in response.body_iterator:
            event = json.loads(frame.removeprefix("data: ").strip())
            if event["type"] == event_type:
                meta = event["metadata"]
                assert meta["pipeline_status"] == status
                assert meta["pipeline_passed"] is False
                assert meta["pipeline_step_results"] == raw["pipeline_step_results"]
                break
    finally:
        await response.body_iterator.aclose()
