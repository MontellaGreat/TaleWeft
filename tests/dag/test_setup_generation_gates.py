import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from application.ai_invocation.dtos import InvocationPolicy, InvocationSession
from interfaces.api.v1.engine.setup_dag_routes import SetupInvocationSteps


class FixtureLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    async def generate(self, prompt, config):
        request = json.loads(prompt.user)
        if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
            from tests.dag.setup_model_fixtures import approved_semantic_review
            return GenerationResult(json.dumps(approved_semantic_review(request)), None)
        self.prompts.append(prompt)
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(json.loads(prompt.user))
        return GenerationResult(json.dumps(reply), None)

    async def stream_generate(self, prompt, config):
        result = await self.generate(prompt, config)
        yield result.content


def approved_review(request):
    return {'passed': True, 'input_hash': request['input_hash'], 'issues': [],
            'checks': {key: True for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}


def candidate(replies=()):
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.units = {'style': {'payload': {'style': 'Use concrete actions and economical dialogue.'}, 'audits': {}}}
    steps.payload = deepcopy(steps.units['style']['payload'])
    steps.bound = {'worldbuilding.style': steps.payload['style']}
    steps.base_context = {'premise': 'A bridge keeper traces a tidal signal.', 'locked_genre': 'mystery'}
    steps.approved = {}
    steps.generator = SimpleNamespace(llm_service=FixtureLLM(replies))
    steps._config = lambda: GenerationConfig()
    steps.session = InvocationSession(id='session', operation='bible.setup.worldbuilding',
        node_key='bible-worldbuilding', policy=InvocationPolicy.FULL_INTERACTIVE,
        context={'novel_id': 'book'})
    attempts = {}
    steps.repos = {'session': SimpleNamespace(save=lambda _: None),
                   'attempt': SimpleNamespace(save=lambda item: attempts.__setitem__(item.id, deepcopy(item)),
                                              get=lambda key: deepcopy(attempts.get(key)))}
    # Keep the fixture wholly in memory; only actual provider I/O is replaced.
    steps._save = lambda: None
    steps._install_candidate(steps.payload, purpose='fixture_generated')
    return steps


@pytest.mark.asyncio
async def test_setup_commit_rejects_candidate_without_hash_bound_audits():
    steps = candidate()
    with pytest.raises(ValueError, match='审核'):
        await steps._commit_audited()


@pytest.mark.asyncio
async def test_public_commit_cannot_bypass_the_audit_gate():
    steps = SetupInvocationSteps('book', 'characters')
    with pytest.raises(ValueError, match='审核'):
        await steps.commit()


@pytest.mark.asyncio
async def test_noop_deviation_repair_cannot_be_reported_as_fixed():
    def rejected(request):
        return {**approved_review(request), 'passed': False, 'issues': [{'description': 'Genre drift.'}]}
    steps = candidate([rejected, {'style': 'Use concrete actions and economical dialogue.'}])
    with pytest.raises(ValueError, match='未修改'):
        await steps.deviation()


@pytest.mark.asyncio
async def test_resume_rejects_manually_changed_approved_predecessor():
    steps = candidate()
    steps.novel = SimpleNamespace(premise='A bridge keeper traces a tidal signal.', locked_genre='mystery')
    steps.base_context = steps._base()
    steps.approved = {'style': 'Confirmed style.'}
    steps._ensure_services = lambda: None
    steps._canonical = lambda: {'style': 'Manual edit.'}
    assert await steps.validate_resume() is False


@pytest.mark.asyncio
async def test_commit_rechecks_novel_base_after_model_audit(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = candidate([approved_review])
    steps.novel = SimpleNamespace(premise='Original premise.', locked_genre='mystery')
    steps.base_context = steps._base()
    steps._ensure_services = lambda: None
    await steps.deviation()
    await steps.anti_ai()
    steps.novel.premise = 'An unrelated novel changed in another tab.'
    with pytest.raises(ValueError, match='基础设定'):
        await steps._commit_audited()


@pytest.mark.asyncio
async def test_audits_cannot_authorize_a_different_persisted_attempt(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = candidate([approved_review])
    await steps.deviation()
    await steps.anti_ai()
    steps.attempt.content = json.dumps({'style': 'An unrelated candidate slipped into the invocation record.'})
    with pytest.raises(ValueError, match='候选'):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_resumed_anti_ai_rechecks_deviation_for_repaired_candidate(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    revised = {'style': 'Use concrete clues and economical dialogue in the bridge mystery.'}
    # Exhaust the single automatic retry, then resume with the retained rewrite.
    steps = candidate([approved_review, revised, RuntimeError('temporary review outage'),
                       RuntimeError('temporary review outage'), approved_review])
    steps._install_candidate({'style': '人物眼中闪过一丝笑意。Keep the bridge mystery grounded in concrete clues.'}, purpose='fixture_generated')
    await steps.deviation()
    with pytest.raises(RuntimeError, match='review outage'):
        await steps.anti_ai()
    assert steps.payload == revised
    await steps.anti_ai()
    steps._assert_audited()
    assert steps._record()['audits']['deviation']['input_hash'] == steps._candidate_hash()


@pytest.mark.asyncio
@pytest.mark.parametrize('review', [{}, {'passed': True}, {'passed': 'true', 'issues': []}])
async def test_deviation_missing_or_malformed_report_fails_closed(review):
    steps = candidate([review])
    with pytest.raises(ValueError, match='审核'):
        await steps.deviation()
    assert not steps.units['style'].get('audits', {}).get('deviation', {}).get('passed')


@pytest.mark.asyncio
async def test_deviation_repairs_candidate_and_rechecks_exact_revised_payload():
    def rejected(request):
        return {**approved_review(request), 'passed': False, 'issues': [{'description': 'Keep the mystery genre.'}]}
    revised = {'style': 'Keep the bridge mystery grounded in physical clues and short dialogue.'}
    steps = candidate([rejected, revised, approved_review])
    await steps.deviation()
    assert steps.payload == revised
    assert steps.units['style']['audits']['deviation']['input_hash'] == steps._candidate_hash()
    assert len(steps.generator.llm_service.prompts) == 3


@pytest.mark.asyncio
async def test_anti_ai_rewrite_reaudits_base_and_scans_leaf_prose(monkeypatch):
    from application.engine.dag.models import NodeResult, NodeStatus
    from application.engine.dag import anti_ai_scan
    seen = []

    def scan(text, context):
        import hashlib
        seen.append(text)
        hits = [{'pattern': 'stock phrase', 'severity': 'warning'}] if 'stock phrase' in text else []
        digest = hashlib.sha256(text.encode()).hexdigest()
        return NodeResult(status=NodeStatus.WARNING if hits else NodeStatus.SUCCESS, outputs={
            'scan_status': 'completed', 'input_hash': digest, 'content_hash': digest,
                'total_hits': len(hits), 'hits': hits, 'rule_version': 'fixture-rules',
                'effective_rule_count': 1, 'severity_score': len(hits)})

    monkeypatch.setattr(anti_ai_scan, 'scan_chapter', scan)
    steps = candidate([approved_review, {'style': 'Keep the bridge mystery grounded in physical clues.'}, approved_review])
    steps.payload = {'style': 'A stock phrase undermines this otherwise concrete style instruction.'}
    steps.validate('validate')
    await steps.deviation()
    await steps.anti_ai()
    assert len(seen) == 2
    assert all('"style"' not in text for text in seen)
    assert steps.units['style']['audits']['deviation']['input_hash'] == steps._candidate_hash()
    assert steps.units['style']['audits']['anti_ai']['passed'] is True


def test_checkpoint_keeps_candidates_and_reports_without_provider_settings():
    steps = candidate()
    steps.generator.api_key = 'must-not-be-serialized'
    checkpoint = steps.export_checkpoint()
    restored = SetupInvocationSteps('book', 'worldbuilding')
    restored.restore_checkpoint(json.loads(json.dumps(checkpoint)))
    assert restored.units['style']['payload'] == steps.payload
    assert 'must-not-be-serialized' not in json.dumps(checkpoint)
