"""Novel 应用服务"""
import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from domain.novel.entities.novel import Novel, NovelStage
from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from domain.novel.value_objects.word_count import WordCount
from domain.novel.repositories.novel_repository import NovelRepository
from domain.novel.repositories.chapter_repository import ChapterRepository
from domain.shared.exceptions import EntityNotFoundError
from application.core.dtos.novel_dto import NovelDTO, ChapterDTO
from application.core.chapter_target_limits import clamp_chapter_target_words
from application.core.v1_length_tiers import resolve_v1_length_params
from application.core.taxonomy.market_selection import normalize_market_preferences, MARKET_FIELDS, PROFILE_FIELDS
from domain.structure.story_node import StoryNode, NodeType, PlanningStatus, PlanningSource
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class NovelService:
    """Novel 应用服务

    协调领域对象和基础设施，实现应用用例。
    """

    def __init__(
        self,
        novel_repository: NovelRepository,
        chapter_repository: ChapterRepository,
        story_node_repository: Optional[StoryNodeRepository] = None,
    ):
        """初始化服务

        Args:
            novel_repository: Novel 仓储
            chapter_repository: Chapter 仓储（统计以落盘章节为准）
            story_node_repository: StoryNode 仓储（用于同步叙事结构）
        """
        self.novel_repository = novel_repository
        self.chapter_repository = chapter_repository
        self.story_node_repository = story_node_repository

    def _hydrate_chapters(self, novel: Novel) -> Novel:
        """用 Chapter 仓储补齐 DTO 所需章节列表。"""
        if self.chapter_repository is None:
            return novel
        try:
            chapters = self.chapter_repository.list_by_novel(novel.novel_id)
            if isinstance(chapters, list):
                novel.chapters = chapters
        except Exception:
            pass
        return novel

    def ensure_default_act_for_chapters(self, novel_id: str) -> None:
        """若无任何「幕」节点，创建默认第一幕，以便 add_chapter 能挂接章节到叙事结构树。"""
        if not self.story_node_repository:
            return
        tree = self.story_node_repository.get_tree_sync(novel_id)
        acts = [n for n in tree.nodes if n.node_type == NodeType.ACT]
        if acts:
            return
        act_node = StoryNode(
            id=f"act-{novel_id}-1",
            novel_id=novel_id,
            node_type=NodeType.ACT,
            number=1,
            title="第一幕",
            description="初始规划自动创建，可在结构视图中重命名",
            parent_id=None,
            order_index=0,
            planning_status=PlanningStatus.CONFIRMED,
            planning_source=PlanningSource.AI_MACRO,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self.story_node_repository.save_sync(act_node)

    @staticmethod
    def _compose_premise_with_presets(
        premise: str,
        genre: str = "",
        world_preset: str = "",
        story_structure: str = "",
        pacing_control: str = "",
        writing_style: str = "",
        special_requirements: str = "",
    ) -> str:
        """Return only the user-authored premise; presets must not be spliced into it."""
        return (premise or "").strip()

    def _sync_variable_hub_from_novel(self, novel: Novel) -> None:
        try:
            from application.ai_invocation.variable_hub import VariableWrite
            from infrastructure.persistence.database.connection import DatabaseConnection
            from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
        except Exception:
            return

        context_key = f"novel_id:{novel.novel_id.value}"
        generation_prefs = getattr(novel, "generation_prefs", None)
        genre_label = str(getattr(generation_prefs, "locked_genre", "") or "").strip()
        world_preset = str(getattr(generation_prefs, "locked_world_preset", "") or "").strip()
        story_structure = str(getattr(generation_prefs, "locked_story_structure", "") or "").strip()
        pacing_control = str(getattr(generation_prefs, "locked_pacing_control", "") or "").strip()
        writing_style = str(getattr(generation_prefs, "locked_writing_style", "") or "").strip()
        special_requirements = str(getattr(generation_prefs, "locked_special_requirements", "") or "").strip()
        audience_mode = str(getattr(generation_prefs, "audience_mode", "") or "").strip()
        canonical_genre_id = str(getattr(generation_prefs, "canonical_genre_id", "") or "").strip()
        canonical_theme_id = str(getattr(generation_prefs, "canonical_theme_id", "") or "").strip()
        market_platform = str(getattr(generation_prefs, "market_platform", "") or "").strip()
        taxonomy_version = str(getattr(generation_prefs, "taxonomy_version", "") or "").strip()
        profile_version = str(getattr(generation_prefs, "profile_version", "") or "").strip()
        reader_contract = str(getattr(generation_prefs, "reader_contract", "") or "").strip()
        category_path = list(getattr(generation_prefs, "market_category_path", []) or [])
        market_tags = list(getattr(generation_prefs, "market_tags", []) or [])
        parts = [part.strip() for part in genre_label.split("/") if part.strip()]
        values = [
            ("novel.title", str(novel.title or "").strip(), "string", "书名"),
            ("novel.premise", str(novel.premise or "").strip(), "string", "故事创意"),
            ("novel.target_chapters", int(getattr(novel, "target_chapters", 0) or 0), "integer", "目标章节数"),
            (
                "novel.target_words_per_chapter",
                int(getattr(novel, "target_words_per_chapter", 0) or 0),
                "integer",
                "每章目标字数",
            ),
            ("novel.genre_label", genre_label, "string", "类型标签"),
            ("novel.genre_major", parts[0] if parts else "", "string", "类型大类"),
            ("novel.genre_theme", " / ".join(parts[1:]) if len(parts) > 1 else "", "string", "类型主题"),
            ("novel.world_preset", world_preset, "string", "世界基调"),
            ("novel.story_structure", story_structure, "string", "剧情结构"),
            ("novel.pacing_control", pacing_control, "string", "节奏把控"),
            ("novel.writing_style", writing_style, "string", "写作风格"),
            ("novel.special_requirements", special_requirements, "string", "特殊要求"),
            ("novel.audience_mode", audience_mode, "string", "读者频道"),
            ("novel.canonical_genre_id", canonical_genre_id, "string", "规范大类 ID"),
            ("novel.canonical_theme_id", canonical_theme_id, "string", "规范主题 ID"),
            ("novel.market_platform", market_platform, "string", "目标平台"),
            ("novel.taxonomy_version", taxonomy_version, "string", "分类版本"),
            ("novel.profile_version", profile_version, "string", "画像版本"),
            ("novel.reader_contract", reader_contract, "string", "读者契约"),
            ("novel.market_category_path", category_path, "list", "市场分类路径"),
            ("novel.market_tags", market_tags, "list", "市场标签"),
        ]
        db = getattr(self.novel_repository, "db", None)
        if not isinstance(db, DatabaseConnection):
            return
        repo = SqliteVariableHubRepository(db)
        for key, value, value_type, display_name in values:
            if value in ("", None):
                continue
            repo.set_value(
                VariableWrite(
                    key=key,
                    value=value,
                    context_key=context_key,
                    source_trace_id="novel_service_sync",
                    source_node_key="novel_service",
                    lineage={"source": "novel_service"},
                    value_type=value_type,
                    display_name=display_name,
                    scope="global",
                    stage="setup",
                )
            )

    def create_novel(
        self,
        novel_id: str,
        title: str,
        author: str,
        target_chapters: int,
        premise: str = "",
        genre: str = "",
        world_preset: str = "",
        story_structure: str = "",
        pacing_control: str = "",
        writing_style: str = "",
        special_requirements: str = "",
        audience_mode: str = "",
        canonical_genre_id: str = "",
        canonical_theme_id: str = "",
        market_platform: str = "",
        market_category_path: Optional[List[str]] = None,
        market_tags: Optional[List[str]] = None,
        taxonomy_version: str = "",
        profile_version: str = "",
        reader_contract: str = "",
        custom_market_profile: Optional[Dict[str, Any]] = None,
        market_profile_snapshot: Optional[Dict[str, Any]] = None,
        profile_snapshot_hash: str = "",
        length_tier: Optional[str] = None,
        target_words_per_chapter: Optional[int] = None,
    ) -> NovelDTO:
        """创建新小说

        Args:
            novel_id: 小说 ID
            title: 标题
            author: 作者
            target_chapters: 目标章节数（未使用 V1 体量档时有效）
            premise: 故事梗概/创意
            genre: 赛道/类型（前端下拉预设；不写入 premise）
            world_preset: 世界观基调（前端下拉预设；不写入 premise）
            story_structure: 剧情结构（前端按题材预设；不写入 premise）
            pacing_control: 节奏把控（前端按题材预设；不写入 premise）
            writing_style: 写作风格（前端按题材预设；不写入 premise）
            special_requirements: 特殊要求（前端按题材预设；不写入 premise）
            length_tier: V1 体量档 short|standard|epic；若指定则由服务端推导章数与每章字数
            target_words_per_chapter: 每章目标字数（可选；与体量档或自定义章数搭配）

        Returns:
            NovelDTO
        """
        chapters, wpc, _tier_norm = resolve_v1_length_params(
            length_tier, target_chapters, target_words_per_chapter
        )
        market_values = normalize_market_preferences({
            "taxonomy_version": taxonomy_version,
            "profile_version": profile_version,
            "audience_mode": audience_mode,
            "canonical_genre_id": canonical_genre_id,
            "canonical_theme_id": canonical_theme_id,
            "market_platform": market_platform,
            "market_category_path": [] if market_category_path is None else market_category_path,
            "market_tags": [] if market_tags is None else market_tags,
            "reader_contract": reader_contract,
            "custom_market_profile": {} if custom_market_profile is None else custom_market_profile,
            "locked_world_preset": world_preset,
            "locked_story_structure": story_structure,
            "locked_pacing_control": pacing_control,
            "locked_writing_style": writing_style,
            "locked_special_requirements": special_requirements,
        })
        world_preset = market_values["locked_world_preset"]
        story_structure = market_values["locked_story_structure"]
        pacing_control = market_values["locked_pacing_control"]
        writing_style = market_values["locked_writing_style"]
        special_requirements = market_values["locked_special_requirements"]
        taxonomy_version = market_values["taxonomy_version"]
        profile_version = market_values["profile_version"]
        custom_market_profile = market_values["custom_market_profile"]
        market_profile_snapshot = market_values["market_profile_snapshot"]
        profile_snapshot_hash = market_values["profile_snapshot_hash"]
        user_block = self._compose_premise_with_presets(
            premise,
            genre,
            world_preset,
            story_structure,
            pacing_control,
            writing_style,
            special_requirements,
        )
        novel = Novel(
            id=NovelId(novel_id),
            title=title,
            author=author,
            target_chapters=chapters,
            premise=user_block,
            stage=NovelStage.PLANNING,
            target_words_per_chapter=wpc,
            generation_prefs=GenerationPreferences(
                locked_genre=str(genre or "").strip(),
                locked_world_preset=str(world_preset or "").strip(),
                locked_story_structure=str(story_structure or "").strip(),
                locked_pacing_control=str(pacing_control or "").strip(),
                locked_writing_style=str(writing_style or "").strip(),
                locked_special_requirements=str(special_requirements or "").strip(),
                audience_mode=market_values["audience_mode"],
                canonical_genre_id=market_values["canonical_genre_id"],
                canonical_theme_id=market_values["canonical_theme_id"],
                market_platform=market_values["market_platform"],
                market_category_path=market_values["market_category_path"],
                market_tags=market_values["market_tags"],
                taxonomy_version=str(taxonomy_version or "").strip(),
                profile_version=str(profile_version or "").strip(),
                reader_contract=market_values["reader_contract"],
                custom_market_profile=dict(custom_market_profile or {}),
                # Always derive the persisted snapshot and digest from the values
                # actually saved below; never accept a client-supplied hash as proof.
                market_profile_snapshot=market_profile_snapshot,
                profile_snapshot_hash=profile_snapshot_hash,
            ),
        )

        self.novel_repository.save(novel)
        self._sync_variable_hub_from_novel(novel)

        return NovelDTO.from_domain(novel)

    def _novel_dto(self, novel: Novel, include_content: bool) -> NovelDTO:
        summary_provider = getattr(self.chapter_repository, 'list_summaries_by_novel', None)
        if not include_content and callable(summary_provider):
            summaries = summary_provider(novel.novel_id)
            if isinstance(summaries, list):
                return NovelDTO.from_domain(novel, chapter_dtos=[ChapterDTO(**row) for row in summaries])
        dto = NovelDTO.from_domain(self._hydrate_chapters(novel))
        if not include_content:
            for chapter in dto.chapters:
                chapter.content = ''
        return dto

    def get_novel(self, novel_id: str, *, include_content: bool = True) -> Optional[NovelDTO]:
        novel = self.novel_repository.get_by_id(NovelId(novel_id))

        if novel is None:
            return None

        dto = self._novel_dto(novel, include_content)

        dto.has_bible = self._check_has_bible(novel_id)
        dto.has_outline = self._check_has_outline(novel_id)

        return dto

    def _check_has_bible(self, novel_id: str) -> bool:
        storage = getattr(self.novel_repository, "storage", None)
        if storage is not None and hasattr(storage, "exists"):
            try:
                return bool(storage.exists(f"novels/{novel_id}/bible.json"))
            except Exception:
                pass

        try:
            from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
            from infrastructure.persistence.database.connection import get_database
            # Respect the repository's project database; a global default would
            # read another project's metadata in isolated/multi-project sessions.
            database = getattr(self.novel_repository, 'db', None) or get_database()
            bible_repo = SqliteBibleRepository(database)
            bible = bible_repo.get_by_novel_id(NovelId(novel_id))
            return bible is not None
        except Exception:
            return False

    def _check_has_outline(self, novel_id: str) -> bool:
        if not self.story_node_repository:
            return False
        try:
            tree = self.story_node_repository.get_tree_sync(novel_id)
            act_nodes = [n for n in tree.nodes if n.node_type == NodeType.ACT]
            return len(act_nodes) > 0
        except Exception:
            return False

    def list_novels(self, *, include_content: bool = True) -> List[NovelDTO]:
        """列出所有小说

        Returns:
            NovelDTO 列表
        """
        novels = self.novel_repository.list_all()
        dtos = []
        for novel in novels:
            dto = self._novel_dto(novel, include_content)
            dto.has_bible = self._check_has_bible(novel.novel_id.value)
            dto.has_outline = self._check_has_outline(novel.novel_id.value)
            dtos.append(dto)
        return dtos

    def delete_novel(self, novel_id: str) -> None:
        """删除小说

        Args:
            novel_id: 小说 ID
        """
        self.novel_repository.delete(NovelId(novel_id))

    def add_chapter(
        self,
        novel_id: str,
        chapter_id: str,
        number: int,
        title: str,
        content: str
    ) -> NovelDTO:
        """添加章节

        Args:
            novel_id: 小说 ID
            chapter_id: 章节 ID
            number: 章节编号
            title: 章节标题
            content: 章节内容

        Returns:
            更新后的 NovelDTO

        Raises:
            ValueError: 如果小说不存在或章节号不连续
        """
        novel = self.novel_repository.get_by_id(NovelId(novel_id))

        if novel is None:
            raise ValueError(f"Novel not found: {novel_id}")

        # 查询数据库中实际的章节数
        existing_chapters = self.chapter_repository.list_by_novel(NovelId(novel_id))
        if not isinstance(existing_chapters, list):
            existing_chapters = list(getattr(novel, "chapters", []) or [])
        expected_number = len(existing_chapters) + 1

        # 验证章节号是否连续
        if number != expected_number:
            raise ValueError(f"Chapter number must be {expected_number}, got {number}")

        chapter = Chapter(
            id=chapter_id,
            novel_id=NovelId(novel_id),
            number=number,
            title=title,
            content=content
        )

        # 直接保存章节，不通过Novel实体
        self.chapter_repository.save(chapter)
        if not any(getattr(c, "number", None) == chapter.number for c in novel.chapters):
            novel.chapters.append(chapter)
        self.novel_repository.save(novel)

        # 同步创建 StoryNode 章节节点，并关联到当前活跃的幕
        if self.story_node_repository:
            try:
                # 查找当前活跃的幕（最新的幕）
                tree = self.story_node_repository.get_tree_sync(novel_id)
                acts = [node for node in tree.nodes if node.node_type == NodeType.ACT]

                if acts:
                    # 获取最新的幕
                    current_act = max(acts, key=lambda x: x.number)

                    # 创建章节节点
                    chapter_node = StoryNode(
                        id=f"chapter-{novel_id}-{number}",
                        novel_id=novel_id,
                        node_type=NodeType.CHAPTER,
                        number=number,
                        title=title,
                        description="",
                        parent_id=current_act.id,  # 关联到当前幕
                        order_index=len(tree.nodes),
                        content=content,
                        word_count=len(content),
                        status="draft",
                        created_at=datetime.now(),
                        updated_at=datetime.now()
                    )

                    self.story_node_repository.save_sync(chapter_node)

                    # 更新幕的章节范围
                    children = self.story_node_repository.get_children_sync(current_act.id)
                    chapter_nodes = [node for node in children if node.node_type == NodeType.CHAPTER]
                    if chapter_nodes:
                        chapter_numbers = [node.number for node in chapter_nodes]
                        current_act.chapter_start = min(chapter_numbers)
                        current_act.chapter_end = max(chapter_numbers)
                        current_act.chapter_count = len(chapter_numbers)
                        self.story_node_repository.save_sync(current_act)

            except Exception as e:
                # 如果同步失败，不影响章节创建
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to sync chapter to story structure: {e}")

        # 重新加载Novel以返回最新状态
        novel = self.novel_repository.get_by_id(NovelId(novel_id)) or novel
        return NovelDTO.from_domain(self._hydrate_chapters(novel))

    def update_novel(
        self,
        novel_id: str,
        title: Optional[str] = None,
        author: Optional[str] = None,
        target_chapters: Optional[int] = None,
        premise: Optional[str] = None,
        target_words_per_chapter: Optional[int] = None,
        generation_prefs: Optional[Dict[str, Any]] = None,
    ) -> NovelDTO:
        """更新小说基本信息

        Args:
            novel_id: 小说 ID
            title: 小说标题（可选）
            author: 作者（可选）
            target_chapters: 目标章节数（可选）
            premise: 故事梗概/创意（可选）
            target_words_per_chapter: 每章目标字数（可选，500–20000）
            generation_prefs: 生成偏好（可选；仅更新传入的键）

        Returns:
            更新后的 NovelDTO

        Raises:
            EntityNotFoundError: 如果小说不存在
        """
        novel = self.novel_repository.get_by_id(NovelId(novel_id))
        if novel is None:
            raise EntityNotFoundError("Novel", novel_id)

        # Validate the complete preference patch before mutating an entity that
        # may be shared by an in-memory repository or request cache.
        merged = None
        if generation_prefs is not None:
            previous = novel.generation_prefs.to_dict()
            merged = GenerationPreferences.merge_patch(novel.generation_prefs, generation_prefs)
            semantic_keys = set(MARKET_FIELDS) | set(PROFILE_FIELDS.values())
            market_keys = semantic_keys | {"market_profile_snapshot", "profile_snapshot_hash"}
            if market_keys.intersection(generation_prefs):
                raw = {**previous, **{key: value for key, value in generation_prefs.items() if key in previous}}
                changed = any(previous.get(key) != raw.get(key) for key in semantic_keys.intersection(generation_prefs))
                if changed and "custom_market_profile" not in generation_prefs and raw.get("custom_market_profile"):
                    raw["custom_market_profile"]["profile_review_status"] = "draft"
                    raw["custom_market_profile"].get("custom_theme", {})["status"] = "draft"
                merged = GenerationPreferences.from_dict(normalize_market_preferences(raw))

        # 更新提供的字段
        if title is not None:
            novel.title = title
        if author is not None:
            novel.author = author
        if target_chapters is not None:
            novel.target_chapters = target_chapters
        if premise is not None:
            novel.premise = premise
        if target_words_per_chapter is not None:
            novel.target_words_per_chapter = clamp_chapter_target_words(target_words_per_chapter)
        if merged is not None:
            novel.generation_prefs = merged

        # 增量 patch：避免全量 save 把 autopilot_status 等未改字段写回 stopped
        patch_fields: Dict[str, Any] = {}
        if title is not None:
            patch_fields["title"] = title
        if author is not None:
            patch_fields["author"] = author
        if target_chapters is not None:
            patch_fields["target_chapters"] = target_chapters
        if premise is not None:
            patch_fields["premise"] = premise
        if target_words_per_chapter is not None:
            patch_fields["target_words_per_chapter"] = novel.target_words_per_chapter
        if generation_prefs is not None:
            patch_fields["generation_prefs_json"] = json.dumps(
                novel.generation_prefs.to_dict(), ensure_ascii=False
            )
        if patch_fields:
            self.novel_repository.patch(NovelId(novel_id), **patch_fields)
        self._sync_variable_hub_from_novel(novel)

        return NovelDTO.from_domain(self._hydrate_chapters(novel))

    def update_novel_stage(self, novel_id: str, stage: str) -> NovelDTO:
        """更新小说阶段

        Args:
            novel_id: 小说 ID
            stage: 阶段

        Returns:
            更新后的 NovelDTO

        Raises:
            EntityNotFoundError: 如果小说不存在
        """
        novel = self.novel_repository.get_by_id(NovelId(novel_id))
        if novel is None:
            raise EntityNotFoundError("Novel", novel_id)

        novel.stage = NovelStage(stage)
        self.novel_repository.save(novel)

        return NovelDTO.from_domain(self._hydrate_chapters(novel))

    def update_auto_approve_mode(self, novel_id: str, auto_approve_mode: bool) -> NovelDTO:
        """更新全自动模式设置

        Args:
            novel_id: 小说 ID
            auto_approve_mode: 是否开启全自动模式

        Returns:
            更新后的 NovelDTO

        Raises:
            EntityNotFoundError: 如果小说不存在
        """
        novel = self.novel_repository.get_by_id(NovelId(novel_id))
        if novel is None:
            raise EntityNotFoundError("Novel", novel_id)

        novel.auto_approve_mode = auto_approve_mode
        self.novel_repository.save(novel)

        return NovelDTO.from_domain(self._hydrate_chapters(novel))

    def get_novel_statistics(self, novel_id: str) -> Dict[str, Any]:
        """获取小说统计信息（以 Chapter 仓储落盘为准，与列表/读写 API 一致）

        Args:
            novel_id: 小说 ID

        Returns:
            与前端顶栏 BookStats 对齐的字段；数据来源为 ``list_by_novel``，非 novel 聚合 JSON 内嵌章节。

        Raises:
            EntityNotFoundError: 如果小说不存在
        """
        novel = self.novel_repository.get_by_id(NovelId(novel_id))
        if novel is None:
            raise EntityNotFoundError("Novel", novel_id)

        summary_provider = getattr(self.chapter_repository, 'list_summaries_by_novel', None)
        summaries = summary_provider(NovelId(novel_id)) if callable(summary_provider) else None
        if isinstance(summaries, list):
            counts = [row['word_count'] for row in summaries]
        else:
            chapters = self.chapter_repository.list_by_novel(NovelId(novel_id))
            counts = [chapter.word_count.value for chapter in chapters]
        total = len(counts)
        total_words = sum(counts)
        completed = sum(count > 0 for count in counts)
        avg = total_words // total if total > 0 else 0
        completion = (completed / total) if total > 0 else 0.0

        return {
            "slug": novel_id,
            "title": novel.title,
            "total_chapters": total,
            "completed_chapters": completed,
            "total_words": total_words,
            "avg_chapter_words": avg,
            "completion_rate": completion,
            "stage": novel.stage.value,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
