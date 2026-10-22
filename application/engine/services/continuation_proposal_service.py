"""Completed novel continuation proposals.

Continuation is deliberately a two-step operation.  Creating a proposal only
captures the completed-book baseline and the author's requested target.  The
proposal must be approved before an autopilot command can move a completed
novel back to a planning stage.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS autopilot_continuation_proposals (
    proposal_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    base_stage TEXT NOT NULL,
    base_target_chapters INTEGER NOT NULL,
    proposed_target_chapters INTEGER NOT NULL,
    completed_chapters INTEGER NOT NULL DEFAULT 0,
    from_chapter INTEGER NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    command_id TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    rejected_at TEXT,
    base_run_epoch INTEGER NOT NULL DEFAULT 0,
    run_epoch INTEGER,
    structure_change_id TEXT,
    structure_json TEXT,
    structure_confirmed_at TEXT,
    UNIQUE(novel_id, proposal_id)
)
"""


def _db(db=None):
    if db is not None:
        return db
    from infrastructure.persistence.database.connection import get_database

    return get_database()


def ensure_schema(db=None) -> None:
    database = _db(db)
    database.execute(TABLE_SQL)
    columns = {row["name"] for row in database.fetch_all("PRAGMA table_info(autopilot_continuation_proposals)")}
    for name, declaration in {
        "base_run_epoch": "INTEGER NOT NULL DEFAULT 0", "run_epoch": "INTEGER",
        "structure_change_id": "TEXT", "structure_json": "TEXT", "structure_confirmed_at": "TEXT",
    }.items():
        if name not in columns:
            database.execute(f"ALTER TABLE autopilot_continuation_proposals ADD COLUMN {name} {declaration}")
    database.commit()
    database.execute("""UPDATE autopilot_continuation_proposals SET status='legacy_unverified'
        WHERE status='applied' AND structure_confirmed_at IS NULL""")
    database.commit()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["plan"] = json.loads(result.pop("plan_json") or "{}")
    except (TypeError, ValueError):
        result["plan"] = {}
    result["structure"] = json.loads(result.pop("structure_json", None) or "null")
    return result


def get(proposal_id: str, novel_id: str, db=None) -> dict[str, Any] | None:
    database = _db(db)
    ensure_schema(database)
    result = _row(database.fetch_one(
        "SELECT * FROM autopilot_continuation_proposals WHERE proposal_id=? AND novel_id=?",
        (proposal_id, novel_id),
    ))
    if result and result.get("structure_change_id"):
        from application.snapshot.services.data_change_journal import DataChangeJournal
        result["structure_review"] = DataChangeJournal(database).get(novel_id, result["structure_change_id"])
    return result


def list_for_novel(novel_id: str, db=None, *, limit: int = 20) -> list[dict[str, Any]]:
    database = _db(db)
    ensure_schema(database)
    rows = database.fetch_all(
        "SELECT * FROM autopilot_continuation_proposals WHERE novel_id=? "
        "ORDER BY created_at DESC LIMIT ?",
        (novel_id, max(1, min(int(limit), 100))),
    )
    return [get(row["proposal_id"], novel_id, database) for row in rows]


def create(
    novel_id: str,
    *,
    target_chapters: int,
    mode: str = "continue",
    rationale: str = "",
    db=None,
) -> dict[str, Any]:
    """Create a proposal without changing the novel or starting execution."""
    database = _db(db)
    ensure_schema(database)
    if mode not in {"continue", "replan"}:
        raise ValueError("不支持的续写提案模式")

    novel = database.fetch_one(
        "SELECT current_stage, autopilot_status, target_chapters, autopilot_run_epoch FROM novels WHERE id=?",
        (novel_id,),
    )
    if novel is None:
        raise LookupError("小说不存在")
    stage = str(novel["current_stage"] or "")
    status = str(novel["autopilot_status"] or "")
    if stage != "completed":
        raise ValueError(f"当前阶段不是已完成，不能创建续写提案（当前：{stage}）")
    if status == "running":
        raise ValueError("自动驾驶仍在运行，不能创建续写提案")
    base_target = int(novel["target_chapters"] or 0)
    requested = int(target_chapters)
    if requested <= base_target:
        raise ValueError(f"续写目标必须大于当前目标章数（当前：{base_target}）")

    existing = database.fetch_one(
        "SELECT * FROM autopilot_continuation_proposals "
        "WHERE novel_id=? AND mode=? AND proposed_target_chapters=? "
        "AND status IN ('proposed','approved') AND base_run_epoch=? ORDER BY created_at DESC LIMIT 1",
        (novel_id, mode, requested, int(novel["autopilot_run_epoch"] or 0)),
    )
    if existing:
        return _row(existing)

    completed_row = database.fetch_one(
        "SELECT COUNT(*) AS count, COALESCE(MAX(number), 0) AS max_number "
        "FROM chapters WHERE novel_id=? AND status='completed'",
        (novel_id,),
    )
    completed_data = dict(completed_row) if completed_row is not None else {}
    completed = int(completed_data.get("count") or 0)
    max_number = int(completed_data.get("max_number") or completed)
    from_chapter = max_number + 1
    additional = requested - base_target
    plan = {
        "kind": "completed_novel_continuation",
        "mode": mode,
        "base_target_chapters": base_target,
        "proposed_target_chapters": requested,
        "completed_chapters": completed,
        "from_chapter": from_chapter,
        "additional_chapters": additional,
        "next_stage": "macro_planning",
        "requires_structure_review": True,
        "preserves_completed_chapters": True,
        "steps": [
            f"将目标从 {base_target} 章扩展到 {requested} 章",
            "重新规划部、卷、幕与后续章节结构",
            "作者确认新增结构后进入幕级规划",
            f"从第 {from_chapter} 章开始写作并逐章审计",
        ],
    }
    now = datetime.now(timezone.utc).isoformat()
    proposal_id = uuid.uuid4().hex
    database.execute(
        "INSERT INTO autopilot_continuation_proposals "
        "(proposal_id,novel_id,mode,base_stage,base_target_chapters,"
        "proposed_target_chapters,completed_chapters,from_chapter,rationale,"
        "plan_json,status,created_at,base_run_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            proposal_id,
            novel_id,
            mode,
            stage,
            base_target,
            requested,
            completed,
            from_chapter,
            str(rationale or "").strip()[:2000],
            _json(plan),
            "proposed",
            now,
            int(novel["autopilot_run_epoch"] or 0),
        ),
    )
    database.commit()
    return get(proposal_id, novel_id, database)


def approve(proposal_id: str, novel_id: str, *, command_id: str, db=None) -> dict[str, Any]:
    """Approve only if the completed-book baseline is still unchanged."""
    database = _db(db)
    ensure_schema(database)
    proposal = get(proposal_id, novel_id, database)
    if proposal is None:
        raise LookupError("续写提案不存在")
    if proposal["status"] != "proposed":
        raise ValueError(f"提案当前状态为 {proposal['status']}，不能重复审批")
    novel = database.fetch_one(
        "SELECT current_stage, autopilot_status, target_chapters, autopilot_run_epoch FROM novels WHERE id=?",
        (novel_id,),
    )
    if novel is None:
        raise LookupError("小说不存在")
    if str(novel["current_stage"] or "") != proposal["base_stage"] or int(novel["target_chapters"] or 0) != proposal["base_target_chapters"]:
        raise ValueError("作品已发生变化，该续写提案已过期，请重新生成提案")
    if int(novel["autopilot_run_epoch"] or 0) != proposal["base_run_epoch"]:
        raise ValueError("作品运行版本已变化，该续写提案已过期")
    if str(novel["current_stage"] or "") != "completed" or str(novel["autopilot_status"] or "") == "running":
        raise ValueError("只有已完成且未运行的作品才能审批续写提案")
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE autopilot_continuation_proposals SET status='approved',command_id=?,approved_at=? "
            "WHERE proposal_id=? AND novel_id=? AND status='proposed' AND EXISTS "
            "(SELECT 1 FROM novels WHERE id=? AND current_stage='completed' AND autopilot_status!='running' "
            "AND target_chapters=? AND autopilot_run_epoch=?)",
            (command_id, now, proposal_id, novel_id, novel_id, proposal["base_target_chapters"], proposal["base_run_epoch"]),
        )
    if cursor.rowcount != 1:
        raise ValueError("续写提案已被其他请求处理或作品版本已变化")
    return get(proposal_id, novel_id, database)


def reject(proposal_id: str, novel_id: str, db=None) -> dict[str, Any]:
    database = _db(db)
    ensure_schema(database)
    from application.engine.services.autopilot_control import ensure_control_schema
    ensure_control_schema(database)
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as conn:
        cursor = conn.execute(
            "UPDATE autopilot_continuation_proposals SET status='rejected',rejected_at=? "
            "WHERE proposal_id=? AND novel_id=? AND status IN ('proposed','approved','macro_planning','awaiting_structure_review') "
            "AND (run_epoch IS NULL OR EXISTS (SELECT 1 FROM novels n WHERE n.id=novel_id AND "
            "(n.autopilot_run_epoch=run_epoch OR EXISTS (SELECT 1 FROM autopilot_commands c WHERE c.novel_id=n.id "
            "AND c.run_epoch=n.autopilot_run_epoch AND c.command_type='stop'))))",
            (now, proposal_id, novel_id),
        )
        conn.execute("""UPDATE novels SET current_stage='completed',autopilot_status='completed',last_stable_stage='completed',
            autopilot_run_epoch=COALESCE(autopilot_run_epoch,0)+1,
            target_chapters=(SELECT base_target_chapters FROM autopilot_continuation_proposals WHERE proposal_id=?),
            updated_at=CURRENT_TIMESTAMP WHERE id=? AND EXISTS
            (SELECT 1 FROM autopilot_continuation_proposals p WHERE p.proposal_id=? AND p.novel_id=novels.id
            AND p.status='rejected' AND (p.run_epoch=novels.autopilot_run_epoch OR EXISTS
            (SELECT 1 FROM autopilot_commands c WHERE c.novel_id=novels.id AND c.run_epoch=novels.autopilot_run_epoch
            AND c.command_type='stop')) AND novels.current_stage IN ('macro_planning','paused_for_review')
            AND NOT EXISTS (SELECT 1 FROM chapters ch WHERE ch.novel_id=novels.id AND ch.number>=p.from_chapter
            AND trim(COALESCE(ch.content,''))!=''))""", (proposal_id, novel_id, proposal_id))
    if cursor.rowcount != 1:
        raise ValueError("提案不存在或已处理")
    return get(proposal_id, novel_id, database)


def active(novel_id: str, db=None) -> dict[str, Any] | None:
    database = _db(db)
    ensure_schema(database)
    row = database.fetch_one("""SELECT proposal_id FROM autopilot_continuation_proposals WHERE novel_id=?
        AND status IN ('approved','macro_planning','awaiting_structure_review') ORDER BY created_at DESC LIMIT 1""", (novel_id,))
    return get(row["proposal_id"], novel_id, database) if row else None


def prepare_structure(proposal_id: str, novel_id: str, structure: list, *, planning_service, db=None) -> dict:
    """Persist a protected dry-run; only a later author confirmation may apply it."""
    from application.snapshot.services.data_change_journal import DataChangeJournal, DataChangeConflict
    database = _db(db)
    proposal = get(proposal_id, novel_id, database)
    if not proposal or proposal["status"] != "macro_planning":
        raise DataChangeConflict("续写提案不在宏观规划阶段")
    if not planning_service._validate_macro_structure_completeness(structure, proposal["proposed_target_chapters"]):
        raise ValueError("续写宏观规划结构无效，必须包含有效部和卷")
    nodes = planning_service._flatten_structure_to_nodes(novel_id, structure)
    preview = DataChangeJournal(database).propose_macro(novel_id, nodes, allow_running=True,
                                                       continuation_proposal_id=proposal_id)
    if preview["run_epoch"] != proposal["run_epoch"]:
        raise DataChangeConflict("续写运行版本已变化，结构预览未接纳")
    with database.transaction() as conn:
        updated = conn.execute("""UPDATE autopilot_continuation_proposals SET status='awaiting_structure_review',
            structure_change_id=?,structure_json=? WHERE proposal_id=? AND novel_id=? AND status='macro_planning'
            AND run_epoch=? AND EXISTS (SELECT 1 FROM novels WHERE id=? AND autopilot_run_epoch=?
            AND current_stage='macro_planning' AND autopilot_status='running')""",
            (preview["proposal_id"], _json(structure), proposal_id, novel_id, preview["run_epoch"], novel_id, preview["run_epoch"]))
        conn.execute("""UPDATE novels SET current_stage='paused_for_review',updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND autopilot_run_epoch=? AND EXISTS (SELECT 1 FROM autopilot_continuation_proposals
            WHERE proposal_id=? AND status='awaiting_structure_review' AND structure_change_id=?)""",
            (novel_id, preview["run_epoch"], proposal_id, preview["proposal_id"]))
    if updated.rowcount != 1:
        raise DataChangeConflict("续写运行状态已变化，结构预览未接纳")
    return get(proposal_id, novel_id, database)


def revert_approval(proposal_id: str, novel_id: str, *, command_id: str, db=None) -> dict[str, Any] | None:
    """Return an approval to proposed when command submission failed synchronously.

    A timeout is intentionally not handled here: the worker may still commit the
    command after the HTTP request ends, so the approved state remains the
    durable intent until the command is reconciled.
    """
    database = _db(db)
    ensure_schema(database)
    with database.transaction() as conn:
        conn.execute(
            "UPDATE autopilot_continuation_proposals SET status='proposed',command_id=NULL,approved_at=NULL "
            "WHERE proposal_id=? AND novel_id=? AND status='approved' AND command_id=?",
            (proposal_id, novel_id, command_id),
        )
    return get(proposal_id, novel_id, database)
