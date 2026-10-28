"""Each awaited model request has a budget, independent of a multi-call audit."""
import asyncio
from copy import deepcopy
import json

import pytest

from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.setup import SetupStepNode
from application.engine.dag.registry import NodeRegistry
from application.onboarding.setup_dag import build_setup_graph
from application.onboarding.setup_generation import SetupInvocationSteps
from tests.dag.test_setup_generation_gates import approved_review
from tests.dag.test_setup_semantic_anti_ai import setup_candidate, semantic_approval


@pytest.mark.asyncio
async def test_anti_ai_multiple_requests_do_not_share_one_request_deadline(monkeypatch):
    revised = {'style': 'Limit narration to concrete actions and dialogue that advances the goal.'}
    def issue(request):
        return {**semantic_approval(request), 'passed': False, 'issues': [{
            'path': '$.style', 'category': 'vague_lyricism', 'evidence': 'economical dialogue',
            'reason': 'The instruction needs a concrete boundary.', 'suggestion': 'Limit narration.'}]}
    steps = setup_candidate(monkeypatch, [approved_review, revised, approved_review], [issue, semantic_approval])
    await steps.deviation()
    original_generate = steps.generator.llm_service.generate
    async def slow_request(*args):
        await asyncio.sleep(.04)
        return await original_generate(*args)
    steps.generator.llm_service.generate = slow_request
    monkeypatch.setattr(SetupStepNode, 'get_timeout', lambda _: .1)
    class Services:
        def handles(self, _):
            return False
        async def run_step(self, action, state):
            steps.model_request_timeout_seconds = state.get('setup_model_request_timeout_seconds', .1)
            return await steps.anti_ai()
    node = next(n for n in build_setup_graph('locations').nodes if n.id == 'locations_anti_ai')
    result = await NodeRegistry.create_executor(node.type, node.id, node.config)({'services': Services()})
    assert result.status == NodeStatus.SUCCESS, result.error
    assert steps.payload == revised
    steps._assert_audited()


@pytest.mark.asyncio
async def test_one_hung_model_request_stops_without_spending_on_automatic_retry(monkeypatch):
    steps = setup_candidate(monkeypatch, [], [])
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0
    async def hung(prompt, config):
        nonlocal calls
        calls += 1
        started.set()
        try:
            yield '{'
            await asyncio.Future()
        finally:
            cancelled.set()
    steps.generator.llm_service.stream_generate = hung
    steps.model_request_timeout_seconds = .03
    with pytest.raises(RuntimeError, match='单次模型请求.*继续生成'):
        await asyncio.wait_for(steps.deviation(), .5)
    assert started.is_set() and cancelled.is_set() and calls == 1
    assert steps._record()['model_call_progress']['status'] == 'timed_out'
    assert steps._record()['audits'].get('deviation', {}).get('passed') is not True
    assert steps._record()['model_calls'][-1]['status'] == 'failed'


@pytest.mark.asyncio
async def test_request_deadline_does_not_swallow_stop_during_stream_cleanup():
    steps = SetupInvocationSteps('offline', 'locations')
    steps.model_request_timeout_seconds = .01
    closing = asyncio.Event()

    async def request():
        try:
            await asyncio.Future()
        finally:
            closing.set()
            await asyncio.Future()

    task = asyncio.create_task(steps._await_model_request(request(), 'deviation_review'))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_node_deadline_does_not_swallow_stop_during_operation_cleanup(monkeypatch):
    monkeypatch.setattr(SetupStepNode, 'MAX_OPERATION_SECONDS', .01)
    closing = asyncio.Event()

    class Services:
        def handles(self, _):
            return False

        async def run_step(self, action, state):
            try:
                await asyncio.Future()
            finally:
                closing.set()
                await asyncio.Future()

    node = next(n for n in build_setup_graph('locations').nodes if n.id == 'locations_anti_ai')
    execute = NodeRegistry.create_executor(node.type, node.id, node.config)
    task = asyncio.create_task(execute({'services': Services()}))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_model_result_completed_before_checkpoint_interruption_is_reused_and_revalidated(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [])
    prompt = steps._review_prompt()
    report, identifier = await steps._model_json(prompt, 'deviation_review')
    calls = deepcopy(steps._record()['model_calls'])
    result, reused = await steps._model_json(prompt, 'deviation_review')
    assert reused == identifier and result == report
    assert steps._record()['model_calls'] == calls
    assert steps._record()['model_call_progress']['reused'] is True


@pytest.mark.asyncio
async def test_cancel_in_successful_stream_checkpoint_retains_reusable_result(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [])
    saved = None
    def stop_at_checkpoint(checkpoint):
        nonlocal saved
        latest = steps.repos['attempt'].get(steps.session.attempts[-1])
        if latest and latest.accounting.get('setup_purpose') == 'deviation_review' and latest.status.value == 'succeeded':
            if saved is None:
                saved = deepcopy(checkpoint)
            raise asyncio.CancelledError()
    steps._emit_callback = stop_at_checkpoint
    with pytest.raises(asyncio.CancelledError):
        await steps._model_json(steps._review_prompt(), 'deviation_review')
    assert saved is not None
    # Reopen exactly what the interrupted checkpoint contained, not the later
    # in-memory state that could have changed after the cancelled emit.
    steps.units = deepcopy(saved['units'])
    steps._emit_callback = None
    await steps.deviation()
    assert steps._record()['model_call_progress']['reused'] is True
    assert len(steps.generator.llm_service.prompts) == 1


@pytest.mark.asyncio
async def test_pending_repair_survives_new_scan_id_without_duplicate_request(monkeypatch):
    revised = {'style': 'Show actions that reveal the cost of opening the gate.'}
    steps = setup_candidate(monkeypatch, [revised], [])
    saved = None
    def interrupt(checkpoint):
        nonlocal saved
        latest = steps.repos['attempt'].get(steps.session.attempts[-1])
        if latest and latest.accounting.get('setup_purpose') == 'anti_ai_repair' and latest.status.value == 'succeeded':
            if saved is None:
                saved = deepcopy(checkpoint)
            raise asyncio.CancelledError()
    report = {'scan_id': 'first-scan', 'passed': False, 'issues': ['Use concrete actions.']}
    steps._emit_callback = interrupt
    with pytest.raises(asyncio.CancelledError):
        await steps._repair('anti_ai', report)
    steps.units = deepcopy(saved['units'])
    steps._emit_callback = None
    await steps._repair('anti_ai', {**report, 'scan_id': 'rescan'})
    assert steps.payload == revised
    assert len(steps.generator.llm_service.prompts) == 1


@pytest.mark.asyncio
async def test_invalid_semantic_report_is_not_permanently_replayed_on_continue(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review], [{}, semantic_approval])
    await steps.deviation()
    with pytest.raises(ValueError):
        await steps.anti_ai()
    await steps.anti_ai()
    assert len(steps.generator.llm_service.semantic_prompts) == 2
    steps._assert_audited()


@pytest.mark.asyncio
async def test_changed_prompt_cannot_reuse_previous_model_result(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review, approved_review], [])
    _, first = await steps._model_json(steps._review_prompt(), 'deviation_review')
    steps.base_context['premise'] = 'Changed story premise.'
    _, second = await steps._model_json(steps._review_prompt(), 'deviation_review')
    assert second != first


@pytest.mark.asyncio
async def test_new_candidate_with_identical_content_requires_fresh_model_review(monkeypatch):
    steps = setup_candidate(monkeypatch, [approved_review, approved_review], [])
    prompt = steps._review_prompt()
    _, first = await steps._model_json(prompt, 'deviation_review')
    previous_candidate = steps.attempt.id
    steps._install_candidate(deepcopy(steps.payload), purpose='fixture_candidate_round_trip')
    assert steps.attempt.id != previous_candidate
    _, second = await steps._model_json(steps._review_prompt(), 'deviation_review')
    assert second != first
    assert len(steps.generator.llm_service.prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['session', 'unit', 'content', 'purpose', 'candidate',
                                   'candidate_attempt', 'ledger_candidate_attempt',
                                   'missing_candidate_attempt'])
async def test_completed_model_result_reuse_requires_durable_matching_source(monkeypatch, tamper):
    steps = setup_candidate(monkeypatch, [approved_review, approved_review], [])
    prompt = steps._review_prompt()
    _, first = await steps._model_json(prompt, 'deviation_review')
    prior = steps.repos['attempt'].get(first)
    if tamper == 'session':
        prior.session_id = 'different-session'
    elif tamper == 'unit':
        prior.accounting['setup_unit'] = 'locations'
    elif tamper == 'content':
        prior.content = '{}'
    elif tamper == 'purpose':
        prior.accounting['setup_purpose'] = 'anti_ai_repair'
    elif tamper == 'candidate':
        prior.accounting['setup_input_hash'] = 'different-candidate'
    elif tamper == 'candidate_attempt':
        prior.accounting['setup_candidate_attempt_id'] = 'different-candidate-attempt'
    elif tamper == 'ledger_candidate_attempt':
        steps._record()['model_calls'][-1]['candidate_attempt_id'] = 'different-candidate-attempt'
    else:
        prior.accounting.pop('setup_candidate_attempt_id', None)
        steps._record()['model_calls'][-1].pop('candidate_attempt_id', None)
    steps.repos['attempt'].save(prior)
    _, second = await steps._model_json(prompt, 'deviation_review')
    assert second != first


@pytest.mark.asyncio
async def test_noop_repair_can_request_a_new_response_on_continue(monkeypatch):
    steps = setup_candidate(monkeypatch, [deepcopy({'style': 'Use concrete actions and economical dialogue.'}),
        {'style': 'Show actions that reveal the cost of opening the gate.'}], [])
    report = {'passed': False, 'issues': [{'description': 'Add a concrete constraint.'}]}
    with pytest.raises(ValueError, match='未修改'):
        await steps._repair('anti_ai', report)
    await steps._repair('anti_ai', report)
    assert steps.payload['style'].startswith('Show actions')


@pytest.mark.asyncio
async def test_stream_progress_is_checkpointed_before_request_finishes(monkeypatch):
    steps = setup_candidate(monkeypatch, [], [])
    snapshots = []
    steps._emit_callback = lambda checkpoint: snapshots.append(checkpoint)
    entered = asyncio.Event()
    async def partial(prompt, config):
        yield '{"passed":'
        entered.set()
        await asyncio.Future()
    steps.generator.llm_service.stream_generate = partial
    task = asyncio.create_task(steps.deviation())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert any(checkpoint['units']['style']['model_call_progress'].get('received_characters') == 10
                   for checkpoint in snapshots)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_multicall_step_still_has_finite_total_deadline(monkeypatch):
    monkeypatch.setattr(SetupStepNode, 'get_timeout', lambda _: .02)
    monkeypatch.setattr(SetupStepNode, 'MAX_OPERATION_SECONDS', .05, raising=False)
    stopped = asyncio.Event()
    class Services:
        def handles(self, _):
            return False
        async def run_step(self, action, state):
            try:
                await asyncio.Future()
            finally:
                stopped.set()
    node = next(n for n in build_setup_graph('locations').nodes if n.id == 'locations_anti_ai')
    result = await NodeRegistry.create_executor(node.type, node.id, node.config)({'services': Services()})
    assert result.status == NodeStatus.ERROR
    assert '总时限' in result.error
    assert stopped.is_set()
