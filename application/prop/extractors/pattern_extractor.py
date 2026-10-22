"""零 token 模式提取器：扫描正文中的 [[prop:id|名称]] 标记。"""
from __future__ import annotations
import re
import uuid
from typing import List

from domain.prop.value_objects.prop_event import PropEvent, PropEventType, PropEventSource
from domain.prop.value_objects.lifecycle_state import LifecycleState
from domain.shared.time_utils import utcnow_iso

# 匹配 [[prop:uuid|名称]] 或仅 [[prop:uuid]]
_PATTERN = re.compile(r'\[\[prop:([a-f0-9\-]{36})(?:\|[^\]]+)?\]\]')


class PatternExtractor:
    """[[prop:id]] 标记扫描提取器（零 token，优先级最高）。"""

    priority: int = 0
    name: str = "pattern"

    async def extract(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        active_props: List[dict],
    ) -> List[PropEvent]:
        prop_by_id = {p["id"]: p for p in active_props}
        events: List[PropEvent] = []
        now = utcnow_iso()
        seen = set()
        for match in _PATTERN.finditer(content):
            prop_id = match.group(1)
            if prop_id not in prop_by_id or prop_id in seen:
                continue
            seen.add(prop_id)
            prop = prop_by_id[prop_id]
            # A mention of a broken/finished prop does not repair or revive it.
            if prop.get('state') in (
                LifecycleState.INTRODUCED.value,
                LifecycleState.DAMAGED.value,
                LifecycleState.RESOLVED.value,
            ):
                continue
            event_type = (
                PropEventType.INTRODUCED
                if prop.get("state") == LifecycleState.DORMANT.value
                else PropEventType.USED
            )
            events.append(PropEvent(
                id=str(uuid.uuid4()),
                prop_id=prop_id,
                novel_id=novel_id,
                chapter_number=chapter_number,
                event_type=event_type,
                source=PropEventSource.AUTO_PATTERN,
                description=f"第{chapter_number}章正文标记命中",
                created_at=now,
                text_position=match.start(),
            ))
        return events
