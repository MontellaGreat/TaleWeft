"""SQLite implementation of voice fingerprint repository."""
from typing import Optional
from uuid import uuid4

from domain.novel.repositories.voice_fingerprint_repository import (
    VoiceFingerprintRepository,
)


class SQLiteVoiceFingerprintRepository(VoiceFingerprintRepository):
    """SQLite implementation of voice fingerprint repository."""

    def __init__(self, db_connection):
        """Initialize repository with database connection.

        Args:
            db_connection: SQLite database connection
        """
        self.db = db_connection

    def get_by_novel(
        self, novel_id: str, pov_character_id: Optional[str] = None
    ) -> Optional[dict]:
        """Get fingerprint by novel ID and optional POV character."""
        row = self.db.fetch_one(
            """
            SELECT fingerprint_id, novel_id, pov_character_id,
                   adjective_density, avg_sentence_length, sentence_count,
                   sample_count, last_updated
            FROM voice_fingerprint
            WHERE novel_id = ? AND (pov_character_id = ? OR (pov_character_id IS NULL AND ? IS NULL))
            """,
            (novel_id, pov_character_id, pov_character_id),
        )
        if not row:
            return None

        return {
            "fingerprint_id": row["fingerprint_id"],
            "novel_id": row["novel_id"],
            "pov_character_id": row["pov_character_id"],
            "adjective_density": row["adjective_density"],
            "avg_sentence_length": row["avg_sentence_length"],
            "sentence_count": row["sentence_count"],
            "sample_count": row["sample_count"],
            "last_updated": row["last_updated"],
        }

    def upsert(
        self,
        novel_id: str,
        fingerprint_data: dict,
        pov_character_id: Optional[str] = None,
    ) -> str:
        """Insert or update fingerprint data."""
        metrics = fingerprint_data["metrics"]
        sample_count = fingerprint_data["sample_count"]
        # Both statements run together on the existing writer. A separate read
        # before an insert races another recomputation and can leave a write open.
        fingerprint_id = str(uuid4())
        with self.db.transaction() as writer:
            writer.execute(
                """
                UPDATE voice_fingerprint
                SET adjective_density = ?,
                    avg_sentence_length = ?,
                    sentence_count = ?,
                    sample_count = ?,
                    last_updated = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND pov_character_id IS ?
                """,
                (
                    metrics["adjective_density"],
                    metrics["avg_sentence_length"],
                    metrics["sentence_count"],
                    sample_count,
                    novel_id, pov_character_id,
                ),
            )
            writer.execute(
                """
                INSERT INTO voice_fingerprint
                (fingerprint_id, novel_id, pov_character_id, adjective_density,
                 avg_sentence_length, sentence_count, sample_count)
                SELECT ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM voice_fingerprint WHERE novel_id=? AND pov_character_id IS ?)
                """,
                (
                    fingerprint_id,
                    novel_id,
                    pov_character_id,
                    metrics["adjective_density"],
                    metrics["avg_sentence_length"],
                    metrics["sentence_count"],
                    sample_count,
                    novel_id, pov_character_id,
                ),
            )
        return self.get_by_novel(novel_id, pov_character_id)["fingerprint_id"]
