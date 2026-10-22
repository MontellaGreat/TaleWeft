"""Keep invalidated chapter facts out of subsequent prose generation."""
from __future__ import annotations


class ChapterDerivationsNotReady(RuntimeError):
    def __init__(self, chapter_number: int, status: str):
        self.chapter_number, self.status = chapter_number, status
        super().__init__(
            f"chapter_derivations_not_ready: 第{chapter_number}章的关联内容尚未更新完成（{status}），"
            "请等待关联工作流完成；若更新失败，请在该章重跑关联工作流后继续生成。"
        )


def assert_previous_derivations_ready(db, novel_id: str, chapter_number: int) -> None:
    """Block only explicitly invalidated predecessors; legacy books remain usable.

    Job revisions must match the current prose. Summary/evolution invalidation
    also covers the suffix before its individual replay jobs have been queued.
    A completed index-only task cannot make a stale summary ready.
    """
    if chapter_number <= 1:
        return
    tables = {row["name"] for row in db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('chapters','chapter_derivation_jobs','chapter_summaries','knowledge','chapter_evolution_snapshots')"
    )}
    if "chapters" not in tables:
        return
    if "chapter_derivation_jobs" in tables:
        blocked = db.fetch_one(
            "SELECT j.chapter_number,j.status FROM chapter_derivation_jobs j "
            "JOIN chapters c ON c.id=j.chapter_id AND c.revision=j.revision "
            "WHERE c.novel_id=? AND c.number<? AND (j.status<>'completed' "
            "OR j.cascade_status NOT IN ('completed','not_requested')) "
            "ORDER BY c.number LIMIT 1", (novel_id, chapter_number),
        )
        if blocked:
            raise ChapterDerivationsNotReady(blocked["chapter_number"], blocked["status"])
    if {"chapter_summaries", "knowledge"} <= tables:
        blocked = db.fetch_one(
            "SELECT s.chapter_number,s.sync_status AS status FROM chapter_summaries s "
            "JOIN knowledge k ON k.id=s.knowledge_id JOIN chapters c "
            "ON c.novel_id=k.novel_id AND c.number=s.chapter_number "
            "WHERE k.novel_id=? AND c.number<? AND s.sync_status IN "
            "('stale','pending','running','failed','partial','unavailable','superseded') "
            "ORDER BY c.number LIMIT 1", (novel_id, chapter_number),
        )
        if blocked:
            raise ChapterDerivationsNotReady(blocked["chapter_number"], blocked["status"])
    if "chapter_evolution_snapshots" in tables:
        blocked = db.fetch_one(
            "SELECT e.chapter_number,e.status FROM chapter_evolution_snapshots e "
            "JOIN chapters c ON c.novel_id=e.novel_id AND c.number=e.chapter_number "
            "WHERE e.novel_id=? AND c.number<? AND trim(COALESCE(c.content,''))<>'' "
            "AND e.branch_id='main' AND e.status='stale' "
            "AND e.rowid=(SELECT latest.rowid FROM chapter_evolution_snapshots latest "
            "WHERE latest.novel_id=e.novel_id AND latest.branch_id=e.branch_id "
            "AND latest.chapter_number=e.chapter_number "
            "ORDER BY julianday(latest.created_at) DESC,latest.rowid DESC LIMIT 1) "
            "ORDER BY c.number LIMIT 1", (novel_id, chapter_number),
        )
        if blocked:
            raise ChapterDerivationsNotReady(blocked["chapter_number"], blocked["status"])


def assert_repository_derivations_ready(repository, novel_id: str, chapter_number: int) -> None:
    """Use the repository's database, without opening an unrelated global store."""
    from infrastructure.persistence.database.connection import DatabaseConnection

    db = getattr(repository, "db", None)
    if isinstance(db, DatabaseConnection):
        assert_previous_derivations_ready(db, novel_id, chapter_number)
