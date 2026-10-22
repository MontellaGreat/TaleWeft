import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from engine.infrastructure.persistence.trace_store import SqliteTraceStore
from infrastructure.ai.trace_privacy import TracePrivacySettings


class _Pool:
    def __init__(self, path: Path):
        self.path = path

    def get_connection(self):
        return sqlite3.connect(self.path)


def _recent_timestamp(offset_seconds: int = 0) -> str:
    """Use a timestamp inside the default retention window.

    The trace store prunes on every write.  A fixed fixture date eventually
    becomes older than the 14-day default and makes an otherwise valid timeline
    disappear as the calendar moves forward.
    """
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat(
        timespec="milliseconds"
    )


def test_ai_trace_store_records_timeline(tmp_path):
    store = SqliteTraceStore(_Pool(tmp_path / "trace.db"))

    store.record_ai_span(
        {
            "trace_id": "trace-1",
            "span_id": "span-1",
            "novel_id": "novel-1",
            "operation": "chapter_generation",
            "phase": "prompt_rendered",
            "contract_key": "chapter-generation-main",
            "variables_preview": {"novel_id": "novel-1"},
            "variables_full": {"novel_id": "novel-1", "chapter_content": "完整正文"},
            "variable_sources": [{"name": "novel_id", "source": "prompt:chapter-generation-main"}],
            "prompt_preview": {"user": "写第一章"},
            "prompt_full": {"system": "系统全文", "user": "用户全文"},
            "metadata": {"fallback_used": False},
            "created_at": _recent_timestamp(),
        }
    )
    store.record_ai_span(
        {
            "trace_id": "trace-1",
            "span_id": "span-2",
            "parent_span_id": "span-1",
            "novel_id": "novel-1",
            "operation": "chapter_generation",
            "phase": "llm_response",
            "response_preview": "预览输出",
            "response_full": "完整输出全文",
            "token_input": 10,
            "token_output": 20,
            "created_at": _recent_timestamp(1),
        }
    )

    summaries = store.list_ai_traces("novel-1")
    assert summaries[0]["trace_id"] == "trace-1"
    assert summaries[0]["span_count"] == 2

    timeline = store.get_ai_timeline("trace-1", "novel-1")
    assert [item["phase"] for item in timeline] == ["prompt_rendered", "llm_response"]
    # Default privacy keeps bounded previews and structural provenance usable
    # for diagnosis, while dropping all full payload columns.
    assert timeline[0]["prompt_preview"] == {"user": "写第一章"}
    assert timeline[0]["variables_preview"] == {"novel_id": "novel-1"}
    assert timeline[0]["variables_full"] is None
    assert timeline[0]["variable_sources"][0]["source"] == "prompt:chapter-generation-main"
    assert timeline[0]["prompt_full"] is None
    assert timeline[1]["response_preview"] == "预览输出"
    assert timeline[1]["response_full"] is None


def test_ai_trace_store_explicit_fulltext_opt_in_preserves_payload(tmp_path):
    store = SqliteTraceStore(_Pool(tmp_path / "trace-full.db"))
    store.save_privacy_settings(
        TracePrivacySettings(enabled=True, capture_fulltext=True, retention_days=14, max_spans=100)
    )

    store.record_ai_span(
        {
            "trace_id": "trace-full",
            "span_id": "span-full",
            "novel_id": "novel-1",
            "operation": "chapter_generation",
            "phase": "llm_response",
            "prompt_preview": {"user": "短预览"},
            "prompt_full": {"system": "系统全文", "user": "用户全文"},
            "response_preview": "短输出",
            "response_full": "完整输出全文",
            "metadata": {"api_key": "must-never-persist"},
            "created_at": _recent_timestamp(),
        }
    )

    timeline = store.get_ai_timeline("trace-full", "novel-1")
    assert len(timeline) == 1
    assert timeline[0]["prompt_full"] == {"system": "系统全文", "user": "用户全文"}
    assert timeline[0]["response_full"] == "完整输出全文"
    assert timeline[0]["prompt_preview"] == {"user": "短预览"}
    assert timeline[0]["metadata"]["api_key"] == "[redacted]"


def test_ai_trace_store_default_privacy_bounds_and_redacts_preview(tmp_path):
    store = SqliteTraceStore(_Pool(tmp_path / "trace-preview.db"))
    long_text = "正文" * 500

    store.record_ai_span(
        {
            "trace_id": "trace-preview",
            "span_id": "span-preview",
            "novel_id": "novel-1",
            "operation": "chapter_generation",
            "phase": "prompt_rendered",
            "variables_preview": {
                "title": long_text,
                "chapter_content": "不能进入预览的正文",
                "api_key": "secret",
                "nested": {"level1": {"level2": {"level3": {"api_key": "deep-secret"}}}},
                "deep_content": {"level1": {"level2": {"chapter_content": "deep-private-body"}}},
                "tuple_value": ({"level1": {"level2": {"token": "deep-tuple-secret"}}},),
            },
            "prompt_preview": {"user": long_text},
            "response_preview": long_text,
            "variables_full": {"title": long_text},
            "metadata": {"prompt": "metadata content", "token": "secret"},
            "created_at": _recent_timestamp(),
        }
    )

    timeline = store.get_ai_timeline("trace-preview", "novel-1")
    assert len(timeline) == 1
    span = timeline[0]
    assert span["variables_full"] is None
    assert span["variables_preview"]["chapter_content"] == "[已脱敏]"
    assert span["variables_preview"]["api_key"] == "[redacted]"
    assert "deep-secret" not in str(span["variables_preview"])
    assert "deep-private-body" not in str(span["variables_preview"])
    assert "deep-tuple-secret" not in str(span["variables_preview"])
    # ``preview_text`` includes a small total-length suffix in addition to
    # the 320-character body limit.
    assert len(span["variables_preview"]["title"]) <= 340
    assert len(span["prompt_preview"]["user"]) <= 340
    assert len(span["response_preview"]) <= 340
    assert span["metadata"]["prompt"] == "[content omitted]"
    assert span["metadata"]["token"] == "[redacted]"


def test_ai_trace_store_preview_formatter_failure_never_persists_unbounded_body(tmp_path, monkeypatch):
    from application.ai import trace_context

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("preview formatter unavailable")

    monkeypatch.setattr(trace_context, "preview_value", unavailable)
    store = SqliteTraceStore(_Pool(tmp_path / "trace-unavailable.db"))
    body = "private-body" * 1000
    store.record_ai_span({
        "trace_id": "trace-unavailable", "span_id": "span", "novel_id": "novel-1",
        "response_preview": body, "response_full": body,
    })

    timeline = store.get_ai_timeline("trace-unavailable", "novel-1")
    assert len(timeline) == 1
    assert timeline[0]["response_preview"] == "[preview unavailable]"
    assert timeline[0]["response_full"] is None
    assert "private-body" not in str(timeline)


def test_ai_trace_store_auto_migrates_new_fulltext_columns(tmp_path):
    db_path = tmp_path / "trace_migrate.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE ai_trace_spans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            novel_id TEXT NOT NULL DEFAULT '',
            operation TEXT NOT NULL DEFAULT 'ai_call',
            phase TEXT NOT NULL,
            node_id TEXT,
            node_type TEXT,
            contract_key TEXT,
            contract_version TEXT,
            source TEXT,
            model TEXT,
            generation_profile TEXT,
            variables_hash TEXT,
            variables_preview TEXT,
            prompt_hash TEXT,
            prompt_preview TEXT,
            response_hash TEXT,
            response_preview TEXT,
            token_input INTEGER,
            token_output INTEGER,
            latency_ms INTEGER,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    store = SqliteTraceStore(_Pool(db_path))
    timeline_columns = {
        row[1]
        for row in sqlite3.connect(db_path).execute("PRAGMA table_info(ai_trace_spans)").fetchall()
    }
    assert "variables_full" in timeline_columns
    assert "variable_sources" in timeline_columns
    assert "prompt_full" in timeline_columns
    assert "response_full" in timeline_columns
