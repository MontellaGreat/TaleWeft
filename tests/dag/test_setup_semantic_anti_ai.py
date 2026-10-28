"""No-rule-hit prose still needs actual semantic review before setup adoption."""
import json

import pytest

from application.audit.services import anti_ai_audit
from domain.ai.services.llm_service import GenerationResult
from tests.dag.test_setup_generation_gates import candidate, FixtureLLM, approved_review


def semantic_approval(request):
    from tests.dag.setup_model_fixtures import approved_semantic_review
    return approved_semantic_review(request)


class ReviewingLLM(FixtureLLM):
    def __init__(self, replies, semantic_replies):
        super().__init__(replies)
        self.semantic_replies = iter(semantic_replies)
        self.semantic_prompts = []

    async def generate(self, prompt, config):
        request = json.loads(prompt.user)
        if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
            self.semantic_prompts.append(request)
            reply = next(self.semantic_replies)
            if isinstance(reply, BaseException):
                raise reply
            return GenerationResult(json.dumps(reply(request) if callable(reply) else reply), None)
        return await super().generate(prompt, config)


def setup_candidate(monkeypatch, replies, semantic_replies):
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = candidate()
    steps.generator.llm_service = ReviewingLLM(replies, semantic_replies)
    return steps


@pytest.mark.asyncio
async def test_zero_local_hits_do_not_skip_model_semantic_review(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [semantic_approval])
    await steps.deviation()
    await steps.anti_ai()
    report = steps._record()['audits']['anti_ai']
    assert steps.generator.llm_service.semantic_prompts
    assert report['semantic_review']['status'] == 'completed'
    assert report['semantic_review']['passed'] is True
    assert report['semantic_review']['attempt_id']
    assert steps.generator.llm_service.semantic_prompts[0]['texts'][0]['role'] == 'instructions'
    steps._assert_audited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [{}, RuntimeError('semantic model offline')])
async def test_malformed_or_failed_semantic_review_cannot_become_passed(monkeypatch, failure):
    steps = setup_candidate(monkeypatch, [approved_review], [failure])
    await steps.deviation()
    with pytest.raises((ValueError, RuntimeError)):
        await steps.anti_ai()
    assert steps._record()['audits'].get('anti_ai', {}).get('passed') is not True
    with pytest.raises(ValueError):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_semantic_issue_alone_triggers_repair_deviation_and_semantic_recheck(monkeypatch):
    def reject(request):
        report = semantic_approval(request)
        report.update(passed=False, issues=[{'path': '$.style', 'category': 'vague_lyricism',
            'evidence': 'economical dialogue', 'reason': '要求抽象，未说明可执行边界。',
            'suggestion': '限制解释性旁白，让对白推动当前目标。'}])
        return report
    revised = {'style': 'Limit explanatory narration; use dialogue to advance the immediate goal.'}
    steps = setup_candidate(monkeypatch, [approved_review, revised, approved_review], [reject, semantic_approval])
    await steps.deviation()
    await steps.anti_ai()
    assert steps.payload == revised
    assert len(steps.generator.llm_service.semantic_prompts) == 2
    record = steps._record()
    assert record['audits']['anti_ai']['semantic_review']['passed'] is True
    assert record['audits']['deviation']['input_hash'] == steps._candidate_hash()
    assert any(row.get('semantic_review', {}).get('issues') for row in record['audit_history'])
    assert any(call['purpose'] == 'anti_ai_repair' for call in record['model_calls'])
    repairs = [prompt for prompt in steps.generator.llm_service.prompts
               if json.loads(prompt.user).get('reason') == 'anti_ai']
    assert repairs and all('每个自然段最多出现一个中文句号' in prompt.system for prompt in repairs)
    steps._assert_audited()


@pytest.mark.asyncio
async def test_old_rule_only_receipt_cannot_authorize_new_commit(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [semantic_approval])
    await steps.deviation()
    await steps.anti_ai()
    steps._record()['audits']['anti_ai'].pop('semantic_review', None)
    with pytest.raises(ValueError, match='语义'):
        steps._assert_audited()


@pytest.mark.asyncio
@pytest.mark.parametrize('historical_version', ['semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3'])
async def test_complete_legacy_durable_review_remains_valid_after_protocol_upgrade(monkeypatch, historical_version):
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview as Review
    steps = setup_candidate(monkeypatch, [approved_review], [semantic_approval])
    await steps.deviation()
    scan = steps._scan_candidate()
    payload, context = steps._audit_payload(), steps._semantic_context()
    legacy_prompt = Review.build_prompt(payload, kind='instructions', context=context, version=historical_version)
    raw, attempt_id = await steps._model_json(legacy_prompt, 'anti_ai_semantic_review')
    scan['semantic_review'] = {**Review.validate_report(raw, payload, kind='instructions', context=context),
                               'attempt_id': attempt_id}
    steps._record()['audits']['anti_ai'] = scan
    steps._assert_audited()
    # A prior real approval must not authorize changed context on the same text.
    steps.base_context = {**steps.base_context, 'synopsis': '作者已改变故事基础。'}
    with pytest.raises(ValueError, match='审稿'):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_new_setup_request_rejects_downgraded_legacy_coverage(monkeypatch):
    def legacy_response(request):
        return {'passed': True, 'input_hash': request['input_hash'], 'issues': [],
                'checks': {key: True for key in request['required_checks']},
                'coverage': [{'path': text['path'], 'input_hash': text['input_hash']} for text in request['texts']]}
    steps = setup_candidate(monkeypatch, [approved_review], [legacy_response])
    await steps.deviation()
    with pytest.raises(ValueError, match='版本'):
        await steps.anti_ai()
    with pytest.raises(ValueError):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_no_effective_rules_cannot_be_forged_as_completed_scan(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [semantic_approval])
    await steps.deviation()
    await steps.anti_ai()
    steps._record()['audits']['anti_ai']['reports'][0]['effective_rule_count'] = 0
    with pytest.raises(ValueError, match='审核报告'):
        steps._assert_audited()


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['missing', 'content', 'prompt', 'purpose'])
async def test_semantic_commit_checks_durable_model_call_not_only_checkpoint(monkeypatch, tamper):
    steps = setup_candidate(monkeypatch, [approved_review], [semantic_approval])
    await steps.deviation()
    await steps.anti_ai()
    report = steps._record()['audits']['anti_ai']['semantic_review']
    repo = steps.repos['attempt']
    attempt = repo.get(report['attempt_id'])
    if tamper == 'missing':
        original_get = repo.get
        repo.get = lambda key: None if key == report['attempt_id'] else original_get(key)
    elif tamper == 'content':
        attempt.content = '{}'
    elif tamper == 'prompt':
        from domain.ai.value_objects.prompt import Prompt
        from dataclasses import replace
        attempt.prompt_snapshot = replace(attempt.prompt_snapshot, prompt=Prompt(system='review', user='{}'))
    elif tamper == 'purpose':
        attempt.accounting['setup_purpose'] = 'deviation_review'
    if tamper != 'missing':
        repo.save(attempt)
    with pytest.raises(ValueError, match='语义'):
        steps._assert_audited()
