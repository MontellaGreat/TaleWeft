"""One evidence-only correction; hard-state validation stays authoritative."""
from __future__ import annotations

import json

from application.ai.structured_json_pipeline import parse_and_repair_json
from application.evolution.services.chapter_end_state_bridge import ChapterEndState, build_chapter_end_actions
from domain.ai.services.llm_service import GenerationConfig


_REPAIRABLE = frozenset({
    'chapter-end evidence must name the asserted entities',
    'character location lacks quoted evidence',
})


def _facts(state):
    """Compare normalized facts in order, excluding only permitted quote fields."""
    data = state.model_dump()
    for record in data['characters']:
        record.pop('evidence')
    for record in data['items']:
        record.pop('evidence')
    return data


def _anchored_extension(content, before, after):
    """Keep the exact, latest original occurrence; never substitute older proof."""
    before, after = before.strip(), after.strip()
    old_start, new_start = content.rfind(before), content.rfind(after)
    if (not before or not after or old_start < 0 or new_start < 0
            or new_start > old_start or new_start + len(after) < old_start + len(before)):
        raise ValueError('chapter-end evidence repair must extend the original exact quote')


async def repair_chapter_end_evidence(novel_id, chapter_number, content, raw, resolver, llm, error):
    """Return corrected raw state and proven actions, or fail without fallback."""
    if type(error) is not ValueError or str(error) not in _REPAIRABLE:
        raise error
    original = ChapterEndState.model_validate(raw)
    for record in [*original.characters, *original.items]:
        _anchored_extension(content, record.evidence, record.evidence)
    from infrastructure.ai.prompt_keys import CHAPTER_END_EVIDENCE_REPAIR
    from infrastructure.ai.prompt_utils import render_required_prompt

    prompt = render_required_prompt(CHAPTER_END_EVIDENCE_REPAIR, {
        'content': content,
        'candidate': json.dumps(raw, ensure_ascii=False),
        'validation_error': str(error),
    })
    result = await llm.generate(prompt, GenerationConfig(max_tokens=8192, temperature=0.1))
    text = result.content if hasattr(result, 'content') else str(result)
    repaired_raw, _ = parse_and_repair_json(text)
    if not isinstance(repaired_raw, dict):
        raise ValueError('chapter-end evidence repair must return a state object')
    repaired = ChapterEndState.model_validate(repaired_raw)
    if _facts(original) != _facts(repaired):
        raise ValueError('chapter-end evidence repair cannot change asserted facts')
    for before, after in zip([*original.characters, *original.items], [*repaired.characters, *repaired.items]):
        _anchored_extension(content, before.evidence, after.evidence)
    validated = build_chapter_end_actions(novel_id, chapter_number, content, repaired_raw, resolver)
    if validated['ignored']:
        raise ValueError('chapter-end evidence repair lacks complete current evidence')
    return repaired_raw, validated
