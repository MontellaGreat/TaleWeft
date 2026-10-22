"""Real database regressions for measured read optimizations and project isolation."""
import pytest
from fastapi import FastAPI
import httpx

from application.core.services.novel_service import NovelService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from interfaces.api.stats.repositories.sqlite_stats_repository_adapter import SqliteStatsRepositoryAdapter
from interfaces.api.stats.repositories.stats_repository import StatsRepository
from interfaces.api.stats.services.stats_service import StatsService
from interfaces.api.dependencies import get_novel_service
from interfaces.api.v1.core.novels import router


@pytest.fixture
def project_databases(tmp_path):
    databases = [DatabaseConnection(str(tmp_path / f'project-{n}.sqlite')) for n in range(2)]
    for n, db in enumerate(databases):
        db.execute("INSERT INTO novels(id,title,slug) VALUES('book',?,'book')", (f'project {n}',))
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES('chapter','book',1,'Title',?)", ('中文 hello world' if n == 0 else '另项目 text',))
        db.commit()
    yield databases
    for db in databases:
        db.close_all()


def test_stats_keep_mixed_language_semantics_cache_hits_and_legacy_writes(project_databases):
    db = project_databases[0]
    repo = SqliteStatsRepositoryAdapter(db)
    service = StatsService(repo)
    assert service.get_book_stats('book').total_words == 4
    statements = []
    db._get_raw_connection().set_trace_callback(statements.append)
    assert service.get_book_stats('book').total_words == 4
    assert not any('SELECT' in sql and ',content FROM' in sql for sql in statements)
    # Old SQL writers still advance the revision trigger and invalidate metrics.
    db.execute("UPDATE chapters SET content='重写 English phrase here' WHERE id='chapter'")
    db.commit()
    assert service.get_book_stats('book').total_words == 5
    db.close_all()
    assert service.get_book_stats('book').total_words == 5
    for text in ['中文 hello world', 'Hello中World', 'word123 test', '𠀀字符 punctuation!', '   ', '一\n二']:
        assert repo.count_words(text) == StatsRepository('.').count_words(text)


def test_same_ids_never_share_statistics_or_novel_content(project_databases):
    left, right = project_databases
    left_repo, right_repo = SqliteStatsRepositoryAdapter(left), SqliteStatsRepositoryAdapter(right)
    assert StatsService(left_repo).get_book_stats('book').total_words == 4
    assert StatsService(right_repo).get_book_stats('book').total_words == 4
    left.execute("UPDATE chapters SET content='仅甲项目修改 seven more words' WHERE id='chapter'")
    left.commit()
    assert StatsService(right_repo).get_book_stats('book').total_words == 4
    for db, expected in [(left, '仅甲项目'), (right, '另项目')]:
        service = NovelService(SqliteNovelRepository(db), SqliteChapterRepository(db))
        assert expected in service.get_novel('book').chapters[0].content


@pytest.mark.asyncio
async def test_compact_http_preserves_metadata_totals_and_full_content_opt_in(project_databases):
    db = project_databases[0]
    service = NovelService(SqliteNovelRepository(db), SqliteChapterRepository(db))
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_novel_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        full = (await client.get('/api/v1/novels/book')).json()
        compact = (await client.get('/api/v1/novels/book?include_content=false')).json()
        compact_list = (await client.get('/api/v1/novels/?include_content=false')).json()
        assert compact['chapters'][0]['content'] == compact_list[0]['chapters'][0]['content'] == ''
        assert full['chapters'][0]['content'] == '中文 hello world'
        assert full['total_word_count'] == compact['total_word_count'] == len('中文 hello world')
        assert full['chapters'][0]['word_count'] == compact['chapters'][0]['word_count']
        statistics = (await client.get('/api/v1/novels/book/statistics')).json()
        assert statistics['total_words'] == full['total_word_count']


def test_entity_rebuild_uses_explicit_project_database(project_databases, monkeypatch):
    from application.manuscript import reindex_job
    from infrastructure.persistence.database.manuscript_entity_repository import ManuscriptEntityRepository
    left, right = project_databases
    monkeypatch.setattr(reindex_job, 'get_database', lambda: (_ for _ in ()).throw(AssertionError('global project database used')))
    calls = []
    original = ManuscriptEntityRepository.replace_chapter_mentions
    def record(self, novel_id, chapter_number, rows):
        calls.append(self.db)
        return original(self, novel_id, chapter_number, rows)
    monkeypatch.setattr(ManuscriptEntityRepository, 'replace_chapter_mentions', record)
    reindex_job.reindex_chapter_entity_mentions('book', 1, '正文', strict=True, database=left)
    assert calls == [left]
    assert right.fetch_all('SELECT * FROM chapter_entity_mentions') == []


@pytest.mark.asyncio
async def test_real_durable_rebuild_persists_vector_and_entity_index_in_only_its_project(project_databases, tmp_path):
    import json
    from types import SimpleNamespace
    from application.core.services.chapter_derivation_jobs import run_chapter_derivation_job
    from application.analyst.services.chapter_indexing_service import ChapterIndexingService
    from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore
    from infrastructure.persistence.database.manuscript_entity_repository import ManuscriptEntityRepository
    left, right = project_databases
    text = '中文 [[prop:prop-1|铜镜]] hello'
    left.execute("UPDATE chapters SET content=? WHERE id='chapter'", (text,))
    revision = left.fetch_one("SELECT revision FROM chapters WHERE id='chapter'")['revision']
    left.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) VALUES('rebuild','chapter','book',1,?,?)",
        (revision, json.dumps({'content': text, 'operation': 'rebuild_indexes'})))
    left.commit()
    class Embedding:
        def get_dimension(self):
            return 2
        async def embed(self, value):
            assert value == text
            return [1.0, 0.0]
    directory = tmp_path / 'project-left-vectors'
    vector = ChromaDBVectorStore(persist_directory=str(directory), database=left)
    indexing = ChapterIndexingService(vector, Embedding())
    pipeline = SimpleNamespace(_indexing=indexing)
    assert await run_chapter_derivation_job(left, 'rebuild', pipeline, None) == 'completed'
    # Reopen the actual local vector backend, without a model/network call.
    restored = ChromaDBVectorStore(persist_directory=str(directory), database=left)
    hits = await restored.search(collection='novel_book_chunks', query_vector=[1.0, 0.0], limit=5)
    assert len(hits) == 1
    assert hits[0]['payload']['text'] == text
    assert hits[0]['payload']['chapter_revision'] == revision
    assert left.fetch_one("SELECT status FROM chapter_derivation_jobs WHERE job_id='rebuild'")['status'] == 'completed'
    assert left.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == text
    assert ManuscriptEntityRepository(left).list_chapter_mentions('book', 1)[0]['entity_id'] == 'prop-1'
    assert right.fetch_all('SELECT * FROM chapter_entity_mentions') == []
    assert right.fetch_all('SELECT * FROM chapter_derivation_jobs') == []
