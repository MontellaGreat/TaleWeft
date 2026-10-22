"""场景生成服务

为单个场景生成正文（500-1000 字）
"""

import logging
from typing import Dict, List, Optional, TYPE_CHECKING

from domain.novel.value_objects.scene import Scene
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.engine.services.scene_director_service import SceneDirectorService

if TYPE_CHECKING:
    from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore

logger = logging.getLogger(__name__)


class SceneGenerationService:
    """场景生成服务

    为单个场景生成正文，集成：
    1. 场记分析（SceneDirectorAnalysis）
    2. 向量检索过滤上下文（POV 防火墙）
    3. 前置场景上下文（previous_scenes）
    4. LLM 生成正文
    """

    def __init__(
        self,
        llm_service: LLMService,
        scene_director: SceneDirectorService,
        vector_store: Optional["ChromaDBVectorStore"] = None,
        embedding_service=None,
        db=None,
    ):
        self.llm_service = llm_service
        self.scene_director = scene_director
        self.vector_store = vector_store
        self.embedding_service = embedding_service
        self.db = db

    async def generate_scene(
        self,
        scene: Scene,
        chapter_number: int,
        previous_scenes: List[str],
        bible_context: Optional[Dict] = None,
        *,
        chapter_id: Optional[str] = None,
    ) -> str:
        """生成单个场景的正文

        Args:
            scene: 场景对象
            chapter_number: 章节号
            previous_scenes: 前置场景的正文列表
            bible_context: Bible 上下文（可选）
            chapter_id: 真实章节 ID，用于核实小说与章节号并绑定去味回执

        Returns:
            生成的场景正文
        """
        from application.core.services.scene_generation_context import (
            InvalidSceneGenerationContextError,
            chapter_generation_scope,
        )
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            raise InvalidSceneGenerationContextError("场景生成必须提供真实章节 ID，无法使用未核实的小说上下文")
        if type(chapter_number) is not int or chapter_number < 1:
            raise InvalidSceneGenerationContextError("章节号必须为正整数")
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
        from domain.novel.value_objects.chapter_id import ChapterId

        db = self.db if self.db is not None else get_database()
        chapters = SqliteChapterRepository(db)
        chapter = chapters.get_by_id(ChapterId(chapter_id.strip()))
        novel_id, chapter_number = chapter_generation_scope(chapter, chapter_id.strip(), chapter_number)
        if bible_context is not None and not isinstance(bible_context, dict):
            raise InvalidSceneGenerationContextError("Bible 上下文必须为包含小说身份的对象")
        bible_context = dict(bible_context or {})
        if bible_context and bible_context.get("novel_id") != novel_id:
            raise InvalidSceneGenerationContextError("Bible 所属小说与实际章节不一致，无法生成场景")
        bible_context["novel_id"] = novel_id
        logger.info(f"Generating scene: {scene.title} (POV: {scene.pov_character})")

        # 1. 场记分析
        scene_analysis = await self.scene_director.analyze(
            chapter_number=chapter_number,
            outline=f"{scene.title}\n{scene.goal}"
        )
        logger.debug(f"Scene analysis: characters={scene_analysis.characters}, "
                    f"locations={scene_analysis.locations}, pov={scene_analysis.pov}")

        # 2. 向量检索过滤上下文（POV 防火墙）
        relevant_context = await self._retrieve_relevant_context(
            scene=scene,
            scene_analysis=scene_analysis,
            bible_context=bible_context,
        )

        # 3. 构建提示词
        prompt = self._build_scene_prompt(
            scene=scene,
            scene_analysis=scene_analysis,
            relevant_context=relevant_context,
            previous_scenes=previous_scenes,
            bible_context=bible_context
        )

        # 4. 生成正文
        config = GenerationConfig(max_tokens=2048, temperature=0.8)
        from application.ai.llm_output_sanitize import strip_reasoning_artifacts
        from application.engine.rules.prose_prompt_hooks import generate_direct_prose
        from application.ai_invocation.prose_execution_evidence import verify_durable_prose_receipts

        evidence = {}
        response = await generate_direct_prose(
            self.llm_service, prompt, config, db=db,
            context={"novel_id": novel_id, "chapter_number": chapter_number,
                     "character_names": [scene.pov_character, *(scene_analysis.characters or [])]},
            evidence_sink=evidence,
            content_transform=lambda text: strip_reasoning_artifacts(text.strip()).strip(),
        )
        content = response if isinstance(response, str) else response.content
        verify_durable_prose_receipts(
            db, novel_id=novel_id, chapter_number=chapter_number,
            attempt_id=evidence["job_id"], content=content, prompt_hash=evidence["prompt_hash"],
            require_stream=False, prompt_run_id=evidence["run_id"],
            scan_run_id=evidence["anti_ai_scan"]["run_id"],
        )
        current_novel_id, _ = chapter_generation_scope(
            chapters.get_by_id(ChapterId(chapter_id.strip())), chapter_id.strip(), chapter_number,
        )
        if current_novel_id != novel_id:
            raise InvalidSceneGenerationContextError("实际章节所属小说在生成期间已变化，场景正文不可采纳")

        logger.info(f"Scene generated: {len(content)} characters")
        return content

    async def _retrieve_relevant_context(
        self,
        scene: Scene,
        scene_analysis,
        bible_context: Optional[Dict] = None,
    ) -> Dict:
        """向量检索：获取与场景相关的上下文
        """
        result = {
            "characters": [],
            "locations": [],
            "foreshadowings": [],
            "chapters": [],
            "bible_snippets": [],
            "facts": [],
            "snippets": [],
        }
        if not self.vector_store or not self.embedding_service:
            return result

        novel_id = str((bible_context or {}).get("novel_id") or "").strip()
        if not novel_id:
            return result

        collection = f"novel_{novel_id}_chunks"
        try:
            collections = await self.vector_store.list_collections()
            if collection not in set(collections or []):
                return result

            query_text = " ".join(
                part for part in [
                    scene.title,
                    scene.goal,
                    scene.pov_character,
                    scene.location or "",
                    scene.tone or "",
                    " ".join(scene_analysis.characters or []),
                    " ".join(scene_analysis.locations or []),
                ]
                if part
            )
            query_vector = await self.embedding_service.embed(query_text)
            hits = await self.vector_store.search(
                collection=collection,
                query_vector=query_vector,
                limit=8,
            )
        except Exception as e:
            logger.warning("场景向量检索失败，已跳过：%s", e)
            return result

        for hit in hits or []:
            payload = dict(hit.get("payload") or {})
            if not payload:
                continue
            kind = str(payload.get("kind") or "").lower()
            text = self._payload_text(payload)
            if not text:
                continue
            item = {
                "text": text,
                "score": hit.get("score"),
                "payload": payload,
            }
            if kind == "chapter_summary":
                result["chapters"].append(item)
            elif kind == "bible_snippet":
                result["bible_snippets"].append(item)
            elif self._looks_like_foreshadowing(payload):
                result["foreshadowings"].append({"description": text, **item})
            elif payload.get("triple_id") or payload.get("subject"):
                result["facts"].append(item)
            else:
                result["snippets"].append(item)
        return result

    def _build_scene_prompt(
        self,
        scene: Scene,
        scene_analysis,
        relevant_context: Dict,
        previous_scenes: List[str],
        bible_context: Optional[Dict]
    ) -> Prompt:
        """构建场景生成提示词（CPMS 渲染）。"""
        from infrastructure.ai.prompt_keys import SCENE_GENERATION
        from infrastructure.ai.prompt_utils import render_required_prompt

        # 构建变量
        analysis_parts = []
        if scene_analysis.characters:
            analysis_parts.append(f"涉及角色：{', '.join(scene_analysis.characters)}")
        if scene_analysis.locations:
            analysis_parts.append(f"涉及地点：{', '.join(scene_analysis.locations)}")
        if scene_analysis.emotional_state:
            analysis_parts.append(f"情绪状态：{scene_analysis.emotional_state}")
        analysis_block = "\n".join(analysis_parts)

        previous_scenes_parts = []
        for i, prev_scene in enumerate(previous_scenes[-2:], 1):
            summary = prev_scene[:200] + "..." if len(prev_scene) > 200 else prev_scene
            previous_scenes_parts.append(f"场景 {i}：\n{summary}")
        previous_scenes_block = "\n".join(previous_scenes_parts)

        foreshadow_parts = []
        for foreshadowing in relevant_context.get("foreshadowings", [])[:3]:
            foreshadow_parts.append(f"- {foreshadowing.get('description', 'N/A')}")
        foreshadowing_block = "\n".join(foreshadow_parts)
        if foreshadowing_block:
            foreshadowing_block = f"相关伏笔：\n{foreshadowing_block}\n"

        bible_context_block = self._format_bible_context(
            bible_context,
            pov_character=scene.pov_character,
        )
        retrieved_context_block = self._format_retrieved_context(relevant_context)

        variables = {
            "title": scene.title,
            "goal": scene.goal,
            "pov_character": scene.pov_character,
            "location": scene.location or "未指定",
            "tone": scene.tone or "未指定",
            "estimated_words": str(scene.estimated_words),
            "analysis_block": analysis_block,
            "previous_scenes_block": previous_scenes_block,
            "foreshadowing_block": foreshadowing_block,
            "bible_context_block": bible_context_block,
            "retrieved_context_block": retrieved_context_block,
        }

        return render_required_prompt(SCENE_GENERATION, variables)

    @staticmethod
    def _payload_text(payload: Dict) -> str:
        for key in ("text", "summary", "description", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        subject = str(payload.get("subject") or "").strip()
        predicate = str(payload.get("predicate") or "").strip()
        obj = str(payload.get("object") or "").strip()
        return " ".join(part for part in [subject, predicate, obj] if part).strip()

    @staticmethod
    def _looks_like_foreshadowing(payload: Dict) -> bool:
        haystack = " ".join(
            str(payload.get(key) or "")
            for key in (
                "kind",
                "subject_type",
                "object_type",
                "subject",
                "predicate",
                "object",
                "text",
                "description",
            )
        ).lower()
        return any(token in haystack for token in ("foreshadow", "伏笔", "暗线"))

    @staticmethod
    def _format_retrieved_context(relevant_context: Dict) -> str:
        lines: List[str] = []
        sections = (
            ("相关章节摘要", "chapters"),
            ("相关设定片段", "bible_snippets"),
            ("相关事实", "facts"),
            ("相关记忆片段", "snippets"),
        )
        for title, key in sections:
            items = relevant_context.get(key) or []
            if not items:
                continue
            lines.append(f"{title}：")
            for item in items[:3]:
                text = str(item.get("text") or "").strip()
                if text:
                    lines.append(f"- {text[:240]}")
        return "\n".join(lines)

    @staticmethod
    def _format_bible_context(
        bible_context: Optional[Dict],
        *,
        pov_character: str,
    ) -> str:
        if not bible_context:
            return ""
        lines: List[str] = []
        characters = bible_context.get("characters") or []
        if characters:
            lines.append("可见人物锚点：")
            for char in characters[:6]:
                name = str(char.get("name") or "").strip()
                public_profile = str(
                    char.get("public_profile")
                    or char.get("description")
                    or ""
                ).strip()
                if not name and not public_profile:
                    continue
                marker = "（当前 POV）" if name and name == pov_character else ""
                lines.append(f"- {name}{marker}：{public_profile[:160]}")
        locations = bible_context.get("locations") or []
        if locations:
            lines.append("地点锚点：")
            for loc in locations[:5]:
                name = str(loc.get("name") or "").strip()
                desc = str(loc.get("description") or "").strip()
                if name or desc:
                    lines.append(f"- {name}：{desc[:160]}")
        world_settings = bible_context.get("world_settings") or []
        if world_settings:
            lines.append("世界规则：")
            for item in world_settings[:4]:
                name = str(item.get("name") or "").strip()
                desc = str(item.get("description") or "").strip()
                if name or desc:
                    lines.append(f"- {name}：{desc[:160]}")
        return "\n".join(lines)
