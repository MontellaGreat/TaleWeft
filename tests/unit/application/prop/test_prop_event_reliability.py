"""Grounded extraction, ordered ownership and real writer ACK regressions."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.prop.extractors.llm_extractor import (
    LlmExtractor, PropExtractionError, PropExtractionLimitError,
)
from application.prop.extractors.pattern_extractor import PatternExtractor
from application.prop.handlers.narrative_event_handler import NarrativePropEventHandler
from application.prop.handlers.triple_handler import TriplePropEventHandler
from application.prop.services.lifecycle_syncer import PropLifecycleSyncer
from domain.prop.entities.prop import Prop
from domain.prop.value_objects.lifecycle_state import LifecycleState
from domain.prop.value_objects.prop_event import PropEvent, PropEventSource, PropEventType
from domain.prop.value_objects.prop_id import PropId
from infrastructure.persistence.database.sqlite_narrative_event_repository import SqliteNarrativeEventRepository
from infrastructure.persistence.database.sqlite_prop_event_repository import SqlitePropEventRepository
from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


PID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
TRACKED = [{'id': PID, 'name': '铜铃', 'holder': 'A', 'state': 'ACTIVE'}]


def event(kind, **kwargs):
    return PropEvent('untrusted-random-id', PID, 'book', 2, PropEventType(kind),
                     PropEventSource.AUTO_LLM, created_at='2026-10-16T00:00:00', **kwargs)


class Extractor:
    priority = 10
    name = 'test'

    def __init__(self, events, priority=10):
        self.events = events
        self.priority = priority

    async def extract(self, *args):
        return list(self.events)


@pytest.fixture
def llm_case(monkeypatch):
    rendered = {}

    def render(key, variables):
        rendered.update(variables)
        return variables['chapter_excerpt']

    monkeypatch.setattr('infrastructure.ai.prompt_utils.render_required_prompt', render)
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content='[]')))
    resolver = SimpleNamespace(resolve=lambda novel_id, raw, **kw:
        SimpleNamespace(id=raw) if raw in ('A', 'B', 'C') else None)
    return LlmExtractor(llm, resolver), llm, rendered


@pytest.mark.asyncio
async def test_complete_chapter_and_all_tracked_props_reach_prompt(llm_case):
    extractor, llm, rendered = llm_case
    content = '路途漫长。' * 400 + '结尾才把铜铃交给B。'
    tracked = [{'id': str(i), 'name': f'道具{i}'} for i in range(21)]
    llm.generate.return_value.content = json.dumps([{
        'prop_id': '20', 'event_type': 'TRANSFERRED', 'from_holder': 'A',
        'to_holder': 'B', 'evidence_quote': '结尾才把铜铃交给B。',
    }])
    events = await extractor.extract('book', 2, content, tracked)
    assert rendered['chapter_excerpt'] == content
    assert '道具20' in rendered['props_summary']
    assert events[0].text_position > 1500
    assert events[0].to_holder_id == 'B'


@pytest.mark.asyncio
async def test_short_chapter_still_extracts_transfer(llm_case):
    extractor, llm, _ = llm_case
    llm.generate.return_value.content = json.dumps([{
        'prop_id': PID, 'event_type': 'TRANSFERRED', 'to_holder': 'B',
        'evidence_quote': '交给B',
    }])
    assert len(await extractor.extract('book', 2, '交给B', TRACKED)) == 1
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    'not json', '{}', '[42]', '[{"prop_id":"unknown","event_type":"USED"}]',
    json.dumps([{'prop_id': PID, 'event_type': 'UNKNOWN'}]),
    json.dumps([{'prop_id': PID}]),
    json.dumps([{'prop_id': PID, 'event_type': 'TRANSFERRED', 'to_holder': 'unknown'}]),
    json.dumps([{'prop_id': PID, 'event_type': 'TRANSFERRED', 'to_holder': 'B', 'from_holder': 'unknown'}]),
    json.dumps([{'prop_id': PID, 'event_type': 'USED', 'evidence_quote': '杜撰原文'}]),
    json.dumps([{'prop_id': PID, 'event_type': 'USED', 'description': {}}]),
])
async def test_invalid_model_result_is_a_visible_failure(llm_case, raw):
    extractor, llm, _ = llm_case
    llm.generate.return_value.content = raw
    with pytest.raises(PropExtractionError):
        await extractor.extract('book', 2, '完整正文', TRACKED)


@pytest.mark.asyncio
async def test_model_failure_is_not_no_events(llm_case):
    extractor, llm, _ = llm_case
    llm.generate.side_effect = TimeoutError('offline fixture')
    with pytest.raises(PropExtractionError):
        await extractor.extract('book', 2, '完整正文', TRACKED)


@pytest.mark.asyncio
@pytest.mark.parametrize('over_limit', ['chapter', 'props'])
async def test_input_limits_fail_before_model_call(llm_case, over_limit):
    extractor, llm, _ = llm_case
    with pytest.raises(PropExtractionLimitError):
        await extractor.extract('book', 2, '文' * 60001 if over_limit == 'chapter' else '正文',
                                TRACKED * 101 if over_limit == 'props' else TRACKED)
    llm.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_json_is_a_successful_no_event_result(llm_case):
    extractor, _, _ = llm_case
    assert await extractor.extract('book', 2, '没有发生事件。', TRACKED) == []


@pytest.mark.asyncio
async def test_repeated_quotes_use_successive_occurrences(llm_case):
    extractor, llm, _ = llm_case
    content = 'A交给B。B交给A。A交给B。'
    llm.generate.return_value.content = json.dumps([
        {'prop_id': PID, 'event_type': 'TRANSFERRED', 'from_holder': a,
         'to_holder': b, 'evidence_quote': quote}
        for a, b, quote in [('A', 'B', 'A交给B'), ('B', 'A', 'B交给A'), ('A', 'B', 'A交给B')]
    ])
    events = await extractor.extract('book', 2, content, TRACKED)
    assert [e.text_position for e in events] == [0, 5, 10]


@pytest.fixture
def store(writer):
    _, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','道具回归','prop-reliability')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('chapter-two','book',2,'正文')")
    props, events = SqliteUnifiedPropRepository(db), SqlitePropEventRepository(db)
    prop = Prop(PropId(PID), 'book', '铜铃', lifecycle_state=LifecycleState.ACTIVE,
                introduced_chapter=1, holder_character_id='A')
    props.save(prop)
    return db, props, events


@pytest.mark.asyncio
@pytest.mark.parametrize('holders', [('A', 'B', 'C'), ('A', 'B', 'A', 'B')])
async def test_ordered_multi_transfer_and_cross_extractor_dedup(store, holders):
    db, props, events = store
    sequence = [event('TRANSFERRED', from_holder_id=a, to_holder_id=b,
                      description=f'{a}交给{b}', text_position=i * 10)
                for i, (a, b) in enumerate(zip(holders, holders[1:]))]
    duplicate = [replace(e, id='different-id', description='另一提取器的描述') for e in sequence]
    syncer = PropLifecycleSyncer(props, events, [Extractor(sequence), Extractor(duplicate, 20)])
    first = await syncer.sync('book', 2, '正文')
    second = await syncer.sync('book', 2, '正文')
    assert first['errors'] == second['errors'] == []
    assert first['events_applied'] == len(sequence)
    assert second['events_applied'] == 0
    assert props.get(PropId(PID)).holder_character_id == holders[-1]
    assert [e.to_holder_id for e in events.list_for_chapter('book', 2)] == list(holders[1:])


@pytest.mark.asyncio
async def test_damage_repair_damage_is_not_collapsed_and_pattern_cannot_repair(store):
    _, props, events = store
    sequence = [event(kind, text_position=i * 10) for i, kind in
                enumerate(['DAMAGED', 'REPAIRED', 'DAMAGED'])]
    syncer = PropLifecycleSyncer(props, events, [PatternExtractor(), Extractor(sequence)])
    result = await syncer.sync('book', 2, f'[[prop:{PID}|铜铃]]' + '正文' * 30)
    assert result['errors'] == [] and result['events_applied'] == 3
    assert props.get(PropId(PID)).lifecycle_state == LifecycleState.DAMAGED
    assert (await syncer.sync('book', 2, '正文'))['events_applied'] == 0
    assert len(events.list_for_chapter('book', 2)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['DAMAGED', 'RESOLVED'])
async def test_retry_does_not_repeat_terminal_state_transition(store, kind):
    _, props, events = store
    syncer = PropLifecycleSyncer(props, events, [Extractor([event(kind)])])
    assert (await syncer.sync('book', 2, '正文'))['errors'] == []
    retry = await syncer.sync('book', 2, '正文')
    assert retry['errors'] == [] and retry['events_applied'] == 0
    assert len(events.list_for_chapter('book', 2)) == 1


@pytest.mark.asyncio
async def test_contradictory_evidence_blocks_prop_instead_of_guessing(store):
    _, props, events = store
    sequence = [event('TRANSFERRED', from_holder_id='A', to_holder_id='B', text_position=20),
                event('TRANSFERRED', from_holder_id='B', to_holder_id='C', text_position=10)]
    result = await PropLifecycleSyncer(props, events, [Extractor(sequence)]).sync('book', 2, '正文')
    assert result['errors'] == ['prop_order: ConflictingEventOrder']
    assert props.get(PropId(PID)).holder_character_id == 'A'
    assert events.list_for_chapter('book', 2) == []


@pytest.mark.asyncio
async def test_text_order_is_preserved_when_extractor_timestamps_disagree(store):
    _, props, events = store
    later = replace(event('TRANSFERRED', from_holder_id='B', to_holder_id='C', text_position=20),
                    created_at='2026-10-15T00:00:00')
    earlier = replace(event('TRANSFERRED', from_holder_id='A', to_holder_id='B', text_position=10),
                      created_at='2026-10-16T00:00:00')
    syncer = PropLifecycleSyncer(props, events, [Extractor([later]), Extractor([earlier], 20)])
    result = await syncer.sync('book', 2, '正文')
    assert result['errors'] == [] and result['events_applied'] == 2
    assert [e.to_holder_id for e in events.list_for_chapter('book', 2)] == ['B', 'C']
    assert props.get(PropId(PID)).holder_character_id == 'C'


@pytest.mark.asyncio
async def test_events_for_different_props_keep_narrative_interleaving(store):
    _, props, events = store
    second_id = 'b1b2c3d4-e5f6-7890-abcd-ef1234567890'
    props.save(Prop(PropId(second_id), 'book', '短剑', lifecycle_state=LifecycleState.ACTIVE))
    sequence = [event('USED', text_position=0),
                replace(event('USED', text_position=10), prop_id=second_id),
                event('DAMAGED', text_position=20)]
    result = await PropLifecycleSyncer(props, events, [Extractor(sequence)]).sync('book', 2, '正文')
    assert result['errors'] == [] and result['events_applied'] == 3
    assert [e.prop_id for e in events.list_for_chapter('book', 2)] == [PID, second_id, PID]


@pytest.mark.asyncio
async def test_extract_failure_reaches_sync_result(store, llm_case):
    _, props, events = store
    extractor, llm, _ = llm_case
    llm.generate.side_effect = TimeoutError('offline fixture')
    result = await PropLifecycleSyncer(props, events, [extractor]).sync('book', 2, '正文')
    assert result['errors'] == ['prop_extract: PropExtractionError']
    assert result['events_applied'] == 0
    assert events.list_for_chapter('book', 2) == []


@pytest.mark.asyncio
async def test_wrong_from_holder_blocks_dependent_following_events(store):
    _, props, events = store
    sequence = [event('TRANSFERRED', from_holder_id='C', to_holder_id='B'),
                event('TRANSFERRED', from_holder_id='B', to_holder_id='C')]
    result = await PropLifecycleSyncer(props, events, [Extractor(sequence)]).sync('book', 2, '正文')
    assert result['errors'] == ['prop_transition: LifecycleTransitionError']
    assert props.get(PropId(PID)).holder_character_id == 'A'
    assert events.list_for_chapter('book', 2) == []


@pytest.mark.asyncio
async def test_real_writer_failure_rolls_back_holder_and_event_together(store, monkeypatch):
    db, props, events = store
    original_save = events.save

    def reject_after_staging(event, *, _writer=None):
        original_save(event, _writer=_writer)
        _writer.execute("INSERT INTO no_such_prop_event_table VALUES (1)")

    monkeypatch.setattr(events, 'save', reject_after_staging)
    sequence = [event('TRANSFERRED', from_holder_id='A', to_holder_id='B'),
                event('TRANSFERRED', from_holder_id='B', to_holder_id='C')]
    syncer = PropLifecycleSyncer(props, events, [Extractor(sequence)])
    result = await syncer.sync('book', 2, '正文')
    assert result['events_applied'] == 0 and len(result['errors']) == 1
    assert props.get(PropId(PID)).holder_character_id == 'A'
    assert events.list_for_chapter('book', 2) == []
    monkeypatch.setattr(events, 'save', original_save)
    assert (await syncer.sync('book', 2, '正文'))['events_applied'] == 2
    assert props.get(PropId(PID)).holder_character_id == 'C'


@pytest.mark.asyncio
async def test_handler_retry_is_idempotent_and_keeps_event_specific_facts(store):
    db, props, events = store
    narrative_repo = SqliteNarrativeEventRepository(db)
    narrative = NarrativePropEventHandler(narrative_repo)

    class FailOnce:
        calls = 0

        async def handle(self, prop, event):
            self.calls += 1
            await narrative.handle(prop, event)
            if self.calls == 1:
                raise RuntimeError('failure after narrative ACK')

    sequence = [event('TRANSFERRED', from_holder_id='A', to_holder_id='B'),
                event('DAMAGED', actor_character_id='B')]
    syncer = PropLifecycleSyncer(props, events, [Extractor(sequence)],
        [FailOnce(), TriplePropEventHandler(db)])
    assert (await syncer.sync('book', 2, '正文'))['errors'] == ['prop_handler: RuntimeError']
    before = db.fetch_one("SELECT COUNT(*) AS count FROM triples WHERE novel_id='book'")['count']
    retry = await syncer.sync('book', 2, '正文')
    assert retry['errors'] == [] and retry['events_applied'] == 0
    narratives = narrative_repo.list_up_to_chapter('book', 2)
    assert len(narratives) == 2
    transfer = next(e for e in narratives if e['tags'][1] == 'prop:transferred')
    assert not any(m['attribute'] == 'lifecycle_state' for m in transfer['mutations'])
    assert next(m['value'] for m in transfer['mutations'] if m['attribute'] == 'holder_character_id') == 'B'
    assert db.fetch_one("SELECT COUNT(*) AS count FROM triples WHERE novel_id='book'")['count'] == before


@pytest.mark.asyncio
async def test_handler_retry_works_when_stateful_extractor_now_returns_no_events(store):
    db, props, events = store
    prop = props.get(PropId(PID))
    prop.lifecycle_state, prop.introduced_chapter = LifecycleState.DORMANT, None
    props.save(prop)
    narrative_repo = SqliteNarrativeEventRepository(db)
    narrative = NarrativePropEventHandler(narrative_repo)

    class FailOnce:
        calls = 0

        async def handle(self, prop, event):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('failure before narrative ACK')
            await narrative.handle(prop, event)

    syncer = PropLifecycleSyncer(props, events, [PatternExtractor()], [FailOnce()])
    content = f'发现了[[prop:{PID}|铜铃]]'
    first = await syncer.sync('book', 2, content)
    assert first['events_applied'] == 1 and first['errors'] == ['prop_handler: RuntimeError']
    assert narrative_repo.list_up_to_chapter('book', 2) == []
    second = await syncer.sync('book', 2, content)
    assert second['events_extracted'] == second['events_applied'] == 0
    assert second['errors'] == []
    assert len(narrative_repo.list_up_to_chapter('book', 2)) == 1


@pytest.mark.asyncio
async def test_edited_chapter_replaces_machine_transfers_and_keeps_author_fields(store):
    db, props, events = store
    from application.world.services.chapter_projection_reconciliation import rollback_chapter_projections
    sequence = [event('TRANSFERRED', from_holder_id='A', to_holder_id='B'),
                event('TRANSFERRED', from_holder_id='B', to_holder_id='C')]
    extractor = Extractor(sequence)
    syncer = PropLifecycleSyncer(props, events, [extractor])
    assert (await syncer.sync('book', 2, '旧正文'))['errors'] == []
    db.execute("UPDATE unified_props SET description='作者补充的设定' WHERE id=?", (PID,))
    events.save(replace(event('UPGRADED'), id='manual', source=PropEventSource.MANUAL))
    rollback_chapter_projections(db, 'book', 2)
    assert props.get(PropId(PID)).holder_character_id == 'A'
    extractor.events = [event('TRANSFERRED', from_holder_id='A', to_holder_id='B')]
    assert (await syncer.sync('book', 2, '修改后的正文'))['errors'] == []
    prop = props.get(PropId(PID))
    assert prop.holder_character_id == 'B'
    assert prop.description == '作者补充的设定'
    assert [e.id for e in events.list_for_chapter('book', 2) if e.source == PropEventSource.MANUAL] == ['manual']
    assert len(events.list_for_chapter('book', 2)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['INTRODUCED', 'DAMAGED', 'RESOLVED'])
async def test_pattern_mentions_do_not_repair_or_revive(state):
    tracked = [dict(TRACKED[0], state=state)]
    assert await PatternExtractor().extract('book', 2, f'[[prop:{PID}|铜铃]]', tracked) == []


@pytest.mark.asyncio
async def test_explicit_user_is_preserved_over_generic_marker(store):
    _, props, events = store
    sequence = [event('USED', actor_character_id='B')]
    result = await PropLifecycleSyncer(props, events, [PatternExtractor(), Extractor(sequence)]).sync(
        'book', 2, f'B借用了[[prop:{PID}|铜铃]]')
    assert result['errors'] == [] and result['events_applied'] == 1
    assert events.list_for_chapter('book', 2)[0].actor_character_id == 'B'
    assert props.get(PropId(PID)).holder_character_id == 'A'
