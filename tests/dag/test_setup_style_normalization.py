import json

import pytest

from application.onboarding.setup_generation import SetupInvocationSteps


STYLE = {
    '核心公约': '以可见行动呈现人物选择，避免旁白替人物解释。',
    '叙事视角': ['限制在当场人物可感知的信息。', '切换视角须另起段落。'],
    '语言规则': {'对白': '按人物身份区分措辞。', '节奏': '线索落地后再揭示代价。'},
}


@pytest.mark.parametrize('value', [STYLE, ['用具体行动呈现选择。', '对白保持人物差异。'],
                                 json.dumps(STYLE, ensure_ascii=False)])
@pytest.mark.parametrize('unit', ['style', 'world_review'])
def test_structured_style_retains_prose_in_current_unit_contract(value, unit):
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = unit
    result = steps._narrow({'style': value, 'worldbuilding': {}})
    assert isinstance(result['style'], str)
    if isinstance(value, list):
        assert all(text in result['style'] for text in value)
    else:
        for text in ['核心公约', '叙事视角', '语言规则', '以可见行动呈现人物选择',
                     '限制在当场人物可感知的信息', '切换视角须另起段落', '按人物身份区分措辞', '线索落地后再揭示代价']:
            assert text in result['style']
        assert "{'" not in result['style']


@pytest.mark.parametrize('value', [None, '', '  ', {}, [], True, 12,
                                 {'passed': True}, {'rules': [None, {}, '  ']}, '{"passed":true}'])
def test_empty_or_non_prose_style_does_not_become_valid_content(value):
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.payload = steps._narrow({'style': value})
    with pytest.raises(ValueError, match='文风公约'):
        steps.validate('validate')


def test_resumed_structured_style_invalidates_old_reports_and_keeps_content():
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.payload = {'style': STYLE}
    steps.units = {'style': {'payload': steps.payload, 'audits': {
        'deviation': {'passed': True, 'input_hash': 'old-object'},
        'anti_ai': {'passed': True, 'input_hash': 'old-object'}}, 'validated_hash': 'old-object'}}
    steps.validate('validate')
    assert isinstance(steps.payload['style'], str)
    assert '按人物身份区分措辞' in steps.payload['style']
    assert steps.units['style']['audits'] == {}
    assert steps.units['style']['validated_hash'] == steps._candidate_hash()


def test_plain_style_remains_unchanged_during_validation():
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.payload = {'style': '用具体行动呈现选择。'}
    steps.units = {'style': {'audits': {'deviation': {'input_hash': steps._candidate_hash()}}}}
    steps.validate('validate')
    assert steps.payload == {'style': '用具体行动呈现选择。'}
    assert steps.units['style']['audits']['deviation']['input_hash'] == steps._candidate_hash()


@pytest.mark.parametrize('value', [
    {'passed': True, 'input_hash': 'candidate-hash', 'issues': [], 'checks': {'synopsis': True}},
    {'passed': False, 'issues': [{'description': 'A review issue is not a style guide.'}]},
    {'error': {'type': 'upstream_error', 'message': 'Provider unavailable'}},
    {'status': 'error', 'message': 'Provider unavailable'},
])
def test_review_and_error_envelopes_are_not_style_prose(value):
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.payload = steps._narrow({'style': value})
    with pytest.raises(ValueError, match='文风公约'):
        steps.validate('validate')


@pytest.mark.parametrize('change', ['content', 'session', 'failed', 'missing'])
def test_resume_cannot_replace_mismatched_source_with_normalized_candidate(change):
    from types import SimpleNamespace
    from application.ai_invocation.dtos import InvocationAttemptStatus, InvocationSession, InvocationPolicy
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.payload = {'style': STYLE}
    steps.session = InvocationSession('current-session', 'bible.setup.worldbuilding', 'bible-worldbuilding',
                                      InvocationPolicy.FULL_INTERACTIVE)
    steps._save = lambda: None
    steps.attempt = SimpleNamespace(id='candidate', status=InvocationAttemptStatus.SUCCEEDED, session_id='current-session',
                                   content=json.dumps(steps.payload, ensure_ascii=False))
    if change == 'content':
        steps.attempt.content = json.dumps({'style': {'unreviewed': 'Different persisted candidate.'}})
    elif change == 'session':
        steps.attempt.session_id = 'other-session'
    elif change == 'failed':
        steps.attempt.status = InvocationAttemptStatus.FAILED
    else:
        steps.attempt = None
    before = steps.payload
    with pytest.raises(ValueError, match='候选'):
        steps.validate('validate')
    assert steps.payload is before
