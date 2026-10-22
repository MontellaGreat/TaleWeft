"""Novel 数据传输对象"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from domain.novel.entities.novel import Novel
    from domain.novel.entities.chapter import Chapter


def _public_stage(novel: 'Novel') -> str:
    """内部 current_stage -> 前端/旧接口粗粒度 stage。"""
    current_stage = getattr(novel, 'current_stage', None)
    current_value = current_stage.value if hasattr(current_stage, 'value') else str(current_stage or '')

    explicit_stage = getattr(novel, 'stage', None)
    explicit_value = explicit_stage.value if hasattr(explicit_stage, 'value') else str(explicit_stage or '')

    # 兼容旧 update_novel_stage：仅显式 stage 被改动时优先保留。
    if explicit_value and explicit_value != 'planning' and current_value in ('', 'planning'):
        return explicit_value

    stage_map = {
        'planning': 'planning',
        'macro_planning': 'planning',
        'act_planning': 'planning',
        'writing': 'writing',
        'auditing': 'reviewing',
        'reviewing': 'reviewing',
        'paused_for_review': 'reviewing',
        'completed': 'completed',
    }
    return stage_map.get(current_value, explicit_value or 'planning')


@dataclass
class ChapterDTO:
    """章节 DTO"""
    id: str
    number: int
    title: str
    content: str
    word_count: int

    @classmethod
    def from_domain(cls, chapter: 'Chapter') -> 'ChapterDTO':
        """从领域对象创建 DTO

        Args:
            chapter: Chapter 领域对象

        Returns:
            ChapterDTO
        """
        return cls(
            id=chapter.id,
            number=chapter.number,
            title=chapter.title,
            content=chapter.content,
            word_count=chapter.word_count.value
        )


@dataclass
class NovelDTO:
    """小说 DTO

    用于在应用层和外部层之间传输数据。
    """
    id: str
    title: str
    author: str
    target_chapters: int
    stage: str
    premise: str
    chapters: List[ChapterDTO]
    total_word_count: int
    slug: str = ""
    has_bible: bool = False
    has_outline: bool = False
    autopilot_status: str = "stopped"
    auto_approve_mode: bool = False
    locked_genre: str = ""
    locked_world_preset: str = ""
    locked_story_structure: str = ""
    locked_pacing_control: str = ""
    locked_writing_style: str = ""
    locked_special_requirements: str = ""
    audience_mode: str = ""
    canonical_genre_id: str = ""
    canonical_theme_id: str = ""
    market_platform: str = ""
    market_category_path: List[str] = field(default_factory=list)
    market_tags: List[str] = field(default_factory=list)
    taxonomy_version: str = ""
    profile_version: str = ""
    reader_contract: str = ""
    custom_market_profile: Dict[str, Any] = field(default_factory=dict)
    market_profile_snapshot: Dict[str, Any] = field(default_factory=dict)
    profile_snapshot_hash: str = ""
    target_words_per_chapter: int = 2500
    generation_prefs: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_domain(cls, novel: 'Novel', *, chapter_dtos: Optional[List[ChapterDTO]] = None) -> 'NovelDTO':
        """从领域对象创建 DTO

        Args:
            novel: Novel 领域对象

        Returns:
            NovelDTO
        """
        chapters = chapter_dtos if chapter_dtos is not None else [ChapterDTO.from_domain(chapter) for chapter in novel.chapters]
        
        _ap = getattr(novel, 'autopilot_status', 'stopped')
        autopilot_status = _ap.value if hasattr(_ap, 'value') else str(_ap)

        premise_text = getattr(novel, 'premise', '') or ''
        from application.core.premise_genre_world import parse_genre_world_from_premise

        _gp = getattr(novel, "generation_prefs", None)
        gp_dict = _gp.to_dict() if _gp is not None and hasattr(_gp, "to_dict") else {}
        gp_locked_genre = str(gp_dict.get("locked_genre") or "").strip()
        gp_locked_world_preset = str(gp_dict.get("locked_world_preset") or "").strip()
        gp_locked_story_structure = str(gp_dict.get("locked_story_structure") or "").strip()
        gp_locked_pacing_control = str(gp_dict.get("locked_pacing_control") or "").strip()
        gp_locked_writing_style = str(gp_dict.get("locked_writing_style") or "").strip()
        gp_locked_special_requirements = str(gp_dict.get("locked_special_requirements") or "").strip()
        audience_mode = str(gp_dict.get("audience_mode") or "").strip()
        canonical_genre_id = str(gp_dict.get("canonical_genre_id") or "").strip()
        canonical_theme_id = str(gp_dict.get("canonical_theme_id") or "").strip()
        market_platform = str(gp_dict.get("market_platform") or "").strip()
        market_category_path = [str(x).strip() for x in (gp_dict.get("market_category_path") or []) if str(x).strip()]
        market_tags = [str(x).strip() for x in (gp_dict.get("market_tags") or []) if str(x).strip()]
        parsed_genre, parsed_world_preset = parse_genre_world_from_premise(premise_text)
        lg = gp_locked_genre or parsed_genre
        lw = gp_locked_world_preset or parsed_world_preset

        return cls(
            id=novel.novel_id.value,
            slug=getattr(novel, 'slug', novel.novel_id.value) or novel.novel_id.value,
            title=novel.title,
            author=novel.author,
            target_chapters=novel.target_chapters,
            stage=_public_stage(novel),
            premise=premise_text,
            chapters=chapters,
            total_word_count=sum(chapter.word_count for chapter in chapters),
            autopilot_status=autopilot_status,
            auto_approve_mode=getattr(novel, 'auto_approve_mode', False),
            locked_genre=lg,
            locked_world_preset=lw,
            locked_story_structure=gp_locked_story_structure,
            locked_pacing_control=gp_locked_pacing_control,
            locked_writing_style=gp_locked_writing_style,
            locked_special_requirements=gp_locked_special_requirements,
            audience_mode=audience_mode,
            canonical_genre_id=canonical_genre_id,
            canonical_theme_id=canonical_theme_id,
            market_platform=market_platform,
            market_category_path=market_category_path,
            market_tags=market_tags,
            taxonomy_version=str(gp_dict.get("taxonomy_version") or ""),
            profile_version=str(gp_dict.get("profile_version") or ""),
            reader_contract=str(gp_dict.get("reader_contract") or ""),
            custom_market_profile=dict(gp_dict.get("custom_market_profile") or {}),
            market_profile_snapshot=dict(gp_dict.get("market_profile_snapshot") or {}),
            profile_snapshot_hash=str(gp_dict.get("profile_snapshot_hash") or ""),
            target_words_per_chapter=int(getattr(novel, "target_words_per_chapter", 2500) or 2500),
            generation_prefs=gp_dict,
        )
