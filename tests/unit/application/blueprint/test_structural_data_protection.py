"""Fault cases must leave real SQLite manuscripts and structure untouched."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.audit.services.macro_merge_engine import MacroMergeEngine, MergeConflictException
from application.blueprint.services.chapter_book_structure_sync import (
    ChapterDataProtectionError,
    assert_chapters_safe_for_structural_removal,
    purge_chapter_book_rows_not_matching_structure,
)
from application.blueprint.services.continuous_planning_service import ContinuousPlanningService
from application.blueprint.services.story_structure_service import StoryStructureService
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


@pytest.fixture
def repositories(tmp_path):
    db = DatabaseConnection(str(tmp_path / "data-guards.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-guard', 'Guard', 'guard')")
    db.commit()
    yield db, StoryNodeRepository(db), SqliteChapterRepository(db)
    db.close()


def _chapter(repo, number, content="", status=ChapterStatus.DRAFT):
    chapter = Chapter(f"book-{number}", NovelId("novel-guard"), number, f"章{number}", content, status=status)
    repo.save(chapter)
    return chapter


def _node(repo, number=1, parent="act-guard"):
    node = StoryNode(f"node-{number}", "novel-guard", NodeType.CHAPTER, number, f"章{number}", number,
                     parent_id=parent, content="", word_count=0, status="draft")
    repo.save_sync(node)
    return node


def _act(repo):
    repo.save_sync(StoryNode("act-guard", "novel-guard", NodeType.ACT, 1, "第一幕", 0))


def _snapshot(db):
    return {
        table: [dict(row) for row in db.fetch_all(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("chapters", "story_nodes")
    }


def _planning(nodes, chapters):
    return ContinuousPlanningService(nodes, SimpleNamespace(delete_by_chapter=AsyncMock()), Mock(), chapter_repository=chapters)


@pytest.mark.parametrize("content,status", [("已经写好的正文", "draft"), ("", "completed"), ("", "reviewing")])
def test_purge_does_not_remove_even_earlier_empty_drafts_when_any_orphan_is_protected(repositories, content, status):
    db, nodes, chapters = repositories
    _chapter(chapters, 1)
    _chapter(chapters, 2, content, status)
    before = _snapshot(db)
    with pytest.raises(ChapterDataProtectionError):
        purge_chapter_book_rows_not_matching_structure(nodes, chapters, "novel-guard")
    assert _snapshot(db) == before


def test_purge_can_remove_proven_empty_drafts(repositories):
    db, nodes, chapters = repositories
    _chapter(chapters, 1)
    _chapter(chapters, 2, "  ")
    assert purge_chapter_book_rows_not_matching_structure(nodes, chapters, "novel-guard") == 2
    assert chapters.list_by_novel(NovelId("novel-guard")) == []


@pytest.mark.parametrize("chapter", [SimpleNamespace(id="unknown"), SimpleNamespace(id="bad", content="", status="future_status")])
def test_unknown_chapter_state_blocks_structural_removal(chapter):
    with pytest.raises(ChapterDataProtectionError):
        assert_chapters_safe_for_structural_removal([chapter])


def test_act_replan_checks_entire_batch_before_deleting_empty_child(repositories):
    db, nodes, chapters = repositories
    _act(nodes)
    _node(nodes, 1)
    _node(nodes, 2)
    _chapter(chapters, 1)
    _chapter(chapters, 2, "实际正文不能被结构节点的空 content 掩盖")
    service = _planning(nodes, chapters)
    before = _snapshot(db)
    with pytest.raises(ChapterDataProtectionError):
        asyncio.run(service._remove_chapter_children_of_act("act-guard"))
    assert _snapshot(db) == before
    service.chapter_element_repo.delete_by_chapter.assert_not_called()


def test_confirm_act_blocks_written_orphan_before_mutating_its_empty_children(repositories):
    db, nodes, chapters = repositories
    _act(nodes)
    _node(nodes, 1)
    _chapter(chapters, 1)
    _chapter(chapters, 2, "结构树遗漏的有效正文")
    service = _planning(nodes, chapters)
    before = _snapshot(db)
    plan = [{"number": 1, "title": "新章", "main_event": "事件",
             "handoff_from_previous": "承接", "handoff_to_next": "后果", "required_threads": ["主线"]}]
    with pytest.raises(ChapterDataProtectionError):
        asyncio.run(service.confirm_act_planning("act-guard", plan))
    assert _snapshot(db) == before
    service.chapter_element_repo.delete_by_chapter.assert_not_called()


def test_empty_child_removal_cannot_delete_later_written_chapter_after_renumber(repositories):
    db, nodes, chapters = repositories
    _act(nodes)
    for number in (1, 2):
        _node(nodes, number)
        _chapter(chapters, number)
    _chapter(chapters, 3, "后续已写正文")
    asyncio.run(_planning(nodes, chapters)._remove_chapter_children_of_act("act-guard"))
    remaining = chapters.get_by_id(ChapterId("book-3"))
    assert remaining is not None and remaining.content == "后续已写正文"
    assert chapters.get_by_id(ChapterId("book-1")) is None
    assert chapters.get_by_id(ChapterId("book-2")) is None


def test_structural_delete_does_not_delete_written_descendant_or_modify_tree(repositories):
    db, nodes, chapters = repositories
    _act(nodes)
    _node(nodes, 1)
    _chapter(chapters, 1, "原始手稿")
    before = _snapshot(db)
    with pytest.raises(ChapterDataProtectionError):
        asyncio.run(StoryStructureService(nodes, chapter_repository=chapters).delete_node("act-guard"))
    assert _snapshot(db) == before


def test_macro_conflict_cannot_be_bypassed_by_legacy_fallback(repositories):
    db, nodes, chapters = repositories
    _act(nodes)
    _node(nodes, 1)
    _chapter(chapters, 1, "手稿")
    before = _snapshot(db)
    with pytest.raises(MergeConflictException):
        asyncio.run(_planning(nodes, chapters).persist_macro_structure_with_fallback("novel-guard", []))
    assert _snapshot(db) == before


@pytest.mark.parametrize("kind", ["chapter", "CHAPTER", NodeType.CHAPTER])
def test_macro_carrier_protection_accepts_domain_and_legacy_type_case(kind):
    old = [dict(id="a", node_type="act", parent_id=None, title="幕", order_index=0),
           dict(id="c", node_type=kind, parent_id="a", title="章", order_index=1)]
    plan = MacroMergeEngine(old, []).execute_diff()
    assert plan.has_fatal_conflict
    assert "a" not in plan.deletes and "c" not in plan.deletes


@pytest.mark.parametrize("nodes", [
    [dict(id="a", node_type="unknown", parent_id=None, title="幕", order_index=0)],
    [dict(id="a", node_type="act", parent_id="a", title="幕", order_index=0)],
    [dict(id="a", node_type="act", parent_id="missing", title="幕", order_index=0)],
    [dict(id="a", node_type="act", parent_id=None, title="幕", order_index=0)] * 2,
])
def test_uncertain_structure_has_no_applicable_mutations(nodes):
    plan = MacroMergeEngine(nodes, []).execute_diff()
    assert plan.has_fatal_conflict
    assert plan.creates == plan.updates == plan.deletes == []


def test_macro_type_change_cannot_repurpose_an_existing_chapter_id():
    old = [dict(id="a", node_type="chapter", parent_id=None, title="章", order_index=0)]
    new = [dict(id="a", node_type="act", parent_id=None, title="幕", order_index=0)]
    plan = MacroMergeEngine(old, new).execute_diff()
    assert plan.has_fatal_conflict
    assert plan.creates == plan.updates == plan.deletes == []
