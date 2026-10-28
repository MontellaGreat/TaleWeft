"""Regressions exercise the production scanner, prompts and saved character readback."""
import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from tests.dag.test_setup_dag_adversarial import setup_db, adapter_services
from tests.dag.test_setup_generation_gates import candidate, approved_review
from application.onboarding.setup_generation import SetupInvocationSteps
from application.world.dtos.bible_dto import CharacterDTO


@pytest.mark.parametrize('unit', ['style', 'core_rules', 'geography', 'society', 'culture', 'daily_life', 'world_review'])
def test_split_world_generation_retains_author_constraints(unit):
    steps = candidate()
    steps.unit = unit
    steps.session.metadata['setup_prompt_source'] = {
        'system': 'AUTHOR_CONSTRAINT: Never introduce a monarchy.',
        'user': 'AUTHOR_SETTING: Keep the salt guild in every relevant unit.'}
    prompt = steps._generation_prompt()
    assert 'Never introduce a monarchy.' in prompt.system
    assert 'Keep the salt guild' in prompt.user
    assert json.loads(prompt.user)['unit'] == unit


@pytest.mark.asyncio
async def test_author_constraints_also_reach_deviation_semantic_and_repair():
    steps = candidate([{'style': 'Use physical evidence and sparse dialogue.'}])
    source = {'system': 'Never introduce a monarchy.', 'user': 'Retain the salt guild setting.'}
    steps.session.metadata['setup_prompt_source'] = source
    assert json.loads(steps._review_prompt().user)['author_instructions'] == source
    assert steps._semantic_context()['author_instructions'] == source
    await steps._repair('anti_ai', {'issues': []})
    request = json.loads(steps.generator.llm_service.prompts[0].user)
    assert request['author_instructions'] == source


def _scan(steps, db, payload, unit):
    steps.stage = steps.unit = unit
    steps.payload = payload
    steps.repos['session']._db = db
    return steps._scan_candidate()


def test_scans_role_after_description_projection_and_nested_semantic_labels(setup_db):
    steps = candidate()
    report = _scan(steps, setup_db, {'characters': [{
        'name': '林渡', 'role': '眼中闪过一丝笑意的主角', 'description': '渡口守桥人。',
        'voice_profile': {'label': '眼中闪过一丝笑意。'}}]}, 'characters')
    assert report['passed'] is False
    assert any('description' in row['path'] and row['total_hits'] >= 2 for row in report['reports'])
    assert any('voice_profile.label' in row['path'] and row['total_hits'] >= 2 for row in report['reports'])


def test_style_prohibition_quotes_are_evidence_exemptions_not_story_violations(setup_db):
    steps = candidate()
    text = '禁止使用“嘴角上扬”“眼中闪过一丝”等套话，用人物动作和对白推进情节。'
    report = _scan(steps, setup_db, {'style': text}, 'style')
    assert report['passed'] is True
    assert sum(len(row.get('instruction_exempted_hits', [])) for row in report['reports']) >= 2
    assert steps.payload['style'] == text


@pytest.mark.parametrize('text', ['她“眼中闪过一丝笑意”，随后开门。',
                                  '禁止使用“嘴角上扬”等套话。她眼中闪过一丝笑意。'])
def test_style_does_not_exempt_unrelated_quotes_or_narrative_after_prohibition(setup_db, text):
    report = _scan(candidate(), setup_db, {'style': text}, 'style')
    assert report['passed'] is False


@pytest.mark.asyncio
async def test_repair_source_is_actual_model_call_not_old_candidate():
    def reject(request):
        return {**approved_review(request), 'passed': False, 'issues': [{'description': 'More concrete.'}]}
    steps = candidate([reject, {'style': 'Describe physical clues in terse dialogue.'}, approved_review])
    old_id = steps.attempt.id
    await steps.deviation()
    repair = next(row for row in steps._record()['model_calls'] if row['purpose'] == 'deviation_repair')
    assert steps.attempt.accounting['source_attempt_id'] == repair['attempt_id']
    assert steps.attempt.accounting['previous_candidate_id'] == old_id


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('description', 'Unreviewed replacement.'), ('background', 'Secret heir.'),
    ('mental_state', 'PARANOID'), ('verbal_tic', 'Unreviewed voice.'),
    ('voice_profile', {'style': 'Unreviewed profile.'}),
    ('relationships', [{'target': 'Unknown', 'relation': 'enemy'}]),
    ('active_wounds', [{'trigger': 'Unreviewed trauma'}]),
    ('hidden_profile', 'Unreviewed spy.'), ('reveal_chapter', 4),
])
async def test_saved_character_readback_rejects_changes_to_every_audited_field(adapter_services, field, value):
    bible, *_ = adapter_services
    original = CharacterDTO('hero', 'Lin', 'A bridge keeper.', [])
    bible.update_bible('novel', characters=[original], world_settings=[], locations=[], timeline_notes=[], style_notes=[])
    expected = asdict(original)
    changed = deepcopy(original)
    setattr(changed, field, value)
    bible.update_bible('novel', characters=[changed], world_settings=[], locations=[], timeline_notes=[], style_notes=[])
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(bible_service=bible)
    steps.commit_result = {'continuation': {'characters': [expected]}}
    with pytest.raises(RuntimeError, match='回读不一致'):
        await steps.verify()


@pytest.mark.asyncio
async def test_saved_character_readback_accepts_exact_persisted_defaults(adapter_services):
    bible, *_ = adapter_services
    original = CharacterDTO('hero', 'Lin', 'A bridge keeper.', [])
    bible.update_bible('novel', characters=[original], world_settings=[], locations=[], timeline_notes=[], style_notes=[])
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(bible_service=bible)
    steps.commit_result = {'continuation': {'characters': [asdict(original)]}}
    assert (await steps.verify())['saved_data_verified'] is True


@pytest.mark.asyncio
async def test_generated_character_projection_roundtrips_legacy_relations_and_wounds(adapter_services):
    from application.world.services.generated_character_normalization import prepare_generated_characters
    bible, *_ = adapter_services
    raw = {'id': 'hero', 'name': 'Lin', 'description': 'A keeper.', 'relationships': ['Ming', {'target': 'Kai'}],
           'active_wounds': ['Avoids the old crossing.']}
    _, projected = prepare_generated_characters('novel', [raw])
    bible.update_bible('novel', characters=projected, world_settings=[], locations=[], timeline_notes=[], style_notes=[])
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(bible_service=bible)
    steps.commit_result = {'continuation': {'characters': [raw]}}
    assert (await steps.verify())['saved_data_verified'] is True
    actual = bible.get_bible_by_novel('novel').characters[0]
    assert actual.active_wounds == [{'trigger': 'Avoids the old crossing.', 'effect': ''}]


def test_duplicate_generated_ids_cannot_collide_with_the_fallback_id():
    from application.world.services.generated_character_normalization import prepare_generated_characters
    rows, projected = prepare_generated_characters('novel', [
        {'id': 'novel-char-2-1', 'name': 'Lin'}, {'id': 'novel-char-2-1', 'name': 'Ming'}])
    assert len({row.id for row in projected}) == len(rows) == 2


@pytest.mark.asyncio
async def test_character_readback_rejects_duplicate_expected_ids(adapter_services):
    bible, *_ = adapter_services
    original = CharacterDTO('hero', 'Lin', 'A keeper.', [])
    bible.update_bible('novel', characters=[original], world_settings=[], locations=[], timeline_notes=[], style_notes=[])
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(bible_service=bible)
    steps.commit_result = {'continuation': {'characters': [asdict(original), asdict(original)]}}
    with pytest.raises(RuntimeError, match='数量或标识'):
        await steps.verify()
