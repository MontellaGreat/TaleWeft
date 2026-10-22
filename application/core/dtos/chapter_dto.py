"""Chapter 数据传输对象"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.novel.entities.chapter import Chapter


@dataclass
class ChapterDTO:
    """章节 DTO"""
    id: str
    novel_id: str
    number: int
    title: str
    content: str
    word_count: int
    status: str
    generation_hint: str = ""
    revision: int = 0
    content_hash: str = ""
    etag: str = ""
    created_at: str = ""
    updated_at: str = ""
    derived_status: str = "not_requested"
    derivation_job_id: str | None = None
    save_request_id: str | None = None

    @classmethod
    def from_domain(cls, chapter: 'Chapter') -> 'ChapterDTO':
        """从领域对象创建 DTO

        Args:
            chapter: Chapter 领域对象

        Returns:
            ChapterDTO
        """
        # 处理 novel_id 和 status 可能是字符串或值对象的情况
        novel_id = chapter.novel_id.value if hasattr(chapter.novel_id, 'value') else chapter.novel_id
        status = chapter.status.value if hasattr(chapter.status, 'value') else chapter.status

        from application.core.services.chapter_version_service import content_hash
        digest = content_hash(chapter.content)
        revision = int(getattr(chapter, "revision", 0) or 0)
        return cls(
            id=chapter.id,
            novel_id=novel_id,
            number=chapter.number,
            title=chapter.title,
            content=chapter.content,
            word_count=chapter.word_count.value,
            status=status,
            generation_hint=getattr(chapter, 'generation_hint', '') or '',
            revision=revision, content_hash=digest, etag=f'"chapter:{chapter.id}:{revision}:{digest}"',
            created_at=str(getattr(chapter, 'created_at', '') or ''),
            updated_at=str(getattr(chapter, 'updated_at', '') or ''),
        )
