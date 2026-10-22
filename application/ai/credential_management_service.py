"""Non-secret credential inventory and lifecycle operations.

The vault owns encrypted values; this service only handles opaque references,
their database owners, and safe deletion.  It deliberately never resolves a
credential and never includes ciphertext in an API response.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
import hashlib

from infrastructure.persistence.database.connection import get_database
from infrastructure.security.credential_store import (
    PREFIX,
    CredentialStore,
    CredentialUnavailable,
    credentials_for_database,
)


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def credential_operation_lock(db=None) -> threading.RLock:
    """Return the process lock for one database's vault lifecycle operations."""
    database = db or get_database()
    key = str(getattr(database, "db_path", "default"))
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _ref(value) -> str | None:
    if isinstance(value, str) and value.startswith(PREFIX):
        return value
    return None


class CredentialReferenceInUse(RuntimeError):
    def __init__(self, reference: str, owners: list[dict]):
        self.reference = reference
        self.owners = owners
        super().__init__("凭据仍被当前配置引用，不能删除")


class CredentialManagementService:
    _SCANNED_FIELDS = (
        ("llm_profiles", "id", ("api_key", "extra_headers", "extra_query", "extra_body")),
        ("embedding_config", "id", ("api_key",)),
    )

    def __init__(self, db=None):
        self.db = db or get_database()
        self.store: CredentialStore = credentials_for_database(self.db)

    def _owners(self) -> dict[str, list[dict]]:
        owners: dict[str, list[dict]] = {}
        for table, identity, fields in self._SCANNED_FIELDS:
            try:
                rows = self.db.fetch_all(f"SELECT * FROM {table}")
            except Exception:
                continue
            for row in rows:
                for field in fields:
                    value = row.get(field) if hasattr(row, "get") else None
                    reference = _ref(value)
                    if reference:
                        owners.setdefault(reference, []).append({
                            "table": table,
                            "record_id_sha256": hashlib.sha256(
                                str(row.get(identity, "")).encode("utf-8")
                            ).hexdigest()[:16],
                            "field": field,
                        })
        return owners

    def list_references(self) -> list[dict]:
        owners = self._owners()
        records = []
        for item in self.store.inventory():
            reference = item["credential_ref"]
            item = dict(item)
            item["status"] = "active" if owners.get(reference) else "orphaned"
            item["owners"] = owners.get(reference, [])
            records.append(item)
        # A missing vault object is kept visible so a damaged backup fails
        # closed instead of appearing as an empty credential.
        for reference, references in owners.items():
            if not any(item["credential_ref"] == reference for item in records):
                records.append({
                    "credential_ref": reference,
                    "ciphertext_sha256": None,
                    "size_bytes": None,
                    "created_at": None,
                    "updated_at": None,
                    "status": "missing",
                    "owners": references,
                })
        return sorted(records, key=lambda item: item["credential_ref"])

    def export_metadata(self) -> dict:
        return {
            "format": "plotpilot-credential-refs-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "warning": "仅包含不可解密的引用和脱敏元数据，不包含凭据明文或密文",
            "credentials": self.list_references(),
        }

    def delete_reference(self, reference: str) -> dict:
        with credential_operation_lock(self.db):
            owners = self._owners().get(reference, [])
            if owners:
                raise CredentialReferenceInUse(reference, owners)
            deleted = self.store.delete(reference)
            return {"credential_ref": reference, "deleted": deleted}

