"""Durable commands; only daemon execution may acknowledge running/stopped."""
import json
import logging
import os
import threading
import time
import uuid

from application.engine.services.execution_lease import (
    get_execution_lease, refresh_execution_lease, release_execution_lease, ensure_execution_schema,
)

logger = logging.getLogger(__name__)
_processing = set()
_lock = threading.RLock()
_command_lock = threading.RLock()
_ready = set()
_last_lease_refresh = 0.0
# A provider request can legitimately exceed two minutes (especially through
# a proxy). The daemon refreshes between stages, but it cannot refresh while
# awaiting a single model response, so the autopilot reservation must cover a
# bounded long call. Stop/replacement commands still fence it by job id.
AUTOPILOT_LEASE_TTL_SECONDS = 900


def _db(db=None):
    if db is not None:
        return db
    from infrastructure.persistence.database.connection import get_database
    return get_database()


def ensure_control_schema(db=None):
    db = _db(db)
    key = (getattr(db, "db_path", None), id(db))
    with _lock:
        if key in _ready:
            return
        db.execute("""CREATE TABLE IF NOT EXISTS autopilot_commands (
            command_id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, command_type TEXT NOT NULL,
            run_epoch INTEGER NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
            requested_at REAL NOT NULL, executed_at REAL, executor_pid INTEGER, error TEXT,
            UNIQUE(novel_id, run_epoch))""")
        db.commit()
        _ready.add(key)


def submit_command(novel_id, command_type, payload, db=None, *, command_id=None):
    """Lease, epoch and journal row commit together; no API-side RUNNING ACK."""
    if command_type not in {"start", "resume", "stop"}:
        raise ValueError("不支持的控制命令")
    db = _db(db)
    ensure_control_schema(db)
    ensure_execution_schema(db)
    command_id = command_id or uuid.uuid4().hex
    existing = get_command(command_id, db=db)
    if existing:
        if existing["novel_id"] != novel_id or existing["command_type"] != command_type:
            raise ValueError("命令 ID 与原请求不一致")
        return existing
    now = time.time()
    guard = "NOT EXISTS (SELECT 1 FROM novel_execution_leases l WHERE l.novel_id=novels.id AND l.owner!='autopilot' AND l.expires_at>?)"
    guard_params = (now,)
    with db.transaction() as conn:
        if command_type in {"start", "resume"}:
            conn.execute("""INSERT INTO novel_execution_leases (novel_id,owner,job_id,expires_at,updated_at)
                SELECT id,'autopilot',?,?,? FROM novels
                WHERE id=? AND (autopilot_status!='running' OR current_stage IN ('paused_for_review','reviewing'))
                ON CONFLICT(novel_id) DO UPDATE SET owner=excluded.owner, job_id=excluded.job_id,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at
                WHERE novel_execution_leases.expires_at<=? OR novel_execution_leases.owner='autopilot'""",
                (command_id, now + AUTOPILOT_LEASE_TTL_SECONDS, now, novel_id, now))
            guard = "EXISTS (SELECT 1 FROM novel_execution_leases l WHERE l.novel_id=novels.id AND l.owner='autopilot' AND l.job_id=? AND l.expires_at>?)"
            guard_params = (command_id, now)
        epoch_cursor = conn.execute(f"""UPDATE novels
            SET autopilot_run_epoch=COALESCE(autopilot_run_epoch,0)+1, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND {guard}""", (novel_id, *guard_params))
        conn.execute(f"""INSERT INTO autopilot_commands
            (command_id,novel_id,command_type,run_epoch,payload_json,state,requested_at)
            SELECT ?,id,?,autopilot_run_epoch,?,'pending',? FROM novels WHERE id=? AND {guard}""",
            (command_id, command_type, json.dumps(payload, ensure_ascii=False), now, novel_id, *guard_params))
    if epoch_cursor.rowcount != 1:
        if db.fetch_one("SELECT id FROM novels WHERE id=?", (novel_id,)) is None:
            raise LookupError("小说不存在")
        raise ValueError("本书已有正在执行的任务，请先停止当前任务再操作")
    command = get_command(command_id, db=db)
    if command is None:
        raise RuntimeError("控制命令提交回执缺失")
    return command


def get_command(command_id, db=None):
    db = _db(db)
    try:
        row = db.fetch_one("SELECT * FROM autopilot_commands WHERE command_id=?", (command_id,))
        return dict(row) if row else None
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def latest_command(novel_id, db=None):
    db = _db(db)
    try:
        row = db.fetch_one("SELECT * FROM autopilot_commands WHERE novel_id=? ORDER BY run_epoch DESC LIMIT 1", (novel_id,))
        return dict(row) if row else None
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def stop_requested(novel_id, db=None):
    """A durable stop remains authoritative while the executor is quiescing."""
    db = _db(db)
    command = latest_command(novel_id, db=db)
    if not command or command["command_type"] != "stop" or command["state"] not in {"pending", "applied", "completed"}:
        return False
    row = db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id=?", (novel_id,))
    return bool(row and row["autopilot_run_epoch"] == command["run_epoch"])


def executor_identity(novel_id, db=None):
    """Read one authoritative ownership snapshot; never adopt another executor."""
    db = _db(db)
    row = db.fetch_one("""SELECT c.command_id AS job_id,c.run_epoch,c.executor_pid
        FROM novels n JOIN novel_execution_leases l ON l.novel_id=n.id
        JOIN autopilot_commands c ON c.command_id=l.job_id AND c.novel_id=n.id
        WHERE n.id=? AND n.autopilot_status='running' AND l.owner='autopilot'
            AND l.expires_at>? AND c.run_epoch=n.autopilot_run_epoch
            AND c.command_type IN ('start','resume') AND c.state='completed'
            AND c.executor_pid=?""", (novel_id, time.time(), os.getpid()))
    return dict(row) if row else None


def command_response(command):
    if not command:
        return {}
    return {
        "command_id": command["command_id"], "autopilot_run_epoch": command["run_epoch"],
        "command_type": command["command_type"], "command_status": command["state"],
        "accepted": True, "execution_confirmed": command["state"] == "completed",
        "command_error": command.get("error"), "command_requested_at": command["requested_at"],
        "command_executed_at": command.get("executed_at"),
        "command_executor_pid": command.get("executor_pid"),
        "command_wait_seconds": max(0.0, time.time() - command["requested_at"])
            if command["state"] in {"pending", "applied"} else 0.0,
    }


def _publish(novel_id, **fields):
    try:
        import sys
        shared = sys.modules.get("__shared_state")
        if shared is not None:
            key = f"novel:{novel_id}"
            value = dict(shared.get(key) or {})
            value.update(fields, _updated_at=time.time())
            shared[key] = value
            return
        from interfaces.runtime_state import update_shared_novel_state
        update_shared_novel_state(novel_id, **fields)
    except Exception as exc:
        logger.warning("控制执行状态投影失败 novel=%s: %s", novel_id, exc)


def mark_processing(novel_id):
    with _lock:
        _processing.add(novel_id)


def confirm_quiescent(novel_id, db=None):
    with _lock:
        _processing.discard(novel_id)
    return process_pending_commands(novel_id=novel_id, db=db)


def refresh_owned_leases(db=None):
    global _last_lease_refresh
    if time.monotonic() - _last_lease_refresh < 20:
        return set()
    _last_lease_refresh = time.monotonic()
    db = _db(db)
    lost = set()
    try:
        rows = db.fetch_all("""SELECT c.* FROM autopilot_commands c
            JOIN novel_execution_leases l ON l.novel_id=c.novel_id AND l.job_id=c.command_id AND l.owner='autopilot'
            WHERE c.executor_pid=? AND c.state='completed' AND c.command_type IN ('start','resume')""", (os.getpid(),))
        for row in rows:
            current = db.fetch_one("SELECT autopilot_status, autopilot_run_epoch FROM novels WHERE id=?", (row["novel_id"],))
            if current and current["autopilot_status"] == "running" and (
                current["autopilot_run_epoch"] == row["run_epoch"] or stop_requested(row["novel_id"], db=db)
            ):
                if not refresh_execution_lease(row["novel_id"], "autopilot", row["command_id"],
                                               ttl_seconds=AUTOPILOT_LEASE_TTL_SECONDS, db=db):
                    lost.add(row["novel_id"])
    except Exception as exc:
        logger.debug("控制租约续期失败: %s", exc)
        with _lock:
            lost.update(_processing)
    for nid in lost:
        from application.engine.services.novel_stop_signal import set_local_novel_stop
        set_local_novel_stop(nid)
    return lost


def process_pending_commands(novel_id=None, active_novel_ids=None, db=None):
    """Daemon-only hook. Stop ACK follows quiescence, not queue consumption."""
    with _command_lock:
        return _process_pending_commands(novel_id, active_novel_ids, db)


def _process_pending_commands(novel_id=None, active_novel_ids=None, db=None):
    db = _db(db)
    try:
        rows = db.fetch_all("SELECT * FROM autopilot_commands WHERE state IN ('pending','applied')" +
                            (" AND novel_id=?" if novel_id else "") + " ORDER BY run_epoch",
                            (novel_id,) if novel_id else ())
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    with _lock:
        active = set(_processing) | set(active_novel_ids or ())
    processed = []
    for raw in rows:
        command = dict(raw)
        nid, epoch = command["novel_id"], command["run_epoch"]
        current = db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id=?", (nid,))
        if not current or current["autopilot_run_epoch"] != epoch:
            db.execute("UPDATE autopilot_commands SET state='superseded' WHERE command_id=?", (command["command_id"],))
            db.commit()
            continue
        payload = json.loads(command["payload_json"])
        continuation_proposal_id = payload.get("continuation_proposal_id") if isinstance(payload, dict) else None
        if continuation_proposal_id and command["command_type"] in {"start", "resume"}:
            from application.engine.services import continuation_proposal_service as proposals
            proposal = proposals.get(continuation_proposal_id, nid, db)
            baseline = db.fetch_one("SELECT current_stage,target_chapters FROM novels WHERE id=?", (nid,))
            if (not proposal or proposal["status"] != "approved" or proposal["command_id"] != command["command_id"]
                    or proposal["base_run_epoch"] + 1 != epoch or not baseline or baseline["current_stage"] != "completed"
                    or baseline["target_chapters"] != proposal["base_target_chapters"]
                    or payload.get("next_stage") != "macro_planning"
                    or payload.get("target_chapters") != proposal["proposed_target_chapters"]):
                db.execute("UPDATE autopilot_commands SET state='failed',error='continuation approval or baseline changed' WHERE command_id=?", (command["command_id"],))
                db.commit()
                continue
        if command["command_type"] == "stop":
            from application.engine.services.novel_stop_signal import set_local_novel_stop
            set_local_novel_stop(nid)
            if nid in active:
                if command["state"] != "applied":
                    db.execute("UPDATE autopilot_commands SET state='applied', executor_pid=? WHERE command_id=?", (os.getpid(), command["command_id"]))
                    db.commit()
                    _publish(nid, **command_response(dict(command, state="applied")))
                continue
            lease_to_release = get_execution_lease(nid, db=db)
            fields, params = "autopilot_status='stopped'", ()
        else:
            if nid in active:
                continue
            # Submission already reserved ownership atomically. An expired
            # reservation must never restart work long after the user's request.
            if not refresh_execution_lease(nid, "autopilot", command["command_id"], db=db):
                error = "execution_reservation_expired: 执行预约已过期或归属已改变，请重新提交启动请求"
                db.execute("UPDATE autopilot_commands SET state='failed', error=?, executed_at=?, executor_pid=? WHERE command_id=?", (error, time.time(), os.getpid(), command["command_id"]))
                db.commit()
                # Remove the reservation that failed ownership validation. A
                # filtered expired row is still a durable conflict for future
                # submitters and can make the next start look unresponsive.
                release_execution_lease(nid, "autopilot", command["command_id"], db=db)
                logger.warning("控制命令执行失败 novel=%s command=%s: %s", nid, command["command_id"], error)
                _publish(nid, **command_response(dict(command, state="failed", error=error,
                                                      executor_pid=os.getpid(), executed_at=time.time())))
                continue
            from application.engine.services.novel_stop_signal import clear_local_novel_stop
            from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
            from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace
            policy = AutopilotRecoveryPolicy(db, workspace=ChapterGenerationWorkspace())
            policy.apply_transient_cleanup(policy.decide_on_start(nid))
            clear_local_novel_stop(nid)
            fields = """autopilot_status='running', current_stage=?, max_auto_chapters=?,
                target_chapters=?, target_words_per_chapter=?, current_act=?, consecutive_error_count=0,
                active_pipeline_step='', active_pipeline_run_id='', last_stable_stage=?"""
            params = (payload["next_stage"], payload["max_auto_chapters"], payload["target_chapters"],
                      payload["target_words_per_chapter"], payload.get("current_act", 0), payload["next_stage"])
        with db.transaction() as conn:
            ownership_guard = "" if command["command_type"] == "stop" else " AND EXISTS (SELECT 1 FROM novel_execution_leases l WHERE l.novel_id=novels.id AND l.owner='autopilot' AND l.job_id=? AND l.expires_at>?)"
            ownership_params = () if command["command_type"] == "stop" else (command["command_id"], time.time())
            continuation_guard = "" if not continuation_proposal_id else " AND EXISTS (SELECT 1 FROM autopilot_continuation_proposals p WHERE p.proposal_id=? AND p.novel_id=novels.id AND p.status='approved' AND p.command_id=? AND p.base_run_epoch+1=novels.autopilot_run_epoch)"
            continuation_params = () if not continuation_proposal_id else (continuation_proposal_id, command["command_id"])
            cursor = conn.execute(f"UPDATE novels SET {fields}, updated_at=CURRENT_TIMESTAMP WHERE id=? AND autopilot_run_epoch=?{ownership_guard}{continuation_guard}",
                                  (*params, nid, epoch, *ownership_params, *continuation_params))
            conn.execute(f"""UPDATE autopilot_commands SET state='completed', executed_at=?, executor_pid=?
                WHERE command_id=? AND state IN ('pending','applied') AND EXISTS
                (SELECT 1 FROM novels WHERE id=? AND autopilot_run_epoch=? AND autopilot_status=?{ownership_guard}{continuation_guard})""",
                (time.time(), os.getpid(), command["command_id"], nid, epoch, "stopped" if command["command_type"] == "stop" else "running", *ownership_params, *continuation_params))
            if continuation_proposal_id and command["command_type"] in {"start", "resume"}:
                # Starting the planner is distinct from applying reviewed structure.
                conn.execute(
                    "UPDATE autopilot_continuation_proposals SET status='macro_planning',run_epoch=? "
                    "WHERE proposal_id=? AND novel_id=? AND status='approved' AND command_id=? AND EXISTS "
                    "(SELECT 1 FROM novels WHERE id=? AND autopilot_run_epoch=? AND current_stage='macro_planning' AND autopilot_status='running')",
                    (epoch, continuation_proposal_id, nid, command["command_id"], nid, epoch),
                )
        if cursor.rowcount != 1:
            continue
        _publish(nid, autopilot_status="stopped" if command["command_type"] == "stop" else "running",
                 **({"current_stage": payload["next_stage"]} if command["command_type"] != "stop" else {}),
                 **command_response(dict(command, state="completed", executed_at=time.time(), executor_pid=os.getpid())))
        if command["command_type"] == "stop":
            if lease_to_release and lease_to_release["owner"] == "autopilot":
                release_execution_lease(nid, "autopilot", lease_to_release["job_id"], db=db)
        logger.info("控制命令执行完成 novel=%s command=%s type=%s epoch=%s executor_pid=%s",
                    nid, command["command_id"], command["command_type"], epoch, os.getpid())
        processed.append(command["command_id"])
    return processed
