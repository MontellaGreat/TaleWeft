"""Late embeddings must not replace or hide the current chapter read model."""
import asyncio
from types import SimpleNamespace

import pytest

from application.analyst.services.chapter_indexing_service import ChapterIndexingService
from application.core.services.chapter_version_service import ChapterVersionService, content_hash
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_write_fence import chapter_write_fence
from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore
from infrastructure.ai.qdrant_vector_store import QdrantVectorStore
from infrastructure.ai.chapter_vector_freshness import current_chapter_vectors


@pytest.fixture
def chapter_db(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "chapter.sqlite"))
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('n','n','n')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('c','n',1,'old')")
    db.commit()
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda *args: db)
    yield db
    db.close_all(skip_checkpoint=True)


def vector(revision, text, *, legacy=False):
    payload = {"kind": "chapter_summary", "novel_id": "n", "chapter_number": 1, "text": text}
    if not legacy:
        payload.update(chapter_id="c", chapter_revision=revision, chapter_content_hash=content_hash(text))
    return {"id": f"r{revision}", "score": 1.0, "payload": payload}


def test_freshness_filters_aba_legacy_and_wrong_hash_but_keeps_bible(chapter_db):
    versions = ChapterVersionService(chapter_db)
    versions.save_if_revision("c", 0, "new")
    versions.save_if_revision("c", 1, "old")
    bible = {"id": "bible", "payload": {"kind": "bible_snippet"}}
    wrong_hash = vector(2, "new")
    rows = [vector(0, "old"), vector(1, "new"), vector(0, "old", legacy=True), wrong_hash, vector(2, "old"), bible]
    assert current_chapter_vectors(rows, chapter_db) == [rows[-2], bible]


@pytest.mark.asyncio
async def test_late_embedding_uses_distinct_id_and_search_overfetches_stale_top_hits(chapter_db, tmp_path):
    store = ChromaDBVectorStore(persist_directory=str(tmp_path / "vectors"))
    entered, release = asyncio.Event(), asyncio.Event()

    class Embedding:
        def get_dimension(self):
            return 1

        async def embed(self, text):
            if text == "old":
                entered.set()
                await release.wait()
                return [0.0]
            return [1.0]

    indexing = ChapterIndexingService(store, Embedding())
    with chapter_write_fence(chapter_db.db_path, "c", 0, content_hash("old")):
        old = asyncio.create_task(indexing.index_chapter_summary("n", 1, "old"))
    await entered.wait()
    ChapterVersionService(chapter_db).save_if_revision("c", 0, "new")
    with chapter_write_fence(chapter_db.db_path, "c", 1, content_hash("new")):
        await indexing.index_chapter_summary("n", 1, "new")
    release.set()
    await old
    collection = "novel_n_chunks"
    assert len(store.collections[collection]["metadata"]) == 2
    results = await store.search(collection, [0.0], 1)
    assert len(results) == 1
    assert results[0]["payload"]["chapter_revision"] == 1
    assert results[0]["payload"]["text"] == "new"


@pytest.mark.asyncio
async def test_qdrant_paginates_past_stale_history(chapter_db):
    ChapterVersionService(chapter_db).save_if_revision("c", 0, "new")
    rows = [SimpleNamespace(id=f"old{i}", score=1.0, payload=vector(0, "old")["payload"]) for i in range(40)]
    rows.append(SimpleNamespace(id="new", score=0.5, payload=vector(1, "new")["payload"]))
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return rows[kwargs["offset"]:kwargs["offset"] + kwargs["limit"]]

    store = object.__new__(QdrantVectorStore)
    store._client = SimpleNamespace(search=search)
    result = await store.search("collection", [1.0], 1)
    assert [row["id"] for row in result] == ["new"]
    assert [call["offset"] for call in calls] == [0, 32]
