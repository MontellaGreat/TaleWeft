"""StoryPipeline 写作委托测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import NovelStage
from engine.runtime.writing_delegate import (
    WritingOutcome,
    get_story_pipeline_mode,
    is_story_pipeline_writing_enabled,
    _build_runner,
    run_writing,
    run_story_pipeline_writing,
    story_pipeline_mode_was_unset,
)
from engine.pipeline.context import PipelineResult


def test_is_story_pipeline_writing_enabled_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert is_story_pipeline_writing_enabled() is True


def test_is_story_pipeline_writing_enabled_explicit_off(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "off")
    assert is_story_pipeline_writing_enabled() is False


def test_get_story_pipeline_mode_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert get_story_pipeline_mode() == "writing"
    assert story_pipeline_mode_was_unset() is True


def test_is_story_pipeline_writing_enabled_on(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "1")
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_full(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "full")
    assert get_story_pipeline_mode() == "full"
    assert story_pipeline_mode_was_unset() is False
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_unknown_warns_and_defaults(monkeypatch, caplog):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "surprise")

    assert get_story_pipeline_mode() == "writing"
    assert "未知 PLOTPILOT_USE_STORY_PIPELINE" in caplog.text


def test_build_runner_uses_the_daemon_that_owns_execution_identity():
    daemon = MagicMock()
    runner = MagicMock()

    with patch("engine.runtime.runner.StoryPipelineRunner", return_value=runner):
        assert _build_runner(daemon) is runner

    runner.attach_legacy_daemon.assert_called_once_with(daemon)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_success_updates_novel():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = "wuxia"
    novel.target_words_per_chapter = 3000
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.current_auto_chapters = 2
    novel.current_chapter_in_act = 1
    novel.current_beat_index = 3
    novel.beats_completed = True

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.chapter_number = 5
    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = mock_ctx
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=True,
            chapter_number=5,
            word_count=2800,
            tension=72,
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_auto_chapters == 3
    assert novel.current_chapter_in_act == 2
    assert novel.current_beat_index == 0
    assert novel.beats_completed is False
    assert novel.last_chapter_tension == 72
    assert novel.current_stage == NovelStage.AUDITING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_transitions_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 1
    novel.current_chapter_in_act = 5

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=True)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 2
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_without_full_act_replans_same_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 0
    novel.current_chapter_in_act = 3

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=False)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "opening"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 0
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_failure_returns_outcome_without_incrementing_counter():
    novel = MagicMock()
    novel.novel_id.value = "novel-failure"
    novel.genre = "wuxia"
    novel.target_words_per_chapter = 3000
    novel.consecutive_error_count = 2

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_ctx = MagicMock(chapter_number=5)
    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = mock_ctx
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="writer unavailable")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        outcome = await run_story_pipeline_writing(daemon, novel)

    assert outcome == WritingOutcome("failure", "writer unavailable")
    assert novel.consecutive_error_count == 2


@pytest.mark.asyncio
async def test_run_writing_dispatches_legacy_when_pipeline_disabled():
    host = MagicMock()
    host.use_story_pipeline_for_writing = False
    novel = MagicMock()

    with patch("engine.runtime.legacy_writing_delegate.run_legacy_writing", new=AsyncMock()) as legacy:
        await run_writing(host, novel)

    legacy.assert_awaited_once_with(host, novel)
