import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.core.services.scene_generation_context import (
    InvalidSceneGenerationContextError,
    SceneChapterNotFoundError,
    SceneGenerationContextProvider,
)
from application.core.services.scene_generation_service import SceneGenerationService
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.value_objects.scene import Scene
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository


@pytest.fixture
def scene_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection

    db = DatabaseConnection(str(tmp_path / "scenes.sqlite"))
    monkeypatch.setattr(connection, "get_database", lambda *a, **kw: db)
    for novel_id, voice in (("one", "本书角色的短句声线"), ("two", "另书角色的长句声线")):
        db.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES (?,?,?,?,?)",
                   (novel_id, novel_id, "测试", "测试", novel_id))
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES (?,?,?,?,?)",
                   (f"chapter-{novel_id}", novel_id, 4, "追踪", "原有章节正文"))
        db.execute("INSERT INTO unified_characters(id,novel_id,name,voice_style) VALUES (?,?,?,?)",
                   (novel_id, novel_id, "沈岚", voice))
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


@pytest.fixture
def scene():
    return Scene(title="旧车站追问", goal="找到失踪证人", pov_character="沈岚",
                 location="旧车站", tone="紧张", estimated_words=800, order_index=0)


@pytest.fixture
def scene_service(scene_db, monkeypatch):
    llm = Mock(generate=AsyncMock(return_value=GenerationResult(
        content=" \n<think>他邪魅一笑，分析追踪路线。</think>\n沈岚呼吸一滞。她把门抵住。 \n",
        token_usage=TokenUsage(input_tokens=100, output_tokens=50),
    )))
    director = Mock(analyze=AsyncMock(return_value=SimpleNamespace(
        characters=["沈岚"], locations=["旧车站"], pov="沈岚", emotional_state="警惕",
    )))
    service = SceneGenerationService(llm, director, db=scene_db)
    monkeypatch.setattr(service, "_build_scene_prompt", lambda **kwargs: Prompt(
        system="你是小说作家。", user="沈岚在旧车站寻找失踪证人。",
    ))
    return service


@pytest.mark.asyncio
async def test_scene_without_bible_scans_clean_final_body_and_binds_real_chapter(scene_db, scene_service, scene):
    provider = SceneGenerationContextProvider(chapter_repository=SqliteChapterRepository(scene_db))
    context = await provider.build("chapter-one", 0, expected_chapter_number=4)
    body = await scene_service.generate_scene(scene, 4, context.previous_scenes,
                                              context.bible_context, chapter_id=context.chapter_id)

    assert body == "沈岚呼吸一滞。她把门抵住。"
    sent = scene_service.llm_service.generate.call_args.args[0]
    assert "去味执行协议" in sent.system and "P1. 信息密度法则" in sent.system
    assert "本书角色的短句声线" in sent.system and "另书角色的长句声线" not in sent.system
    prompt_row = scene_db.fetch_one("SELECT * FROM anti_ai_prompt_runs")
    scan_row = scene_db.fetch_one("SELECT * FROM anti_ai_audit_runs")
    prompt = json.loads(prompt_row["evidence_json"])
    scan = json.loads(scan_row["report_json"])
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert prompt_row["novel_id"] == scan_row["novel_id"] == "one"
    assert prompt_row["chapter_number"] == scan_row["chapter_number"] == 4
    assert prompt_row["status"] == "completed"
    assert scan_row["scan_status"] == "completed" and scan_row["persistence_status"] == "committed"
    assert prompt["content_hash"] == scan_row["content_hash"] == scan["content_hash"] == expected_hash
    assert prompt["anti_ai_scan"]["run_id"] == scan_row["run_id"]
    assert scan["job_id"] == prompt_row["job_id"]
    assert scan["metrics"]["critical_hits"] == 1
    assert scene_db.fetch_one("SELECT content FROM chapters WHERE id='chapter-one'")["content"] == "原有章节正文"


@pytest.mark.asyncio
@pytest.mark.parametrize("chapter_number", [1, 0, True, 4.0, "4"])
async def test_scene_rejects_unverified_chapter_before_provider(scene_service, scene, chapter_number):
    with pytest.raises(InvalidSceneGenerationContextError):
        await scene_service.generate_scene(scene, chapter_number, [], chapter_id="chapter-one")
    scene_service.scene_director.analyze.assert_not_called()
    scene_service.llm_service.generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bible_context", [{"novel_id": "two"}, {"characters": [{"name": "沈岚"}]}])
async def test_scene_rejects_cross_book_or_unscoped_bible_before_provider(scene_service, scene, bible_context):
    with pytest.raises(InvalidSceneGenerationContextError, match="Bible 所属小说"):
        await scene_service.generate_scene(scene, 4, [], bible_context, chapter_id="chapter-one")
    scene_service.scene_director.analyze.assert_not_called()
    scene_service.llm_service.generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chapter_id", [None, "missing"])
async def test_scene_requires_existing_chapter(scene_service, scene, chapter_id):
    with pytest.raises((InvalidSceneGenerationContextError, SceneChapterNotFoundError)):
        await scene_service.generate_scene(scene, 4, [], chapter_id=chapter_id)
    scene_service.scene_director.analyze.assert_not_called()
    scene_service.llm_service.generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["anti_ai_audit_runs", "anti_ai_prompt_runs"])
async def test_scene_receipt_failure_cannot_return_success(scene_db, scene_service, scene, table):
    scene_db.execute(f"CREATE TRIGGER deny_receipt BEFORE INSERT ON {table} "
                     "BEGIN SELECT RAISE(FAIL, 'receipt store unavailable'); END")
    scene_db.commit()
    with pytest.raises((RuntimeError, ValueError), match="正文不可采纳|回执持久化失败"):
        await scene_service.generate_scene(scene, 4, [], chapter_id="chapter-one")
    assert scene_db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    assert scene_db.fetch_one("SELECT content FROM chapters WHERE id='chapter-one'")["content"] == "原有章节正文"
    if table == "anti_ai_audit_runs":
        assert scene_db.fetch_one("SELECT status FROM anti_ai_prompt_runs")["status"] == "failed"


@pytest.mark.asyncio
async def test_scene_reasoning_only_output_does_not_archive_success(scene_db, scene_service, scene):
    scene_service.llm_service.generate.return_value.content = "<think>只包含内部推理。</think>"
    with pytest.raises(ValueError, match="正文清洗后为空"):
        await scene_service.generate_scene(scene, 4, [], chapter_id="chapter-one")
    assert scene_db.fetch_one("SELECT status FROM anti_ai_prompt_runs")["status"] == "failed"
    assert scene_db.fetch_one("SELECT COUNT(*) AS n FROM anti_ai_audit_runs")["n"] == 0


@pytest.mark.asyncio
async def test_scene_rechecks_durable_receipt_before_returning(scene_db, scene_service, scene, monkeypatch):
    from application.engine.rules import prose_prompt_hooks
    archive = prose_prompt_hooks.archive_prompt_use

    def archive_then_remove(*args, **kwargs):
        receipt = archive(*args, **kwargs)
        scene_db.execute("DELETE FROM anti_ai_prompt_runs WHERE run_id=?", (receipt["run_id"],))
        scene_db.commit()
        return receipt

    monkeypatch.setattr(prose_prompt_hooks, "archive_prompt_use", archive_then_remove)
    with pytest.raises(ValueError, match="缺少耐久去味执行回执"):
        await scene_service.generate_scene(scene, 4, [], chapter_id="chapter-one")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "renumbered", "moved_book"])
async def test_scene_rechecks_real_chapter_scope_after_provider(scene_db, scene_service, scene, change):
    result = scene_service.llm_service.generate.return_value

    async def generate_then_change_chapter(*_):
        if change == "deleted":
            scene_db.execute("DELETE FROM chapters WHERE id='chapter-one'")
        elif change == "renumbered":
            scene_db.execute("UPDATE chapters SET number=5 WHERE id='chapter-one'")
        else:
            scene_db.execute("DELETE FROM chapters WHERE id='chapter-two'")
            scene_db.execute("UPDATE chapters SET novel_id='two' WHERE id='chapter-one'")
        scene_db.commit()
        return result

    scene_service.llm_service.generate.side_effect = generate_then_change_chapter
    with pytest.raises((InvalidSceneGenerationContextError, SceneChapterNotFoundError)):
        await scene_service.generate_scene(scene, 4, [], chapter_id="chapter-one")


@pytest.mark.asyncio
async def test_scene_api_keeps_request_contract_and_passes_verified_scope(scene_db, scene_service, scene):
    from interfaces.api.v1.core.scene_generation_routes import GenerateSceneRequest, generate_scene

    context = SceneGenerationContextProvider(chapter_repository=SqliteChapterRepository(scene_db))
    beat = Mock(get_beat_sheet=AsyncMock(return_value=SimpleNamespace(scenes=[scene])))
    response = await generate_scene(GenerateSceneRequest(chapter_id="chapter-one", chapter_number=4, scene_index=0),
                                    scene_service, beat, context)
    assert response.content == "沈岚呼吸一滞。她把门抵住。"
    assert response.scene_index == 0 and response.word_count == len(response.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("chapter_id,chapter_number,status", [("chapter-one", 1, 422), ("missing", 4, 404)])
async def test_scene_api_rejects_identity_mismatch_before_generation(
        scene_db, scene_service, scene, chapter_id, chapter_number, status):
    from fastapi import HTTPException
    from interfaces.api.v1.core.scene_generation_routes import GenerateSceneRequest, generate_scene

    context = SceneGenerationContextProvider(chapter_repository=SqliteChapterRepository(scene_db))
    beat = Mock(get_beat_sheet=AsyncMock(return_value=SimpleNamespace(scenes=[scene])))
    with pytest.raises(HTTPException) as error:
        await generate_scene(GenerateSceneRequest(chapter_id=chapter_id, chapter_number=chapter_number, scene_index=0),
                             scene_service, beat, context)
    assert error.value.status_code == status
    scene_service.llm_service.generate.assert_not_called()
    scene_service.scene_director.analyze.assert_not_called()
