from types import SimpleNamespace

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext


@pytest.mark.asyncio
async def test_preplanning_refreshes_revision_before_final_chapter_commit():
    class ChapterRepository:
        revision = 2

        def get_by_novel_and_number(self, _novel_id, _chapter_number):
            return SimpleNamespace(revision=self.revision)

    class PreplanningService:
        def __init__(self, repository):
            self.repository = repository

        async def ensure_execution_plan(self, **_kwargs):
            self.repository.revision += 1
            return "一、开篇切入点\n二、场景转换列表\n三、关键对话\n四、剧情事件链\n五、角色关键决策\n六、爽点/反转设计\n七、主角状态变化"

    repository = ChapterRepository()
    ctx = PipelineContext(
        novel_id="book",
        chapter_number=1,
        outline="chapter outline",
        chapter_repository=repository,
        llm_service=object(),
        chapter_node=SimpleNamespace(title="第一章", metadata={}),
        metadata={"chapter_base_absent": False, "chapter_base_revision": 2},
    )
    ctx.inject(chapter_preplanning_service=PreplanningService(repository))
    pipeline = BaseStoryPipeline()
    pipeline._attach_chapter_preplan_metadata = lambda _ctx: None

    result = await pipeline._step_prepare_chapter_plan(ctx)

    assert result.passed
    assert ctx.metadata["chapter_base_absent"] is False
    assert ctx.metadata["chapter_base_revision"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("edited_field", ["content", "title"])
async def test_preplanning_does_not_adopt_an_authors_concurrent_edit(edited_field):
    """Only the preplan's outline write may advance the final prose CAS token."""
    class ChapterRepository:
        revision = 2
        content = "original draft"
        title = "original title"

        def get_by_novel_and_number(self, _novel_id, _chapter_number):
            return SimpleNamespace(revision=self.revision, content=self.content, title=self.title)

    class PreplanningService:
        async def ensure_execution_plan(self, **_kwargs):
            # An author edits while the provider is preparing the plan; the
            # preplan then persists only its outline on that newer chapter.
            setattr(repository, edited_field, "author's saved edit")
            repository.revision += 2
            return "prepared execution plan"

    repository = ChapterRepository()
    ctx = PipelineContext(
        novel_id="book", chapter_number=1, outline="act outline",
        chapter_repository=repository, llm_service=object(),
        chapter_node=SimpleNamespace(title="original title", metadata={}),
        metadata={"chapter_base_absent": False, "chapter_base_revision": 2},
    )
    ctx.inject(chapter_preplanning_service=PreplanningService())
    pipeline = BaseStoryPipeline()
    pipeline._attach_chapter_preplan_metadata = lambda _ctx: None

    result = await pipeline._step_prepare_chapter_plan(ctx)

    assert not result.passed
    assert ctx.metadata["chapter_base_revision"] == 2
    assert getattr(repository, edited_field) == "author's saved edit"
