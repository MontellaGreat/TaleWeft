from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext, PipelineResult
from engine.pipeline.steps import StepResult
from engine.runtime.policy_validator import PolicyReport
from engine.runtime.writing_delegate import run_story_pipeline_writing


class ResultPipeline(BaseStoryPipeline):
    def __init__(self, governance=None, aftermath=None, voice=None):
        super().__init__()
        self.calls = []
        self.governance = governance or StepResult.ok()
        self.aftermath = aftermath or StepResult.ok()
        self.voice = voice or StepResult.skip_step("voice disabled")

    async def _step_find_next_chapter(self, ctx):
        return StepResult.ok()

    async def _step_prepare_governance(self, ctx):
        return self.governance

    async def _step_prepare_chapter_plan(self, ctx):
        self.calls.append("plan")
        return StepResult.ok()

    async def _step_build_context(self, ctx):
        return StepResult.ok()

    async def _step_generate(self, ctx):
        self.calls.append("generate")
        ctx.chapter_content = "new body"
        return StepResult.ok()

    async def _step_save_chapter(self, ctx):
        self.calls.append("save")
        ctx.chapter_saved = True
        return StepResult.ok()

    async def _step_validate_voice(self, ctx):
        return self.voice

    async def _step_run_post_commit(self, ctx):
        return self.aftermath

    async def _step_score_tension(self, ctx):
        return StepResult.skip_step("tension disabled")


def passing_validator():
    return SimpleNamespace(advise=lambda **_: PolicyReport(passed=True, overall_score=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["advise", "enforce"])
async def test_quality_rejection_blocks_save_in_every_policy(policy):
    pipeline = ResultPipeline()
    ctx = PipelineContext(novel_id="rejected", metadata={"quality_policy": policy})
    ctx.policy_validator = SimpleNamespace(advise=lambda **_: PolicyReport(passed=False, overall_score=0.1))
    result = await pipeline.run_chapter(ctx)
    assert result.status == "failed"
    assert not result.success and not result.passed
    assert "save" not in pipeline.calls
    assert result.step_results["validate_content"]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,success", [("advise", True), ("enforce", False)])
async def test_governance_unavailable_uses_explicit_policy_without_passing(policy, success):
    pipeline = ResultPipeline(governance=StepResult.unavailable("continuity storage unavailable"))
    ctx = PipelineContext(novel_id="unavailable", metadata={"quality_policy": policy})
    ctx.policy_validator = passing_validator()
    result = await pipeline.run_chapter(ctx)
    assert result.status == "unavailable" and not result.passed
    assert result.success is success
    assert ("save" in pipeline.calls) is success
    assert result.step_status["prepare_governance"] == "unavailable"
    if not success:
        assert pipeline.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "deferred"])
async def test_derived_outcome_survives_body_commit_and_progress_projection(status):
    factory = getattr(StepResult, status)
    pipeline = ResultPipeline(aftermath=factory("derived work pending"))
    snapshots = []
    ctx = PipelineContext(novel_id="committed", writing_progress_sink=lambda _s, _l, p: snapshots.append(p))
    ctx.policy_validator = passing_validator()
    result = await pipeline.run_chapter(ctx)
    assert result.success and ctx.chapter_saved
    assert result.status == status and not result.passed
    assert result.step_status["run_post_commit"] == status
    assert not result.step_results["run_post_commit"]["passed"]
    assert snapshots[-1]["pipeline_status"] == status
    assert snapshots[-1]["pipeline_success"] is True
    assert snapshots[-1]["pipeline_passed"] is False
    assert snapshots[-1]["pipeline_complete"] is True
    assert result.to_dict()["legacy_step_status"]["save_chapter"] == "ok"


@pytest.mark.asyncio
async def test_voice_rejection_is_not_a_warning_success():
    pipeline = ResultPipeline(voice=StepResult.fail("voice quality rejected"))
    ctx = PipelineContext(novel_id="voice")
    ctx.policy_validator = passing_validator()
    result = await pipeline.run_chapter(ctx)
    assert result.status == "failed" and not result.success
    assert result.step_status["validate_voice"] == "failed"
    assert "finalize" not in result.step_status


@pytest.mark.asyncio
@pytest.mark.parametrize("score,status", [(None, "unavailable"), (-1, "unavailable"), (101, "unavailable"),
                                         (float("nan"), "unavailable"), (float("inf"), "unavailable"),
                                         ("bad", "unavailable"), (0, "passed")])
async def test_tension_unassessed_is_not_passed_and_zero_is_valid(score, status):
    result = await BaseStoryPipeline()._step_score_tension(PipelineContext(tension_composite=score))
    assert result.status == status
    assert result.passed is (status == "passed")


@pytest.mark.asyncio
async def test_superseded_aftermath_and_missing_voice_cannot_pass():
    aftermath = SimpleNamespace(run_after_chapter_saved=AsyncMock(return_value={
        "derivation_status": "superseded", "narrative_sync_status": "superseded"}))
    pipeline = BaseStoryPipeline()
    ctx = PipelineContext(aftermath_pipeline=aftermath)
    result = await pipeline._step_run_post_commit(ctx)
    assert result.status == "unavailable" and not result.passed
    assert ctx.derived_status == "retryable"
    assert (await pipeline._step_validate_voice(ctx)).status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,success", [("failed", False), ("unavailable", True), ("deferred", True)])
async def test_writing_delegate_publishes_identical_authoritative_result(status, success):
    novel = MagicMock()
    novel.novel_id.value = "novel-status"
    novel.genre = ""
    novel.target_words_per_chapter = 2000
    novel.current_auto_chapters = 0
    novel.current_chapter_in_act = 0
    daemon = MagicMock()
    daemon._pending_story_pipeline_aftermath = {}
    result = PipelineResult(success=success, error="quality failed" if not success else None,
                            step_status={"run_post_commit": status},
                            step_results={"run_post_commit": StepResult(status=status).to_dict()})
    runner = MagicMock()
    runner._make_context.return_value = PipelineContext(chapter_number=1)
    pipeline = SimpleNamespace(run_chapter=AsyncMock(return_value=result))
    with patch("engine.runtime.writing_delegate._build_runner", return_value=runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as registry:
        registry.return_value.create_pipeline.return_value = pipeline
        outcome = await run_story_pipeline_writing(daemon, novel)
    if status in {"unavailable", "deferred"}:
        assert outcome.status == "neutral"
    else:
        assert outcome.status == "failure"
    projected = next(call.kwargs for call in daemon._update_shared_state.call_args_list if "pipeline_status" in call.kwargs)
    assert projected["pipeline_status"] == status
    assert projected["pipeline_success"] is success
    assert projected["pipeline_passed"] is False
    assert projected["pipeline_step_results"] == result.step_results
