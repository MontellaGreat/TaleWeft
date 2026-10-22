"""结构同步只可清理已确认为空的草稿，正文和未确定状态一律保护。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable, Optional, Set

from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.entities.chapter import Chapter

if TYPE_CHECKING:
    from domain.novel.repositories.chapter_repository import ChapterRepository
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

logger = logging.getLogger(__name__)


class ChapterDataProtectionError(ValueError):
    """A structural operation requires a reviewed manuscript deletion proposal."""


def _known_empty_draft(chapter, *, allow_null_content: bool = False) -> bool:
    content = getattr(chapter, "content", None)
    status = getattr(chapter, "status", None)
    status = getattr(status, "value", status)
    if status != "draft":
        return False
    if content is None and allow_null_content:
        content = ""
    if not isinstance(content, str) or content.strip():
        return False
    # Chapter.word_count constructs a nonempty ChapterContent value object and
    # raises for whitespace-only drafts. Content above already proves zero here.
    count = 0 if isinstance(chapter, Chapter) else getattr(chapter, "word_count", 0)
    count = getattr(count, "value", count)
    return isinstance(count, int) and count == 0


def assert_chapters_safe_for_structural_removal(chapters: Iterable) -> None:
    """Validate the entire batch before the first destructive side effect."""
    protected = [str(getattr(ch, "id", "unknown")) for ch in chapters if not _known_empty_draft(ch)]
    if protected:
        raise ChapterDataProtectionError(
            "结构变更已阻断：包含已有正文、非草稿或状态不明的章节 "
            + ", ".join(protected[:10])
            + "。请保留原结构；正文删除需要独立预览、确认和可恢复快照。"
        )


def assert_story_chapters_safe_for_removal(nodes: Iterable, chapter_repository, novel_id: str) -> None:
    """Check both copies: a stale empty story node cannot hide a written book row."""
    nodes = list(nodes)
    if not nodes:
        return
    if chapter_repository is None:
        raise ChapterDataProtectionError("结构变更已阻断：无法核实章节正文是否为空。")
    book_rows = []
    for node in nodes:
        if not _known_empty_draft(node, allow_null_content=True):
            raise ChapterDataProtectionError(f"结构变更已阻断：章节节点 {node.id} 含正文或状态不明。")
        try:
            number = int(node.number)
        except (TypeError, ValueError):
            raise ChapterDataProtectionError(f"结构变更已阻断：章节节点 {node.id} 的编号无效。") from None
        if number < 1:
            raise ChapterDataProtectionError(f"结构变更已阻断：章节节点 {node.id} 的编号无效。")
        row = chapter_repository.get_by_novel_and_number(NovelId(novel_id), number)
        if row is not None:
            book_rows.append(row)
    assert_chapters_safe_for_structural_removal(book_rows)


def preflight_chapter_book_purge(story_node_repo, chapter_repository, novel_id: str, *, structure_nums=None):
    """Return proven-empty orphan drafts; an unsafe orphan blocks the whole batch."""
    if chapter_repository is None:
        return []
    if structure_nums is None:
        structure_nums = collect_structure_chapter_numbers(story_node_repo, novel_id)
    candidates = []
    for chapter in list(chapter_repository.list_by_novel(NovelId(novel_id))):
        try:
            number = int(chapter.number)
        except (TypeError, ValueError):
            raise ChapterDataProtectionError("结构变更已阻断：正文表存在无效章节编号。") from None
        if number not in structure_nums:
            candidates.append(chapter)
    assert_chapters_safe_for_structural_removal(candidates)
    return candidates


def collect_structure_chapter_numbers(
    story_node_repo: "StoryNodeRepository",
    novel_id: str,
) -> Set[int]:
    """全书结构树上 chapter 节点的全局章节号集合。"""
    nums: Set[int] = set()
    for n in story_node_repo.get_by_novel_sync(novel_id):
        if not n.is_chapter():
            continue
        try:
            nums.add(int(n.number))
        except (TypeError, ValueError):
            raise ChapterDataProtectionError(f"结构变更已阻断：章节节点 {n.id} 的编号无效。") from None
    return nums


def purge_chapter_book_rows_not_matching_structure(
    story_node_repo: "StoryNodeRepository",
    chapter_repository: Optional["ChapterRepository"],
    novel_id: str,
) -> int:
    """只删除树外空草稿；发现受保护章节则在任何删除前报错。

    Returns:
        删除的行数
    """
    candidates = preflight_chapter_book_purge(story_node_repo, chapter_repository, novel_id)
    removed = 0
    for ch in candidates:
        cid = getattr(ch.id, "value", ch.id)
        chapter_repository.delete(ChapterId(cid))
        removed += 1
    if removed:
        logger.info("[chapter↔structure] novel=%s 已删与树不一致的正文行 %s 条", novel_id, removed)
    return removed
