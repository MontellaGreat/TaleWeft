"""Keep canonical setup material locked until its audited workflow finishes."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar

from application.ai_invocation.variable_hub import variable_key_candidates
from application.engine.services.execution_lease import get_execution_lease


class SetupEditConflict(RuntimeError):
    """The requested write would bypass an unfinished setup audit."""


_write_scope: ContextVar[tuple[str, str, str] | None] = ContextVar("setup_write_scope", default=None)
_DEPENDENCIES = {
    "worldbuilding": {"worldbuilding"},
    "characters": {"worldbuilding", "characters"},
    "locations": {"worldbuilding", "characters", "locations"},
    "plot_outline": {"worldbuilding", "characters", "locations", "plot_outline"},
}
_LABELS = {"worldbuilding": "文风 / 世界观", "characters": "人物", "locations": "地图", "plot_outline": "剧情总纲"}
_BASE_VARIABLES = frozenset({"title", "premise", "genre_major", "genre_theme", "genre_label", "genre",
    "locked_genre", "world_preset", "locked_world_preset", "story_structure", "locked_story_structure",
    "pacing_control", "locked_pacing_control", "writing_style", "locked_writing_style", "special_requirements",
    "locked_special_requirements", "target_chapters", "target_words_per_chapter", "canonical_genre_id",
    "canonical_theme_id", "reader_contract", "audience_mode", "market_platform", "market_category_path", "market_tags"})
_PLOT_VARIABLES = frozenset({"plot.outline", "plot.stage_plan", "plot.main_story_overview", "plot.expected_ending", "plot.core_conflict"})


@contextmanager
def setup_write_scope(novel_id: str, stage: str, run_id: str):
    """Authorize an internal workflow commit; HTTP input cannot create this scope.

    Ownership is checked at each protected write, rather than when entering the
    scope, because a cancelled or superseded task can retain its Python context.
    """
    token = _write_scope.set((novel_id, stage, run_id))
    try:
        yield
    finally:
        _write_scope.reset(token)


def assert_setup_editable(novel_id: str, stage: str, *, db=None) -> None:
    if stage not in _DEPENDENCIES:
        raise ValueError("不支持的新书设置阶段")
    if db is None:
        from infrastructure.persistence.database.connection import get_database
        db = get_database()
    scope = _write_scope.get()
    if scope and scope[:2] == (novel_id, stage):
        lease = get_execution_lease(novel_id, db)
        if not lease or lease["owner"] != "setup" or lease["job_id"] != scope[2]:
            raise SetupEditConflict("新书工作流执行租约已失效，不能保存设定")
        return
    try:
        rows = db.fetch_all(
            "SELECT stage,status,snapshot_json FROM setup_dag_runs WHERE novel_id=? ORDER BY started_at DESC,rowid DESC",
            (novel_id,),
        )
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return
        raise
    seen = set()
    for row in rows:
        run_stage = row["stage"]
        if run_stage in seen:
            continue
        seen.add(run_stage)
        if stage not in _DEPENDENCIES.get(run_stage, set()):
            continue
        try:
            snapshot = json.loads(row["snapshot_json"])
        except (ValueError, TypeError) as exc:
            raise SetupEditConflict("新书工作流记录无法读取，请恢复工作流后再编辑") from exc
        if not isinstance(snapshot, dict):
            raise SetupEditConflict("新书工作流记录无效，请恢复工作流后再编辑")
        version = snapshot.get("workflow_version", 1)
        # Pre-audit runs have no durable audit contract and must not lock old
        # books forever. Only the versioned workflow owns this editing gate.
        if isinstance(version, (int, float)) and not isinstance(version, bool) and version >= 2 and row["status"] != "completed":
            raise SetupEditConflict(f"{_LABELS.get(run_stage, run_stage)}审核与修正尚未完成，请继续工作流后再编辑")


def assert_setup_bible_generation_editable(novel_id: str, stage: str = "all", *, db=None) -> None:
    """Legacy generators must respect the same locks as canonical editors."""
    stages = ("worldbuilding", "characters", "locations") if stage == "all" else (stage,)
    for target in stages:
        assert_setup_editable(novel_id, target, db=db)


def assert_setup_variable_writes(writes, *, repository=None, db=None) -> None:
    """Validate the whole batch before a caller writes any setup input/output.

    Unrelated prompt variables stay editable. Equal effective values may be
    materialized by session creation without changing the frozen setup inputs.
    """
    relevant = []
    for write in writes:
        keys = variable_key_candidates(str(write.key))
        if any(key.startswith("worldbuilding.") for key in keys):
            stages = ("worldbuilding",)
        elif any(key.startswith("characters.") for key in keys):
            stages = ("characters",)
        elif any(key.startswith("locations.") for key in keys):
            stages = ("locations",)
        elif any(key in _PLOT_VARIABLES for key in keys):
            stages = ("plot_outline",)
        elif any(key.startswith("novel.setup.") or (key.startswith("novel.") and key[6:] in _BASE_VARIABLES)
                 for key in keys):
            stages = tuple(_DEPENDENCIES)
        else:
            continue
        if callable(getattr(repository, "get_value", None)):
            current = repository.get_value(str(write.key), write.context_key)
            if current is not None and current.value == write.value:
                continue
        relevant.append((write.context_key, stages))
    if not relevant:
        return
    if db is None:
        db = getattr(repository, "_db", None)
    if db is None:
        from infrastructure.persistence.database.connection import get_database
        db = get_database()
    global_books = None
    for context_key, stages in relevant:
        parts = str(context_key or "global").split("|")
        novel_ids = [part.removeprefix("novel_id:") for part in parts if part.startswith("novel_id:")]
        if not novel_ids:
            if global_books is None:
                try:
                    global_books = [row["novel_id"] for row in db.fetch_all("SELECT DISTINCT novel_id FROM setup_dag_runs")]
                except Exception as exc:
                    if "no such table" not in str(exc).lower():
                        raise
                    global_books = []
            novel_ids = global_books
        for novel_id in novel_ids:
            scope = _write_scope.get()
            if scope and scope[0] == novel_id:
                # Preparing a setup invocation can materialize its already
                # frozen predecessor values or first optional defaults. Only
                # the internal owner may do so, under its exact current lease.
                assert_setup_editable(novel_id, scope[1], db=db)
                continue
            for stage in stages:
                assert_setup_editable(novel_id, stage, db=db)
