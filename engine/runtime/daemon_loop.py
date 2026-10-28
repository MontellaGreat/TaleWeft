"""Shared command dispatch and lifecycle loop for desktop and CLI daemons."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DaemonLoopHost(Protocol):
    poll_interval: int
    circuit_breaker: Any

    def _write_daemon_heartbeat(self) -> None: ...
    def _get_active_novels(self) -> list: ...
    def _cleanup_stale_stop_signals(self, active_novels: list) -> None: ...
    async def _process_novel(self, novel: Any) -> None: ...


async def _process_novel_with_lease_heartbeat(host, novel, interval_seconds: float = 20.0) -> None:
    """Renew ownership and honor durable stops during long provider calls."""
    task = asyncio.create_task(host._process_novel(novel))
    novel_id = getattr(novel, "novel_id", None)
    novel_id = str(getattr(novel_id, "value", novel_id)) if novel_id is not None else None
    process_control = getattr(host, "_process_pending_control", None)
    heartbeat_interval = max(0.01, interval_seconds)
    next_heartbeat = time.monotonic() + heartbeat_interval
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=min(0.5, heartbeat_interval))
            if done:
                return task.result()
            if novel_id is not None and callable(process_control):
                await asyncio.to_thread(process_control, novel_id)
                from application.engine.services.novel_stop_signal import is_novel_stopped

                if is_novel_stopped(novel_id):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    logger.info("[%s] 停止请求已取消当前工作，等待退出回执", novel_id)
                    return
            if time.monotonic() >= next_heartbeat:
                await asyncio.to_thread(host._write_daemon_heartbeat)
                next_heartbeat = time.monotonic() + heartbeat_interval
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


def run_daemon_loop(
    host: DaemonLoopHost, *, banner: str | None = None,
    stop_event=None, error_sleep_seconds: float | None = None,
    lease_refresh_interval_seconds: float = 20.0,
) -> None:
    """Dispatch durable intent before looking for committed RUNNING novels."""
    from application.engine.services.autopilot_control import (
        process_pending_commands, mark_processing, confirm_quiescent,
    )

    if banner:
        logger.info(banner)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    def wait(seconds):
        if stop_event is not None:
            stop_event.wait(timeout=seconds)
        else:
            time.sleep(seconds)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("守护进程：持久化事件循环已创建")
    loop_count = 0
    try:
        while not stopped():
            loop_count += 1
            loop_start = time.time()
            delay = host.poll_interval
            try:
                host._write_daemon_heartbeat()
                # Durable stops must be acknowledged even while model work is
                # suspended by the breaker.
                process_pending_commands(active_novel_ids=set())
                try:
                    from application.engine.services.streaming_bus import streaming_bus

                    streaming_bus.consume_stop_signals()
                except Exception:
                    logger.debug("停止信号队列暂不可用；保留持久化控制命令", exc_info=True)

                if host.circuit_breaker and host.circuit_breaker.is_open():
                    delay = min(host.circuit_breaker.wait_seconds(), host.poll_interval)
                    logger.warning("熔断器打开，暂停 %.0fs", delay)
                else:
                    active_novels = host._get_active_novels()
                    if active_novels:
                        host._cleanup_stale_stop_signals(active_novels)
                    if loop_count % 10 == 1:
                        logger.info("Loop #%s: 发现 %s 本活跃小说", loop_count, len(active_novels))
                    for novel in active_novels:
                        if stopped():
                            break
                        novel_id = str(getattr(novel.novel_id, "value", novel.novel_id))
                        mark_processing(novel_id)
                        try:
                            loop.run_until_complete(
                                _process_novel_with_lease_heartbeat(
                                    host, novel, lease_refresh_interval_seconds,
                                )
                            )
                        finally:
                            confirm_quiescent(novel_id)
            except Exception:
                logger.exception("Daemon 顶层异常（Loop #%s）", loop_count)
                delay = error_sleep_seconds if error_sleep_seconds is not None else host.poll_interval

            elapsed = time.time() - loop_start
            if elapsed > host.poll_interval * 2:
                logger.warning("Loop #%s 耗时过长: %.2fs", loop_count, elapsed)
            if not stopped():
                wait(delay)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
