"""Explicit, recoverable structural changes on the existing single SQLite writer."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from uuid import uuid4


COMMAND = "APPLY_DATA_CHANGE_JOURNAL"
_EXCLUDED = {"data_change_proposals", "data_change_audits", "content_versions", "novel_execution_leases", "autopilot_commands", "autopilot_continuation_proposals", "chapter_save_receipts"}


class DataChangeConflict(ValueError):
    pass


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _encode(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    return value


def _decode(value):
    return base64.b64decode(value["$bytes"]) if isinstance(value, dict) and set(value) == {"$bytes"} else value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _snapshot(conn, novel_id):
    """Capture novel rows and FK children in one read transaction, including blobs."""
    schema, rows = {}, {}
    for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        table = table_row[0]
        if table.startswith("sqlite_") or table in _EXCLUDED or table == "novels":
            continue
        info = list(conn.execute(f"PRAGMA table_info({_quote(table)})"))
        columns = [column[1] for column in info]
        primary = [column[1] for column in sorted(info, key=lambda item: item[5]) if column[5]]
        if not primary:
            continue
        schema[table] = {"columns": columns, "pk": primary, "fk": list(conn.execute(f"PRAGMA foreign_key_list({_quote(table)})"))}
        if "novel_id" in columns:
            rows[table] = list(conn.execute(f"SELECT * FROM {_quote(table)} WHERE novel_id=?", (novel_id,)))
    # Discover FK children and refuse inconsistent links across novel boundaries.
    for _ in range(len(schema) + 1):
        changed = False
        for table, meta in schema.items():
            groups = {}
            for fk in meta["fk"]:
                groups.setdefault(fk[0], []).append(fk)
            predicates, params = [], []
            for group in groups.values():
                parent = group[0][2]
                if parent not in rows or parent not in schema:
                    continue
                for parent_row in rows[parent]:
                    clauses = []
                    for fk in group:
                        target = fk[4] or schema[parent]["pk"][fk[1]]
                        clauses.append(f"{_quote(fk[3])} IS ?")
                        params.append(parent_row[target])
                    predicates.append("(" + " AND ".join(clauses) + ")")
            # Use batches for SQLite's variable/expression limits.
            found = {}
            offset = 0
            for start in range(0, len(predicates), 100):
                batch = predicates[start:start + 100]
                count = sum(clause.count("?") for clause in batch)
                for row in conn.execute(f"SELECT * FROM {_quote(table)} WHERE " + " OR ".join(batch), params[offset:offset + count]):
                    found[tuple(row[key] for key in meta["pk"])] = row
                offset += count
            fresh = list(found.values())
            if "novel_id" in meta["columns"]:
                if any(row["novel_id"] != novel_id for row in fresh):
                    raise DataChangeConflict("检测到跨作品外键关联，结构变更已阻断；请先修复关联数据")
                continue
            if fresh and len(fresh) != len(rows.get(table, [])):
                rows[table] = fresh
                changed = True
        if not changed:
            break
    state = {}
    for table, table_rows in rows.items():
        encoded = [{key: _encode(row[key]) for key in row.keys()} for row in table_rows]
        state[table] = {"pk": schema[table]["pk"], "rows": sorted(encoded, key=_json)}
    if len(_json(state).encode("utf-8")) > 32 * 1024 * 1024:
        raise ValueError("变更快照超过32MB，请先创建数据库备份并缩小变更范围")
    return state


def _key(row, primary):
    return _json([row[column] for column in primary])


def _restore(conn, before, after):
    """Undo only this transaction's differences; keep immutable content history."""
    conn.execute("PRAGMA defer_foreign_keys=ON")
    changes = []
    for table in before.keys() | after.keys():
        primary = (before.get(table) or after[table])["pk"]
        old = {_key(row, primary): row for row in before.get(table, {}).get("rows", [])}
        new = {_key(row, primary): row for row in after.get(table, {}).get("rows", [])}
        for identity in new.keys() - old.keys():
            row = new[identity]
            conn.execute(f"DELETE FROM {_quote(table)} WHERE " + " AND ".join(f"{_quote(column)} IS ?" for column in primary), [_decode(row[column]) for column in primary])
        for identity, row in old.items():
            if row == new.get(identity):
                continue
            changes.append((table, primary, row, identity in new))
    # Deferred FK checking permits parents and children to be restored in any order.
    for table, primary, row, exists in changes:
        columns = list(row)
        if exists:
            mutable = [column for column in columns if column not in primary]
            conn.execute(f"UPDATE {_quote(table)} SET " + ",".join(f"{_quote(column)}=?" for column in mutable)
                + " WHERE " + " AND ".join(f"{_quote(column)} IS ?" for column in primary),
                [_decode(row[column]) for column in mutable + primary])
        else:
            conn.execute(f"INSERT INTO {_quote(table)} (" + ",".join(_quote(column) for column in columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [_decode(row[column]) for column in columns])


def _apply(conn, novel_id, action):
    operation = action["operation"]
    if operation in {"delete_nodes", "snapshot_rollback"}:
        for chapter_id in action.get("chapter_ids", []):
            conn.execute("DELETE FROM chapters WHERE id=? AND novel_id=?", (chapter_id, novel_id))
        for node_id in action.get("node_ids", []):
            conn.execute("DELETE FROM story_nodes WHERE id=? AND novel_id=?", (node_id, novel_id))
    elif operation == "macro_merge":
        for node_id in action["deletes"]:
            conn.execute("DELETE FROM story_nodes WHERE id=? AND novel_id=?", (node_id, novel_id))
        for node in action["updates"]:
            conn.execute("UPDATE story_nodes SET title=?,description=?,order_index=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND novel_id=?",
                (node["title"], node.get("description", ""), node["order_index"], node["id"], novel_id))
        pending = list(action["creates"])
        while pending:
            progress = False
            for node in pending[:]:
                parent = node.get("parent_id")
                if parent and not conn.execute("SELECT 1 FROM story_nodes WHERE id=? AND novel_id=?", (parent, novel_id)).fetchone():
                    continue
                conn.execute("""INSERT INTO story_nodes(id,novel_id,parent_id,node_type,number,title,description,order_index,
                    planning_status,planning_source,chapter_start,chapter_end,suggested_chapter_count)
                    VALUES (?,?,?,?,?,?,?,?,'ai_generated','ai_macro',?,?,?)""",
                    (node["id"], novel_id, parent, node["node_type"], node.get("number", 0), node["title"], node.get("description", ""),
                     node["order_index"], node.get("chapter_start"), node.get("chapter_end"), node.get("suggested_chapter_count")))
                pending.remove(node)
                progress = True
            if not progress:
                raise ValueError("结构含有循环或未找到父节点")
    else:
        raise ValueError("不支持的结构变更")


def handle_data_change(payload):
    """Runs only in the existing PersistenceQueue consumer; it owns one transaction."""
    from infrastructure.persistence.database.connection import get_database
    conn = get_database(payload["db_path"]).get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = conn.execute("SELECT * FROM data_change_proposals WHERE proposal_id=? AND novel_id=?", (payload["proposal_id"], payload["novel_id"])).fetchone()
        if proposal is None:
            raise LookupError("变更提案不存在")
        proposal = dict(proposal)
        action = json.loads(proposal["proposal_json"])
        automatic_safe = action.get("automatic_safe") is True and action["operation"] == "macro_merge" and not payload.get("rollback")
        novel = conn.execute("SELECT autopilot_status,current_stage,autopilot_run_epoch FROM novels WHERE id=?", (payload["novel_id"],)).fetchone()
        continuation_id = action.get("continuation_proposal_id")
        if not continuation_id and action["operation"] == "macro_merge":
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autopilot_continuation_proposals'").fetchone():
                pending_continuation = conn.execute("""SELECT 1 FROM autopilot_continuation_proposals WHERE novel_id=?
                    AND status IN ('macro_planning','awaiting_structure_review') LIMIT 1""", (payload["novel_id"],)).fetchone()
                if pending_continuation:
                    raise DataChangeConflict("续写结构必须确认绑定的结构预览，不能通过其他宏观保存绕过")
        if continuation_id:
            continuation = conn.execute("""SELECT * FROM autopilot_continuation_proposals WHERE proposal_id=?
                AND novel_id=? AND structure_change_id=?""", (continuation_id, payload["novel_id"], proposal["proposal_id"])).fetchone()
            if (payload.get("rollback") or not continuation or continuation["status"] != "awaiting_structure_review"
                    or continuation["run_epoch"] != payload["run_epoch"] or not novel
                    or novel["current_stage"] != "paused_for_review"):
                raise DataChangeConflict("续写结构尚未处于本次运行的作者确认状态")
        if not novel or novel["autopilot_run_epoch"] != payload["run_epoch"] or (not automatic_safe and novel["autopilot_status"] == "running" and novel["current_stage"] != "paused_for_review"):
            raise DataChangeConflict("运行状态或版本已变化，请刷新影响预览")
        if not payload.get("rollback") and proposal["run_epoch"] != payload["run_epoch"]:
            raise DataChangeConflict("提案所属运行已经变化，请重新预览")
        current = _snapshot(conn, payload["novel_id"])
        rolling_back = payload.get("rollback", False)
        expected_status = "applied" if rolling_back else "pending"
        expected_hash = proposal["after_hash"] if rolling_back else proposal["base_hash"]
        if proposal["status"] != expected_status or payload["expected_hash"] != expected_hash or _digest(current) != expected_hash:
            raise DataChangeConflict("提案或作品版本已变化；不会覆盖当前正文，请重新生成预览")
        if payload["proposal_hash"] != proposal["proposal_hash"] or _digest(json.loads(proposal["proposal_json"])) != proposal["proposal_hash"]:
            raise DataChangeConflict("确认对象与提案不一致")
        if not rolling_back and time.time() > proposal["expires_at"]:
            raise DataChangeConflict("提案已过期，请重新生成影响预览")
        before = json.loads(proposal["snapshot_json"])
        if _digest(before) != proposal["base_hash"]:
            raise DataChangeConflict("保护快照校验失败")
        if rolling_back:
            _restore(conn, before, current)
            state = _snapshot(conn, payload["novel_id"])
            comparison = json.loads(_json(state))
            # Resurrecting a deleted chapter creates a new monotonic revision;
            # retaining its former token would permit an author/worker ABA.
            original_chapters = {row["id"]: row for row in before.get("chapters", {}).get("rows", [])}
            surviving = {row["id"] for row in current.get("chapters", {}).get("rows", [])}
            for row in comparison.get("chapters", {}).get("rows", []):
                if row["id"] in original_chapters and row["id"] not in surviving:
                    old_revision = original_chapters[row["id"]].get("revision", 0)
                    if row.get("revision", -1) <= old_revision:
                        raise DataChangeConflict("恢复正文未获得新版本，事务已中止")
                    row["revision"] = old_revision
            if "chapters" in comparison:
                comparison["chapters"]["rows"].sort(key=_json)
            if _digest(comparison) != proposal["base_hash"]:
                mismatches = []
                for table in before.keys() | comparison.keys():
                    if before.get(table) != comparison.get(table):
                        old = before.get(table, {}).get("rows", [])
                        new = comparison.get(table, {}).get("rows", [])
                        columns = sorted({column for previous, current_row in zip(old, new) for column in previous if previous[column] != current_row.get(column)})
                        mismatches.append(f"{table}[{len(old)}→{len(new)}]:{','.join(columns)}")
                raise DataChangeConflict("恢复结果未完整匹配保护快照，事务已中止：" + "; ".join(mismatches))
            conn.execute("UPDATE data_change_proposals SET status='rolled_back',rolled_back_at=? WHERE proposal_id=?", (datetime.now(timezone.utc).isoformat(), proposal["proposal_id"]))
        else:
            _apply(conn, payload["novel_id"], action)
            state = _snapshot(conn, payload["novel_id"])
            conn.execute("UPDATE data_change_proposals SET status='applied',after_json=?,after_hash=?,applied_at=? WHERE proposal_id=?",
                (_json(state), _digest(state), datetime.now(timezone.utc).isoformat(), proposal["proposal_id"]))
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise DataChangeConflict("变更后外键校验失败，全部变更已撤销")
        audit_id = uuid4().hex
        conn.execute("INSERT INTO data_change_audits VALUES (?,?,?,?,?,?,?)", (audit_id, proposal["proposal_id"], payload["novel_id"], "rolled_back" if rolling_back else "applied",
            proposal["snapshot_id"], _digest(state), datetime.now(timezone.utc).isoformat()))
        if continuation_id:
            confirmed = datetime.now(timezone.utc).isoformat()
            conn.execute("""UPDATE autopilot_continuation_proposals SET status='applied',structure_confirmed_at=?
                WHERE proposal_id=? AND novel_id=? AND status='awaiting_structure_review' AND run_epoch=?""",
                (confirmed, continuation_id, payload["novel_id"], payload["run_epoch"]))
            conn.execute("""UPDATE novels SET current_stage='act_planning',last_stable_stage='act_planning',
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND autopilot_run_epoch=?""", (payload["novel_id"], payload["run_epoch"]))
        conn.commit()
        return {"status": "rolled_back" if rolling_back else "applied", "proposal_id": proposal["proposal_id"], "snapshot_id": proposal["snapshot_id"], "audit_id": audit_id, "state_hash": _digest(state)}
    except Exception:
        conn.rollback()
        raise


class DataChangeJournal:
    def __init__(self, db):
        self.db = db

    def _read(self, novel_id):
        uri = __import__("pathlib").Path(self.db.db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            novel = conn.execute("SELECT id,autopilot_status,current_stage,autopilot_run_epoch FROM novels WHERE id=?", (novel_id,)).fetchone()
            if novel is None:
                raise LookupError("作品不存在")
            return dict(novel), _snapshot(conn, novel_id)

    def create(self, novel_id, action, ttl_seconds=900, *, _snapshot_bundle=None):
        novel, state = _snapshot_bundle or self._read(novel_id)
        if novel["autopilot_status"] == "running" and novel["current_stage"] != "paused_for_review" and not (action.get("automatic_safe") and action["operation"] == "macro_merge"):
            raise DataChangeConflict("请先暂停生成，再预览结构变更")
        chapters = {row["id"]: row for row in state.get("chapters", {}).get("rows", [])}
        impact = [{"id": chapter_id, "number": chapters[chapter_id]["number"], "title": chapters[chapter_id].get("title"),
                   "has_content": bool((chapters[chapter_id].get("content") or "").strip()), "revision": chapters[chapter_id].get("revision", 0)}
                  for chapter_id in action.get("chapter_ids", []) if chapter_id in chapters]
        action = {**action, "affected_chapters": impact}
        proposal_id, snapshot_id, audit_id = uuid4().hex, uuid4().hex, uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as writer:
            writer.execute("""INSERT INTO data_change_proposals(proposal_id,novel_id,operation,proposal_json,proposal_hash,base_hash,
                snapshot_id,snapshot_json,run_epoch,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, novel_id, action["operation"], _json(action), _digest(action), _digest(state), snapshot_id, _json(state),
                 novel["autopilot_run_epoch"], created, time.time() + ttl_seconds))
            writer.execute("INSERT INTO data_change_audits VALUES (?,?,?,?,?,?,?)", (audit_id, proposal_id, novel_id, "proposed", snapshot_id, _digest(state), created))
        return self.get(novel_id, proposal_id)

    def propose_macro(self, novel_id, new_nodes, *, allow_running=False, continuation_proposal_id=None):
        from application.audit.services.macro_merge_engine import MacroMergeEngine, MergeConflictException
        novel, state = self._read(novel_id)
        old_nodes = state.get("story_nodes", {}).get("rows", [])
        if len(new_nodes) > 5000 or len(old_nodes) > 5000:
            raise ValueError("结构节点超过5000个，请缩小规划范围")
        if any(node.get("novel_id") != novel_id or node.get("node_type") not in {"part", "volume", "act"} for node in new_nodes):
            raise ValueError("宏观提案只允许本作品的部、卷、幕节点")
        plan = MacroMergeEngine(old_nodes, new_nodes).execute_diff()
        if plan.has_fatal_conflict:
            raise MergeConflictException("新结构与已有正文冲突，保护快照前已阻断", plan.conflicts)
        return self.create(novel_id, {"operation": "macro_merge", "creates": plan.creates, "updates": plan.updates,
            "deletes": plan.deletes, "summary": plan.summary, "automatic_safe": bool(allow_running),
            **({"continuation_proposal_id": continuation_proposal_id} if continuation_proposal_id else {})}, _snapshot_bundle=(novel, state))

    def propose_delete(self, novel_id, node_id):
        novel, state = self._read(novel_id)
        nodes = state.get("story_nodes", {}).get("rows", [])
        if not any(node["id"] == node_id for node in nodes):
            raise LookupError("本作品中没有此结构节点")
        ids = {node_id}
        for _ in range(len(nodes)):
            enlarged = ids | {node["id"] for node in nodes if node.get("parent_id") in ids}
            if enlarged == ids:
                break
            ids = enlarged
        numbers = {node["number"] for node in nodes if node["id"] in ids and node["node_type"] == "chapter"}
        chapter_ids = [row["id"] for row in state.get("chapters", {}).get("rows", []) if row["number"] in numbers]
        return self.create(novel_id, {"operation": "delete_nodes", "node_ids": sorted(ids), "chapter_ids": chapter_ids}, _snapshot_bundle=(novel, state))

    def propose_snapshot_rollback(self, novel_id, snapshot_id):
        snapshot = self.db.fetch_one("SELECT chapter_pointers FROM novel_snapshots WHERE id=? AND novel_id=?", (snapshot_id, novel_id))
        if snapshot is None:
            raise LookupError("本作品中没有此快照")
        pointers = json.loads(snapshot["chapter_pointers"])
        if not isinstance(pointers, list) or not all(isinstance(item, str) for item in pointers):
            raise ValueError("快照章节指针无效")
        novel, state = self._read(novel_id)
        removed = [row for row in state.get("chapters", {}).get("rows", []) if row["id"] not in pointers]
        numbers = {row["number"] for row in removed}
        node_ids = [node["id"] for node in state.get("story_nodes", {}).get("rows", []) if node["node_type"] == "chapter" and node["number"] in numbers]
        return self.create(novel_id, {"operation": "snapshot_rollback", "target_snapshot_id": snapshot_id,
            "chapter_ids": [row["id"] for row in removed], "node_ids": node_ids}, _snapshot_bundle=(novel, state))

    def get(self, novel_id, proposal_id):
        proposal = self.db.fetch_one("SELECT * FROM data_change_proposals WHERE proposal_id=? AND novel_id=?", (proposal_id, novel_id))
        if proposal is None:
            raise LookupError("变更提案不存在")
        result = dict(proposal)
        result["proposal"] = json.loads(result.pop("proposal_json"))
        result.pop("snapshot_json")
        result.pop("after_json")
        result["expired"] = result["status"] == "pending" and time.time() > result["expires_at"]
        current = self.db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id=?", (novel_id,))
        result["current_run_epoch"] = current["autopilot_run_epoch"] if current else None
        result["audits"] = self.db.fetch_all("SELECT * FROM data_change_audits WHERE proposal_id=? ORDER BY created_at", (proposal_id,))
        return result

    def list(self, novel_id):
        return [self.get(novel_id, row["proposal_id"]) for row in self.db.fetch_all("SELECT proposal_id FROM data_change_proposals WHERE novel_id=? ORDER BY created_at DESC LIMIT 100", (novel_id,))]

    def apply(self, novel_id, proposal_id, *, expected_hash, proposal_hash, run_epoch, rollback=False):
        from application.engine.services.persistence_queue import get_persistence_queue
        try:
            return get_persistence_queue().push_and_wait(COMMAND, {"db_path": self.db.db_path, "novel_id": novel_id, "proposal_id": proposal_id,
                "expected_hash": expected_hash, "proposal_hash": proposal_hash, "run_epoch": run_epoch, "rollback": rollback}).result
        except Exception as exc:
            from application.engine.services.persistence_queue import PersistenceCommitTimeout
            if isinstance(exc, PersistenceCommitTimeout):
                raise
            raise DataChangeConflict(str(exc)) from exc
