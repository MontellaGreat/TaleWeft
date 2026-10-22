"""Durable, replayable manual chapter generation; disconnect never starts a duplicate."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

logger = logging.getLogger(__name__)


class GenerationRequestConflict(ValueError):
    pass


class GenerationRequestNotFound(LookupError):
    pass


class GenerationStreamRequests:
    def __init__(self, db):
        self.db = db
        self.owner_id = uuid.uuid4().hex
        self.tasks = {}

    def _read(self, request_id, novel_id):
        row = self.db.fetch_one("SELECT * FROM generation_stream_requests WHERE request_id=? AND novel_id=?", (request_id, novel_id))
        if row is None:
            raise GenerationRequestNotFound("生成请求不存在")
        return row

    def _append(self, request_id, event, terminal_status=None):
        body = json.dumps(event, ensure_ascii=False)
        with self.db.transaction() as conn:
            if terminal_status:
                updated = conn.execute("UPDATE generation_stream_requests SET last_sequence=last_sequence+1,status=?,terminal_json=?,updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='running'",
                                       (terminal_status, body, request_id))
            else:
                updated = conn.execute("UPDATE generation_stream_requests SET last_sequence=last_sequence+1,updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='running'", (request_id,))
            conn.execute("INSERT INTO generation_stream_events(request_id,sequence,event_json) SELECT request_id,last_sequence,? FROM generation_stream_requests WHERE request_id=? AND changes()=1", (body, request_id))
        return updated.rowcount == 1

    async def start(self, novel_id, chapter_number, payload, producer, request_id=None, *, terminal_event='done'):
        request_id = request_id or uuid.uuid4().hex
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

        def claim():
            cursor = self.db.execute("INSERT INTO generation_stream_requests(request_id,novel_id,chapter_number,payload_hash,owner_id) VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
                                     (request_id, novel_id, chapter_number, fingerprint, self.owner_id))
            self.db.commit()
            return cursor.rowcount == 1

        created = await asyncio.to_thread(claim)
        row = await asyncio.to_thread(self._read, request_id, novel_id)
        if row['status'] == 'cancelled' and row['payload_hash'] == '':
            return request_id
        if row["payload_hash"] != fingerprint or row["chapter_number"] != chapter_number:
            raise GenerationRequestConflict("同一 request_id 已用于不同生成参数，请创建新请求")
        if created:
            task = asyncio.create_task(self._produce(request_id, producer, terminal_event), name=f"chapter-generation:{request_id}")
            self.tasks[request_id] = task
            task.add_done_callback(lambda done: self.tasks.pop(request_id, None) if self.tasks.get(request_id) is done else None)
        elif row["status"] == "running" and row["owner_id"] != self.owner_id:
            # A restarted local API cannot resume the model call itself. It can
            # replay committed chunks and explicitly expose interruption.
            await asyncio.to_thread(self._append, request_id,
                {"type": "terminal", "status": "interrupted", "message": "服务已重启；保留已有生成片段，请明确发起新的生成请求"}, "interrupted")
        return request_id

    async def _produce(self, request_id, producer, terminal_event):
        stream = None
        try:
            await asyncio.to_thread(self._append, request_id, {"type": "request_started"})
            stream = producer()
            async for event in stream:
                if not isinstance(event, dict):
                    raise ValueError("生成工作流返回无效事件")
                if not await asyncio.to_thread(self._append, request_id, event):
                    return
                terminal_status = {terminal_event: "completed", "error": "failed", "approval_required": "awaiting_review"}.get(event.get("type"))
                if terminal_status:
                    terminal = {"type": "terminal", "status": terminal_status}
                    if terminal_status == 'completed':
                        terminal['result'] = event
                    await asyncio.to_thread(self._append, request_id, terminal, terminal_status)
                    return
            await asyncio.to_thread(self._append, request_id, {"type": "error", "message": "生成流结束但未返回完成事件，已有片段已保留"})
            await asyncio.to_thread(self._append, request_id, {"type": "terminal", "status": "failed"}, "failed")
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self._append, request_id, {"type": "terminal", "status": "cancelled"}, "cancelled"))
            raise
        except Exception:
            logger.exception("章节生成请求失败 request=%s", request_id)
            # Detailed provider errors belong in protected logs, not SSE bodies.
            await asyncio.to_thread(self._append, request_id, {"type": "error", "message": "章节生成失败，已有片段已保留"})
            await asyncio.to_thread(self._append, request_id, {"type": "terminal", "status": "failed"}, "failed")
        finally:
            if stream is not None and hasattr(stream, "aclose"):
                try:
                    await stream.aclose()
                except Exception:
                    logger.debug("生成流关闭失败 request=%s", request_id, exc_info=True)

    async def status(self, novel_id, request_id):
        row = await asyncio.to_thread(self._read, request_id, novel_id)
        if row["status"] == "running" and row["owner_id"] != self.owner_id:
            await asyncio.to_thread(self._append, request_id,
                {"type": "terminal", "status": "interrupted", "message": "服务已重启，已有片段已保留"}, "interrupted")
            row = await asyncio.to_thread(self._read, request_id, novel_id)
        return {"request_id": request_id, "chapter_number": row["chapter_number"], "status": row["status"], "last_sequence": row["last_sequence"]}

    async def recover_interrupted(self):
        rows = await asyncio.to_thread(self.db.fetch_all,
            "SELECT request_id FROM generation_stream_requests WHERE status='running' AND owner_id<>?", (self.owner_id,))
        for row in rows:
            await asyncio.to_thread(self._append, row["request_id"],
                {"type": "terminal", "status": "interrupted", "message": "服务已重启，已有片段已保留"}, "interrupted")

    async def events(self, novel_id, request_id, after=0):
        await self.status(novel_id, request_id)
        row = await asyncio.to_thread(self._read, request_id, novel_id)
        if after > row["last_sequence"]:
            raise GenerationRequestConflict("恢复游标超出已提交事件范围")
        while True:
            rows = await asyncio.to_thread(self.db.fetch_all,
                "SELECT sequence,event_json FROM generation_stream_events WHERE request_id=? AND sequence>? ORDER BY sequence LIMIT 256", (request_id, after))
            for saved in rows:
                event = json.loads(saved["event_json"])
                event.update(request_id=request_id, sequence=saved["sequence"])
                after = saved["sequence"]
                yield event
                if event.get("type") == "terminal":
                    return
            row = await asyncio.to_thread(self._read, request_id, novel_id)
            if row["status"] != "running" and after >= row["last_sequence"]:
                terminal = json.loads(row["terminal_json"] or '{}')
                terminal.update(type="terminal", status=row["status"], request_id=request_id, sequence=row["last_sequence"])
                yield terminal
                return
            if not rows:
                # Event polling is intentionally bounded; no model work occurs
                # in this subscription task, so disconnect only detaches it.
                await asyncio.sleep(0.1)

    async def cancel(self, novel_id, request_id):
        try:
            row = await asyncio.to_thread(self._read, request_id, novel_id)
        except GenerationRequestNotFound:
            # Cancellation can reach the API before the initial POST is claimed.
            # A tombstone prevents that delayed POST from starting model work.
            def cancel_before_start():
                self.db.execute("INSERT INTO generation_stream_requests(request_id,novel_id,chapter_number,payload_hash,owner_id) VALUES (?,?,0,'',?) ON CONFLICT(request_id) DO NOTHING", (request_id, novel_id, self.owner_id))
                self.db.commit()
            await asyncio.to_thread(cancel_before_start)
            row = await asyncio.to_thread(self._read, request_id, novel_id)
        if row["status"] == "running":
            await asyncio.to_thread(self._append, request_id, {"type": "terminal", "status": "cancelled"}, "cancelled")
            task = self.tasks.get(request_id)
            if task and not task.done():
                if task.get_loop() is asyncio.get_running_loop():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    task.get_loop().call_soon_threadsafe(task.cancel)
        return await self.status(novel_id, request_id)

    async def shutdown(self):
        for request_id, task in list(self.tasks.items()):
            await asyncio.to_thread(self._append, request_id, {"type": "terminal", "status": "interrupted", "message": "服务关闭，已有片段已保留"}, "interrupted")
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
