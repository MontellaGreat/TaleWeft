"""Actual ASGI, SQLite and writer-ACK tests; only the model stream is fake."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from application.engine.services.generation_stream_requests import GenerationStreamRequests
from application.engine.services import persistence_queue as queue_module
from application.engine.services.persistence_queue import PersistenceQueue, register_persistence_handlers
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces.api.v1.engine import generation


@pytest.fixture
def request_api(tmp_path, monkeypatch):
    monkeypatch.delenv('PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES', raising=False)
    monkeypatch.delenv('AITEXT_ALLOW_DIRECT_SQLITE_WRITES', raising=False)
    from infrastructure.persistence.database import connection as connection_module
    db = DatabaseConnection(str(tmp_path / 'streams.sqlite'))
    monkeypatch.setattr(connection_module, '_db_instance', db)
    monkeypatch.setattr(connection_module, '_db_instances_by_path', {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, '_persistence_queue', queue)
    register_persistence_handlers()
    queue.start_consumer()
    manager = GenerationStreamRequests(db)
    workflow = SimpleNamespace(generate_chapter_stream=None)
    app = FastAPI()
    app.include_router(generation.router, prefix='/api/v1')
    app.dependency_overrides[generation.get_auto_workflow] = lambda: workflow
    app.dependency_overrides[generation.get_generation_stream_requests] = lambda: manager
    try:
        yield app, manager, workflow
    finally:
        queue.stop_consumer()
        db.close_all(skip_checkpoint=True)
        queue.get_queue().close()
        queue.get_queue().join_thread()


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


@pytest.mark.asyncio
async def test_duplicate_http_requests_share_one_model_run_and_replay_terminal(request_api):
    app, manager, workflow = request_api
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def stream(**kwargs):
        nonlocal calls
        calls += 1
        yield {'type': 'chunk', 'text': '片段'}
        entered.set()
        await release.wait()
        yield {'type': 'done', 'content': '完成正文', 'token_count': 2}

    workflow.generate_chapter_stream = stream
    payload = {'chapter_number': 1, 'outline': 'outline', 'request_id': 'repeat'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        first = asyncio.create_task(client.post('/api/v1/novels/book/generate-chapter-stream', json=payload))
        await entered.wait()
        second = asyncio.create_task(client.post('/api/v1/novels/book/generate-chapter-stream', json=payload))
        release.set()
        one, two = await asyncio.gather(first, second)
        assert calls == 1
        assert events(one) == events(two)
        assert events(one)[-1]['status'] == 'completed'
        assert events(one)[-1]['result']['content'] == '完成正文'
        assert one.headers['x-generation-request-id'] == 'repeat'
        replay = await client.get('/api/v1/novels/book/generation-requests/repeat/events', headers={'Last-Event-ID': 'repeat:2'})
        assert [event['sequence'] for event in events(replay)] == [3, 4]
        assert (await client.post('/api/v1/novels/book/generate-chapter-stream', json={**payload, 'outline': 'different'})).status_code == 409
        assert calls == 1
        assert (await manager.status('book', 'repeat'))['status'] == 'completed'


@pytest.mark.asyncio
async def test_eof_without_done_produces_failed_terminal(request_api):
    app, _manager, workflow = request_api

    async def incomplete(**kwargs):
        yield {'type': 'chunk', 'text': '未完成片段'}

    workflow.generate_chapter_stream = incomplete
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/novels/book/generate-chapter-stream', json={'chapter_number': 1, 'outline': 'x'})
    records = events(response)
    assert any(record['type'] == 'error' for record in records)
    assert records[-1]['type'] == 'terminal'
    assert records[-1]['status'] == 'failed'
    assert not any(record['type'] == 'done' for record in records)


@pytest.mark.asyncio
async def test_cancel_api_stops_model_and_commits_exactly_one_terminal(request_api):
    app, manager, workflow = request_api
    entered, exited = asyncio.Event(), asyncio.Event()

    async def pending(**kwargs):
        try:
            yield {'type': 'chunk', 'text': '已生成片段'}
            entered.set()
            await asyncio.Event().wait()
        finally:
            exited.set()

    workflow.generate_chapter_stream = pending
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        generating = asyncio.create_task(client.post('/api/v1/novels/book/generate-chapter-stream', json={'chapter_number': 1, 'outline': 'x', 'request_id': 'cancel'}))
        await entered.wait()
        cancelled = await client.post('/api/v1/novels/book/generation-requests/cancel/cancel')
        assert cancelled.json()['status'] == 'cancelled'
        response = await generating
        assert exited.is_set()
        assert [record['status'] for record in events(response) if record['type'] == 'terminal'] == ['cancelled']
        assert '已生成片段' in response.text
        assert (await manager.status('book', 'cancel'))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_cancellation_before_initial_post_creates_no_model_work(request_api):
    app, _manager, workflow = request_api
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield {'type': 'done', 'content': 'should never happen'}

    workflow.generate_chapter_stream = stream
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/api/v1/novels/book/generation-requests/notyet/cancel')).json()['status'] == 'cancelled'
        response = await client.post('/api/v1/novels/book/generate-chapter-stream', json={'chapter_number': 1, 'outline': 'x', 'request_id': 'notyet'})
    assert calls == []
    assert events(response)[-1]['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_detaching_subscriber_does_not_cancel_or_restart_generation(request_api):
    _app, manager, _workflow = request_api
    release = asyncio.Event()
    calls = []

    async def stream():
        calls.append(1)
        yield {'type': 'chunk', 'text': 'first'}
        await release.wait()
        yield {'type': 'done', 'content': 'all'}

    request_id = await manager.start('book', 1, {'outline': 'x'}, stream, 'detach')
    subscriber = manager.events('book', request_id)
    first = await anext(subscriber)
    await subscriber.aclose()
    release.set()
    await manager.tasks[request_id]
    reattached = [event async for event in manager.events('book', request_id, first['sequence'])]
    assert calls == [1]
    assert reattached[-1]['status'] == 'completed'


@pytest.mark.asyncio
async def test_restart_marks_orphaned_requests_interrupted_and_never_restarts_model(request_api):
    _app, manager, _workflow = request_api
    manager.db.execute("INSERT INTO generation_stream_requests(request_id,novel_id,chapter_number,payload_hash,owner_id) VALUES ('lost','book',1,'hash','old-process')")
    manager.db.commit()
    manager._append('lost', {'type': 'chunk', 'text': '保留片段'})
    restarted = GenerationStreamRequests(manager.db)
    await restarted.recover_interrupted()
    records = [event async for event in restarted.events('book', 'lost')]
    assert records[0]['text'] == '保留片段'
    assert records[-1]['status'] == 'interrupted'
    assert restarted.tasks == {}
