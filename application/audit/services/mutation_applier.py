"""Mutation Applier Service - 应用 mutations 到事件"""
import logging
from copy import deepcopy
from typing import List, Dict, Any, Optional
from domain.novel.repositories.narrative_event_repository import NarrativeEventRepository


logger = logging.getLogger(__name__)


def validate_mutations(mutations):
    if not isinstance(mutations, list) or len(mutations) > 1000:
        raise ValueError('mutation列表无效或超过1000项')
    fields = {'add_tag': ('tag',), 'remove_tag': ('tag',), 'replace_tag': ('old', 'new'), 'replace_summary': ('new_summary',)}
    for index, mutation in enumerate(mutations):
        if not isinstance(mutation, dict) or not isinstance(mutation.get('type'), str) or mutation['type'] not in fields:
            raise ValueError(f'mutation[{index + 1}]类型不支持，整批变更未应用')
        for field in fields[mutation['type']]:
            value = mutation.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > (100000 if field == 'new_summary' else 4096):
                raise ValueError(f'mutation[{index + 1}]的{field}无效，整批变更未应用')


class MutationApplier:
    """Mutation 应用器 - 将 mutations 应用到事件"""

    def __init__(self, event_repository: NarrativeEventRepository):
        self.event_repository = event_repository

    def apply_mutations(
        self,
        novel_id: str,
        event_id: str,
        mutations: List[Dict[str, Any]],
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        应用 mutations 到事件

        Args:
            novel_id: 小说 ID
            event_id: 事件 ID
            mutations: mutation 列表
            reason: 修改原因（用于审计）

        Returns:
            包含 success, updated_event, applied_mutations 的字典
        """
        validate_mutations(mutations)
        # Build a private copy so a failed operation cannot mutate a cached event.
        event = self.event_repository.get_event(novel_id, event_id)
        if not event:
            raise ValueError(f"Event not found: {event_id}")
        event = deepcopy(event)

        # 记录审计信息
        if reason:
            logger.info(f"Applying mutations to event {event_id}, reason: {reason}")

        # 应用 mutations
        applied_mutations = []
        for mutation in mutations:
            mutation_type = mutation.get("type")

            if mutation_type == "add_tag":
                tag = mutation.get("tag")
                if tag and tag not in event["tags"]:
                    event["tags"].append(tag)
                    applied_mutations.append(mutation)
                    logger.debug(f"Added tag '{tag}' to event {event_id}")
                else:
                    applied_mutations.append(mutation)

            elif mutation_type == "remove_tag":
                tag = mutation.get("tag")
                if tag in event["tags"]:
                    event["tags"].remove(tag)
                    applied_mutations.append(mutation)
                    logger.debug(f"Removed tag '{tag}' from event {event_id}")
                else:
                    # 幂等性：标签不存在也算成功
                    applied_mutations.append(mutation)

            elif mutation_type == "replace_summary":
                new_summary = mutation.get("new_summary")
                if new_summary:
                    event["event_summary"] = new_summary
                    applied_mutations.append(mutation)
                    logger.debug(f"Replaced summary for event {event_id}")

            elif mutation_type == 'replace_tag':
                old, new = mutation['old'], mutation['new']
                if old in event['tags']:
                    position = event['tags'].index(old)
                    event['tags'].remove(old)
                    if new not in event['tags']:
                        event['tags'].insert(position, new)
                elif new not in event['tags']:
                    raise ValueError(f'待替换标签不存在：{old}；整批变更未应用')
                applied_mutations.append(mutation)

            else:
                raise ValueError(f'不支持的mutation类型：{mutation_type}')

        # 更新事件到数据库
        self.event_repository.update_event(
            novel_id=novel_id,
            event_id=event_id,
            event_summary=event["event_summary"],
            tags=event["tags"]
        )

        return {
            "success": True,
            "updated_event": event,
            "applied_mutations": applied_mutations
        }
