"""AOF（Append Only File）崩溃恢复机制

核心设计：
1. 守护进程写作时，每个 chunk 追加写入 .draft 临时文件（无缓冲，fsync 保证落盘）
2. 章节完成后合并入 DB 并删除 .draft 文件
3. 启动时将残留 .draft 注册为 SQLite 持久恢复任务，CAS 恢复正文/断点后清理
4. job 持久保存原文、hash、恢复意图和提交 revision/hash，失败可查询/重试

文件路径：{DATA_DIR}/drafts/{novel_id}_ch{chapter_number}.draft
- 每行一个 chunk，UTF-8 编码
- 线程内以 RLock 协调 append 与清理；跨进程仍以提交校验/CAS 保留证据
- 章节完成后删除

性能影响：
- append 操作 ~10μs（远小于 DB 写入的 ~1ms）
- fsync 每 5 秒一次（而非每次 append，平衡安全与性能）
- 不涉及任何 SQLite 锁
"""
import os
import logging
import threading
import hashlib
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_draft_lock = threading.RLock()


def _draft_dir() -> Path:
    """获取 .draft 文件存储目录"""
    from application.paths import DATA_DIR
    draft_dir = DATA_DIR / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    return draft_dir


def _draft_path(novel_id: str, chapter_number: int) -> Path:
    """获取指定章节的 .draft 文件路径"""
    return _draft_dir() / f"{novel_id}_ch{chapter_number}.draft"


def _recovery_db():
    """Return the application database without retaining a second connection."""
    from application.paths import get_db_path
    from infrastructure.persistence.database.connection import get_database
    return get_database(get_db_path())


def _ensure_recovery_jobs_table(db=None) -> None:
    """Keep isolated/test databases usable when migrations were not run yet."""
    db = db or _recovery_db()
    if db.fetch_one("SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name='aof_recovery_jobs'"):
        return
    db.execute(
        """CREATE TABLE IF NOT EXISTS aof_recovery_jobs (
            job_id TEXT PRIMARY KEY,
            novel_id TEXT NOT NULL,
            chapter_number INTEGER NOT NULL,
            draft_path TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'recover',
            repair_resume_state INTEGER NOT NULL DEFAULT 0,
            committed_revision INTEGER,
            committed_content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            UNIQUE(novel_id, chapter_number, content_hash)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_aof_recovery_jobs_status "
        "ON aof_recovery_jobs(status, updated_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_aof_recovery_jobs_novel "
        "ON aof_recovery_jobs(novel_id, chapter_number, updated_at DESC)"
    )
    db.commit()


def _recovery_job_id(novel_id: str, chapter_number: int, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"{novel_id}\0{chapter_number}\0{digest}".encode("utf-8")
    ).hexdigest()[:32]


def _register_recovery_job(db, novel_id: str, chapter_number: int, path: Path, content: str, *, job_type="recover") -> str:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    job_id = _recovery_job_id(novel_id, chapter_number, content)
    current = db.fetch_one(
        "SELECT content,status FROM chapters WHERE novel_id=? AND number=?",
        (novel_id, chapter_number),
    )
    needs_repair = bool(job_type == "recover" and (
        not current or (current.get("status") != "completed"
                        and not str(current.get("content") or "").startswith(content))
    ))
    db.execute(
        """INSERT INTO aof_recovery_jobs
           (job_id, novel_id, chapter_number, draft_path, content, content_hash,
            job_type, repair_resume_state, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
           ON CONFLICT(novel_id, chapter_number, content_hash) DO UPDATE SET
             draft_path=excluded.draft_path,
             updated_at=CURRENT_TIMESTAMP""",
        (job_id, novel_id, chapter_number, str(path), content, content_hash, job_type, int(needs_repair)),
    )
    db.commit()
    return job_id


def list_recovery_jobs(*, novel_id: str | None = None, limit: int = 100) -> list[dict]:
    """Return durable AOF recovery state for UI/diagnostics after restart."""
    db = _recovery_db()
    _ensure_recovery_jobs_table(db)
    limit = max(1, min(500, int(limit)))
    if novel_id:
        return db.fetch_all(
            "SELECT job_id,novel_id,chapter_number,draft_path,content_hash,job_type,"
            "status,attempts,last_error,committed_revision,committed_content_hash,"
            "created_at,updated_at,completed_at FROM aof_recovery_jobs WHERE novel_id=? "
            "ORDER BY updated_at DESC LIMIT ?", (novel_id, limit)
        )
    return db.fetch_all(
        "SELECT job_id,novel_id,chapter_number,draft_path,content_hash,job_type,"
        "status,attempts,last_error,committed_revision,committed_content_hash,"
        "created_at,updated_at,completed_at FROM aof_recovery_jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
    )


def retry_recovery_job(job_id: str) -> dict | None:
    """Make one failed/conflicted AOF job pending; the next recovery pass owns it."""
    db = _recovery_db()
    _ensure_recovery_jobs_table(db)
    cursor = db.execute(
        """UPDATE aof_recovery_jobs
           SET status='pending', last_error='', updated_at=CURRENT_TIMESTAMP,
               completed_at=NULL
           WHERE job_id=? AND status IN ('failed','conflict','superseded')""",
        (str(job_id),),
    )
    db.commit()
    if cursor.rowcount != 1:
        return db.fetch_one("SELECT * FROM aof_recovery_jobs WHERE job_id=?", (str(job_id),))
    return db.fetch_one("SELECT * FROM aof_recovery_jobs WHERE job_id=?", (str(job_id),))


def _set_recovery_job_status(db, job_id: str, status: str, error: str = "", *, committed=None) -> None:
    completed = "CURRENT_TIMESTAMP" if status == "completed" else "NULL"
    db.execute(
        f"""UPDATE aof_recovery_jobs
            SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP,
                committed_revision=COALESCE(?,committed_revision),
                committed_content_hash=COALESCE(?,committed_content_hash),
                completed_at={completed}
            WHERE job_id=?""",
        (status, str(error)[:1000], (committed or {}).get("revision"), (committed or {}).get("content_hash"), job_id),
    )
    db.commit()


def _claim_recovery_job(db, job_id: str) -> bool:
    cursor = db.execute(
        """UPDATE aof_recovery_jobs
           SET status='running', attempts=attempts+1, updated_at=CURRENT_TIMESTAMP
           WHERE job_id=? AND status IN ('pending','failed','conflict','superseded','verified')""",
        (job_id,),
    )
    db.commit()
    return cursor.rowcount == 1


def append_chunk(novel_id: str, chapter_number: int, chunk: str) -> None:
    """追加写入一个 chunk 到 .draft 文件

    Args:
        novel_id: 小说 ID
        chapter_number: 章节号
        chunk: 增量文字片段
    """
    if not chunk or not chunk.strip():
        return

    path = _draft_path(novel_id, chapter_number)
    try:
        # 'a' 模式：追加写入；encoding='utf-8'；无缓冲（buffering=1 行缓冲）
        with _draft_lock, open(path, 'a', encoding='utf-8', buffering=1) as f:
            f.write(chunk)
            # 注意：不每次 fsync，由 _periodic_fsync 定期刷盘
    except Exception as e:
        logger.warning("[AOF] append_chunk 失败: %s (novel=%s, ch=%d): %s",
                       path, novel_id, chapter_number, e)


def read_draft(novel_id: str, chapter_number: int) -> Optional[str]:
    """读取 .draft 文件的完整内容

    Returns:
        文件内容字符串，文件不存在时返回 None
    """
    path = _draft_path(novel_id, chapter_number)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8')
        return content if content.strip() else None
    except Exception as e:
        logger.warning("[AOF] read_draft 失败: %s: %s", path, e)
        return None


def prepare_draft_cleanup(novel_id: str, chapter_number: int) -> str | None:
    """Durably register legacy completion evidence before waiting for its ACK.

    An interrupted/rejected completion is recovered as a draft, never replayed
    as an unfenced completed chapter.  Successful completion upgrades the same
    job to a verified cleanup receipt in ``delete_draft``.
    """
    path = _draft_path(novel_id, chapter_number)
    with _draft_lock:
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        db = _recovery_db()
        _ensure_recovery_jobs_table(db)
        job_id = _register_recovery_job(db, novel_id, chapter_number, path, content)
        db.execute(
            "UPDATE aof_recovery_jobs SET repair_resume_state=1 WHERE job_id=? AND status != 'completed'",
            (job_id,),
        )
        db.commit()
        return job_id


def delete_draft(novel_id: str, chapter_number: int) -> None:
    """Delete only a draft whose full content is verified in committed DB text."""
    path = _draft_path(novel_id, chapter_number)
    if path.exists():
        try:
            with _draft_lock:
                content = path.read_text(encoding="utf-8")
                db = _recovery_db()
                _ensure_recovery_jobs_table(db)
                job_id = _register_recovery_job(db, novel_id, chapter_number, path, content, job_type="cleanup")
                committed = _verify_persisted_draft(novel_id, chapter_number, content)
                # Persist the matching revision/hash before unlink.  A crash
                # after unlink can then be reconciled from this durable receipt.
                _set_recovery_job_status(db, job_id, "verified", committed=committed)
                if _delete_unchanged_draft(path, content):
                    _set_recovery_job_status(db, job_id, "completed", committed=committed)
                    logger.info("[AOF] 已删除已提交 .draft 文件: %s", path)
                else:
                    _set_recovery_job_status(db, job_id, "superseded", "draft file changed during cleanup")
        except Exception as e:
            logger.warning("[AOF] 删除 .draft 文件失败: %s: %s", path, e)


def fsync_draft(novel_id: str, chapter_number: int) -> None:
    """强制刷盘（定期调用，如每 5 秒）"""
    path = _draft_path(novel_id, chapter_number)
    if not path.exists():
        return
    try:
        # 打开文件并 fsync
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as e:
        logger.debug("[AOF] fsync 失败（可忽略）: %s: %s", path, e)


def recover_all_drafts(*, startup=False) -> int:
    """启动时扫描所有残留 .draft 文件，尝试恢复到 DB

    Returns:
        成功恢复的章节数
    """
    draft_dir = _draft_dir()
    if not draft_dir.exists():
        return 0

    db = _recovery_db()
    try:
        _ensure_recovery_jobs_table(db)
    except Exception as exc:
        logger.warning("[AOF] 恢复任务登记未取得提交确认，保留草稿: %s", exc)
        return 0
    if startup:
        # Startup runs before the daemon starts.  Only that quiescent entry
        # point may release a claim left by a dead process.
        db.execute("UPDATE aof_recovery_jobs SET status='pending',updated_at=CURRENT_TIMESTAMP "
                   "WHERE status='running'")
        db.commit()

    for path in draft_dir.glob("*.draft"):
        try:
            # 解析文件名：{novel_id}_ch{chapter_number}.draft
            name = path.stem  # e.g., "novel-123_ch5"
            if "_ch" not in name:
                logger.warning("[AOF] 无法解析 .draft 文件名: %s", path)
                continue

            novel_id, ch_part = name.rsplit("_ch", 1)
            chapter_number = int(ch_part)

            # Read an immutable snapshot; a changed file must never be removed.
            with _draft_lock:
                content = path.read_text(encoding='utf-8')
            if not content.strip():
                logger.info("[AOF] 空文件，直接删除: %s", path)
                _delete_unchanged_draft(path, content)
                continue

            job_id = _register_recovery_job(db, novel_id, chapter_number, path, content)
            row = db.fetch_one("SELECT status FROM aof_recovery_jobs WHERE job_id=?", (job_id,))
            if row and row.get("status") == "completed":
                # A new file with identical content is still evidence.  Verify
                # again instead of treating an old ledger row as proof.
                _verify_persisted_draft(novel_id, chapter_number, content)
                _delete_unchanged_draft(path, content)
                continue

        except Exception as e:
            logger.error("[AOF] 恢复 .draft 失败: %s: %s", path, e)
            # A failed or pending commit is never proof that the draft is disposable.

    recovered = _replay_recovery_jobs(db, limit=None)

    if recovered > 0:
        logger.info("[AOF] 共恢复 %d 个章节的草稿数据", recovered)

    return recovered


def _replay_recovery_jobs(db, *, job_id=None, limit=None) -> int:
    """Replay an accepted job, including a process crash after its file unlink."""
    condition = "status IN ('pending','failed','verified')"
    params = []
    if job_id is not None:
        condition += " AND job_id=?"
        params.append(str(job_id))
    query = "SELECT * FROM aof_recovery_jobs WHERE " + condition + " ORDER BY created_at"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, min(500, int(limit))))
    jobs = db.fetch_all(query, tuple(params))
    recovered = 0
    for job in jobs:
        try:
            if not _claim_recovery_job(db, job["job_id"]):
                continue
            content = job["content"]
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != job["content_hash"]:
                raise RuntimeError("AOF recovery payload hash mismatch")
            # A verified receipt has already committed the exact chapter
            # revision/hash; no new chapter mutation is needed after unlink.
            if job["status"] == "verified":
                committed = _verify_persisted_draft(job["novel_id"], job["chapter_number"], content)
                path = Path(job["draft_path"])
                if path.exists() and not _delete_unchanged_draft(path, content):
                    _set_recovery_job_status(db, job["job_id"], "superseded", "draft file changed after verification")
                    continue
                _set_recovery_job_status(db, job["job_id"], "completed", committed=committed)
                recovered += 1
                continue
            path = Path(job["draft_path"])
            if path.exists() and path.read_text(encoding="utf-8") != content:
                _set_recovery_job_status(db, job["job_id"], "superseded", "draft file changed before recovery")
                continue
            if job["job_type"] == "recover":
                _recover_draft_to_db(job["novel_id"], job["chapter_number"], content)
                # Intent is durable before the chapter write.  If the process
                # died after the write, the already-committed branch still
                # repairs the interrupted resume state on the next startup.
                chapter = db.fetch_one(
                    "SELECT status FROM chapters WHERE novel_id=? AND number=?",
                    (job["novel_id"], job["chapter_number"]),
                )
                if job["repair_resume_state"] and (chapter or {}).get("status") != "completed":
                    _mark_recovered_chapter_state(job["novel_id"])
            committed = _verify_persisted_draft(job["novel_id"], job["chapter_number"], content)
            _set_recovery_job_status(db, job["job_id"], "verified", committed=committed)
            if path.exists() and not _delete_unchanged_draft(path, content):
                _set_recovery_job_status(db, job["job_id"], "superseded", "draft file changed during recovery")
                continue
            _set_recovery_job_status(db, job["job_id"], "completed", committed=committed)
            recovered += 1
        except Exception as exc:
            status = "conflict" if "AOF 与数据库正文冲突" in str(exc) else "failed"
            try:
                _set_recovery_job_status(db, job["job_id"], status, f"{type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("[AOF] 恢复任务状态未取得提交确认 job=%s", job["job_id"])
            logger.error("[AOF] 恢复任务失败，保留文件 job=%s: %s", job["job_id"], exc)
    return recovered


def run_recovery_job(job_id: str) -> dict | None:
    """Explicit retry of one job; never steals a running claim."""
    db = _recovery_db()
    _ensure_recovery_jobs_table(db)
    existing = db.fetch_one("SELECT novel_id FROM aof_recovery_jobs WHERE job_id=?", (str(job_id),))
    if existing:
        novel = db.fetch_one("SELECT autopilot_status FROM novels WHERE id=?", (existing["novel_id"],))
        if (novel or {}).get("autopilot_status") == "running":
            raise ValueError("请先停止小说写作，再重试 AOF 恢复")
    job = retry_recovery_job(job_id)
    if job is None:
        return None
    _replay_recovery_jobs(db, job_id=job_id, limit=1)
    return db.fetch_one(
        "SELECT job_id,novel_id,chapter_number,draft_path,content_hash,job_type,"
        "status,attempts,last_error,committed_revision,committed_content_hash,"
        "created_at,updated_at,completed_at FROM aof_recovery_jobs WHERE job_id=?",
        (str(job_id),),
    )


def _delete_unchanged_draft(path: Path, snapshot: str) -> bool:
    """Coordinate local appenders and retain a different/newer file snapshot."""
    with _draft_lock:
        if not path.exists() or path.read_text(encoding="utf-8") != snapshot:
            return False
        path.unlink()
        return True


def _verify_persisted_draft(novel_id: str, chapter_number: int, content: str) -> dict:
    from application.paths import get_db_path
    from infrastructure.persistence.database.connection import get_database
    row = get_database(get_db_path()).fetch_one(
        "SELECT content,revision FROM chapters WHERE novel_id = ? AND number = ?",
        (novel_id, chapter_number),
    )
    # A prefix is not proof that this exact AOF snapshot was committed.  The
    # database may contain a later edit that happens to start with the draft;
    # retain the file unless the complete content matches byte-for-byte.
    if row is None or str(row.get("content") or "") != content:
        raise RuntimeError("草稿正文尚未确认提交，保留 AOF 文件")
    return {"revision": int(row.get("revision") or 0),
            "content_hash": hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()}


def _recover_draft_to_db(novel_id: str, chapter_number: int, content: str) -> bool:
    """将 .draft 文件内容恢复到 DB"""
    from application.paths import get_db_path
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.entities.chapter import Chapter, ChapterStatus
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from infrastructure.persistence.database.connection import get_database

    db = get_database(get_db_path())
    chapter_repo = SqliteChapterRepository(db)

    novel_id_obj = NovelId(novel_id)
    existing = chapter_repo.get_by_novel_and_number(novel_id_obj, chapter_number)

    if existing:
        existing_content = existing.content or ""
        if existing_content == content:
            return False  # Already committed: do not disturb novel recovery state.
        if existing.status == ChapterStatus.COMPLETED:
            raise RuntimeError("AOF 与已完成章节正文不完全匹配，保留草稿供人工恢复")
        if (existing.status != ChapterStatus.COMPLETED
                and content.startswith(existing_content)):
            chapter_id = existing.id.value if hasattr(existing.id, "value") else existing.id
            # Compare-and-swap prevents a concurrent editor/daemon write from
            # being overwritten between the recovery read and queued commit.
            cursor = db.execute(
                """UPDATE chapters SET content = ?, word_count = ?, status = 'draft',
                       updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND COALESCE(content, '') = ? AND revision=? AND status != 'completed'
                      AND EXISTS (SELECT 1 FROM novels n WHERE n.id=chapters.novel_id
                                  AND COALESCE(n.autopilot_status,'stopped')!='running')""",
                (content, len(content), chapter_id, existing_content, existing.revision),
            )
            db.commit()
            if cursor.rowcount != 1:
                _verify_persisted_draft(novel_id, chapter_number, content)
                return False
        else:
            raise RuntimeError("AOF 与数据库正文冲突，保留草稿供人工恢复")
    else:
        # 无记录：创建新章节（draft 状态）
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
        story_repo = StoryNodeRepository(get_db_path())

        # 查找对应的故事节点
        title = f"第 {chapter_number} 章（AOF 恢复）"
        outline = ""
        node_id = f"chapter-{novel_id}-{chapter_number}"

        # 尝试从故事树找到章节节点
        try:
            all_nodes = story_repo.get_by_novel_sync(novel_id)
            chapter_nodes = sorted(
                [n for n in all_nodes if n.node_type.value == "chapter"],
                key=lambda n: n.number
            )
            target_node = next((n for n in chapter_nodes if n.number == chapter_number), None)
            if target_node:
                node_id = target_node.id
                title = target_node.title or title
                outline = target_node.outline or ""
        except Exception:
            pass

        # Creation is also conditional: a concurrent chapter insert must never
        # be turned into an unconditional upsert of an older AOF snapshot.
        cursor = db.execute(
            """INSERT INTO chapters
                   (id, novel_id, number, title, content, outline, status, word_count,
                    created_at, updated_at)
               SELECT ?, ?, ?, ?, ?, ?, 'draft', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
               WHERE EXISTS (SELECT 1 FROM novels WHERE id=?
                             AND COALESCE(autopilot_status,'stopped')!='running')
               ON CONFLICT(novel_id, number) DO NOTHING""",
            (node_id, novel_id, chapter_number, title, content, outline, len(content), novel_id),
        )
        db.commit()
        if cursor.rowcount != 1:
            _verify_persisted_draft(novel_id, chapter_number, content)
            return False
    return True


def _mark_recovered_chapter_state(novel_id: str) -> None:
    """AOF 恢复后修正小说级断点状态。

    AOF 只能恢复正文，不知道精确节拍索引。这里采用保守口径：
    - 清掉 beats_completed，避免恢复草稿被误判为已跑完全章；
    - 如果索引仍是 0，则推进到 1，避免已有正文从第 1 拍整段叠写。
    """
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database

        db = get_database(get_db_path())
        db.execute(
            """
            UPDATE novels
            SET
                current_beat_index = CASE
                    WHEN COALESCE(current_beat_index, 0) <= 0 THEN 1
                    ELSE current_beat_index
                END,
                beats_completed = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND COALESCE(autopilot_status,'stopped')!='running'
            """,
            (novel_id,),
        )
        db.commit()
    except Exception as e:
        logger.warning("[AOF] 恢复断点状态修正失败 novel=%s: %s", novel_id, e)
        raise
