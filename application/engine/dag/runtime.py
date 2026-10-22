"""API-owned production chapter DAG jobs with durable evidence and ownership.

The existing scheduler, repositories, CPMS, scanner and writer are reused. A
running job owns the same SQLite lease as Autopilot. A start ACK follows both
lease acquisition and committed job creation; a stop ACK follows task cleanup.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from application.engine.dag.chapter_services import DAGChapterServices, content_hash
from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import DAGDefinition, DAGRunResult, EdgeCondition, NodeConfig, NodeResult, NodeStatus, get_default_dag
from application.engine.dag.registry import NodeRegistry
from application.engine.services.execution_lease import claim_execution_lease, get_execution_lease, refresh_execution_lease, release_execution_lease
from infrastructure.persistence.database.sqlite_dag_version_repository import SqliteDAGVersionRepository


def _now():
    return datetime.now(timezone.utc).isoformat()


class DAGRuntimeConflict(RuntimeError):
    pass


class DAGJobStore:
    def __init__(self, db):
        self.db = db
        self._ready = False

    def ensure(self):
        if self._ready:
            return
        self.db.execute("""CREATE TABLE IF NOT EXISTS dag_jobs (
            id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
            chapter_number INTEGER NOT NULL, status TEXT NOT NULL,
            graph_json TEXT NOT NULL, initial_json TEXT NOT NULL, result_json TEXT,
            started_at TEXT NOT NULL, completed_at TEXT, error TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS dag_jobs_novel ON dag_jobs(novel_id, started_at DESC)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS dag_job_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            novel_id TEXT NOT NULL, event_json TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES dag_jobs(id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS dag_events_run ON dag_job_events(run_id, sequence)")
        self.db.commit()
        self._ready = True

    def create(self, run_id, novel_id, chapter, dag, initial):
        self.ensure()
        self.db.execute("""INSERT INTO dag_jobs(id,novel_id,chapter_id,chapter_number,status,
            graph_json,initial_json,started_at) VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, novel_id, chapter["id"], chapter["number"], "running", dag.model_dump_json(),
             json.dumps(initial, ensure_ascii=False), _now()))
        self.db.commit()

    def event(self, run_id, novel_id, event):
        cursor = self.db.execute("INSERT INTO dag_job_events(run_id,novel_id,event_json,created_at) VALUES (?,?,?,?)",
            (run_id, novel_id, json.dumps(event, ensure_ascii=False), _now()))
        self.db.commit()
        return cursor.lastrowid

    def finish(self, run_id, result, error=None):
        self.db.execute("UPDATE dag_jobs SET status=?,result_json=?,completed_at=?,error=? WHERE id=?",
            (result.status, result.model_dump_json(), _now(), error, run_id))
        self.db.commit()

    def latest(self, novel_id):
        self.ensure()
        return self.db.fetch_one("SELECT * FROM dag_jobs WHERE novel_id=? ORDER BY started_at DESC LIMIT 1", (novel_id,))

    def get(self, novel_id, run_id):
        self.ensure()
        return self.db.fetch_one("SELECT * FROM dag_jobs WHERE novel_id=? AND id=?", (novel_id, run_id))

    def events(self, run_id, after=0):
        return [{"sequence": row["sequence"], **json.loads(row["event_json"])} for row in self.db.fetch_all(
            "SELECT sequence,event_json FROM dag_job_events WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, after))]

    def history(self, novel_id, limit=20):
        self.ensure()
        return self.db.fetch_all("SELECT id,chapter_number,status,started_at,completed_at,error FROM dag_jobs WHERE novel_id=? ORDER BY started_at DESC LIMIT ?", (novel_id, limit))


class DAGExecutionManager:
    REQUIRED = {"exec_writer", "val_style", "val_tension", "val_anti_ai", "gw_retry", "exec_commit"}

    @classmethod
    def validate_production(cls, dag):
        errors = DAGEngine().validate(dag)
        required = {}
        for kind in sorted(cls.REQUIRED):
            nodes = [node for node in dag.nodes if node.type == kind and node.enabled]
            if len(nodes) != 1:
                errors.append(f"必要节点 {kind} 必须启用且唯一")
            else:
                required[kind] = nodes[0].id
        if errors:
            return errors
        enabled = {node.id for node in dag.nodes if node.enabled}
        memo = {}
        def guaranteed(node_id):
            if node_id in memo:
                return memo[node_id]
            incoming = [edge for edge in dag.edges if edge.target == node_id and edge.source in enabled]
            mandatory = set()
            alternatives = []
            for edge in incoming:
                prerequisites = {edge.source} | guaranteed(edge.source)
                if edge.condition == EdgeCondition.ALWAYS:
                    mandatory.update(prerequisites)
                else:
                    alternatives.append(prerequisites)
            # Conditional incoming branches are alternatives. Only prerequisites
            # common to every branch are guaranteed to run before the target.
            if alternatives:
                mandatory.update(set.intersection(*alternatives))
            memo[node_id] = mandatory
            return mandatory
        for kind in ("val_style", "val_tension", "val_anti_ai"):
            if required['exec_writer'] not in guaranteed(required[kind]):
                errors.append(f"必要审计 {kind} 必须依赖正文生成成功")
            if required[kind] not in guaranteed(required['gw_retry']):
                errors.append(f"提交门控必须依赖必要审计 {kind}")
        if required['gw_retry'] not in guaranteed(required['exec_commit']):
            errors.append("正文提交必须依赖审计/重写门控 gw_retry")
        return errors

    def __init__(self, db, llm, *, publish: Callable | None = None, services_factory=None, lease_seconds=120):
        self.db, self.llm, self.publish = db, llm, publish
        self.store, self.versions = DAGJobStore(db), SqliteDAGVersionRepository(db)
        self.services_factory = services_factory or DAGChapterServices
        self.lease_seconds = lease_seconds
        self.tasks: dict[str, asyncio.Task] = {}
        self.run_ids: dict[str, str] = {}
        self.live: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def load_dag(self, novel_id):
        if not self.db.fetch_one("SELECT id FROM novels WHERE id=?", (novel_id,)):
            raise LookupError("小说不存在")
        existing = self.versions.get_latest(novel_id)
        if existing and existing.get_node("exec_commit"):
            return existing
        # Prior persisted definitions were display-only. Preserve applicable
        # settings while adopting the validated production commit topology.
        dag = get_default_dag()
        dag.id = f"dag_{novel_id}"
        if existing:
            for node in dag.nodes:
                before = existing.get_node(node.id)
                if before:
                    node.config = before.config.model_copy(deep=True)
                    node.enabled = before.enabled if node.type not in self.REQUIRED and NodeRegistry.get_meta(node.type).can_disable else True
        self.versions.save(novel_id, dag)
        saved = self.versions.get_latest(novel_id)
        if saved is None or saved.fingerprint() != dag.fingerprint():
            raise RuntimeError("DAG 配置提交后回读不一致")
        return saved

    async def configure(self, novel_id, node_id, *, patch=None, toggle=False):
        async with self._lock:
            if await asyncio.to_thread(get_execution_lease, novel_id, self.db):
                raise DAGRuntimeConflict("小说正在运行；停止并确认退出后再修改 DAG 配置")
            dag = await asyncio.to_thread(self.load_dag, novel_id)
            node = dag.get_node(node_id)
            if node is None:
                raise LookupError("节点不存在")
            meta = NodeRegistry.get_meta(node.type)
            if toggle:
                if node.type in self.REQUIRED or not meta.can_disable:
                    raise ValueError("必要执行/审计/提交节点不可禁用")
                node.enabled = not node.enabled
            else:
                if not meta.is_configurable:
                    raise ValueError("此节点不支持修改配置")
                node.config = NodeConfig.model_validate({**node.config.model_dump(), **(patch or {})})
            errors = self.validate_production(dag)
            if errors:
                raise ValueError("; ".join(errors))
            await asyncio.to_thread(self.versions.save, novel_id, dag)
            saved = await asyncio.to_thread(self.versions.get_latest, novel_id)
            if saved.fingerprint() != dag.fingerprint():
                raise RuntimeError("配置提交后没有得到一致回读")
            return saved

    def _chapter(self, novel_id, chapter_number=None):
        novel = self.db.fetch_one("SELECT * FROM novels WHERE id=?", (novel_id,))
        if novel is None:
            raise LookupError("小说不存在")
        from application.core.taxonomy.market_prompt import load_verified_market_profile
        snapshot, profile_block = load_verified_market_profile(self.db, novel_id)
        if chapter_number is None:
            chapter = self.db.fetch_one("SELECT * FROM chapters WHERE novel_id=? AND status='draft' AND TRIM(COALESCE(outline,''))<>'' ORDER BY number LIMIT 1", (novel_id,))
        else:
            chapter = self.db.fetch_one("SELECT * FROM chapters WHERE novel_id=? AND number=?", (novel_id, chapter_number))
        if chapter is None:
            raise ValueError("没有可运行的已规划草稿章节；请先创建章纲或选择章节")
        if chapter["status"] == "completed":
            raise DAGRuntimeConflict("已完成章节不能自动覆盖，请先明确进入修订流程")
        if not (chapter.get("outline") or "").strip():
            raise ValueError("章节没有章纲，不能启动正文生成")
        previous = self.db.fetch_one("SELECT tension_score FROM chapters WHERE novel_id=? AND number<? ORDER BY number DESC LIMIT 1", (novel_id, chapter["number"]))
        return chapter, {"novel_id": novel_id, "chapter_id": chapter["id"], "chapter_number": chapter["number"],
            "outline": chapter["outline"], "chapter_before": chapter,
            "market_profile_snapshot": snapshot, "genre_profile_block": profile_block,
            "target_chapter_words": novel.get("target_words_per_chapter") or 2500,
            "prev_tension": previous["tension_score"] if previous else 50, "attempt": 0}

    async def start(self, novel_id, chapter_number=None, *, resume_run_id=None):
        async with self._lock:
            run_id = f"dag_{uuid.uuid4().hex}"
            if novel_id in self.tasks and not self.tasks[novel_id].done():
                raise DAGRuntimeConflict("本小说已有 DAG 任务正在运行")
            claimed = await asyncio.to_thread(claim_execution_lease, novel_id, "dag", run_id, self.lease_seconds, self.db)
            if not claimed:
                raise DAGRuntimeConflict("本小说正在由 DAG 或全托管执行，不能并发启动")
            try:
                dag = await asyncio.to_thread(self.load_dag, novel_id)
                errors = self.validate_production(dag)
                if errors:
                    raise ValueError("; ".join(errors))
                previous = None
                if resume_run_id:
                    previous = await asyncio.to_thread(self.store.get, novel_id, resume_run_id)
                    if not previous or previous["status"] in {"completed", "awaiting_review"}:
                        raise DAGRuntimeConflict("只能从已结束的失败/取消任务恢复候选正文")
                    # Claiming this new run's lease already proves that a prior
                    # running job has no valid owner. A process crash leaves its
                    # durable status running; status() exposes it as interrupted.
                    # Recover its candidate through all gates rather than making
                    # that interrupted job permanently impossible to resume.
                    if previous["status"] in {"running", "stopping"}:
                        lease = await asyncio.to_thread(get_execution_lease, novel_id, self.db)
                        if not lease or lease["owner"] != "dag" or lease["job_id"] != run_id:
                            raise DAGRuntimeConflict("旧运行仍有执行所有者，不能恢复")
                    if chapter_number is not None and chapter_number != previous["chapter_number"]:
                        raise DAGRuntimeConflict("恢复任务与指定章节不一致")
                    chapter_number = previous["chapter_number"]
                chapter, initial = await asyncio.to_thread(self._chapter, novel_id, chapter_number)
                if previous is not None:
                    original = json.loads(previous["initial_json"])["chapter_before"]
                    if any(chapter.get(key) != original.get(key) for key in ("id", "content", "outline", "status", "revision", "content_hash")):
                        raise DAGRuntimeConflict("章节已变化，旧候选不能用于自动恢复")
                    events = await asyncio.to_thread(self.store.events, resume_run_id)
                    candidates = [e["outputs"]["content"] for e in events if e.get("outputs", {}).get("content")]
                    if not candidates:
                        raise ValueError("该任务没有可恢复候选正文")
                    initial["resume_content"] = candidates[-1]
                    evidence = [e["outputs"]["anti_ai_prompt_evidence"] for e in events
                        if e.get("outputs", {}).get("content") == candidates[-1] and e["outputs"].get("anti_ai_prompt_evidence")]
                    if evidence:
                        prior = evidence[-1]
                        initial["resume_prompt_evidence"] = prior.get("original_prompt_evidence", prior)
                    initial["resumed_from"] = resume_run_id
                initial["dag_run_id"] = run_id
                await asyncio.to_thread(self.store.create, run_id, novel_id, chapter, dag, initial)
                self.live[novel_id] = {"status": "running", "dag_run_id": run_id, "chapter_number": chapter["number"], "node_states": {}}
                self.run_ids[novel_id] = run_id
                self.tasks[novel_id] = asyncio.create_task(self._run(dag.model_copy(deep=True), initial), name=run_id)
                return {"status": "started", "dag_run_id": run_id, "novel_id": novel_id, "chapter_number": chapter["number"], "version": dag.version}
            except BaseException:
                await asyncio.to_thread(release_execution_lease, novel_id, "dag", run_id, self.db)
                raise

    async def stop(self, novel_id, run_id=None):
        novel = await asyncio.to_thread(self.db.fetch_one, 'SELECT id FROM novels WHERE id=?', (novel_id,))
        if novel is None:
            raise LookupError('小说不存在')
        task = self.tasks.get(novel_id)
        current = self.run_ids.get(novel_id)
        if run_id and current and run_id != current:
            raise DAGRuntimeConflict("停止请求的运行编号已过期")
        if task is None or task.done():
            lease = await asyncio.to_thread(get_execution_lease, novel_id, self.db)
            if lease:
                raise DAGRuntimeConflict("此进程未持有运行任务，不能伪造停止确认")
            return {"status": "stopped", "novel_id": novel_id, "dag_run_id": current, "quiescent": True}
        if self.live[novel_id]["status"] != "stopping":
            self.live[novel_id]["status"] = "stopping"
            task.cancel()
        # The commit adapter shields queued SQL until its ACK. Do not time out
        # this await and release ownership while a chapter write remains alive.
        await asyncio.gather(task, return_exceptions=True)
        return {"status": "stopped", "novel_id": novel_id, "dag_run_id": current, "quiescent": True}

    async def _run(self, dag, initial):
        novel_id, run_id, start = initial["novel_id"], initial["dag_run_id"], time.monotonic()
        states = self.live[novel_id]["node_states"]
        service = None
        lease_error = None

        async def event(data):
            if data.get("node_id"):
                states[data["node_id"]] = {**data, "enabled": bool(dag.get_node(data["node_id"]).enabled)}
            data["chapter_number"] = initial["chapter_number"]
            data["sequence"] = await asyncio.to_thread(self.store.event, run_id, novel_id, data)
            if self.publish:
                self.publish(novel_id, data)
                if data.get("outputs") and data.get("node_id"):
                    self.publish(novel_id, {**data, "type": "node_output"})

        owner = asyncio.current_task()

        async def renew():
            nonlocal lease_error
            while True:
                await asyncio.sleep(max(0.05, self.lease_seconds / 3))
                try:
                    ok = await asyncio.to_thread(refresh_execution_lease, novel_id, "dag", run_id, self.lease_seconds, self.db)
                    if not ok:
                        raise DAGRuntimeConflict("DAG 执行租约已丢失")
                except Exception as exc:
                    lease_error = str(exc)
                    owner.cancel()
                    return

        heartbeat = asyncio.create_task(renew())
        result = None
        try:
            service = self.services_factory(self.db, self.llm, dag=dag)
            state = {**initial, "llm_service": self.llm, "services": service, "on_node_event": event}
            result = await DAGEngine().run(dag, state, run_id)
            # Events include re-audits. Their latest terminal outcomes, rather
            # than the original pre-rewrite scores, describe the saved body.
            result.node_results.update({nid: NodeResult.model_validate(data) for nid, data in states.items()
                if data["status"] not in {"running", "pending"}})
            result.error_count = sum(r.status == NodeStatus.ERROR for r in result.node_results.values())
            commit = getattr(service, "commit_result", None)
            if commit:
                result.final_state.update(commit.outputs)
            if result.status == "completed" and not result.final_state.get("chapter_committed"):
                result.status = "error"
                lease_error = "工作流结束但没有章节提交确认"
            elif result.final_state.get("chapter_committed") and result.final_state.get("review_required") and not result.error_count:
                result.status = "awaiting_review"
        except asyncio.CancelledError:
            result = DAGRunResult(dag_run_id=run_id, novel_id=novel_id, status="error" if lease_error else "cancelled",
                started_at=_now(), total_duration_ms=int((time.monotonic() - start) * 1000))
            commit = getattr(service, "commit_result", None)
            if commit:
                result.final_state.update(commit.outputs)
                result.node_results["exec_commit"] = commit
        except Exception as exc:
            lease_error = str(exc)
            result = DAGRunResult(dag_run_id=run_id, novel_id=novel_id, status="error", started_at=_now())
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            for node in dag.nodes:
                snapshot = states.get(node.id)
                if snapshot and snapshot["status"] in {"running", "pending"}:
                    from application.engine.dag.execution_evidence import did_execute, receipt_for, unexecuted_result
                    from application.engine.dag.models import NodeExecutionReceipt
                    prior = snapshot.get("execution_receipt")
                    receipt = NodeExecutionReceipt.model_validate(prior) if prior else receipt_for(node.id, node.type, initial)
                    receipt.executed = receipt.executor_started
                    receipt.executor_completed = False
                    receipt.completed_at = _now()
                    receipt.reason = lease_error or "任务已取消，执行未完成" if receipt.executor_started else "任务已取消，执行器未开始"
                    final = NodeResult(status=NodeStatus.ERROR, error=lease_error or "任务已取消")
                    final.execution_receipt = receipt
                    try:
                        await event({"type": "node_status_change", "node_id": node.id, "novel_id": novel_id,
                            "dag_run_id": run_id, "timestamp": _now(), "source": "dag_runner", "executed": did_execute(final), **final.model_dump(mode="json")})
                    except Exception as exc:
                        lease_error = f"节点终态归档失败: {exc}"
                        result.status = "error"
                if node.id in states and node.id not in result.node_results:
                    result.node_results[node.id] = NodeResult.model_validate(states[node.id])
                elif node.id not in result.node_results:
                    from application.engine.dag.execution_evidence import unexecuted_result
                    result.node_results[node.id] = unexecuted_result(NodeResult(status=NodeStatus.DISABLED if not node.enabled else NodeStatus.BYPASSED,
                        error="任务结束前未执行"), node, initial, "任务结束前未执行")
            result.completed_at = _now()
            result.total_duration_ms = int((time.monotonic() - start) * 1000)
            result.error_count = sum(r.status == NodeStatus.ERROR for r in result.node_results.values())
            # Always release only the exact run after finishing all owned work.
            # Failure of the durable result write is itself a visible run error.
            try:
                await asyncio.to_thread(self.store.finish, run_id, result, lease_error)
            except Exception as exc:
                result.status = "error"
                lease_error = f"运行结果归档失败: {exc}"
            finally:
                try:
                    await asyncio.to_thread(release_execution_lease, novel_id, "dag", run_id, self.db)
                except Exception as exc:
                    result.status = "error"
                    lease_error = f"运行已退出，但租约释放没有确认: {exc}"
                self.live[novel_id].update(status=result.status, result=result.model_dump(mode="json"), error=lease_error)
                if self.publish:
                    self.publish(novel_id, {"type": "dag_run_complete", **result.model_dump(mode="json"), "error": lease_error})

    async def status(self, novel_id):
        live = self.live.get(novel_id)
        if live:
            return live
        job = await asyncio.to_thread(self.store.latest, novel_id)
        if not job:
            return None
        events = await asyncio.to_thread(self.store.events, job["id"])
        states = {event["node_id"]: {**event, "enabled": True} for event in events if event.get("node_id")}
        status = job["status"]
        if status in {"running", "stopping"}:
            lease = await asyncio.to_thread(get_execution_lease, novel_id, self.db)
            if lease is None or lease["job_id"] != job["id"]:
                status = "interrupted"
        return {"status": status, "dag_run_id": job["id"], "chapter_number": job["chapter_number"],
            "node_states": states, "result": json.loads(job["result_json"]) if job["result_json"] else None,
            "error": job["error"] or ("服务重启或租约丢失，候选正文与执行事件已保留" if status == "interrupted" else None)}

    async def shutdown(self):
        await asyncio.gather(*(self.stop(novel_id) for novel_id in list(self.tasks)), return_exceptions=True)
