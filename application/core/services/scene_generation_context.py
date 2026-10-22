"""场景生成上下文装配。

把 API 层的“前置场景正文 / Bible 上下文”读取集中到可测试的应用服务，
避免路由里散落数据库细节，也避免在缺少关键上下文时静默降级。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from domain.novel.value_objects.chapter_id import ChapterId


class MissingPreviousSceneContextError(RuntimeError):
    """生成非首场景时缺少必须的前置场景正文。"""


class InvalidSceneGenerationContextError(ValueError):
    """请求或 Bible 的身份与实际章节不一致。"""


class SceneChapterNotFoundError(LookupError):
    """场景生成所依赖的实际章节不存在。"""


def chapter_generation_scope(chapter, chapter_id: str, expected_chapter_number=None):
    """Read generation identity from the loaded chapter, never from request/Bible data."""
    if chapter is None:
        raise SceneChapterNotFoundError(f"章节不存在：{chapter_id}")
    actual_id = getattr(chapter.id, "value", chapter.id)
    novel_id = getattr(chapter.novel_id, "value", chapter.novel_id)
    number = chapter.number
    if (actual_id != chapter_id or not isinstance(novel_id, str) or not novel_id.strip()
            or type(number) is not int or number < 1):
        raise InvalidSceneGenerationContextError("实际章节缺少有效的小说或章节身份，无法生成场景")
    if expected_chapter_number is not None and (
            type(expected_chapter_number) is not int or expected_chapter_number != number):
        raise InvalidSceneGenerationContextError(f"请求章节号与实际章节不一致：实际为第 {number} 章")
    return novel_id.strip(), number


@dataclass(frozen=True)
class SceneGenerationContext:
    """场景生成所需的外部上下文。"""

    previous_scenes: List[str]
    bible_context: Optional[Dict[str, Any]]
    chapter_id: str
    novel_id: str
    chapter_number: int


class SceneGenerationContextProvider:
    """从章节场景仓储、章节仓储和 Bible 服务装配场景生成上下文。"""

    def __init__(
        self,
        *,
        chapter_scene_repository=None,
        chapter_repository=None,
        bible_service=None,
    ) -> None:
        self._chapter_scene_repository = chapter_scene_repository
        self._chapter_repository = chapter_repository
        self._bible_service = bible_service

    async def build(self, chapter_id: str, scene_index: int, *, expected_chapter_number=None) -> SceneGenerationContext:
        """构建场景生成上下文。

        非首场景依赖之前场景的已生成正文；若缺失则显式阻塞，避免续写
        链路在没有承接文本的情况下悄悄退化成单场景生成。
        """
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            raise InvalidSceneGenerationContextError("场景生成缺少有效章节 ID")
        if type(scene_index) is not int or scene_index < 0:
            raise InvalidSceneGenerationContextError("场景索引必须为非负整数")
        chapter_id = chapter_id.strip()
        if self._chapter_repository is None:
            raise InvalidSceneGenerationContextError("缺少章节仓储，无法核实场景生成身份")
        chapter = self._chapter_repository.get_by_id(ChapterId(chapter_id))
        novel_id, chapter_number = chapter_generation_scope(chapter, chapter_id, expected_chapter_number)
        previous_scenes = await self._load_previous_scene_contents(
            chapter_id=chapter_id,
            scene_index=scene_index,
        )
        bible_context = self._load_bible_context(novel_id)
        return SceneGenerationContext(
            previous_scenes=previous_scenes,
            bible_context=bible_context,
            chapter_id=chapter_id,
            novel_id=novel_id,
            chapter_number=chapter_number,
        )

    async def _load_previous_scene_contents(
        self,
        *,
        chapter_id: str,
        scene_index: int,
    ) -> List[str]:
        if scene_index <= 0:
            return []
        if self._chapter_scene_repository is None:
            raise MissingPreviousSceneContextError("缺少章节场景仓储，无法读取前置场景正文")

        scenes = await self._chapter_scene_repository.get_by_chapter(chapter_id)
        content_by_index: Dict[int, str] = {}
        for scene in scenes:
            content = (getattr(scene, "content", None) or "").strip()
            if content:
                content_by_index[int(getattr(scene, "order_index", -1))] = content

        missing = [idx for idx in range(scene_index) if idx not in content_by_index]
        if missing:
            labels = "、".join(str(idx) for idx in missing)
            raise MissingPreviousSceneContextError(
                f"缺少前置场景正文：索引 {labels}。请先生成并保存前置场景。"
            )

        return [content_by_index[idx] for idx in range(scene_index)]

    def _load_bible_context(self, novel_id: str) -> Dict[str, Any]:
        if self._bible_service is None:
            return {"novel_id": novel_id}
        bible = self._bible_service.get_bible_by_novel(novel_id)
        if bible is None:
            return {"novel_id": novel_id}
        context = asdict(bible)
        if context.get("novel_id") != novel_id:
            raise InvalidSceneGenerationContextError("Bible 所属小说与实际章节不一致，无法生成场景")
        return context
