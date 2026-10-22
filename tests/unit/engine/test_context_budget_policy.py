from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.context_budget_policy import allocate_tier, truncate_t0_slots


def _slot(content, tokens, priority=0, max_tokens=100, min_tokens=0):
    return ContextSlot(
        name="slot",
        tier=PriorityTier.T1_COMPRESSIBLE,
        content=content,
        tokens=tokens,
        max_tokens=max_tokens,
        min_tokens=min_tokens,
        priority=priority,
    )


def test_allocate_tier_keeps_high_priority_and_logs_original_tokens():
    slots = {
        "high": _slot("高" * 10, tokens=10, priority=100),
        "low": _slot("低" * 30, tokens=30, priority=1, max_tokens=30),
    }
    log = []

    used = allocate_tier(slots, 20, log, chars_per_token_zh=1.0)

    assert used == 20
    assert slots["high"].tokens == 10
    assert slots["low"].tokens == 10
    assert log == ["压缩 low: 30 → 10 tokens"]


def test_allocate_tier_discards_when_remaining_below_minimum():
    slots = {
        "high": _slot("高" * 10, tokens=10, priority=100),
        "low": _slot("低" * 30, tokens=30, priority=1, max_tokens=30, min_tokens=12),
    }
    log = []

    used = allocate_tier(slots, 20, log, chars_per_token_zh=1.0)

    assert used == 10
    assert slots["low"].content == ""
    assert slots["low"].tokens == 0
    assert log == ["舍弃 low（预算不足）"]


def test_truncate_t0_slots_clears_every_slot_after_budget_exhaustion():
    slots = {
        "first": ContextSlot(
            name="first",
            tier=PriorityTier.T0_CRITICAL,
            content="一" * 10,
            tokens=10,
        ),
        "second": ContextSlot(
            name="second",
            tier=PriorityTier.T0_CRITICAL,
            content="二" * 10,
            tokens=10,
        ),
        "third": ContextSlot(name="third", tier=PriorityTier.T0_CRITICAL,
                             content="不能漏回最终上下文", tokens=10),
    }

    used = truncate_t0_slots(slots, 15, chars_per_token_zh=1.0)

    assert used == 15
    assert slots["first"].tokens == 10
    assert slots["second"].tokens == 5
    assert slots["second"].content.endswith("...")
    assert slots["third"].content == ""
    assert slots["third"].tokens == 0


def test_slot_cap_and_truncation_suffix_are_counted_with_real_counter():
    slots = {"low": _slot("中文Mixed" * 20, tokens=1, max_tokens=12)}
    log = []
    used = allocate_tier(slots, 1000, log, chars_per_token_zh=1.5,
                         token_counter=lambda text: len(text.encode('utf-8')))
    assert used <= 12
    assert used == len(slots['low'].content.encode('utf-8'))
    assert slots['low'].content.endswith('...')
