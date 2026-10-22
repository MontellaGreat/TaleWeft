"""TaleWeft creation accepts long premises without truncating persisted text."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.core.services.novel_service import NovelService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from interfaces.api.dependencies import get_novel_service
from interfaces.api.v1.core.novels import router


@pytest.fixture
def novel_client(tmp_path):
    db = DatabaseConnection(str(tmp_path / "novels.db"))
    service = NovelService(SqliteNovelRepository(db), SqliteChapterRepository(db))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_novel_service] = lambda: service
    try:
        with TestClient(app) as client:
            yield client, db
    finally:
        db.close_all()


@pytest.mark.parametrize("length", [2001, 10000])
def test_long_premise_round_trips_through_api_and_database(novel_client, length):
    client, db = novel_client
    # Distinct trailing text detects truncation at either the old or new boundary.
    premise = "故事" * ((length - 1) // 2) + "终"
    if len(premise) < length:
        premise = "序" + premise
    assert len(premise) == length
    response = client.post("/api/v1/novels/", json={
        "novel_id": "long-premise", "title": "长梗概", "author": "测试作者",
        "target_chapters": 2, "premise": premise,
    })
    assert response.status_code == 201, response.text
    assert response.json()["premise"] == premise
    assert client.get("/api/v1/novels/long-premise").json()["premise"] == premise
    assert db.fetch_one("SELECT premise FROM novels WHERE id = ?", ("long-premise",))["premise"] == premise


def test_overlong_premise_is_rejected_before_creating_book(novel_client):
    client, db = novel_client
    response = client.post("/api/v1/novels/", json={
        "novel_id": "over-limit", "title": "超限", "author": "测试作者",
        "premise": "文" * 10001,
    })
    assert response.status_code == 422
    assert any(error["loc"] == ["body", "premise"] for error in response.json()["detail"])
    assert db.fetch_one("SELECT id FROM novels WHERE id = ?", ("over-limit",)) is None
