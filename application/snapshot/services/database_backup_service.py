"""SQLite online backup and verified isolated recovery; never copy a live WAL DB file."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
from uuid import uuid4


class DatabaseBackupError(ValueError):
    pass


def _checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _tree_inventory(root):
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        raise DatabaseBackupError("向量目录必须是项目内普通目录")
    result = []
    for path in sorted(root.rglob("*")):
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise DatabaseBackupError("向量目录包含越界链接") from exc
        if path.is_symlink():
            raise DatabaseBackupError("向量备份不接受符号链接")
        if path.is_file():
            result.append({"filename": path.relative_to(root).as_posix(),
                           "size": path.stat().st_size, "sha256": _checksum(path)})
    return result


def _copy_tree(source, destination):
    before = _tree_inventory(source)
    destination.mkdir(parents=True)
    for item in before:
        target = destination / item["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / item["filename"], target)
        with target.open("r+b") as stream:
            os.fsync(stream.fileno())
    if _tree_inventory(source) != before or _tree_inventory(destination) != before:
        raise DatabaseBackupError("向量目录在备份时变化，备份未确认完成")
    return before


def _inventory(conn):
    schema = list(conn.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name,type"))
    counts = {}
    for name, kind, _ in schema:
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return {"counts": counts, "schema_hash": hashlib.sha256(json.dumps(schema, ensure_ascii=False).encode("utf-8")).hexdigest(),
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0]}


def _validate_database(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise DatabaseBackupError("数据库完整性检查未通过")
            foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
            if foreign_keys:
                raise DatabaseBackupError("数据库外键检查未通过")
            return {"integrity_check": "ok", "foreign_key_check": "ok", **_inventory(conn)}
    except sqlite3.Error as exc:
        raise DatabaseBackupError(f"数据库完整性检查失败：{exc}") from exc


class DatabaseBackupService:
    def __init__(self, db_path, backup_root=None, *, vector_path=None, vector_mode="not_configured"):
        self.source = Path(db_path).resolve()
        self.root = Path(backup_root).resolve() if backup_root else self.source.parent / "backups"
        self.root.mkdir(parents=True, exist_ok=True)
        self.vector_path = Path(vector_path).resolve() if vector_path is not None else None
        self.vector_mode = vector_mode
        if self.vector_path is not None:
            try:
                self.vector_path.relative_to(self.source.parent)
                if (self.vector_path == self.source.parent or self.vector_path == self.root
                        or self.vector_path in self.root.parents or self.root in self.vector_path.parents):
                    raise ValueError("目录重叠")
            except ValueError as exc:
                raise DatabaseBackupError("只允许备份当前项目数据目录内独立的向量目录") from exc

    def _folder(self, backup_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,95}", backup_id):
            raise DatabaseBackupError("备份编号无效")
        folder = (self.root / backup_id).resolve()
        try:
            folder.relative_to(self.root.resolve())
        except ValueError as exc:
            raise DatabaseBackupError("备份路径超出备份目录") from exc
        return folder

    def create(self):
        if self.vector_mode == "external":
            raise DatabaseBackupError("外部 Qdrant 需先导出到项目本地目录，不能把外部向量遗漏为完整备份")
        if not self.source.is_file():
            raise DatabaseBackupError("活动数据库不存在")
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:12]
        final = self._folder(backup_id)
        staged = self.root / ("partial_" + backup_id)
        staged.resolve().relative_to(self.root.resolve())
        staged.mkdir()
        destination = staged / "plotpilot.db"
        try:
            with closing(sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(destination)) as target:
                    source.backup(target, pages=128)
                    target.execute("PRAGMA journal_mode=DELETE")
            validation = _validate_database(destination)
            vector_files = _copy_tree(self.vector_path, staged / "vectors") if self.vector_path else []
            from application.release import APP_RELEASE_VERSION
            manifest = {"format": "plotpilot-sqlite-online-backup", "format_version": 2, "backup_id": backup_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "application_version": APP_RELEASE_VERSION,
                "source_database_name": self.source.name, "scope": "sqlite_database_and_vectors" if self.vector_path else "sqlite_database",
                "vectors": {"mode": "local" if self.vector_path else self.vector_mode,
                            "directory": "vectors", "files": vector_files},
                "database": {"filename": "plotpilot.db", "size": destination.stat().st_size, "sha256": _checksum(destination)},
                "validation": validation}
            manifest_path = staged / "manifest.json"
            with manifest_path.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows fsync requires a descriptor opened for writing.
            with destination.open("r+b") as stream:
                os.fsync(stream.fileno())
            # Both resolved targets are inside this task's chosen backup root.
            staged.resolve().relative_to(self.root.resolve())
            final.resolve().relative_to(self.root.resolve())
            os.replace(staged, final)
            return {**manifest, "directory": str(final), "status": "verified"}
        except Exception as exc:
            # Keep incomplete artifacts for diagnosis; no success manifest is published.
            raise DatabaseBackupError(f"备份未确认完成，临时结果保留在 {staged.name}：{exc}") from exc

    def list(self):
        results = []
        for manifest_file in sorted(self.root.glob("*/manifest.json"), reverse=True):
            if manifest_file.parent.name.startswith("partial_"):
                continue
            try:
                folder = self._folder(manifest_file.parent.name)
                manifest = self._manifest(folder, folder.name)
                results.append({**manifest, "directory": str(folder), "status": "verification_required"})
            except (ValueError, OSError):
                continue
        return results[:100]

    def _manifest(self, folder, backup_id):
        try:
            path = folder / 'manifest.json'
            path.resolve().relative_to(folder)
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('备份清单超过1MB')
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise DatabaseBackupError('找不到有效备份清单') from exc
        if (not isinstance(manifest, dict) or manifest.get('format') != 'plotpilot-sqlite-online-backup'
                or manifest.get('format_version') not in (1, 2) or manifest.get('backup_id') != backup_id):
            raise DatabaseBackupError('备份清单格式或编号不匹配')
        metadata, validation = manifest.get('database'), manifest.get('validation')
        if (not isinstance(metadata, dict) or type(metadata.get('size')) is not int or metadata['size'] <= 0
                or not isinstance(metadata.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', metadata['sha256'])
                or metadata.get('filename') != 'plotpilot.db' or not isinstance(validation, dict)
                or not isinstance(validation.get('counts'), dict)
                or any(not isinstance(name, str) or type(count) is not int or count < 0 for name, count in validation['counts'].items())
                or not isinstance(validation.get('schema_hash'), str) or not re.fullmatch('[a-f0-9]{64}', validation['schema_hash'])
                or type(validation.get('user_version')) is not int or validation.get('integrity_check') != 'ok'
                or validation.get('foreign_key_check') != 'ok'):
            raise DatabaseBackupError('备份清单数据库字段或校验报告无效')
        if manifest["format_version"] == 2:
            vectors = manifest.get("vectors")
            if (not isinstance(vectors, dict) or vectors.get("mode") not in ("local", "not_configured", "disabled")
                    or vectors.get("directory") != "vectors" or not isinstance(vectors.get("files"), list)):
                raise DatabaseBackupError("备份清单向量字段无效")
            names = set()
            for item in vectors["files"]:
                if (not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                        or not item["filename"] or "\\" in item["filename"]
                        or Path(item["filename"]).is_absolute() or any(p in (".", "..") for p in item["filename"].split("/"))
                        or item["filename"] in names or type(item.get("size")) is not int or item["size"] < 0
                        or not isinstance(item.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", item["sha256"])):
                    raise DatabaseBackupError("备份清单向量文件无效")
                names.add(item["filename"])
        return manifest

    def verify(self, backup_id):
        folder = self._folder(backup_id)
        manifest = self._manifest(folder, backup_id)
        metadata = manifest.get("database") or {}
        database = folder / "plotpilot.db"
        try:
            database.resolve().relative_to(folder)
        except ValueError as exc:
            raise DatabaseBackupError("备份文件路径超出备份目录") from exc
        if metadata.get("filename") != "plotpilot.db" or not database.is_file() or database.stat().st_size != metadata.get("size") or _checksum(database) != metadata.get("sha256"):
            raise DatabaseBackupError("备份文件大小或SHA-256校验不匹配")
        validation = _validate_database(database)
        expected = manifest.get("validation") or {}
        if any(validation.get(field) != expected.get(field) for field in ("counts", "schema_hash", "user_version")):
            raise DatabaseBackupError("备份数据库内容数量或结构与清单不匹配")
        if manifest.get("format_version") == 2:
            if _tree_inventory(folder / "vectors") != manifest["vectors"]["files"]:
                raise DatabaseBackupError("向量文件大小、清单或SHA-256校验不匹配")
        return {**manifest, "directory": str(folder), "status": "verified", "validation": validation}

    def restore_copy(self, backup_id):
        manifest = self.verify(backup_id)
        source = self._folder(backup_id) / "plotpilot.db"
        restored_root = self.root / "restored"
        restored_root.mkdir(exist_ok=True)
        destination = restored_root / (backup_id + "_" + uuid4().hex[:12] + ".db")
        if destination.resolve() == self.source or destination.exists():
            raise DatabaseBackupError("恢复目标必须是新建的独立数据库")
        destination.resolve().relative_to(self.root.resolve())
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
        validation = _validate_database(destination)
        if any(validation[field] != manifest["validation"][field] for field in ("counts", "schema_hash", "user_version")):
            raise DatabaseBackupError("恢复副本与备份清单不匹配，活动库保持不变")
        vector_destination = destination.with_suffix(".vectors")
        if manifest.get("vectors", {}).get("mode") == "local":
            if _copy_tree(self._folder(backup_id) / "vectors", vector_destination) != manifest["vectors"]["files"]:
                raise DatabaseBackupError("恢复向量副本与清单不匹配")
        receipt = {"backup_id": backup_id, "status": "restored_verified", "restored_database": str(destination),
                   "active_database_changed": False, "sha256": _checksum(destination), "validation": validation,
                   "restored_vectors": str(vector_destination) if vector_destination.exists() else None,
                   "completed_at": datetime.now(timezone.utc).isoformat()}
        with destination.open('r+b') as stream:
            os.fsync(stream.fileno())
        with destination.with_suffix('.restore.json').open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return receipt

    @property
    def pending_path(self):
        return self.source.parent / "database-restore-request.json"

    def request_restore(self, backup_id, *, expected_sha256):
        """Prepare immutable verified copies; activation only happens on next startup."""
        if self.pending_path.exists():
            raise DatabaseBackupError("已有待重启恢复请求，请先完成或取消")
        manifest = self.verify(backup_id)
        if expected_sha256 != manifest["database"]["sha256"]:
            raise DatabaseBackupError("备份已改变，请重新检查后确认恢复")
        if self.vector_mode == "external":
            raise DatabaseBackupError("外部向量存储无法通过本地恢复切换")
        if self.vector_path is not None and manifest.get("vectors", {}).get("mode") != "local":
            raise DatabaseBackupError("此旧备份未包含本地向量目录，请先创建完整备份")
        restored = self.restore_copy(backup_id)
        request = {"format": "plotpilot-controlled-restore", "request_id": uuid4().hex,
                   "backup_id": backup_id, "status": "pending_restart", "active_database_changed": False,
                   "expected_sha256": expected_sha256, "prepared_database": Path(restored["restored_database"]).name,
                   "prepared_sha256": restored["sha256"],
                   "created_at": datetime.now(timezone.utc).isoformat()}
        _write_json(self.pending_path, request)
        return request

    def cancel_restore(self):
        if self.pending_path.exists():
            self.pending_path.unlink()
        return {"status": "cancelled", "active_database_changed": False}

    def restore_status(self):
        if self.pending_path.exists():
            return json.loads(self.pending_path.read_text(encoding="utf-8"))
        path = self.source.parent / "database-restore-receipt.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "none"}

    def activate_pending(self, ownership, *, quiescent):
        """Offline-only switching, with raw originals retained for rollback."""
        ownership.assert_held(self.source)
        if not quiescent:
            raise DatabaseBackupError("writer/daemon/后台任务尚未全部停止，禁止切换")
        from application.snapshot.services.database_recovery_service import DatabaseRecoveryService
        recovery = DatabaseRecoveryService(self.source)
        recovery.rollback_interrupted_switch(ownership)
        if not self.pending_path.exists():
            return None
        request = json.loads(self.pending_path.read_text(encoding="utf-8"))
        if (request.get("format") != "plotpilot-controlled-restore" or request.get("status") != "pending_restart"
                or not re.fullmatch("[a-f0-9]{32}", request.get("request_id", ""))):
            raise DatabaseBackupError("恢复请求格式无效")
        manifest = self.verify(request["backup_id"])
        prepared = self.root / "restored" / request["prepared_database"]
        try:
            prepared.resolve().relative_to((self.root / "restored").resolve())
        except ValueError as exc:
            raise DatabaseBackupError("恢复副本路径越界") from exc
        if (manifest["database"]["sha256"] != request["expected_sha256"]
                or _checksum(prepared) != request["prepared_sha256"]
                or _validate_database(prepared) != manifest["validation"]):
            raise DatabaseBackupError("恢复副本已改变，禁止切换")
        vectors = prepared.with_suffix(".vectors") if self.vector_path is not None else None
        if vectors is not None and _tree_inventory(vectors) != manifest.get("vectors", {}).get("files"):
            raise DatabaseBackupError("恢复向量副本已改变，禁止切换")
        # This online API reads committed WAL even though all producers are now stopped.
        previous = self.create() if self.source.exists() else None
        raw = recovery.capture_originals(ownership)
        receipt = recovery.atomic_switch(prepared, ownership, vector_source=vectors,
                                         vector_target=self.vector_path)
        receipt.update(backup_id=request["backup_id"], request_id=request["request_id"],
                       previous_backup_id=previous["backup_id"] if previous else None,
                       original_manifest=raw["manifest_path"], status="activated_verified",
                       active_database_changed=True, validation=manifest["validation"])
        _write_json(self.source.parent / "database-restore-receipt.json", receipt)
        self.pending_path.unlink()
        recovery.finalize_switch()
        return receipt
