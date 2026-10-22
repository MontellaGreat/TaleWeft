"""Data models for context budget allocation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from engine.core.entities.story import StoryPhase
from domain.ai.services.token_budget import conservative_token_count, truncate_to_token_budget


CONTEXT_LAYER2_HEADER = 'RECENT CHAPTERS'
CONTEXT_LAYER3_HEADER = 'VECTOR RECALL'


def serialize_context_layers(payload: dict) -> str:
    """The actual layered chapter-context text sent by the workflow."""
    return (f"{payload['layer1_text']}\n\n=== {CONTEXT_LAYER2_HEADER} ===\n{payload['layer2_text']}\n\n"
            f"=== {CONTEXT_LAYER3_HEADER} ===\n{payload['layer3_text']}")


class PriorityTier(str, Enum):
    """优先级层级（洋葱模型）"""

    T0_CRITICAL = "t0_critical"
    T1_COMPRESSIBLE = "t1_compressible"
    T2_DYNAMIC = "t2_dynamic"
    T3_SACRIFICIAL = "t3_sacrificial"


@dataclass
class ContextSlot:
    """上下文槽位"""

    name: str
    tier: PriorityTier
    content: str = ""
    tokens: int = 0
    max_tokens: Optional[int] = None
    min_tokens: int = 0
    priority: int = 0

    @property
    def is_mandatory(self) -> bool:
        """是否强制保留"""
        return self.tier == PriorityTier.T0_CRITICAL


@dataclass
class BudgetAllocation:
    """预算分配结果"""

    slots: Dict[str, ContextSlot] = field(default_factory=dict)
    total_budget: int = 35000
    used_tokens: int = 0
    remaining_tokens: int = 0

    t0_reserved: int = 0
    t1_allocated: int = 0
    t2_allocated: int = 0
    t3_allocated: int = 0

    compression_applied: bool = False
    compression_log: List[str] = field(default_factory=list)
    expired_foreshadows: List[str] = field(default_factory=list)

    progress: float = 0.0
    phase: StoryPhase = StoryPhase.OPENING
    total_chapters: int = 0
    token_counter: Callable[[str], int] = field(default=conservative_token_count, repr=False)
    budget_measure: str = 'utf8_upper_bound'
    serialization_tokens: int = 0

    def get_final_context(self) -> str:
        """组装最终上下文"""
        return self.enforce_budget()

    def enforce_budget(self, serializer: Callable[[], str] | None = None) -> str:
        """Check serialized headers, separators and closing instructions too."""
        if self.total_budget < 0:
            raise ValueError('total_budget must be non-negative')
        counter = self.token_counter
        serialize = serializer or self._serialize_context
        context = serialize()
        tier_order = list(PriorityTier)
        removable = sorted(self.slots.items(), key=lambda item: (
            tier_order.index(item[1].tier), -item[1].priority,
        ), reverse=True)
        for name, slot in removable:
            overflow = counter(context) - self.total_budget
            if overflow <= 0:
                break
            if not slot.content.strip():
                continue
            before = counter(slot.content)
            slot.content = truncate_to_token_budget(slot.content, max(0, before - overflow), counter)
            self.compression_log.append(f'序列化预算裁剪 {name}: {before} → {counter(slot.content)}')
            context = serialize()
        if counter(context) > self.total_budget:
            # Expired-foreshadow instructions are mandatory. Fail explicitly
            # rather than drop them or send an over-budget prompt.
            raise ValueError('Mandatory closing instructions exceed the context budget')
        for slot in self.slots.values():
            slot.tokens = counter(slot.content)
        self.t0_reserved = sum(slot.tokens for slot in self.slots.values() if slot.tier == PriorityTier.T0_CRITICAL)
        self.t1_allocated = sum(slot.tokens for slot in self.slots.values() if slot.tier == PriorityTier.T1_COMPRESSIBLE)
        self.t2_allocated = sum(slot.tokens for slot in self.slots.values() if slot.tier == PriorityTier.T2_DYNAMIC)
        self.t3_allocated = sum(slot.tokens for slot in self.slots.values() if slot.tier == PriorityTier.T3_SACRIFICIAL)
        self.used_tokens = counter(context)
        self.serialization_tokens = self.used_tokens - sum(slot.tokens for slot in self.slots.values())
        self.remaining_tokens = self.total_budget - self.used_tokens
        self.compression_applied = bool(self.compression_log)
        return context

    def _serialize_context(self) -> str:
        parts = []

        for tier in [
            PriorityTier.T0_CRITICAL,
            PriorityTier.T1_COMPRESSIBLE,
            PriorityTier.T2_DYNAMIC,
            PriorityTier.T3_SACRIFICIAL,
        ]:
            tier_slots = [
                (name, slot) for name, slot in self.slots.items() if slot.tier == tier
            ]
            tier_slots.sort(key=lambda item: item[1].priority, reverse=True)

            for _name, slot in tier_slots:
                if slot.content.strip():
                    parts.append(f"\n=== {slot.name.upper()} ===\n{slot.content}")

        if self.expired_foreshadows:
            parts.append(self.closing_instructions())

        return "\n".join(parts)

    def closing_instructions(self) -> str:
        if not self.expired_foreshadows:
            return ''
        return ("\n=== 强制剧情收束令 ===\n"
                "以下伏笔已超出预期揭晓章节，必须在本章或本节拍的行文中，通过回忆、对话、意外发展或直接揭露等方式去解答或明显推进悬念：\n"
                + "\n".join(f"- {f}" for f in self.expired_foreshadows)
                + "\n【如果你无视此指令，长篇小说的情节网将陷入崩溃】")
