"""Text budget helpers with an explicit estimate and a conservative fallback."""
from __future__ import annotations

from math import ceil
from typing import Callable


def estimate_text_tokens(text: str, *, chars_per_token_zh: float = 1.5,
                         chars_per_token_en: float = 4.0) -> int:
    if not text:
        return 0
    chinese = sum('\u4e00' <= char <= '\u9fff' for char in text)
    # Counts already reflect the language shares; weighting them again halves
    # the estimate for an evenly mixed input.
    return ceil(chinese / chars_per_token_zh + (len(text) - chinese) / chars_per_token_en)


def conservative_token_count(text: str) -> int:
    """UTF-8 byte upper bound when a matching model tokenizer is unavailable.

    Byte-level tokenizers cannot emit more tokens than input bytes. This is a
    budget guard, not provider-reported usage or a calibrated token estimate.
    """
    return len(text.encode('utf-8'))


def truncate_to_token_budget(text: str, budget: int, counter: Callable[[str], int],
                             *, suffix: str = '...') -> str:
    if budget <= 0:
        return ''
    if counter(text) <= budget:
        return text
    if counter(suffix) > budget:
        suffix = ''
    low, high = 0, len(text)
    best = ''
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid] + suffix if mid else ''
        if counter(candidate) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    # Recheck the exact result; callers can supply model-specific tokenizers.
    return best if counter(best) <= budget else ''
