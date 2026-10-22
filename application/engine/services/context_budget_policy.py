"""Budget compression policy for context slots."""
from __future__ import annotations

from typing import Callable, Dict, List

from application.engine.services.context_budget_models import ContextSlot
from domain.ai.services.token_budget import estimate_text_tokens, truncate_to_token_budget


def truncate_t0_slots(
    t0_slots: Dict[str, ContextSlot],
    budget: int,
    *,
    chars_per_token_zh: float,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    """Keep T0 slots by priority; clear every slot outside the remaining budget."""
    counter = token_counter or (lambda text: estimate_text_tokens(text, chars_per_token_zh=chars_per_token_zh))
    budget = max(0, budget)
    total = 0
    for slot in sorted(t0_slots.values(), key=lambda slot: slot.priority, reverse=True):
        remaining = budget - total
        limit = min(remaining, slot.max_tokens) if slot.max_tokens and slot.max_tokens > 0 else remaining
        slot.content = truncate_to_token_budget(slot.content, limit, counter)
        slot.tokens = counter(slot.content)
        total += slot.tokens
    return total


def allocate_tier(
    tier_slots: Dict[str, ContextSlot],
    budget: int,
    compression_log: List[str],
    *,
    chars_per_token_zh: float,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    """Allocate a budget for one tier by priority, mutating overflowing slots."""
    sorted_slots = sorted(
        tier_slots.items(),
        key=lambda item: item[1].priority,
        reverse=True,
    )

    counter = token_counter or (lambda text: estimate_text_tokens(text, chars_per_token_zh=chars_per_token_zh))
    budget = max(0, budget)
    total_used = 0
    for name, slot in sorted_slots:
        original_tokens = counter(slot.content)
        remaining = budget - total_used
        limit = min(remaining, slot.max_tokens) if slot.max_tokens and slot.max_tokens > 0 else remaining
        if original_tokens <= limit:
            slot.tokens = original_tokens
            total_used += original_tokens
            continue
        if limit > slot.min_tokens:
            slot.content = truncate_to_token_budget(slot.content, limit, counter)
            slot.tokens = counter(slot.content)
            total_used += slot.tokens
            verb = '压缩' if slot.max_tokens and slot.max_tokens > 0 else '截断'
            compression_log.append(f"{verb} {name}: {original_tokens} → {slot.tokens} tokens")
        else:
            slot.content = ""
            slot.tokens = 0
            compression_log.append(f"舍弃 {name}（预算不足）")

    return total_used
