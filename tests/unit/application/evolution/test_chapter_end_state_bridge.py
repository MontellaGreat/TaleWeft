"""The existing extraction call must produce sourced, replayable final facts."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.evolution.services.action_extractor import EvolutionActionExtractor
from application.evolution.services.chapter_end_state_bridge import (
    build_chapter_end_actions,
    make_chapter_end_resolver,
)
from application.evolution.services.snapshot_service import EvolutionSnapshotService
from application.narrative.entity_resolver import EntityResolver
from application.world.services import chapter_narrative_sync as narrative
from domain.evolution.models import ChapterEvolutionSnapshot, EvolutionAction, EvolutionState
from domain.evolution.reducer import EvolutionReducer
from tests.unit.application.evolution.test_snapshot_service import Repo
from tests.unit.application.world.test_chapter_narrative_failure_protection import knowledge
from tests.unit.application.engine.test_persistence_commit_ack import writer


def _entity(identifier, name, **kwargs):
    return SimpleNamespace(id=SimpleNamespace(value=identifier), name=name, **kwargs)


def resolver(props=True):
    return EntityResolver(
        character_repo=SimpleNamespace(list_by_novel=lambda _: [
            _entity('char-a', '林岚'), _entity('char-b', '周衡')]),
        prop_repo=SimpleNamespace(list_by_novel=lambda _: [
            _entity('prop-key', '铜钥匙', aliases=['旧钥匙'])] if props else []))


def item_state(holder='林岚', quote=None, **overrides):
    item = {'item_name': '铜钥匙', 'holder_name': holder, 'holder_state': 'held',
            'ownership_basis': 'current_possession', 'evidence': quote or f'{holder}握着铜钥匙。',
            'time_scope': 'chapter_end', 'certainty': 'explicit'}
    item.update(overrides)
    return {'characters': [], 'items': [item]}


def evidence(number, text, raw=None, **kwargs):
    bridge = build_chapter_end_actions('book', number, text, raw or {}, resolver())
    return {'evolution_actions': bridge['actions'], 'evolution_state_coverage': 'validated', **kwargs}


def test_canonical_and_stable_new_item_identity_is_sourced():
    text = '林岚握着铜钥匙。'
    for known in (True, False):
        actions = build_chapter_end_actions('book', 1, text, item_state(), resolver(known))['actions']
        repeated = build_chapter_end_actions('book', 2, text, item_state(), resolver(known))['actions']
        action = actions[0]
        item_id = action['payload']['item_id']
        assert item_id == ('prop-key' if known else repeated[0]['payload']['item_id'])
        assert action['payload']['to_id'] == 'char-a'
        assert action['payload']['item_name'] == action['source_refs'][0]['item_name'] == '铜钥匙'
        final = EvolutionActionExtractor().extract('book', 1, text, {'evolution_actions': actions})
        state, errors = EvolutionReducer().reduce(EvolutionState.empty(), final)
        assert not errors
        assert state.items[item_id]['name'] == '铜钥匙'
        assert state.characters['char-a']['inventory'] == [item_id]


def test_transfer_updates_owner_and_both_inventory_projections():
    repo = Repo()
    service = EvolutionSnapshotService(repo, EvolutionActionExtractor())
    first = service.build_after_chapter_saved('book', 1, '林岚握着铜钥匙。',
        evidence=evidence(1, '林岚握着铜钥匙。', item_state()))
    repo.previous = first
    second = service.build_after_chapter_saved('book', 2, '周衡握着铜钥匙。',
        evidence=evidence(2, '周衡握着铜钥匙。', item_state('周衡')))
    assert second.status == 'active'
    assert second.ending_state.items['prop-key']['owner_id'] == 'char-b'
    assert second.ending_state.characters['char-a']['inventory'] == []
    assert second.ending_state.characters['char-b']['inventory'] == ['prop-key']
    assert first.ending_state.items['prop-key']['owner_id'] == 'char-a'


def test_edit_first_chapter_then_rebuild_second_inherits_new_opening_and_patch():
    repo = Repo()
    service = EvolutionSnapshotService(repo, EvolutionActionExtractor())
    first = service.build_after_chapter_saved('book', 1, '林岚握着铜钥匙。',
        evidence=evidence(1, '林岚握着铜钥匙。', item_state()))
    repo.previous = first
    old_second = service.build_after_chapter_saved('book', 2, '屋外下着雨。', evidence=evidence(2, '屋外下着雨。'))
    old_second.human_override_patches = [{'op': 'replace', 'path': '/items/prop-key/status', 'value': 'author_checked'}]
    repo.previous = None
    repo.existing = None
    edited_first = service.build_after_chapter_saved('book', 1, '周衡握着铜钥匙。',
        evidence=evidence(1, '周衡握着铜钥匙。', item_state('周衡')))
    repo.previous, repo.existing = edited_first, old_second
    second = service.build_after_chapter_saved('book', 2, '屋外下着雨。', evidence=evidence(2, '屋外下着雨。'))
    assert second.opening_state.items['prop-key']['owner_id'] == 'char-b'
    assert second.ending_state.items['prop-key']['owner_id'] == 'char-b'
    assert second.ending_state.items['prop-key']['status'] == 'author_checked'
    assert second.human_override_patches == old_second.human_override_patches


@pytest.mark.parametrize('overrides', [
    {'time_scope': 'historical'}, {'certainty': 'uncertain'},
    {'ownership_basis': 'historical_transfer'}, {'holder_state': 'unknown'},
    {'evidence': '林岚曾把铜钥匙交给周衡。', 'holder_name': '周衡'},
])
def test_historical_unknown_and_uncertain_do_not_assert_current_owner(overrides):
    text = '林岚曾把铜钥匙交给周衡。林岚握着铜钥匙。'
    assert build_chapter_end_actions('book', 1, text, item_state(**overrides), resolver())['actions'] == []


@pytest.mark.parametrize('raw', [None, {'characters': 'invalid'},
    {'characters': [{'character_name': '林岚', 'status': 'immortal', 'evidence': '林岚在码头。',
                     'time_scope': 'chapter_end', 'certainty': 'explicit'}]},
    item_state(quote='不存在的证据'), item_state(holder=None),
])
def test_malformed_or_unproven_state_fails_validation(raw):
    with pytest.raises(ValueError):
        build_chapter_end_actions('book', 1, '林岚在码头。林岚握着铜钥匙。', raw, resolver())


def test_conflicting_final_holders_and_ambiguous_names_are_rejected():
    raw = item_state()
    raw['items'].append(item_state('周衡')['items'][0])
    text = '林岚握着铜钥匙。周衡握着铜钥匙。'
    with pytest.raises(ValueError, match='conflicting'):
        build_chapter_end_actions('book', 1, text, raw, resolver())
    ambiguous = resolver()
    ambiguous.character_repo = SimpleNamespace(list_by_novel=lambda _: [
        _entity('one', '林岚'), _entity('two', '林岚')])
    with pytest.raises(ValueError, match='ambiguous'):
        build_chapter_end_actions('book', 1, text, item_state(), ambiguous)


class _ResolverDb:
    def __init__(self, rows):
        self.rows = rows

    def fetch_all(self, query, params):
        return self.rows


class _BibleRepo:
    def __init__(self, characters):
        self.bible = SimpleNamespace(characters=characters)

    def get_by_novel_id(self, novel_id):
        return self.bible


def _bible_character(identifier, name):
    return SimpleNamespace(character_id=SimpleNamespace(value=identifier), name=name)


def _unified_row(identifier, name):
    return {'id': identifier, 'novel_id': 'book', 'name': name}


def test_unique_bible_id_disambiguates_stale_unified_duplicate():
    resolver_from_sources = make_chapter_end_resolver(
        'book',
        database=_ResolverDb([_unified_row('canonical-id', '林岚'), _unified_row('stale-id', '林岚')]),
        bible_repository=_BibleRepo([_bible_character('canonical-id', '林岚')]),
    )
    raw = {'characters': [{'character_name': '林岚', 'status': 'alive',
                           'evidence': '林岚还活着。', 'time_scope': 'chapter_end',
                           'certainty': 'explicit'}], 'items': []}
    actions = build_chapter_end_actions('book', 1, '林岚还活着。', raw, resolver_from_sources)['actions']
    assert actions[0]['payload']['character_id'] == 'canonical-id'


def test_multiple_bible_canonical_ids_remain_ambiguous():
    resolver_from_sources = make_chapter_end_resolver(
        'book',
        database=_ResolverDb([_unified_row('canonical-a', '林岚'), _unified_row('canonical-b', '林岚')]),
        bible_repository=_BibleRepo([
            _bible_character('canonical-a', '林岚'),
            _bible_character('canonical-b', '林岚'),
        ]),
    )
    raw = {'characters': [{'character_name': '林岚', 'status': 'alive',
                           'evidence': '林岚还活着。', 'time_scope': 'chapter_end',
                           'certainty': 'explicit'}], 'items': []}
    with pytest.raises(ValueError, match='ambiguous'):
        build_chapter_end_actions('book', 1, '林岚还活着。', raw, resolver_from_sources)


def test_explicit_transfer_previous_holder_conflict_blocks_without_mutating_ownership():
    opening = EvolutionState.empty()
    opening.items['prop-key'] = {'owner_id': 'third', 'name': '铜钥匙'}
    previous = ChapterEvolutionSnapshot('old', 'book', 'main', 1, ending_state=opening)
    extractor = SimpleNamespace(extract=lambda *args, **kwargs: [EvolutionAction(
        'transfer', 'TRANSFER_ITEM', {'item_id': 'prop-key', 'to_id': 'char-b', 'from_id': 'char-a'})])
    snapshot = EvolutionSnapshotService(Repo(previous=previous), extractor).build_after_chapter_saved(
        'book', 2, '正文')
    assert snapshot.status == 'blocked'
    assert snapshot.ending_state.items['prop-key']['owner_id'] == 'third'
    assert any('previous holder' in conflict.get('message', '') for conflict in snapshot.conflicts)


def test_multiple_transfers_in_one_chapter_final_owner_is_not_guarded_by_intermediate_owner():
    repo = Repo()
    service = EvolutionSnapshotService(repo, EvolutionActionExtractor())
    first = service.build_after_chapter_saved('book', 1, '林岚握着铜钥匙。',
        evidence=evidence(1, '林岚握着铜钥匙。', item_state()))
    repo.previous = first
    text = '林岚把铜钥匙交给周衡。周衡握着铜钥匙。周衡又交还给林岚。最后林岚握着铜钥匙。'
    raw = item_state(quote='最后林岚握着铜钥匙。', previous_holder_name='周衡',
                     previous_holder_evidence='周衡握着铜钥匙。')
    data = evidence(2, text, raw)
    assert 'from_id' not in data['evolution_actions'][0]['payload']
    assert data['evolution_actions'][0]['source_refs'][0]['previous_holder_evidence']['character_id'] == 'char-b'
    second = service.build_after_chapter_saved('book', 2, text, evidence=data)
    assert second.status == 'active'
    assert second.ending_state.items['prop-key']['owner_id'] == 'char-a'


def test_death_at_new_location_succeeds_but_resurrection_and_move_remain_blocked():
    text = '林岚死在码头。'
    raw = {'characters': [{'character_name': '林岚', 'status': 'dead', 'location': '码头',
                         'evidence': text, 'time_scope': 'chapter_end', 'certainty': 'explicit'}]}
    repo = Repo()
    service = EvolutionSnapshotService(repo, EvolutionActionExtractor())
    first = service.build_after_chapter_saved('book', 1, text, evidence=evidence(1, text, raw))
    assert first.status == 'active'
    assert first.ending_state.characters['char-a']['status'] == 'dead'
    repo.previous = first
    new_text = '林岚活着走到山门。'
    raw['characters'][0].update(status='alive', location='山门', evidence=new_text)
    second = service.build_after_chapter_saved('book', 2, new_text, evidence=evidence(2, new_text, raw))
    assert second.status == 'blocked'
    assert second.ending_state.characters['char-a']['status'] == 'dead'
    assert second.ending_state.characters['char-a']['location'] == '码头'


@pytest.mark.parametrize('corrupt', ['payload', 'quote', 'chapter', 'envelope'])
def test_invalid_action_evidence_persists_blocked_snapshot(corrupt):
    text = '林岚握着铜钥匙。'
    bundle = evidence(1, text, item_state())
    action = bundle['evolution_actions'][0]
    if corrupt == 'payload':
        action['payload']['extra'] = True
    elif corrupt == 'quote':
        action['source_refs'][0]['quote'] = '伪造的证据'
    elif corrupt == 'chapter':
        action['source_refs'][0]['chapter_number'] = 2
    else:
        action['confidence'] = 'high'
    repo = Repo()
    snapshot = EvolutionSnapshotService(repo, EvolutionActionExtractor()).build_after_chapter_saved('book', 1, text, evidence=bundle)
    assert snapshot.status == 'blocked' and repo.saved is snapshot
    assert snapshot.ending_state.items == {}
    assert snapshot.conflicts[0]['conflict_type'] == 'EXTRACTION_EVIDENCE_INVALID'


@pytest.mark.asyncio
async def test_same_bundle_call_includes_state_without_another_llm_request(knowledge):
    text = '林岚握着铜钥匙。'
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=json.dumps(
        {'summary': '摘要', 'evolution_end_state': item_state()}, ensure_ascii=False))))
    bundle = await narrative.llm_chapter_extract_bundle(llm, text, 1)
    assert bundle['evolution_end_state'] == item_state()
    assert bundle['_meta']['evolution_end_state_provided'] is True
    llm.generate.assert_awaited_once()
    assert llm.generate.call_args.args[1].max_tokens >= 8192


@pytest.mark.asyncio
async def test_long_chapter_is_rejected_before_any_request(knowledge):
    llm = SimpleNamespace(generate=AsyncMock())
    with pytest.raises(ValueError, match='24000'):
        await narrative.llm_chapter_extract_bundle(llm, '文' * 24001, 1)
    llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_end_state_marks_sync_partial_and_snapshot_blocked(knowledge):
    service, db = knowledge
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=json.dumps(
        {'summary': '有效的新摘要', 'evolution_end_state': None}, ensure_ascii=False))))
    flags = await narrative.sync_chapter_narrative_after_save('novel-summary', 2, '正文', service, None, llm)
    assert flags['sync_status'] == 'partial' and flags['narrative_sync_ok'] is False
    assert flags['evolution_state_coverage'] == 'invalid'
    assert flags['evolution_errors'] and any('evolution_end_state' in error for error in flags['stage_errors'])
    snapshot = EvolutionSnapshotService(Repo(), EvolutionActionExtractor()).build_after_chapter_saved(
        'novel-summary', 2, '正文', evidence=flags)
    assert snapshot.status == 'blocked'


@pytest.mark.asyncio
async def test_legacy_bundle_coverage_is_missing_instead_of_claiming_state_updated(knowledge):
    service, _ = knowledge
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content='{"summary":"摘要"}')))
    flags = await narrative.sync_chapter_narrative_after_save('novel-summary', 2, '正文', service, None, llm)
    assert flags['sync_status'] == 'synced'
    assert flags['evolution_actions'] == [] and flags['evolution_state_coverage'] == 'missing'


@pytest.mark.asyncio
async def test_real_sync_bundle_resolves_project_entities_and_persists_evolution(knowledge):
    from infrastructure.persistence.database.sqlite_evolution_repository import SqliteEvolutionRepository
    service, db = knowledge
    db.execute("INSERT INTO unified_characters(id,novel_id,name) VALUES ('real-char','novel-summary','林岚')")
    db.commit()
    text = '林岚握着铜钥匙。'
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=json.dumps(
        {'summary': '新摘要', 'evolution_end_state': item_state()}, ensure_ascii=False))))
    flags = await narrative.sync_chapter_narrative_after_save('novel-summary', 2, text, service, None, llm)
    assert flags['sync_status'] == 'synced'
    assert flags['evolution_state_coverage'] == 'validated'
    assert flags['evolution_actions'][0]['payload']['to_id'] == 'real-char'
    extracted = json.loads(db.fetch_one('SELECT bundle_json FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=2',
                                      ('novel-summary',))['bundle_json'])
    assert extracted['evolution_end_state'] == item_state()
    repo = SqliteEvolutionRepository(db)
    snapshot = EvolutionSnapshotService(repo, EvolutionActionExtractor()).build_after_chapter_saved(
        'novel-summary', 2, text, evidence=flags)
    persisted = repo.get_by_id(snapshot.snapshot_id)
    item = next(iter(persisted.ending_state.items.values()))
    assert persisted.status == 'active' and item['owner_id'] == 'real-char' and item['name'] == '铜钥匙'
    assert persisted.source_refs[0]['chapter_end_state_coverage'] == 'validated'
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_aftermath_forwards_end_state_actions_and_failures_to_auxiliary(monkeypatch, writer):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    text = '林岚握着铜钥匙。'
    flags = evidence(2, text, item_state(), evolution_errors=['broken field'])
    flags.update(narrative_sync_ok=False, sync_status='partial', stage_errors=['evolution_end_state: ValueError'])
    monkeypatch.setattr(narrative, 'sync_chapter_narrative_after_save', AsyncMock(return_value=flags))
    pipeline = ChapterAftermathPipeline(Mock(), None, Mock())
    pipeline._database = lambda: writer[1]
    pipeline._extract_chapter_bridge = AsyncMock()
    pipeline._schedule_auxiliary_stages = Mock()
    result = await pipeline.run_after_chapter_saved('book', 2, text)
    scheduled = pipeline._schedule_auxiliary_stages.call_args.args[3]
    assert result['evolution_actions'] == scheduled['evolution_actions'] == flags['evolution_actions']
    assert scheduled['evolution_errors'] == ['broken field']
    assert scheduled['evolution_state_coverage'] == 'validated'
    assert 'evolution_end_state: ValueError' in scheduled['stage_errors']
