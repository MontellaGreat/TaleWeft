from types import SimpleNamespace

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult


class Pipeline(BaseStoryPipeline):
    def __init__(self, governance=None):
        super().__init__()
        self.calls = []
        self.governance = governance

    async def _step_find_next_chapter(self, ctx):
        return StepResult.ok()

    async def _step_prepare_governance(self, ctx):
        return self.governance or StepResult.ok()

    async def _step_prepare_chapter_plan(self, ctx):
        self.calls.append("plan")
        return StepResult.ok()

    async def _step_build_context(self, ctx):
        return StepResult.ok()

    async def _step_generate(self, ctx):
        self.calls.append("generate")
        ctx.chapter_content = "generated body"
        return StepResult.ok()

    async def _step_save_chapter(self, ctx):
        self.calls.append("save")
        return StepResult.ok()

    async def _step_validate_voice(self, ctx):
        return StepResult.skip_step()

    async def _step_run_post_commit(self, ctx):
        return StepResult.skip_step()

    async def _step_score_tension(self, ctx):
        return StepResult.skip_step()

    async def _step_finalize(self, ctx):
        return StepResult.ok()


@pytest.mark.asyncio
async def test_continuity_rejection_never_plans_generates_or_saves():
    pipeline = Pipeline(StepResult.fail("continuity rejected"))
    result = await pipeline.run_chapter(PipelineContext(novel_id="isolated", chapter_number=1))
    assert not result.success
    assert result.step_status["prepare_governance"] == "failed"
    assert pipeline.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,success,saved", [("advise", True, True), ("enforce", False, False)])
async def test_missing_validator_is_unavailable_and_enforce_blocks_save(mode, success, saved):
    pipeline = Pipeline()
    ctx = PipelineContext(novel_id="isolated", chapter_number=1, metadata={"quality_policy": mode})
    result = await pipeline.run_chapter(ctx)
    assert result.success == success
    assert ("save" in pipeline.calls) == saved
    assert result.validation_status == "unavailable"
    assert not result.validation_passed
    assert result.validation_score == 0


@pytest.mark.asyncio
async def test_old_completed_chapter_cannot_ack_new_body(monkeypatch):
    pipeline = BaseStoryPipeline()
    ctx = PipelineContext(novel_id="isolated", chapter_number=1, chapter_content="new body")
    ctx.chapter_repository = SimpleNamespace(get_by_novel_and_number=lambda *_: SimpleNamespace(
        status="completed", content="old body"))
    discarded = []
    monkeypatch.setattr(pipeline, "_push_persistence_command", lambda *_: True)
    monkeypatch.setattr(pipeline, "_discard_generation_workspace", lambda *_: discarded.append(True))
    result = await pipeline._step_save_chapter(ctx)
    assert not result.passed
    assert not ctx.chapter_saved
    assert discarded == []


@pytest.mark.asyncio
async def test_commit_timeout_keeps_preview_without_fallback(monkeypatch):
    pipeline = BaseStoryPipeline()
    ctx = PipelineContext(novel_id="isolated", chapter_number=1, chapter_content="new body")
    ctx.chapter_repository = object()
    called = []

    def timed_out(*_):
        raise TimeoutError("commit unknown")

    async def fallback(*_):
        called.append("fallback")

    monkeypatch.setattr(pipeline, "_push_persistence_command", timed_out)
    monkeypatch.setattr(pipeline, "_save_chapter_via_repository", fallback)
    monkeypatch.setattr(pipeline, "_discard_generation_workspace", lambda *_: called.append("discard"))
    result = await pipeline._step_save_chapter(ctx)
    assert not result.passed
    assert not ctx.chapter_saved
    assert called == []


@pytest.mark.asyncio
async def test_post_commit_failure_is_retryable_without_failing_body_pipeline():
    pipeline = BaseStoryPipeline()

    class FailedAftermath:
        async def run_after_chapter_saved(self, *_args, **_kwargs):
            return {
                "narrative_sync_status": "failed",
                "narrative_sync_error": "knowledge store unavailable",
            }

    ctx = PipelineContext(
        novel_id="isolated",
        chapter_number=1,
        chapter_content="正文已经提交",
        word_count=6,
        aftermath_pipeline=FailedAftermath(),
    )
    result = await pipeline._step_run_post_commit(ctx)

    assert result.status == "unavailable"
    assert not result.passed
    assert ctx.derived_status == "retryable"
    assert "knowledge store unavailable" in (ctx.derived_error or "")


@pytest.mark.asyncio
async def test_post_commit_auxiliary_deferred_is_explicit():
    pipeline = BaseStoryPipeline()

    class DeferredAftermath:
        async def run_after_chapter_saved(self, *_args, **_kwargs):
            return {"auxiliary_deferred": True, "narrative_sync_ok": True}

    ctx = PipelineContext(
        novel_id="isolated",
        chapter_number=1,
        chapter_content="正文已经提交",
        word_count=6,
        aftermath_pipeline=DeferredAftermath(),
    )
    result = await pipeline._step_run_post_commit(ctx)

    assert result.status == "deferred"
    assert not result.passed
    assert ctx.derived_status == "deferred"
