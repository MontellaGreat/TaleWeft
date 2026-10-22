"""A failed quote can be repaired once; asserted facts cannot be changed."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.application.evolution.test_chapter_end_state_bridge import item_state, resolver
from tests.unit.application.world.test_chapter_narrative_failure_protection import knowledge


def _llm(payload):
    return SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))))


def _candidate():
    return item_state('周衡', quote='周衡收回钥匙。')


TEXT = '周衡拿着铜钥匙，随后林岚借用。周衡收回钥匙。'


async def _repair(llm, raw=None):
    from application.evolution.services.chapter_end_evidence_repair import repair_chapter_end_evidence
    return await repair_chapter_end_evidence('book', 3, TEXT, raw or _candidate(), resolver(), llm,
        ValueError('chapter-end evidence must name the asserted entities'))


@pytest.mark.asyncio
async def test_wider_exact_quote_repairs_without_changing_final_holder(knowledge):
    repaired = _candidate()
    repaired['items'][0]['evidence'] = TEXT
    raw, result = await _repair(llm := _llm(repaired))
    assert raw == repaired and result['ignored'] == []
    assert result['actions'][0]['payload']['to_id'] == 'char-b'
    assert result['actions'][0]['source_refs'][0]['quote'] == TEXT
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['empty', 'holder', 'name', 'certainty', 'previous', 'extra', 'fabricated'])
async def test_repair_cannot_drop_facts_change_identity_or_forge_quotes(knowledge, mutation):
    repaired = _candidate()
    repaired['items'][0]['evidence'] = TEXT
    if mutation == 'empty':
        repaired['items'] = []
    elif mutation == 'holder':
        repaired['items'][0]['holder_name'] = '林岚'
    elif mutation == 'name':
        repaired['items'][0]['item_name'] = '钥匙'
    elif mutation == 'certainty':
        repaired['items'][0]['certainty'] = 'uncertain'
    elif mutation == 'previous':
        repaired['items'][0]['previous_holder_name'] = '林岚'
    elif mutation == 'extra':
        repaired['extra'] = True
    else:
        repaired['items'][0]['evidence'] = '周衡把铜钥匙收进口袋。'
    llm = _llm(repaired)
    with pytest.raises(ValueError):
        await _repair(llm)
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_irreparable_error_makes_no_extra_request(knowledge):
    from application.evolution.services.chapter_end_evidence_repair import repair_chapter_end_evidence
    llm = _llm(_candidate())
    with pytest.raises(ValueError, match='ambiguous'):
        await repair_chapter_end_evidence('book', 3, TEXT, _candidate(), resolver(), llm,
                                         ValueError('ambiguous chapter-end entity name'))
    llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_provider_error_is_not_retried_recursively(knowledge):
    llm = SimpleNamespace(generate=AsyncMock(side_effect=TimeoutError('unavailable')))
    with pytest.raises(TimeoutError):
        await _repair(llm)
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['location', 'status', 'order', 'too_long', 'noncurrent'])
async def test_character_facts_order_and_quote_guards_remain_strict(knowledge, mutation):
    from application.evolution.services.chapter_end_evidence_repair import repair_chapter_end_evidence
    raw = {'characters': [
        {'character_name': name, 'location': '码头', 'status': 'alive', 'evidence': '等候。',
         'time_scope': 'chapter_end', 'certainty': 'explicit'} for name in ('林岚', '周衡')], 'items': []}
    text = '林岚和周衡在码头等候。'
    repaired = copy.deepcopy(raw)
    for record in repaired['characters']:
        record['evidence'] = text
    if mutation == 'location':
        repaired['characters'][0]['location'] = None
    elif mutation == 'status':
        repaired['characters'][0]['status'] = None
    elif mutation == 'order':
        repaired['characters'].reverse()
    elif mutation == 'too_long':
        text += '文' * 1201
        repaired['characters'][0]['evidence'] = text
    else:
        text = '林岚和周衡打算去码头等候。'
        for record in repaired['characters']:
            record['evidence'] = text
    llm = _llm(repaired)
    with pytest.raises(ValueError):
        await repair_chapter_end_evidence('book', 3, text, raw, resolver(), llm,
            ValueError('chapter-end evidence must name the asserted entities'))
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_repairs_once_persists_proven_state_and_audit(knowledge):
    from application.world.services import chapter_narrative_sync as narrative
    service, db = knowledge
    db.execute("INSERT INTO unified_characters(id,novel_id,name) VALUES ('real-char','novel-summary','周衡')")
    db.commit()
    original = _candidate()
    repaired = copy.deepcopy(original)
    repaired['items'][0]['evidence'] = TEXT
    llm = SimpleNamespace(generate=AsyncMock(side_effect=[
        SimpleNamespace(content=json.dumps({'summary': '摘要', 'evolution_end_state': original}, ensure_ascii=False)),
        SimpleNamespace(content=json.dumps(repaired, ensure_ascii=False)),
    ]))
    flags = await narrative.sync_chapter_narrative_after_save('novel-summary', 3, TEXT, service, None, llm)
    assert flags['narrative_sync_ok'] is True
    assert flags['evolution_state_coverage'] == 'validated'
    assert not flags.get('evolution_errors') and not flags.get('stage_errors')
    assert flags['evolution_actions'][0]['payload']['to_id'] == 'real-char'
    saved = json.loads(db.fetch_one('SELECT bundle_json FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=3',
                                    ('novel-summary',))['bundle_json'])
    assert saved['evolution_end_state'] == repaired
    assert saved['_meta']['evolution_evidence_repair']['original'] == original
    assert llm.generate.await_count == 2


@pytest.mark.asyncio
async def test_sync_exhausted_repair_remains_invalid(knowledge):
    from application.world.services import chapter_narrative_sync as narrative
    service, _ = knowledge
    payload = {'summary': '摘要', 'evolution_end_state': _candidate()}
    llm = SimpleNamespace(generate=AsyncMock(side_effect=[
        SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
        SimpleNamespace(content=json.dumps(_candidate(), ensure_ascii=False)),
    ]))
    flags = await narrative.sync_chapter_narrative_after_save('novel-summary', 3, TEXT, service, None, llm)
    assert flags['narrative_sync_ok'] is False and flags['evolution_state_coverage'] == 'invalid'
    assert flags['evolution_errors'] and llm.generate.await_count == 2


@pytest.mark.asyncio
async def test_repair_cannot_replace_final_anchor_with_earlier_possession(knowledge):
    from application.evolution.services.chapter_end_evidence_repair import repair_chapter_end_evidence
    from application.evolution.services.chapter_end_state_bridge import build_chapter_end_actions

    text = '周衡握着铜钥匙。随后周衡把铜钥匙交给林岚。林岚握着铜钥匙。周衡站在门口，钥匙已经不在他手中。'
    original = item_state('周衡', quote='周衡站在门口，钥匙已经不在他手中。')
    with pytest.raises(ValueError, match='must name the asserted entities') as failure:
        build_chapter_end_actions('book', 3, text, original, resolver())
    repaired = copy.deepcopy(original)
    repaired['items'][0]['evidence'] = '周衡握着铜钥匙。'
    llm = _llm(repaired)

    with pytest.raises(ValueError):
        await repair_chapter_end_evidence('book', 3, text, original, resolver(), llm, failure.value)

    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_repair_expansion_must_cover_latest_occurrence_of_original_quote(knowledge):
    from application.evolution.services.chapter_end_evidence_repair import repair_chapter_end_evidence
    from application.evolution.services.chapter_end_state_bridge import build_chapter_end_actions

    original_quote = '周衡收回钥匙。'
    earlier_span = original_quote + '周衡把铜钥匙交给林岚。'
    text = earlier_span + '林岚握着铜钥匙。' + original_quote
    original = item_state('周衡', quote=original_quote)
    with pytest.raises(ValueError, match='must name the asserted entities') as failure:
        build_chapter_end_actions('book', 3, text, original, resolver())
    repaired = copy.deepcopy(original)
    repaired['items'][0]['evidence'] = earlier_span
    llm = _llm(repaired)

    with pytest.raises(ValueError):
        await repair_chapter_end_evidence('book', 3, text, original, resolver(), llm, failure.value)

    llm.generate.assert_awaited_once()


