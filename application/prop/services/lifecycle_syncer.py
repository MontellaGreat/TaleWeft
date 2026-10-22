"""PropLifecycleSyncer — 章节收稿后编排道具生命周期同步。

设计原则：
- 依赖注入：仓储 + 提取器列表 + 事件处理器列表 全部从外部注入
- 不依赖具体 LLM / DB 实现，只依赖 Protocol / 抽象仓储
- 失败隔离：单个提取器失败不影响其他提取器
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from typing import Any, Dict, List, Optional

from domain.prop.entities.prop import Prop
from domain.prop.repositories.prop_repository import PropRepository
from domain.prop.repositories.prop_event_repository import PropEventRepository
from domain.prop.value_objects.lifecycle_state import LifecycleTransitionError
from domain.prop.value_objects.prop_event import PropEvent, PropEventType, PropEventSource
from domain.shared.time_utils import utcnow_iso
from application.prop.ports.extractor_port import PropExtractor
from application.prop.ports.event_handler_port import PropEventHandler

logger = logging.getLogger(__name__)


class PropLifecycleSyncer:
    """道具生命周期编排器。

    注册多个 PropExtractor（按 priority 升序执行），
    将提取的事件应用到 Prop 聚合根，持久化，并触发 PropEventHandler 副作用。
    """

    def __init__(
        self,
        prop_repo: PropRepository,
        event_repo: PropEventRepository,
        extractors: Optional[List[PropExtractor]] = None,
        handlers: Optional[List[PropEventHandler]] = None,
    ) -> None:
        self._prop_repo = prop_repo
        self._event_repo = event_repo
        self._extractors: List[PropExtractor] = sorted(
            extractors or [], key=lambda e: e.priority
        )
        self._handlers: List[PropEventHandler] = handlers or []

    def register_extractor(self, extractor: PropExtractor) -> None:
        """运行时注册新提取器（热插拔）。"""
        self._extractors.append(extractor)
        self._extractors.sort(key=lambda e: e.priority)

    def register_handler(self, handler: PropEventHandler) -> None:
        self._handlers.append(handler)

    async def sync(
        self, novel_id: str, chapter_number: int, content: str,
    ) -> Dict[str, Any]:
        from infrastructure.persistence.database.connection import DatabaseConnection
        from application.world.services.chapter_projection_reconciliation import record_chapter_projection_changes
        db = getattr(self._prop_repo, '_db', None)
        db = db if isinstance(db, DatabaseConnection) else None
        with record_chapter_projection_changes(db, novel_id, chapter_number, ('unified_props',)):
            return await self._sync(novel_id, chapter_number, content)

    async def _sync(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> Dict[str, Any]:
        """执行完整同步流程，返回执行摘要。"""
        active_props = self._prop_repo.list_active(novel_id, chapter_number)
        all_props = self._prop_repo.list_by_novel(novel_id)
        if not active_props and not all_props:
            return {"skipped": True, "reason": "no_props"}

        candidate_props = self._candidate_props(active_props, all_props)
        props_input = [
            {
                "id": p.id.value,
                "name": p.name,
                "aliases": p.aliases,
                "holder": p.holder_character_id,
                "state": p.lifecycle_state.value,
            }
            for p in candidate_props
        ]

        event_groups: List[List[PropEvent]] = []
        errors = []
        tasks = [
            ext.extract(novel_id, chapter_number, content, props_input)
            for ext in self._extractors
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for evts in results:
            if isinstance(evts, BaseException):
                errors.append('prop_extract: ' + type(evts).__name__)
            elif not isinstance(evts, list) or any(not isinstance(e, PropEvent) for e in evts):
                errors.append('prop_extract: InvalidEventList')
            else:
                event_groups.append(evts)

        deduped, order_errors = self._merge_events(event_groups)
        errors.extend(order_errors)

        prop_map: Dict[str, Prop] = {p.id.value: p for p in candidate_props}
        existing = self._event_repo.list_for_chapter(novel_id, chapter_number)
        committed = {}
        previous_counts = Counter()
        committed_events = []
        for event in existing if isinstance(existing, list) else []:
            if event.source == PropEventSource.MANUAL:
                continue
            committed_events.append(event)
            signature = self._signature(event)
            previous_counts[signature] += 1
            committed[(signature, previous_counts[signature])] = event
        occurrence = Counter()
        failed_props = set()
        committed_at = utcnow_iso()
        applied = 0
        for event in committed_events:
            prop = prop_map.get(event.prop_id)
            if prop is not None:
                # Extraction depends on current state and may omit an event
                # already applied before a handler failed (e.g. first mention).
                await self._run_handlers(prop, event, errors)
        for event in deduped:
            prop = prop_map.get(event.prop_id)
            if not prop or event.novel_id != novel_id or event.chapter_number != chapter_number:
                errors.append('prop_apply: InvalidEventScope')
                continue
            if event.prop_id in failed_props:
                continue
            signature = self._signature(event)
            occurrence[signature] += 1
            key = (signature, occurrence[signature])
            previous = committed.get(key)
            if previous is not None:
                # State changes and handler retries were handled above.
                continue
            event_id = hashlib.sha256(json.dumps(
                [novel_id, chapter_number, signature, occurrence[signature]],
                ensure_ascii=False, separators=(',', ':'),
            ).encode()).hexdigest()
            event = replace(event, id='prop-auto-' + event_id, created_at=committed_at)
            event = self._enrich_event_from_prop(event, prop)
            try:
                if event.is_transfer() and (not event.to_holder_id or (
                    event.from_holder_id and event.from_holder_id != prop.holder_character_id
                )):
                    raise LifecycleTransitionError('转交来源与当前持有人不符或缺少目标角色')
                updated = deepcopy(prop)
                updated.apply_event(event)
                self._save_event(updated, event)
                # Publish aggregate changes only after both writes acknowledge.
                prop.__dict__.update(updated.__dict__)
                applied += 1
                await self._run_handlers(prop, event, errors)
            except LifecycleTransitionError as lte:
                failed_props.add(event.prop_id)
                errors.append('prop_transition: LifecycleTransitionError')
                logger.warning(
                    "[PropSync] 状态转换被拒绝 prop=%s: %s", event.prop_id, lte
                )
            except Exception as e:
                failed_props.add(event.prop_id)
                errors.append('prop_apply: ' + type(e).__name__)
                logger.warning(
                    "[PropSync] 事件应用失败 prop=%s: %s", event.prop_id, e
                )

        return {
            "novel_id": novel_id,
            "chapter": chapter_number,
            "props_checked": len(candidate_props),
            "events_extracted": sum(len(group) for group in event_groups),
            "events_applied": applied,
            "errors": errors,
        }

    async def _run_handlers(self, prop, event, errors):
        for handler in self._handlers:
            try:
                await handler.handle(prop, event)
            except Exception as error:
                errors.append('prop_handler: ' + type(error).__name__)

    def _save_event(self, prop, event):
        from infrastructure.persistence.database.connection import DatabaseConnection
        from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository
        from infrastructure.persistence.database.sqlite_prop_event_repository import SqlitePropEventRepository
        db = getattr(self._prop_repo, '_db', None)
        if (isinstance(db, DatabaseConnection)
            and db is getattr(self._event_repo, '_db', None)
            and isinstance(self._prop_repo, SqliteUnifiedPropRepository)
            and isinstance(self._event_repo, SqlitePropEventRepository)):
            with db.transaction() as writer:
                self._prop_repo.save(prop, _writer=writer)
                self._event_repo.save(event, _writer=writer)
        else:
            self._prop_repo.save(prop)
            self._event_repo.save(event)

    @staticmethod
    def _signature(event):
        return (event.prop_id, event.event_type.value,
                event.from_holder_id if event.is_transfer() else None,
                event.to_holder_id if event.is_transfer() else None)

    @classmethod
    def _merge_events(cls, groups):
        """Merge extractor occurrence sequences without losing repeat transfers.

        Semantic occurrence keys deduplicate across extractors. Each extractor's
        order and grounded text positions are constraints; contradictions stay
        visible and block that prop instead of inventing a holder history.
        """
        explicit = {e.prop_id for group in groups for e in group
                    if e.source != PropEventSource.AUTO_PATTERN}
        nodes, edges = {}, {}
        for group in groups:
            counts, previous = Counter(), {}
            for event in group:
                if (event.source == PropEventSource.AUTO_PATTERN
                    and event.event_type == PropEventType.USED and event.prop_id in explicit):
                    continue
                signature = cls._signature(event)
                prior = previous.get(event.prop_id)
                if prior is not None and prior[0] == signature:
                    continue  # duplicate evidence with no intervening event
                counts[signature] += 1
                key = (signature, counts[signature])
                if key not in nodes:
                    nodes[key] = event
                    edges[key] = set()
                elif nodes[key].text_position is None and event.text_position is not None:
                    nodes[key] = replace(nodes[key], text_position=event.text_position)
                if prior is not None:
                    edges[prior].add(key)
                previous[event.prop_id] = key

        by_prop = {}
        for key, event in nodes.items():
            by_prop.setdefault(event.prop_id, []).append(key)
        sequences, errors = [], []
        for keys in by_prop.values():
            positioned = sorted((nodes[k].text_position, i, k) for i, k in enumerate(keys)
                                if nodes[k].text_position is not None)
            for (left_pos, _, left), (right_pos, _, right) in zip(positioned, positioned[1:]):
                if left_pos != right_pos:
                    edges[left].add(right)
            indegree = {k: 0 for k in keys}
            for key in keys:
                for successor in edges[key]:
                    indegree[successor] += 1
            pending, ordered = list(keys), []
            while pending:
                ready = next((k for k in pending if indegree[k] == 0), None)
                if ready is None:
                    errors.append('prop_order: ConflictingEventOrder')
                    break
                pending.remove(ready)
                ordered.append(nodes[ready])
                for successor in edges[ready]:
                    indegree[successor] -= 1
            else:
                sequences.append(ordered)
        # Interleave independent props in narrative order while retaining each
        # prop's validated sequence. Legacy extractors without positions retain
        # their stable input order.
        ranks = {id(event): rank for rank, event in enumerate(nodes.values())}
        merged = []
        while sequences:
            if all(sequence[0].text_position is not None for sequence in sequences):
                chosen = min(range(len(sequences)), key=lambda i: (
                    sequences[i][0].text_position, ranks[id(sequences[i][0])]))
            else:
                chosen = min(range(len(sequences)), key=lambda i: ranks[id(sequences[i][0])])
            merged.append(sequences[chosen].pop(0))
            if not sequences[chosen]:
                sequences.pop(chosen)
        return merged, errors

    @staticmethod
    def _candidate_props(active_props: List[Prop], all_props: List[Prop]) -> List[Prop]:
        """提取候选包含已激活道具和未登场道具，避免首次标记被漏掉。"""
        props: Dict[str, Prop] = {}
        for prop in active_props:
            props[prop.id.value] = prop
        for prop in all_props:
            props.setdefault(prop.id.value, prop)
        return list(props.values())

    @staticmethod
    def _enrich_event_from_prop(event: PropEvent, prop: Prop) -> PropEvent:
        """用道具当前持有人补齐 actor，便于 TripleHandler 写出角色-道具事实。"""
        if event.actor_character_id or not prop.holder_character_id:
            return event
        if event.event_type in {
            PropEventType.INTRODUCED,
            PropEventType.USED,
            PropEventType.DAMAGED,
            PropEventType.REPAIRED,
            PropEventType.UPGRADED,
            PropEventType.RESOLVED,
        }:
            return replace(event, actor_character_id=prop.holder_character_id)
        return event
