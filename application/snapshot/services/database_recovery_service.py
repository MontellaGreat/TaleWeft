"""Non-destructive SQLite WAL recovery and offline, rollbackable activation.

Reuse SQLite's open-source recovery/backup API; never interpret a WAL as cache.
Raw evidence is immutable and SQLite opens a separate working copy only.
WAL checks follow https://sqlite.org/fileformat2.html#wal_file_format.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
from uuid import uuid4

from application.snapshot.services.database_backup_service import (
    DatabaseBackupError, _checksum, _validate_database, _write_json, _copy_tree,
)


def _wal_checksum(data, seed, endian):
    words = struct.unpack(endian + "I" * (len(data) // 4), data)
    first, second = seed
    for i in range(0, len(words), 2):
        first = (first + words[i] + second) & 0xffffffff
        second = (second + words[i + 1] + first) & 0xffffffff
    return first, second


def validate_wal(path):
    if not path.exists() or path.stat().st_size == 0:
        return {"frames": 0, "last_commit_frame": 0}
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise DatabaseBackupError("WAL 头部损坏；保留原件，无法确认最后提交")
        magic, version, page_size, _sequence, salt1, salt2, check1, check2 = struct.unpack(">8I", header)
        if (magic not in (0x377f0682, 0x377f0683) or version != 3007000
                or page_size < 512 or page_size > 65536 or page_size & (page_size - 1)):
            raise DatabaseBackupError("WAL 格式损坏；无法确认最后提交")
        endian = "<" if magic == 0x377f0682 else ">"
        seed = _wal_checksum(header[:24], (0, 0), endian)
        if seed != (check1, check2):
            raise DatabaseBackupError("WAL 头部校验失败；无法确认最后提交")
        frames = last_commit = 0
        while True:
            frame = stream.read(24 + page_size)
            if not frame:
                break
            if len(frame) != 24 + page_size:
                raise DatabaseBackupError("WAL 帧不完整；无法确认最后提交")
            page_number, database_size, frame_salt1, frame_salt2, check1, check2 = struct.unpack(">6I", frame[:24])
            seed = _wal_checksum(frame[:8] + frame[24:], seed, endian)
            if page_number == 0 or (frame_salt1, frame_salt2) != (salt1, salt2) or seed != (check1, check2):
                raise DatabaseBackupError("WAL 帧校验失败；无法确认最后提交")
            frames += 1
            if database_size:
                last_commit = frames
    return {"frames": frames, "last_commit_frame": last_commit, "page_size": page_size}


class DatabaseRecoveryService:
    def __init__(self, database, recovery_root=None):
        self.source = Path(database).resolve()
        self.root = Path(recovery_root).resolve() if recovery_root else self.source.parent / "recovery"
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = self.source.parent / "database-switch-journal.json"

    def capture_originals(self, ownership):
        ownership.assert_held(self.source)
        folder = self.root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex)
        raw = folder / "originals"
        raw.mkdir(parents=True)
        inventory = []
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.source) + suffix)
            if not source.exists():
                continue
            before = (source.stat().st_size, _checksum(source))
            target = raw / source.name
            shutil.copyfile(source, target)
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            if before != (target.stat().st_size, _checksum(target)) or before != (source.stat().st_size, _checksum(source)):
                raise DatabaseBackupError("原始 DB/WAL/SHM 复制时变化；禁止继续恢复")
            inventory.append({"filename": source.name, "size": before[0], "sha256": before[1]})
        manifest = {"format": "plotpilot-raw-database-recovery", "format_version": 1,
                    "source_database": str(self.source), "instance_id": ownership.instance_id,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": inventory,
                    "original_files_modified": False, "status": "originals_preserved"}
        _write_json(folder / "manifest.json", manifest)
        return {**manifest, "directory": str(folder), "manifest_path": str(folder / "manifest.json")}

    def recover_copy(self, ownership):
        captured = self.capture_originals(ownership)
        folder = Path(captured["directory"])
        working = folder / "working"
        working.mkdir()
        database = working / self.source.name
        try:
            raw = folder / "originals"
            wal_validation = validate_wal(raw / (self.source.name + "-wal"))
            # SHM is preserved as evidence and rebuilt by SQLite in the working copy.
            for suffix in ("", "-wal"):
                original = raw / (self.source.name + suffix)
                if original.exists():
                    shutil.copyfile(original, Path(str(database) + suffix))
            if not database.exists():
                raise DatabaseBackupError("原始数据库不存在，禁止创建空库作为恢复成功")
            destination = folder / "recovered.db"
            with closing(sqlite3.connect(database)) as recovered:
                recovered.execute("PRAGMA busy_timeout=1000")
                recovered.execute("PRAGMA integrity_check").fetchall()
                with closing(sqlite3.connect(destination)) as target:
                    recovered.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
            validation = _validate_database(destination)
            receipt = {**captured, "status": "recovered_verified", "recovered_database": str(destination),
                       "active_database_changed": False, "sha256": _checksum(destination),
                       "validation": validation, "wal_validation": wal_validation}
            _write_json(folder / "recovery-receipt.json", receipt)
            return receipt
        except Exception as exc:
            _write_json(folder / "recovery-receipt.json", {**captured, "status": "failed",
                       "active_database_changed": False, "error": str(exc)})
            raise DatabaseBackupError(f"无法确认恢复完整性；原始副本位于 {captured['manifest_path']}：{exc}") from exc

    def atomic_switch(self, prepared_database, ownership, *, vector_source=None, vector_target=None):
        ownership.assert_held(self.source)
        if self.journal.exists():
            raise DatabaseBackupError("存在未完成切换，必须先回滚")
        prepared = Path(prepared_database).resolve()
        _validate_database(prepared)
        switch_id = uuid4().hex
        rollback = self.root / ("switch_" + switch_id)
        rollback.mkdir()
        # Stage on the destination volume so os.replace remains atomic on Windows.
        staged = self.source.with_name(self.source.name + ".switch_" + switch_id)
        shutil.copyfile(prepared, staged)
        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        vector_target = Path(vector_target).resolve() if vector_target is not None else None
        vector_stage = self.source.parent / (".vectors-switch_" + switch_id) if vector_target is not None else None
        if vector_target is not None:
            vector_target.relative_to(self.source.parent)
            if vector_target == self.source.parent or vector_source is None:
                raise DatabaseBackupError("向量切换目标无效")
            _copy_tree(Path(vector_source).resolve(), vector_stage)
        journal = {"format": "plotpilot-atomic-database-switch", "switch_id": switch_id,
                   "source": str(self.source), "rollback": str(rollback), "staged": str(staged),
                   "original_files": [self.source.name + suffix for suffix in ("", "-wal", "-shm")
                                      if Path(str(self.source) + suffix).exists()],
                   "vector_target": str(vector_target) if vector_target else None,
                   "vector_existed": bool(vector_target and vector_target.exists()),
                   "vector_stage": str(vector_stage) if vector_stage else None, "status": "prepared"}
        _write_json(self.journal, journal)
        try:
            for name in journal["original_files"]:
                os.replace(self.source.parent / name, rollback / name)
            if vector_target is not None:
                if vector_target.exists():
                    os.replace(vector_target, rollback / "vectors")
                os.replace(vector_stage, vector_target)
            os.replace(staged, self.source)
            validation = _validate_database(self.source)
            journal["status"] = "switched_verified"
            _write_json(self.journal, journal)
            return {"switch_id": switch_id, "rollback_directory": str(rollback),
                    "validation": validation, "completed_at": datetime.now(timezone.utc).isoformat()}
        except Exception:
            self.rollback_interrupted_switch(ownership)
            raise

    def finalize_switch(self):
        if self.journal.exists():
            self.journal.unlink()

    def rollback_interrupted_switch(self, ownership):
        ownership.assert_held(self.source)
        if not self.journal.exists():
            return
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        if journal.get("format") != "plotpilot-atomic-database-switch" or journal.get("source") != str(self.source):
            raise DatabaseBackupError("切换回滚日志无效，保留所有文件")
        rollback = Path(journal["rollback"]).resolve()
        rollback.relative_to(self.root)
        receipt_file = self.source.parent / "database-restore-receipt.json"
        if receipt_file.exists():
            receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            if receipt.get("switch_id") == journal["switch_id"] and receipt.get("status") == "activated_verified":
                self.finalize_switch()
                pending = self.source.parent / "database-restore-request.json"
                if pending.exists():
                    pending.unlink()
                return
        for suffix in ("", "-wal", "-shm"):
            active = Path(str(self.source) + suffix)
            original = rollback / active.name
            if original.exists():
                if active.exists():
                    os.replace(active, rollback / (active.name + ".failed_" + uuid4().hex))
                os.replace(original, active)
            elif active.name not in journal["original_files"] and active.exists():
                os.replace(active, rollback / (active.name + ".failed_" + uuid4().hex))
        target = Path(journal["vector_target"]).resolve() if journal.get("vector_target") else None
        if target is not None:
            target.relative_to(self.source.parent)
            original = rollback / "vectors"
            stage = Path(journal["vector_stage"]).resolve()
            stage.relative_to(self.source.parent)
            if original.exists():
                if target.exists():
                    os.replace(target, rollback / ("vectors.failed_" + uuid4().hex))
                os.replace(original, target)
            elif not journal["vector_existed"] and target.exists():
                os.replace(target, rollback / ("vectors.failed_" + uuid4().hex))
        journal["status"] = "rolled_back"
        _write_json(rollback / "rollback-receipt.json", journal)
        self.finalize_switch()
