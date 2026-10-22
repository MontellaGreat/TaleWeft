import sqlite3
from contextlib import contextmanager

import pytest

from application.core.config.config_loader import reload_config
from application.engine.services.persistence_queue_v2 import (
    PersistentQueueV2,
    get_persistent_queue_settings,
)


class SingleConnectionPool:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def get_connection(self):
        yield self.conn

    def close(self):
        self.conn.close()


def test_persistent_queue_v2_uses_performance_config(tmp_path):
    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        """
persistence_queue:
  batch_size: 2
  cleanup_days: 3
  max_retries: 4
  zombie_timeout_minutes: 9
  queue_bloat_warning: 123
  cleanup_every_processed: 7
  consumer_idle_sleep_seconds: 0.25
  consumer_error_sleep_seconds: 0.75
  consumer_stop_timeout_seconds: 1.5
  legacy_lock_max_retries: 6
  legacy_lock_backoff_base_seconds: 0.1
  legacy_lock_backoff_max_seconds: 0.9
  legacy_drain_timeout_seconds: 2.5
  legacy_idle_wait_sleep_seconds: 0.02
  legacy_idle_stable_checks: 4
""",
        encoding="utf-8",
    )

    try:
        reload_config(str(config_path))

        settings = get_persistent_queue_settings()
        assert settings.batch_size == 2
        assert settings.max_retries == 4
        assert settings.zombie_timeout_minutes == 9
        assert settings.cleanup_every_processed == 7
        assert settings.consumer_stop_timeout_seconds == 1.5
        assert settings.legacy_lock_max_retries == 6
        assert settings.legacy_lock_backoff_base_seconds == 0.1
        assert settings.legacy_lock_backoff_max_seconds == 0.9
        assert settings.legacy_drain_timeout_seconds == 2.5
        assert settings.legacy_idle_wait_sleep_seconds == 0.02
        assert settings.legacy_idle_stable_checks == 4

        pool = SingleConnectionPool(tmp_path / "queue.db")
        try:
            queue = PersistentQueueV2(pool)
            queue.push("cmd", {"n": 1})
            queue.push("cmd", {"n": 2})
            queue.push("cmd", {"n": 3})

            commands = queue.pop()
            assert len(commands) == 2
            assert [cmd.payload["n"] for cmd in commands] == [1, 2]

            row = pool.conn.execute(
                "SELECT max_retries FROM persistence_queue WHERE id = ?",
                (commands[0].command_id,),
            ).fetchone()
            assert row["max_retries"] == 4
        finally:
            pool.close()
    finally:
        reload_config()


def test_persistent_queue_v2_idempotency_and_durable_receipt(tmp_path):
    pool = SingleConnectionPool(tmp_path / "receipt.db")
    try:
        queue = PersistentQueueV2(pool)
        first = queue.push("receipt", {"value": 7}, idempotency_key="job-7")
        duplicate = queue.push("receipt", {"value": 7}, idempotency_key="job-7")
        assert duplicate == first
        with pytest.raises(ValueError, match="幂等键"):
            queue.push("receipt", {"value": 8}, idempotency_key="job-7")

        calls = []
        queue.register_handler("receipt", lambda payload: calls.append(payload) or {"ok": True, "n": payload["value"]})
        command = queue.pop(batch_size=1)[0]
        queue._process_command(command)

        receipt = queue.get_command(first)
        assert receipt["status"] == "completed"
        assert receipt["result"] == {"ok": True, "n": 7}
        assert receipt["commit_lsn"] == 1
        assert calls == [{"value": 7}]

        # A new queue instance can reconcile the same command after a process restart.
        restarted = PersistentQueueV2(pool)
        assert restarted.wait_for_completion(first, timeout=0) == receipt
    finally:
        pool.close()


def test_persistent_queue_v2_retry_and_terminal_error_are_queryable(tmp_path):
    pool = SingleConnectionPool(tmp_path / "failure.db")
    try:
        queue = PersistentQueueV2(pool)
        command_id = queue.push("broken", {}, max_retries=1, idempotency_key="broken-1")
        queue.register_handler("broken", lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")))

        command = queue.pop(batch_size=1)[0]
        queue._process_command(command)
        pending = queue.get_command(command_id)
        assert pending["status"] == "pending"
        assert pending["retry_count"] == 1
        assert pending["error_message"] == "RuntimeError: boom"

        command = queue.pop(batch_size=1)[0]
        queue._process_command(command)
        failed = queue.get_command(command_id)
        assert failed["status"] == "failed"
        assert failed["commit_lsn"] == 1
        assert "RuntimeError: boom" == failed["error_message"]
    finally:
        pool.close()
