"""AIGenerationService 单元测试"""
import hashlib
import json

import pytest
from unittest.mock import Mock, AsyncMock
from domain.novel.entities.novel import Novel, NovelStage
from domain.novel.value_objects.novel_id import NovelId
from domain.bible.entities.bible import Bible
from domain.bible.entities.character import Character
from domain.bible.entities.world_setting import WorldSetting
from domain.bible.value_objects.character_id import CharacterId
from domain.ai.services.llm_service import GenerationResult, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.shared.exceptions import EntityNotFoundError
from application.services.ai_generation_service import AIGenerationService
from infrastructure.persistence.database.connection import DatabaseConnection


class TestAIGenerationService:
    """AIGenerationService 单元测试"""

    @pytest.fixture
    def mock_llm_service(self):
        """创建 mock LLM 服务"""
        return Mock()

    @pytest.fixture
    def mock_novel_repository(self):
        """创建 mock Novel 仓储"""
        return Mock()

    @pytest.fixture
    def mock_bible_repository(self):
        """创建 mock Bible 仓储"""
        return Mock()

    @pytest.fixture
    def generation_db(self, tmp_path, monkeypatch):
        """Keep generation receipts away from the user's manuscript database."""
        from infrastructure.persistence.database import connection

        db = DatabaseConnection(str(tmp_path / "generation.sqlite"))
        monkeypatch.setattr(connection, "get_database", lambda *a, **kw: db)
        db.execute(
            "INSERT INTO novels(id,title,author,premise,slug) VALUES (?,?,?,?,?)",
            ("test-novel", "测试小说", "测试作者", "测试故事", "test-novel"),
        )
        db.commit()
        yield db
        db.close_all(skip_checkpoint=True)

    @pytest.fixture
    def service(self, mock_llm_service, mock_novel_repository, mock_bible_repository, generation_db):
        """创建服务实例"""
        return AIGenerationService(
            llm_service=mock_llm_service,
            novel_repository=mock_novel_repository,
            bible_repository=mock_bible_repository,
            db=generation_db,
        )

    @pytest.fixture
    def sample_novel(self):
        """创建示例小说"""
        return Novel(
            id=NovelId("test-novel"),
            title="测试小说",
            author="测试作者",
            target_chapters=10,
            stage=NovelStage.WRITING
        )

    @pytest.fixture
    def sample_bible(self):
        """创建示例 Bible"""
        bible = Bible(id="bible-1", novel_id=NovelId("test-novel"))

        # 添加人物
        character = Character(
            id=CharacterId("char-1"),
            name="张三",
            description="主角，勇敢的战士"
        )
        bible.add_character(character)

        # 添加世界设定
        setting = WorldSetting(
            id="setting-1",
            name="魔法学院",
            description="培养魔法师的地方",
            setting_type="location"
        )
        bible.add_world_setting(setting)

        return bible

    @pytest.mark.asyncio
    async def test_generate_chapter_success(
        self,
        service,
        mock_llm_service,
        mock_novel_repository,
        mock_bible_repository,
        sample_novel
    ):
        """测试成功生成章节"""
        # 准备 mock 数据
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None

        # Mock LLM 返回
        mock_result = GenerationResult(
            content="这是生成的章节内容...",
            token_usage=TokenUsage(input_tokens=100, output_tokens=200)
        )
        mock_llm_service.generate = AsyncMock(return_value=mock_result)

        # 执行
        content = await service.generate_chapter(
            novel_id="test-novel",
            chapter_number=1,
            outline="主角开始冒险"
        )

        # 验证
        assert content == "这是生成的章节内容..."
        mock_novel_repository.get_by_id.assert_called_once_with(NovelId("test-novel"))
        mock_bible_repository.get_by_novel_id.assert_called_once_with(NovelId("test-novel"))
        mock_llm_service.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_chapter_with_bible(
        self,
        service,
        mock_llm_service,
        mock_novel_repository,
        mock_bible_repository,
        sample_novel,
        sample_bible
    ):
        """测试使用 Bible 生成章节"""
        # 准备 mock 数据
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = sample_bible

        # Mock LLM 返回
        mock_result = GenerationResult(
            content="包含人物和世界设定的章节内容...",
            token_usage=TokenUsage(input_tokens=150, output_tokens=300)
        )
        mock_llm_service.generate = AsyncMock(return_value=mock_result)

        # 执行
        content = await service.generate_chapter(
            novel_id="test-novel",
            chapter_number=2,
            outline="主角遇到张三"
        )

        # 验证
        assert content == "包含人物和世界设定的章节内容..."

        # 验证 LLM 调用的 prompt 包含 Bible 信息
        call_args = mock_llm_service.generate.call_args
        prompt = call_args[0][0]
        assert isinstance(prompt, Prompt)
        assert "张三" in prompt.system
        assert "主角，勇敢的战士" in prompt.system
        assert "魔法学院" in prompt.system
        assert "培养魔法师的地方" in prompt.system
        assert "主角遇到张三" in prompt.user

    @pytest.mark.asyncio
    async def test_generate_chapter_without_bible(
        self,
        service,
        mock_llm_service,
        mock_novel_repository,
        mock_bible_repository,
        sample_novel
    ):
        """测试没有 Bible 时生成章节"""
        # 准备 mock 数据
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None

        # Mock LLM 返回
        mock_result = GenerationResult(
            content="没有 Bible 的章节内容...",
            token_usage=TokenUsage(input_tokens=80, output_tokens=150)
        )
        mock_llm_service.generate = AsyncMock(return_value=mock_result)

        # 执行
        content = await service.generate_chapter(
            novel_id="test-novel",
            chapter_number=3,
            outline="简单的情节"
        )

        # 验证
        assert content == "没有 Bible 的章节内容..."

        # 验证 prompt 不包含 Bible 信息
        call_args = mock_llm_service.generate.call_args
        prompt = call_args[0][0]
        assert isinstance(prompt, Prompt)
        assert "主要人物" not in prompt.system
        assert "世界设定" not in prompt.system
        assert "测试小说" in prompt.system

    @pytest.mark.asyncio
    async def test_generate_chapter_novel_not_found(
        self,
        service,
        mock_novel_repository,
        mock_bible_repository
    ):
        """测试小说不存在时抛出异常"""
        # 准备 mock 数据
        mock_novel_repository.get_by_id.return_value = None

        # 执行并验证
        with pytest.raises(EntityNotFoundError) as exc_info:
            await service.generate_chapter(
                novel_id="nonexistent",
                chapter_number=1,
                outline="测试大纲"
            )

        # 验证异常信息
        assert exc_info.value.entity_type == "Novel"
        assert exc_info.value.entity_id == "nonexistent"

        # 验证没有调用 Bible 仓储和 LLM
        mock_bible_repository.get_by_novel_id.assert_not_called()

    def test_build_chapter_prompt(self, service, sample_novel, sample_bible):
        """测试构建章节提示词"""
        # 执行
        prompt = service._build_chapter_prompt(
            novel=sample_novel,
            bible=sample_bible,
            chapter_number=5,
            outline="测试大纲内容"
        )

        # 验证
        assert isinstance(prompt, Prompt)

        # 验证 system message
        assert "测试小说" in prompt.system
        assert "专业的小说作家" in prompt.system
        assert "张三" in prompt.system
        assert "主角，勇敢的战士" in prompt.system
        assert "魔法学院" in prompt.system
        assert "培养魔法师的地方" in prompt.system

        # 验证 user message
        assert "第5章" in prompt.user
        assert "测试大纲内容" in prompt.user

    @pytest.mark.asyncio
    async def test_generate_chapter_empty_outline(
        self,
        service,
        mock_novel_repository
    ):
        """测试空大纲时抛出异常"""
        # 测试空字符串
        with pytest.raises(ValueError, match="Outline cannot be empty"):
            await service.generate_chapter(
                novel_id="test-novel",
                chapter_number=1,
                outline=""
            )

        # 测试只有空格
        with pytest.raises(ValueError, match="Outline cannot be empty"):
            await service.generate_chapter(
                novel_id="test-novel",
                chapter_number=1,
                outline="   "
            )

        # 验证没有调用仓储
        mock_novel_repository.get_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_chapter_llm_error(
        self,
        service,
        mock_llm_service,
        mock_novel_repository,
        mock_bible_repository,
        sample_novel
    ):
        """测试 LLM 调用失败时的异常处理"""
        # 准备 mock 数据
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None

        # Mock LLM 抛出异常
        mock_llm_service.generate = AsyncMock(side_effect=Exception("LLM service unavailable"))

        # 执行并验证
        with pytest.raises(RuntimeError, match="Failed to generate chapter"):
            await service.generate_chapter(
                novel_id="test-novel",
                chapter_number=1,
                outline="测试大纲"
            )

        # 验证调用了仓储
        mock_novel_repository.get_by_id.assert_called_once()
        mock_bible_repository.get_by_novel_id.assert_called_once()

    @pytest.mark.asyncio
    async def test_generation_archives_exact_body_scan_and_scoped_prompt(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel,
    ):
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        body = "呼吸一滞。张三把门抵住，听着外面的脚步。"
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(
            content=body, token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        ))

        assert await service.generate_chapter("test-novel", 4, "张三躲避追踪") == body

        sent_prompt = mock_llm_service.generate.call_args[0][0]
        assert "去味执行协议" in sent_prompt.system
        assert "P1. 信息密度法则" in sent_prompt.system
        prompt_row = generation_db.fetch_one("SELECT * FROM anti_ai_prompt_runs")
        scan_row = generation_db.fetch_one("SELECT * FROM anti_ai_audit_runs")
        evidence = json.loads(prompt_row["evidence_json"])
        scan = json.loads(scan_row["report_json"])
        expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert prompt_row["novel_id"] == scan_row["novel_id"] == "test-novel"
        assert prompt_row["chapter_number"] == scan_row["chapter_number"] == 4
        assert prompt_row["status"] == "completed"
        assert scan_row["scan_status"] == "completed"
        assert scan_row["persistence_status"] == "committed"
        assert evidence["content_hash"] == scan_row["content_hash"] == expected_hash
        assert evidence["anti_ai_scan"]["run_id"] == scan_row["run_id"]
        assert scan["job_id"] == prompt_row["job_id"]
        assert scan["metrics"]["critical_hits"] >= 1
        assert generation_db.fetch_one("SELECT COUNT(*) AS n FROM chapters")["n"] == 0

    @pytest.mark.asyncio
    async def test_generation_consumes_book_scoped_character_and_scene_policy(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel,
    ):
        from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates

        generation_db.execute(
            "INSERT INTO novels(id,title,author,premise,slug) VALUES (?,?,?,?,?)",
            ("other-novel", "另一部小说", "测试作者", "测试故事", "other-novel"),
        )
        for novel_id, voice in (("test-novel", "目标书角色的短句声线"), ("other-novel", "另一部书角色的长句声线")):
            generation_db.execute(
                "INSERT INTO unified_characters(id,novel_id,name,voice_style) VALUES (?,?,?,?)",
                (novel_id, novel_id, "张三", voice),
            )
        generation_db.commit()
        ChapterRewriteCandidates(generation_db).set_policy("test-novel", "scan_only", scene_type="horror")
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(
            content="张三把门抵住，听着外面的脚步。",
            token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        ))

        await service.generate_chapter(" test-novel ", 4, "张三躲避追踪")

        sent_prompt = mock_llm_service.generate.call_args[0][0]
        assert "目标书角色的短句声线" in sent_prompt.system
        assert "另一部书角色的长句声线" not in sent_prompt.system
        assert "当前场景类型：恐怖" in sent_prompt.system
        mock_novel_repository.get_by_id.assert_called_once_with(NovelId("test-novel"))
        row = generation_db.fetch_one("SELECT * FROM anti_ai_prompt_runs")
        evidence = json.loads(row["evidence_json"])
        assert row["novel_id"] == "test-novel" and row["chapter_number"] == 4
        assert evidence["scene_type"] == "horror"
        assert evidence["layers"]["L4_state_vector"]["applied"] is True

    @pytest.mark.parametrize("chapter_number", [0, -1, True, 1.5, "1", None])
    @pytest.mark.asyncio
    async def test_invalid_chapter_never_reaches_repository_or_provider(
        self, service, mock_novel_repository, mock_llm_service, chapter_number,
    ):
        with pytest.raises(ValueError, match="positive integer"):
            await service.generate_chapter("test-novel", chapter_number, "测试大纲")
        mock_novel_repository.get_by_id.assert_not_called()
        mock_llm_service.generate.assert_not_called()

    @pytest.mark.parametrize("novel_id", ["", "   ", None])
    @pytest.mark.asyncio
    async def test_invalid_novel_never_reaches_repository(
        self, service, mock_novel_repository, novel_id,
    ):
        with pytest.raises(ValueError, match="Novel ID"):
            await service.generate_chapter(novel_id, 1, "测试大纲")
        mock_novel_repository.get_by_id.assert_not_called()

    @pytest.mark.parametrize("table", ["anti_ai_audit_runs", "anti_ai_prompt_runs"])
    @pytest.mark.asyncio
    async def test_receipt_archive_failure_prevents_generation_success(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel, table,
    ):
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(
            content="张三把门抵住，听着外面的脚步。",
            token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        ))
        generation_db.execute(
            f"CREATE TRIGGER deny_receipt BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(FAIL, 'receipt store unavailable'); END"
        )
        generation_db.commit()

        with pytest.raises(RuntimeError, match="正文不可采纳|回执持久化失败"):
            await service.generate_chapter("test-novel", 1, "测试大纲")
        assert generation_db.fetch_one("SELECT COUNT(*) AS n FROM chapters")["n"] == 0
        assert generation_db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
        if table == "anti_ai_audit_runs":
            assert generation_db.fetch_one("SELECT status FROM anti_ai_prompt_runs")["status"] == "failed"

    @pytest.mark.asyncio
    async def test_reasoning_is_removed_before_the_final_scan(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel,
    ):
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        body = "张三把钥匙放进抽屉。"
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(
            content="<think>呼吸一窒，这是模型分析。</think>\n" + body,
            token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        ))
        assert await service.generate_chapter("test-novel", 1, "收好钥匙") == body
        scan = generation_db.fetch_one("SELECT * FROM anti_ai_audit_runs")
        assert scan["content_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert json.loads(scan["report_json"])["metrics"]["critical_hits"] == 0

    @pytest.mark.asyncio
    async def test_reasoning_only_output_is_not_a_completed_prose_attempt(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel,
    ):
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(
            content="<think>分析写作步骤</think>", token_usage=None,
        ))
        with pytest.raises(RuntimeError, match="正文清洗后为空"):
            await service.generate_chapter("test-novel", 1, "收好钥匙")
        assert generation_db.fetch_one("SELECT status FROM anti_ai_prompt_runs")["status"] == "failed"
        assert generation_db.fetch_one("SELECT count(*) AS n FROM anti_ai_audit_runs")["n"] == 0

    @pytest.mark.parametrize("table", ["anti_ai_prompt_runs", "anti_ai_audit_runs"])
    @pytest.mark.asyncio
    async def test_receipt_deleted_after_archive_cannot_return_success(
        self, service, generation_db, mock_llm_service, mock_novel_repository,
        mock_bible_repository, sample_novel, monkeypatch, table,
    ):
        from application.engine.rules import prose_prompt_hooks
        original = prose_prompt_hooks.archive_prompt_use
        def archive_then_delete(*args, **kwargs):
            receipt = original(*args, **kwargs)
            generation_db.execute(f"DELETE FROM {table}")
            generation_db.commit()
            return receipt
        monkeypatch.setattr(prose_prompt_hooks, "archive_prompt_use", archive_then_delete)
        mock_novel_repository.get_by_id.return_value = sample_novel
        mock_bible_repository.get_by_novel_id.return_value = None
        mock_llm_service.generate = AsyncMock(return_value=GenerationResult(content="张三收好了钥匙。", token_usage=None))
        with pytest.raises(RuntimeError, match="去味.*回执"):
            await service.generate_chapter("test-novel", 1, "收好钥匙")
        assert generation_db.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"] == 0

