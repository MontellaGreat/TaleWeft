"""New paragraph policy reaches real review requests without rewriting old evidence."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from application.audit.services.semantic_anti_ai import SemanticAntiAIReview as Review


@pytest.mark.parametrize('kind', ['story', 'setup', 'instructions'])
@pytest.mark.asyncio
async def test_actual_review_request_requires_one_period_per_paragraph(kind):
    calls = []
    body = '守门人合上账簿。'
    class Provider:
        async def generate(self, prompt, config):
            calls.append(prompt)
            request = json.loads(prompt.user)
            return SimpleNamespace(content=json.dumps({
                'review_version': request['review_version'], 'passed': True,
                'input_hash': request['input_hash'], 'issues': [],
                'checks': {check: True for check in request['required_checks']},
                'coverage': [{'text_id': leaf['text_id']} for leaf in request['texts']]}))
    assert (await Review.review(body, kind=kind, llm=Provider()))['passed'] is True
    assert len(calls) == 1
    assert '每个自然段最多出现一个中文句号' in calls[0].system
    assert 'mechanical_structure' in calls[0].system
    assert '逗号、分号' in calls[0].system


@pytest.mark.parametrize('version,system_hash,user_hash', [
    ('semantic-style-v1', '5b2e12b9595b331f20d8cebe1cb7fad790992d1d4d69e817c29fa10052ba1ab6',
        '17e1829f2d31666a3f9ba0b7a8b55fc8b431cf7f10e8250c0201a09d1adacbc6'),
    ('semantic-style-v2', '34a357e3e1abc9c5755ab278d125f55763c9d3c6599a7d69d54d703e26272086',
        '4edd2dab46d192bab565b2a9234c1df71d6b3432b26e3d6d8555382f91d648a1'),
])
def test_historical_prompts_are_byte_identical(version, system_hash, user_hash):
    prompt = Review.build_prompt({'description': '渡船停在旧码头。', 'style': '用动作推进，保留事实。'},
        kind='setup', context={'instructions_paths': ['$.style']}, version=version)
    assert hashlib.sha256(prompt.system.encode()).hexdigest() == system_hash
    assert hashlib.sha256(prompt.user.encode()).hexdigest() == user_hash


@pytest.mark.parametrize('old_version', ['semantic-style-v1', 'semantic-style-v2'])
def test_new_review_cannot_accept_a_downgraded_receipt(old_version):
    request = json.loads(Review.build_prompt('旧港的灯还亮着。').user)
    report = {'review_version': old_version, 'passed': True, 'input_hash': request['input_hash'],
        'issues': [], 'checks': {check: True for check in request['required_checks']},
        'coverage': [{'text_id': leaf['text_id']} for leaf in request['texts']]}
    with pytest.raises(ValueError, match='版本'):
        Review.validate_report(report, '旧港的灯还亮着。', version=Review.VERSION)


def test_multiple_periods_can_be_reported_with_exact_prose_evidence():
    body = '守卫收起灯罩。渡船靠上石阶。'
    request = json.loads(Review.build_prompt(body).user)
    report = {'review_version': request['review_version'], 'passed': False,
        'input_hash': request['input_hash'], 'checks': {check: True for check in request['required_checks']},
        'coverage': [{'text_id': leaf['text_id']} for leaf in request['texts']],
        'issues': [{'path': '$', 'category': 'mechanical_structure', 'evidence': body,
            'reason': '同一自然段有两个中文句号。', 'suggestion': '按动作先后用逗号衔接，保留段末句号。'}]}
    actual = Review.validate_report(report, body, version=Review.VERSION)
    assert actual['passed'] is False
    assert actual['issues'] == report['issues']
