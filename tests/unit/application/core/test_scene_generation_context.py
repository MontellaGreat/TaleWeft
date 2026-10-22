from types import SimpleNamespace

import pytest

from application.core.services.scene_generation_context import (
    MissingPreviousSceneContextError,
    InvalidSceneGenerationContextError,
    SceneChapterNotFoundError,
    SceneGenerationContextProvider,
)
from application.core.services.scene_generation_service import SceneGenerationService
from application.world.dtos.bible_dto import (
    BibleDTO,
    CharacterDTO,
    LocationDTO,
    TimelineNoteDTO,
    WorldSettingDTO,
)
from domain.novel.value_objects.scene import Scene


class FakeChapterSceneRepository:
    def __init__(self, scenes):
        self._scenes = scenes

    async def get_by_chapter(self, chapter_id):
        return self._scenes


class FakeChapterRepository:
    def get_by_id(self, chapter_id):
        return SimpleNamespace(id="chapter-1", number=3, novel_id=SimpleNamespace(value="novel-1"))


class FakeBibleService:
    def get_bible_by_novel(self, novel_id):
        return BibleDTO(
            id="bible-1",
            novel_id=novel_id,
            characters=[
                CharacterDTO(
                    id="char-1",
                    name="沈岚",
                    description="公开身份是调查员",
                    relationships=[],
                    public_profile="公开身份是调查员",
                    hidden_profile="隐藏身份不应进入场景提示",
                )
            ],
            world_settings=[
                WorldSettingDTO(
                    id="rule-1",
                    name="城规",
                    description="夜间通行需要路引",
                    setting_type="rule",
                )
            ],
            locations=[
                LocationDTO(
                    id="loc-1",
                    name="旧车站",
                    description="停运多年但仍有人值守",
                    location_type="建筑",
                )
            ],
            timeline_notes=[
                TimelineNoteDTO(
                    id="time-1",
                    event="前夜失踪",
                    time_point="第一章前",
                    description="关键证人失踪",
                )
            ],
            style_notes=[],
        )


@pytest.mark.asyncio
async def test_scene_context_provider_blocks_when_previous_scene_content_missing():
    provider = SceneGenerationContextProvider(
        chapter_scene_repository=FakeChapterSceneRepository(
            [SimpleNamespace(order_index=0, content="")]
        ),
        chapter_repository=FakeChapterRepository(),
    )

    with pytest.raises(MissingPreviousSceneContextError, match="缺少前置场景正文"):
        await provider.build(chapter_id="chapter-1", scene_index=1)


@pytest.mark.asyncio
async def test_scene_context_provider_loads_previous_scenes_and_bible_context():
    provider = SceneGenerationContextProvider(
        chapter_scene_repository=FakeChapterSceneRepository(
            [
                SimpleNamespace(order_index=0, content="第一场正文"),
                SimpleNamespace(order_index=1, content="第二场正文"),
            ]
        ),
        chapter_repository=FakeChapterRepository(),
        bible_service=FakeBibleService(),
    )

    context = await provider.build(chapter_id="chapter-1", scene_index=2)

    assert context.previous_scenes == ["第一场正文", "第二场正文"]
    assert context.bible_context is not None
    assert context.bible_context["novel_id"] == "novel-1"
    assert context.chapter_id == "chapter-1"
    assert context.chapter_number == 3
    assert context.novel_id == "novel-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("bible_service", [None, SimpleNamespace(get_bible_by_novel=lambda _: None)])
async def test_scene_context_keeps_chapter_scope_without_bible(bible_service):
    provider = SceneGenerationContextProvider(
        chapter_repository=FakeChapterRepository(), bible_service=bible_service,
    )
    context = await provider.build("chapter-1", 0, expected_chapter_number=3)
    assert context.bible_context == {"novel_id": "novel-1"}
    assert (context.chapter_id, context.novel_id, context.chapter_number) == ("chapter-1", "novel-1", 3)


@pytest.mark.asyncio
async def test_scene_context_rejects_request_chapter_number_mismatch():
    provider = SceneGenerationContextProvider(chapter_repository=FakeChapterRepository())
    with pytest.raises(InvalidSceneGenerationContextError, match="实际章节不一致"):
        await provider.build("chapter-1", 0, expected_chapter_number=2)


@pytest.mark.asyncio
async def test_scene_context_rejects_cross_book_bible():
    bible_service = SimpleNamespace(get_bible_by_novel=lambda _: FakeBibleService().get_bible_by_novel("other"))
    provider = SceneGenerationContextProvider(chapter_repository=FakeChapterRepository(), bible_service=bible_service)
    with pytest.raises(InvalidSceneGenerationContextError, match="Bible 所属小说"):
        await provider.build("chapter-1", 0)


@pytest.mark.asyncio
async def test_scene_context_requires_real_chapter():
    provider = SceneGenerationContextProvider(chapter_repository=SimpleNamespace(get_by_id=lambda _: None))
    with pytest.raises(SceneChapterNotFoundError, match="章节不存在"):
        await provider.build("missing", 0)


def test_scene_prompt_bible_context_uses_public_character_profile_only():
    bible_context = {
        "characters": [
            {
                "name": "沈岚",
                "description": "公开身份是调查员",
                "public_profile": "公开身份是调查员",
                "hidden_profile": "隐藏身份不应进入场景提示",
            }
        ],
        "locations": [{"name": "旧车站", "description": "停运多年但仍有人值守"}],
        "world_settings": [{"name": "城规", "description": "夜间通行需要路引"}],
    }

    block = SceneGenerationService._format_bible_context(
        bible_context,
        pov_character="沈岚",
    )

    assert "公开身份是调查员" in block
    assert "隐藏身份" not in block
    assert "旧车站" in block


class FakeEmbeddingService:
    async def embed(self, text):
        return [0.1, 0.2, 0.3]


class FakeVectorStore:
    async def list_collections(self):
        return ["novel_novel-1_chunks"]

    async def search(self, collection, query_vector, limit):
        assert collection == "novel_novel-1_chunks"
        assert query_vector == [0.1, 0.2, 0.3]
        return [
            {"score": 0.91, "payload": {"kind": "chapter_summary", "text": "上一章摘要"}},
            {"score": 0.88, "payload": {"kind": "bible_snippet", "text": "地点设定"}},
            {"score": 0.83, "payload": {"subject": "旧钥匙", "predicate": "关联", "object": "伏笔"}},
        ]


@pytest.mark.asyncio
async def test_scene_generation_service_retrieves_vector_context_by_novel_collection():
    service = SceneGenerationService(
        llm_service=object(),
        scene_director=object(),
        vector_store=FakeVectorStore(),
        embedding_service=FakeEmbeddingService(),
    )
    scene = Scene(
        title="旧车站追问",
        goal="逼近失踪真相",
        pov_character="沈岚",
        location="旧车站",
        tone="紧张",
        estimated_words=800,
        order_index=0,
    )

    context = await service._retrieve_relevant_context(
        scene=scene,
        scene_analysis=SimpleNamespace(characters=["沈岚"], locations=["旧车站"]),
        bible_context={"novel_id": "novel-1"},
    )

    assert context["chapters"][0]["text"] == "上一章摘要"
    assert context["bible_snippets"][0]["text"] == "地点设定"
    assert context["foreshadowings"][0]["description"] == "旧钥匙 关联 伏笔"
