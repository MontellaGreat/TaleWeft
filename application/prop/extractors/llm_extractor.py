"""LLM 异步提取器：从正文中抽取持有者变更、损毁、修复等高价值事件。"""
from __future__ import annotations
import json
import uuid
from typing import Any, Dict, List

from domain.prop.value_objects.prop_event import PropEvent, PropEventType, PropEventSource
from domain.shared.time_utils import utcnow_iso

class PropExtractionError(ValueError):
    """A tracked prop could not be analysed reliably; never an empty success."""


class PropExtractionLimitError(PropExtractionError):
    pass


class LlmExtractor:
    """提取既有道具的完整有序事件；无事件与提取失败有明确区别。"""

    priority: int = 10
    name: str = "llm"
    max_content_chars: int = 60_000
    max_props: int = 100

    def __init__(self, llm_service, entity_resolver=None):
        self._llm = llm_service
        self._entity_resolver = entity_resolver

    async def extract(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        active_props: List[dict],
    ) -> List[PropEvent]:
        if not active_props or not content.strip():
            return []
        if len(content) > self.max_content_chars or len(active_props) > self.max_props:
            raise PropExtractionLimitError('道具分析输入超出显式上限，请缩短章节或减少本次跟踪对象后重跑')

        props_summary = "\n".join(
            f"- {p['name']}（id={p['id']}，持有者={p.get('holder', '无')}，"
            f"状态={p.get('state', '未知')}，别名={','.join(p.get('aliases', []))}）"
            for p in active_props
        )

        from infrastructure.ai.prompt_keys import PROP_EVENT_EXTRACTION
        from infrastructure.ai.prompt_utils import render_required_prompt

        variables = {
            "props_summary": props_summary,
            "chapter_excerpt": content,
        }
        prompt = render_required_prompt(PROP_EVENT_EXTRACTION, variables)

        try:
            from domain.ai.services.llm_service import GenerationConfig
            result = await self._llm.generate(
                prompt,
                GenerationConfig(max_tokens=4000, temperature=0.1),
            )
            raw = result.content if hasattr(result, "content") else str(result)
            items: List[Dict[str, Any]] = json.loads(raw)
            if not isinstance(items, list):
                raise PropExtractionError('道具事件响应必须是 JSON 数组')
        except Exception as e:
            raise PropExtractionError('道具事件提取失败，关联分析需要重跑') from e

        events: List[PropEvent] = []
        now = utcnow_iso()
        prop_id_set = {p["id"] for p in active_props}
        search_offset = 0
        last_position = 0
        for item in items:
            if not isinstance(item, dict):
                raise PropExtractionError('道具事件数组包含非对象项')
            pid = item.get("prop_id", "")
            if not isinstance(pid, str) or pid not in prop_id_set:
                raise PropExtractionError('道具事件引用了未跟踪的道具')
            try:
                etype = PropEventType(item["event_type"])
            except (ValueError, KeyError, TypeError) as error:
                raise PropExtractionError('道具事件类型无效') from error
            quote = item.get('evidence_quote')
            position = None
            if quote is not None:
                if not isinstance(quote, str) or not quote.strip() or quote not in content:
                    raise PropExtractionError('道具事件的原文证据无法在本章定位')
                position = content.find(quote, search_offset)
                if position < 0:
                    position = content.find(quote, last_position)
                if position < 0:
                    raise PropExtractionError('道具事件的原文证据顺序与响应顺序冲突')
                last_position = position
                search_offset = position + len(quote)
            for field in ('to_holder', 'from_holder', 'actor_character', 'description'):
                if item.get(field) is not None and not isinstance(item[field], str):
                    raise PropExtractionError('道具事件角色或描述字段类型无效')
            target_holder = self._resolve_character_id(novel_id, item.get('to_holder', ''))
            from_holder = self._resolve_character_id(novel_id, item.get('from_holder', ''))
            if etype == PropEventType.TRANSFERRED and not target_holder:
                raise PropExtractionError('道具转交目标无法关联到现有角色')
            if etype == PropEventType.TRANSFERRED and item.get('from_holder') and not from_holder:
                raise PropExtractionError('道具转交来源无法关联到现有角色')
            events.append(PropEvent(
                id=str(uuid.uuid4()),
                prop_id=pid,
                novel_id=novel_id,
                chapter_number=chapter_number,
                event_type=etype,
                source=PropEventSource.AUTO_LLM,
                description=item.get("description") or "",
                actor_character_id=self._resolve_character_id(
                    novel_id, item.get("actor_character", "")
                ),
                from_holder_id=from_holder,
                to_holder_id=target_holder,
                created_at=now,
                text_position=position,
            ))
        return events

    def _resolve_character_id(self, novel_id: str, raw: str) -> str | None:
        if not raw or not self._entity_resolver:
            return None
        entity = self._entity_resolver.resolve(
            novel_id, raw, allowed_types=["character"]
        )
        if not entity:
            return None
        return entity.id
