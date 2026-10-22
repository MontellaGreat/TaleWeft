"""Versioned summary vectors cannot become current through a late upsert."""
def current_chapter_vectors(rows, db=None):
    chapter_rows = [row for row in rows if (row.get("payload") or {}).get("chapter_revision") is not None or (row.get("payload") or {}).get("kind") == "chapter_summary"]
    if not chapter_rows:
        return rows
    try:
        if db is None:
            from infrastructure.persistence.database.connection import get_database
            db = get_database()
        from application.core.services.chapter_version_service import content_hash
        cache = {}
        accepted = []
        for row in rows:
            payload = row.get("payload") or {}
            if payload.get("chapter_revision") is None and payload.get("kind") != "chapter_summary":
                accepted.append(row)
                continue
            chapter_id = payload.get("chapter_id")
            key = chapter_id or (payload.get("novel_id"), payload.get("chapter_number"))
            if key not in cache:
                current = db.fetch_one("SELECT revision,content FROM chapters WHERE id=?", (chapter_id,)) if chapter_id else db.fetch_one(
                    "SELECT revision,content FROM chapters WHERE novel_id=? AND number=?", key)
                cache[key] = (current["revision"], content_hash(current["content"])) if current else None
            snapshot = cache[key]
            if payload.get("chapter_revision") is None:
                # Legacy summaries become unverifiable after the first body edit.
                if snapshot is None or snapshot[0] == 0:
                    accepted.append(row)
            elif snapshot == (payload["chapter_revision"], payload.get("chapter_content_hash")):
                accepted.append(row)
        return accepted
    except Exception:
        # A versioned vector is a read model, never a substitute for SQLite truth.
        return [row for row in rows if (row.get("payload") or {}).get("chapter_revision") is None and (row.get("payload") or {}).get("kind") != "chapter_summary"]
