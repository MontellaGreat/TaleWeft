"""SQLite Bible repository.

Bible-owned world/location/style/timeline data stays in Bible tables; characters
are read and written through unified_characters, the canonical character store.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from domain.bible.entities.bible import Bible
from domain.bible.repositories.bible_repository import BibleRepository
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.mappers.bible_mapper import BibleMapper
from infrastructure.persistence.database.character_voice_profile import read_character_voice_profile

logger = logging.getLogger(__name__)


class SqliteBibleRepository(BibleRepository):
    """Persist Bible aggregate data with unified characters as the character truth."""

    def __init__(self, db: DatabaseConnection):
        self.db = db

    def _conn(self):
        return self.db.get_connection()

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    def _clear_children(self, conn, novel_id: str, keep_character_ids=()) -> None:
        conn.execute("DELETE FROM bible_style_notes WHERE novel_id = ?", (novel_id,))
        conn.execute("DELETE FROM bible_timeline_notes WHERE novel_id = ?", (novel_id,))
        conn.execute("DELETE FROM bible_locations WHERE novel_id = ?", (novel_id,))
        conn.execute("DELETE FROM bible_world_settings WHERE novel_id = ?", (novel_id,))
        # Replacing Bible-owned collections must not delete/reinsert surviving
        # unified characters: chapter state and external relationships live on
        # these identities. A Bible deletion passes no retained identities.
        conn.execute(
            "DELETE FROM unified_characters WHERE novel_id = ? "
            "AND id NOT IN (SELECT value FROM json_each(?))",
            (novel_id, json.dumps(list(keep_character_ids))),
        )

    def save(self, bible: Bible) -> None:
        novel_id = bible.novel_id.value
        now = self._now()
        character_ids = [char.character_id.value for char in bible.characters]
        owner = self.db.fetch_one('SELECT novel_id FROM bibles WHERE id=?', (bible.id,))
        if owner and owner['novel_id'] != novel_id:
            raise ValueError('Bible identity belongs to another novel')
        conflicts = self.db.fetch_one(
            'SELECT id FROM unified_characters WHERE novel_id<>? '
            'AND id IN (SELECT value FROM json_each(?)) LIMIT 1',
            (novel_id, json.dumps(character_ids)),
        )
        if conflicts:
            raise ValueError('Character identity belongs to another novel')
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO bibles (id, novel_id, schema_version, extensions, created_at, updated_at)
                VALUES (?, ?, 1, '{}', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    novel_id = CASE WHEN bibles.novel_id = excluded.novel_id
                        THEN bibles.novel_id ELSE NULL END,
                    updated_at = excluded.updated_at
                """,
                (bible.id, novel_id, now, now),
            )
            self._clear_children(conn, novel_id, character_ids)

            for char in bible.characters:
                self._save_unified_character(conn, novel_id, char, now)

            for ws in bible.world_settings:
                conn.execute(
                    """
                    INSERT INTO bible_world_settings
                    (id, novel_id, name, description, setting_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ws.id, novel_id, ws.name, ws.description or "", ws.setting_type or "other", now, now),
                )

            for loc in bible.locations:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bible_locations
                    (id, novel_id, name, description, location_type, parent_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        loc.id,
                        novel_id,
                        loc.name,
                        loc.description or "",
                        loc.location_type or "other",
                        loc.parent_id,
                        now,
                        now,
                    ),
                )

            for order, note in enumerate(bible.timeline_notes):
                conn.execute(
                    """
                    INSERT INTO bible_timeline_notes
                    (id, novel_id, event, time_point, description, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note.id,
                        novel_id,
                        note.event or "",
                        note.time_point or "",
                        note.description or "",
                        order,
                        now,
                        now,
                    ),
                )

            for sn in bible.style_notes:
                conn.execute(
                    """
                    INSERT INTO bible_style_notes
                    (id, novel_id, category, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (sn.id, novel_id, sn.category, sn.content or "", now, now),
                )

    def restore_missing_character_voices(self, novel_id, before, wanted, *, run_id, validate_basis):
        """One writer-side CAS: restore voices without rewriting any other field.

        Capture SQL snapshots before the final DTO read. The materialized guard
        then rejects edits, relationship changes and lost leases at execution,
        including changes made while this batch was waiting for the writer.
        """
        from application.ai_invocation.dtos import stable_hash
        from application.world.dtos.bible_dto import BibleDTO
        scopes = {
            'novels': 'id=?', 'bibles': 'novel_id=?', 'worldbuilding': 'novel_id=?',
            'unified_characters': 'novel_id=?',
            'unified_character_relationships': 'character_id IN (SELECT id FROM unified_characters WHERE novel_id=?)',
            'ai_invocation_sessions': "json_extract(context_json,'$.novel_id')=?",
            **{name: "session_id IN (SELECT id FROM ai_invocation_sessions WHERE json_extract(context_json,'$.novel_id')=?)"
                for name in ('ai_invocation_attempts', 'ai_adoption_decisions', 'ai_adoption_commits')},
            **{name: 'novel_id=?' for name in ('bible_style_notes', 'bible_world_settings',
                'bible_locations', 'bible_timeline_notes')},
        }
        guards, params = [], []
        for table, scope in scopes.items():
            columns = [row['name'] for row in self.db.fetch_all(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            names = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
            order = 'id' if 'id' in columns else 'novel_id'
            expression = (f'(SELECT json_group_array(json_array({names})) '
                          f'FROM (SELECT * FROM "{table}" WHERE {scope} ORDER BY "{order}"))')
            snapshot = self.db.fetch_one(f'SELECT {expression} AS snapshot', (novel_id,))['snapshot']
            guards.append(f'{expression} IS ?')
            params.extend((novel_id, snapshot))
        validate_basis()
        def projection(rows):
            output = {}
            for row in rows:
                data = asdict(row)
                data['relationships'] = sorted(data.get('relationships') or [], key=stable_hash)
                output[row.id] = data
            return output
        current = self.get_by_novel_id(NovelId(novel_id))
        if current is None or projection(BibleDTO.from_domain(current).characters) != projection(before):
            raise RuntimeError('人物恢复前资料已变化，已阻止补写')
        expected = {row.id: row for row in wanted}
        if set(expected) != {row.id for row in before}:
            raise ValueError('人物恢复标识不一致')
        patches = []
        for row in before:
            target = expected[row.id]
            if projection([row]) == projection([target]):
                continue
            if row.voice_profile or not target.voice_profile:
                raise ValueError('人物恢复声线冲突')
            from dataclasses import replace
            if projection([replace(row, voice_profile=target.voice_profile)]) != projection([target]):
                raise ValueError('人物恢复包含非声线改动')
            patches.append({'id': row.id, **{key: target.voice_profile.get(key, '')
                for key in ('style', 'sentence_pattern', 'speech_tempo')}})
        if not patches:
            return
        guards.append("EXISTS (SELECT 1 FROM novel_execution_leases WHERE novel_id=? AND owner='setup' "
                      "AND job_id=? AND expires_at>(julianday('now')-2440587.5)*86400.0)")
        params.extend((novel_id, run_id))
        sql = ('WITH repair_guard AS MATERIALIZED (SELECT (' + ' AND '.join(guards) + ') AS ok), '
               'patches AS MATERIALIZED (SELECT value FROM json_each(?)) '
               'UPDATE unified_characters SET ' + ','.join(
                   f'{column}=(SELECT json_extract(value,\'$.{key}\') FROM patches '
                   "WHERE json_extract(value,'$.id')=unified_characters.id)"
                   for column, key in (('voice_style', 'style'), ('sentence_pattern', 'sentence_pattern'),
                                       ('speech_tempo', 'speech_tempo'))) +
               " WHERE novel_id=? AND (SELECT ok FROM repair_guard)=1 "
               "AND id IN (SELECT json_extract(value,'$.id') FROM patches)")
        with self.db.transaction() as conn:
            conn.execute(sql, tuple(params) + (json.dumps(patches, ensure_ascii=False), novel_id))
            result = conn.execute('SELECT changes() AS repaired')
        if result.fetchone()['repaired'] != len(patches):
            raise RuntimeError('人物恢复保存前资料或执行租约已变化，已阻止补写')

    def _save_unified_character(self, conn, novel_id: str, char: Any, now: str) -> None:
        cid = char.character_id.value
        voice_profile = getattr(char, "voice_profile", None) or {}
        conn.execute(
            """
            INSERT INTO unified_characters (
                id, novel_id, name, description, public_profile, hidden_profile,
                reveal_chapter, gender, age, appearance, personality, background,
                core_motivation, inner_lack, verbal_tic, idle_behavior, voice_style,
                sentence_pattern, speech_tempo, core_belief, moral_taboos_json,
                active_wounds_json, mental_state, mental_state_reason,
                emotional_arc_json, current_state_summary, last_updated_chapter,
                created_at, updated_at, voice_profile_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                -- NOT NULL rejects an ownership race on the actual writer,
                -- before any relationship or aggregate changes can commit.
                novel_id=CASE WHEN unified_characters.novel_id=excluded.novel_id
                    THEN unified_characters.novel_id ELSE NULL END,
                name=excluded.name, description=excluded.description,
                public_profile=excluded.public_profile, hidden_profile=excluded.hidden_profile,
                reveal_chapter=excluded.reveal_chapter,
                gender=excluded.gender, age=excluded.age,
                appearance=excluded.appearance, personality=excluded.personality,
                background=excluded.background, core_motivation=excluded.core_motivation,
                inner_lack=excluded.inner_lack,
                verbal_tic=excluded.verbal_tic, idle_behavior=excluded.idle_behavior,
                voice_style=excluded.voice_style, sentence_pattern=excluded.sentence_pattern,
                speech_tempo=excluded.speech_tempo, core_belief=excluded.core_belief,
                voice_profile_json=excluded.voice_profile_json,
                moral_taboos_json=excluded.moral_taboos_json,
                active_wounds_json=excluded.active_wounds_json,
                mental_state=excluded.mental_state, mental_state_reason=excluded.mental_state_reason,
                updated_at=excluded.updated_at
            """,
            (
                cid,
                novel_id,
                char.name,
                char.description or "",
                getattr(char, "public_profile", "") or "",
                getattr(char, "hidden_profile", "") or "",
                getattr(char, "reveal_chapter", None),
                getattr(char, "gender", "") or "",
                getattr(char, "age", "") or "",
                getattr(char, "appearance", "") or "",
                getattr(char, "personality", "") or "",
                getattr(char, "background", "") or "",
                getattr(char, "core_motivation", "") or "",
                getattr(char, "inner_lack", "") or "",
                getattr(char, "verbal_tic", "") or "",
                getattr(char, "idle_behavior", "") or "",
                voice_profile.get("style", "") or voice_profile.get("voice_style", "") or "",
                voice_profile.get("sentence_pattern", "") or "",
                voice_profile.get("speech_tempo", "") or "",
                getattr(char, "core_belief", "") or "",
                json.dumps(getattr(char, "moral_taboos", None) or [], ensure_ascii=False),
                json.dumps(getattr(char, "active_wounds", None) or [], ensure_ascii=False),
                getattr(char, "mental_state", None) or "NORMAL",
                getattr(char, "mental_state_reason", None) or "",
                "[]",
                "",
                0,
                now,
                now,
                json.dumps(voice_profile, ensure_ascii=False),
            ),
        )

        # This collection belongs to the Bible snapshot; retained character
        # identities no longer cascade-delete it as a side effect of REPLACE.
        conn.execute('DELETE FROM unified_character_relationships WHERE character_id=?', (cid,))
        for i, rel in enumerate(char.relationships or []):
            rid = f"{cid}-rel-{i}-{uuid.uuid4().hex[:6]}"
            if isinstance(rel, str):
                target_name = rel
                relation = ""
                description = ""
            else:
                if hasattr(rel, "model_dump"):
                    rel_dict = rel.model_dump()
                elif hasattr(rel, "dict"):
                    rel_dict = rel.dict()
                else:
                    rel_dict = rel
                target_name = rel_dict.get("target", "") or ""
                relation = rel_dict.get("relation", "") or ""
                description = rel_dict.get("description", "") or ""
            conn.execute(
                """
                INSERT INTO unified_character_relationships
                (id, character_id, target_name, relation, description)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rid, cid, target_name, relation, description),
            )

    def _character_rows(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM unified_characters WHERE novel_id = ? ORDER BY id",
            (novel_id,),
        )
        return [dict(r) for r in rows]

    def _rels_for_character(self, character_id: str) -> List[Dict[str, str]]:
        rows = self.db.fetch_all(
            """
            SELECT target_name, relation, description
            FROM unified_character_relationships
            WHERE character_id = ?
            ORDER BY id
            """,
            (character_id,),
        )
        return [
            {
                "target": r["target_name"],
                "relation": r["relation"],
                "description": r["description"],
            }
            for r in rows
        ]

    def _to_mapper_dict(self, bible_id: str, novel_id: str) -> Dict[str, Any]:
        chars_out: List[Dict[str, Any]] = []
        for row in self._character_rows(novel_id):
            chars_out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"] or "",
                    "relationships": self._rels_for_character(row["id"]),
                    "gender": row.get("gender") or "",
                    "age": row.get("age") or "",
                    "appearance": row.get("appearance") or "",
                    "personality": row.get("personality") or "",
                    "background": row.get("background") or "",
                    "core_motivation": row.get("core_motivation") or "",
                    "inner_lack": row.get("inner_lack") or "",
                    "mental_state": row.get("mental_state") or "NORMAL",
                    "mental_state_reason": row.get("mental_state_reason") or "",
                    "verbal_tic": row.get("verbal_tic") or "",
                    "idle_behavior": row.get("idle_behavior") or "",
                    "public_profile": row.get("public_profile") or "",
                    "hidden_profile": row.get("hidden_profile") or "",
                    "reveal_chapter": row.get("reveal_chapter"),
                    "core_belief": row.get("core_belief") or "",
                    "moral_taboos": self._loads(row.get("moral_taboos_json"), []),
                    "voice_profile": read_character_voice_profile(row),
                    "active_wounds": self._loads(row.get("active_wounds_json"), []),
                }
            )

        return {
            "id": bible_id,
            "novel_id": novel_id,
            "characters": chars_out,
            "world_settings": self._world_settings(novel_id),
            "locations": self._locations(novel_id),
            "timeline_notes": self._timeline_notes(novel_id),
            "style_notes": self._style_notes(novel_id),
        }

    @staticmethod
    def _loads(raw: str | None, default):
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return default
        return parsed if isinstance(parsed, type(default)) else default

    def _world_settings(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM bible_world_settings WHERE novel_id = ? ORDER BY id",
            (novel_id,),
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "setting_type": r["setting_type"] or "other",
            }
            for r in rows
        ]

    def _locations(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM bible_locations WHERE novel_id = ? ORDER BY id",
            (novel_id,),
        )
        locations: List[Dict[str, Any]] = []
        for row in rows:
            r = dict(row)
            item = {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "location_type": r["location_type"] or "other",
            }
            if r.get("parent_id"):
                item["parent_id"] = r["parent_id"]
            locations.append(item)
        return locations

    def _timeline_notes(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, event, time_point, description
            FROM bible_timeline_notes
            WHERE novel_id = ?
            ORDER BY sort_order, id
            """,
            (novel_id,),
        )
        return [
            {
                "id": r["id"],
                "event": r["event"] or "",
                "time_point": r["time_point"] or "",
                "description": r["description"] or "",
            }
            for r in rows
        ]

    def _style_notes(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT id, category, content FROM bible_style_notes WHERE novel_id = ? ORDER BY id",
            (novel_id,),
        )
        return [{"id": r["id"], "category": r["category"], "content": r["content"] or ""} for r in rows]

    def get_by_id(self, bible_id: str) -> Optional[Bible]:
        row = self.db.fetch_one("SELECT * FROM bibles WHERE id = ?", (bible_id,))
        if not row:
            return None
        return self._from_row(row)

    def get_by_novel_id(self, novel_id: NovelId) -> Optional[Bible]:
        row = self.db.fetch_one("SELECT * FROM bibles WHERE novel_id = ?", (novel_id.value,))
        if not row:
            return None
        return self._from_row(row)

    def _from_row(self, row) -> Optional[Bible]:
        data = self._to_mapper_dict(row["id"], row["novel_id"])
        try:
            return BibleMapper.from_dict(data)
        except ValueError as e:
            logger.warning("Bible %s invalid: %s", row["id"], e)
            return None

    def delete(self, bible_id: str) -> None:
        row = self.db.fetch_one("SELECT novel_id FROM bibles WHERE id = ?", (bible_id,))
        if not row:
            return
        with self.db.transaction() as conn:
            self._clear_children(conn, row["novel_id"])
            conn.execute("DELETE FROM bibles WHERE id = ?", (bible_id,))

    def exists(self, bible_id: str) -> bool:
        return self.db.fetch_one("SELECT 1 AS o FROM bibles WHERE id = ?", (bible_id,)) is not None

    def update_character_anchors(
        self,
        novel_id: str,
        character_id: str,
        *,
        mental_state: str,
        verbal_tic: str,
        idle_behavior: str,
    ) -> None:
        row = self.db.fetch_one(
            "SELECT id FROM unified_characters WHERE novel_id = ? AND id = ?",
            (novel_id, character_id),
        )
        if not row:
            from domain.shared.exceptions import EntityNotFoundError

            raise EntityNotFoundError("Character", f"{novel_id}/{character_id}")
        self.db.execute(
            """
            UPDATE unified_characters
            SET mental_state = ?, verbal_tic = ?, idle_behavior = ?, updated_at = ?
            WHERE novel_id = ? AND id = ?
            """,
            (mental_state, verbal_tic, idle_behavior, self._now(), novel_id, character_id),
        )
        self.db.get_connection().commit()
