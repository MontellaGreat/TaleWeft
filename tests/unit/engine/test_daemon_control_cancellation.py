import asyncio

import pytest

from engine.runtime.daemon_host import DaemonHostMixin


@pytest.mark.asyncio
async def test_stop_cancels_inflight_provider_and_awaits_cleanup(monkeypatch):
    from application.engine.services import novel_stop_signal
    host = DaemonHostMixin()
    cancelled = asyncio.Event()
    entered = asyncio.Event()
    monkeypatch.setattr(host, "_process_pending_control", lambda *_: None)
    monkeypatch.setattr(host, "_write_daemon_heartbeat", lambda: None)
    monkeypatch.setattr(novel_stop_signal, "is_novel_stopped", lambda *_: entered.is_set())

    async def provider():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await asyncio.wait_for(host._call_with_timeout(
        provider(), timeout=30, novel_id="novel", timeout_default={"status": "cancelled"}), timeout=2)
    assert result == {"status": "cancelled"}
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_cancels_operation_before_returning(monkeypatch):
    host = DaemonHostMixin()
    cancelled = asyncio.Event()

    async def provider():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    assert await host._call_with_timeout(provider(), timeout=0.02, timeout_default="timeout") == "timeout"
    assert cancelled.is_set()
