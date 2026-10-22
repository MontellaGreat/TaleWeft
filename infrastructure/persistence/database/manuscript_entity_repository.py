"""手稿实体：道具 CRUD + 章节提及索引。"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from infrastructure.persistence.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class ManuscriptEntityRepository:
    def __init__(self, db: DatabaseConnection):
        self.db = db
        # Canonical stores replaced bible_props in migration 013. Keep support
        # for pre-migration databases used by import/compatibility tooling.
        canonical = db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='unified_props'")
        self._canonical = bool(canonical)
        self._table = 'unified_props' if self._canonical else 'bible_props'

    def _projection(self):
        if self._canonical:
            return "id, novel_id, name, description, aliases_json, holder_character_id, introduced_chapter AS first_chapter, COALESCE(json_extract(attributes_json, '$.key_context'), json_extract(attributes_json, '$.is_key'), 0) AS is_key, created_at, updated_at"
        return "id, novel_id, name, description, aliases_json, holder_character_id, first_chapter, COALESCE(is_key, 0) AS is_key, created_at, updated_at"

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    def list_props(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            f"""
            SELECT {self._projection()}
            FROM {self._table}
            WHERE novel_id = ?
            ORDER BY name COLLATE NOCASE
            """,
            (novel_id,),
        )
        return [dict(r) for r in rows]

    def create_prop(
        self,
        novel_id: str,
        *,
        name: str,
        description: str = "",
        aliases: Optional[List[str]] = None,
        holder_character_id: Optional[str] = None,
        first_chapter: Optional[int] = None,
    ) -> Dict[str, Any]:
        pid = str(uuid.uuid4())
        now = self._now()
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        self.db.execute(
            f"""
            INSERT INTO {self._table} (
                id, novel_id, name, description, aliases_json, holder_character_id, {'introduced_chapter' if self._canonical else 'first_chapter'}, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                novel_id,
                name.strip(),
                description or "",
                aliases_json,
                holder_character_id,
                first_chapter,
                now,
                now,
            ),
        )
        self.db.get_connection().commit()
        return self.get_prop(novel_id, pid) or {}

    def get_prop(self, novel_id: str, prop_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetch_one(
            f"""SELECT {self._projection()}
               FROM {self._table} WHERE novel_id = ? AND id = ?""",
            (novel_id, prop_id),
        )
        return dict(row) if row else None

    def update_prop(
        self,
        novel_id: str,
        prop_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        holder_character_id: Optional[str] = None,
        first_chapter: Optional[int] = None,
        is_key: Optional[bool] = None,
    ) -> None:
        cur = self.get_prop(novel_id, prop_id)
        if not cur:
            return
        now = self._now()
        nm = name if name is not None else cur["name"]
        desc = description if description is not None else cur["description"]
        aj = json.dumps(aliases, ensure_ascii=False) if aliases is not None else cur["aliases_json"]
        hc = holder_character_id if holder_character_id is not None else cur.get("holder_character_id")
        fc = first_chapter if first_chapter is not None else cur.get("first_chapter")
        ik = int(is_key) if is_key is not None else cur.get("is_key", 0)
        self.db.execute(
            f"""
            UPDATE {self._table}
            SET name = ?, description = ?, aliases_json = ?, holder_character_id = ?, {'introduced_chapter' if self._canonical else 'first_chapter'} = ?,
                {"attributes_json = json_set(attributes_json, '$.key_context', ?)" if self._canonical else 'is_key = ?'}, updated_at = ?
            WHERE novel_id = ? AND id = ?
            """,
            (nm, desc, aj, hc, fc, ik, now, novel_id, prop_id),
        )
        self.db.get_connection().commit()

    def delete_prop(self, novel_id: str, prop_id: str) -> None:
        self.db.execute(f"DELETE FROM {self._table} WHERE novel_id = ? AND id = ?", (novel_id, prop_id))
        self.db.get_connection().commit()

    def replace_chapter_mentions(
        self,
        novel_id: str,
        chapter_number: int,
        rows: Sequence[Tuple[str, str, str, int]],
    ) -> None:
        """rows: (kind, entity_id, display_label, count)"""
        now = self._now()
        # Delete and replacement rows share one writer ACK and one transaction.
        # A failed insert or superseded body cannot leave a partial index.
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM chapter_entity_mentions WHERE novel_id = ? AND chapter_number = ?",
                (novel_id, chapter_number),
            )
            for kind, eid, label, cnt in rows:
                conn.execute(
                    """
                    INSERT INTO chapter_entity_mentions (
                        novel_id, chapter_number, entity_kind, entity_id, display_label, mention_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (novel_id, chapter_number, kind, eid, label or eid, max(1, int(cnt)), now),
                )

    def list_chapter_mentions(self, novel_id: str, chapter_number: int) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT entity_kind, entity_id, display_label, mention_count, updated_at
            FROM chapter_entity_mentions
            WHERE novel_id = ? AND chapter_number = ?
            ORDER BY mention_count DESC, display_label COLLATE NOCASE
            """,
            (novel_id, chapter_number),
        )
        return [dict(r) for r in rows]
