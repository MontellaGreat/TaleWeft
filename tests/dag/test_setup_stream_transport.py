"""Gateway timeouts must not discard setup candidates or bypass full audits."""
import asyncio
import copy
import json

import httpx
import openai
import pytest

from application.ai.llm_retry_policy import is_retryable_llm_error
from application.ai_invocation.dtos import InvocationAttemptStatus
from tests.dag.test_setup_generation_gates import candidate, approved_review


def gateway_error(status=524, **body):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://offline.invalid/v1/responses'))
    return openai.APIStatusError('Gateway timeout', response=response, body={'retry_after': 120, **body})


class StreamOnlyLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def generate(self, *_):
        raise AssertionError('Setup audit must not wait for a non-stream response')

    async def stream_generate(self, prompt, config):
        self.calls.append((prompt, config))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        if reply == 'partial':
            yield '{"passed":true'
            raise gateway_error()
        if reply == 'invalid':
            yield '{}'
            return
        text = json.dumps(reply(json.loads(prompt.user)))
        yield text[:10]
        yield text[10:]


def streamed_candidate(replies):
    steps = candidate()
    steps.generator.llm_service = StreamOnlyLLM(replies)
    return steps


@pytest.mark.asyncio
async def test_review_aggregates_stream_before_full_validation_and_retains_candidate():
    steps = streamed_candidate([approved_review])
    original = steps.attempt.id
    await steps.deviation()
    assert steps._record()['audits']['deviation']['passed'] is True
    assert steps.attempt.id == original
    call = steps._record()['model_calls'][0]
    assert steps.repos['attempt'].get(call['attempt_id']).status == InvocationAttemptStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_524_waits_for_server_and_retries_only_failed_call(monkeypatch):
    steps = streamed_candidate([gateway_error(), approved_review])
    original = copy.deepcopy(steps.payload)
    waits = []
    snapshots = []

    async def wait(delay):
        waits.append(delay)

    monkeypatch.setattr('application.onboarding.setup_generation.asyncio.sleep', wait)
    steps._emit_callback = lambda data: snapshots.append(data)
    await steps.deviation()
    assert waits == [120]
    assert steps.payload == original
    assert len(steps.generator.llm_service.calls) == 2
    calls = steps._record()['model_calls']
    assert [row['status'] for row in calls] == ['failed', 'succeeded']
    assert steps.repos['attempt'].get(calls[0]['attempt_id']).status == InvocationAttemptStatus.FAILED
    assert any(s['units']['style']['model_call_progress']['status'] == 'retry_wait' for s in snapshots)
    assert steps._record()['model_call_progress']['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['partial', gateway_error(401), gateway_error(retryable=False), gateway_error(retry_after=300)])
async def test_partial_permanent_and_long_cooldown_errors_do_not_repeat(failure):
    steps = streamed_candidate([failure, approved_review])
    original = steps.attempt.id
    with pytest.raises(Exception):
        await steps.deviation()
    assert len(steps.generator.llm_service.calls) == 1
    assert steps.attempt.id == original
    assert steps._record()['audits'].get('deviation', {}).get('passed') is not True


@pytest.mark.asyncio
async def test_cancel_during_backoff_preserves_failed_attempt_without_new_request(monkeypatch):
    steps = streamed_candidate([gateway_error(), approved_review])

    async def cancel(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr('application.onboarding.setup_generation.asyncio.sleep', cancel)
    with pytest.raises(asyncio.CancelledError):
        await steps.deviation()
    assert len(steps.generator.llm_service.calls) == 1
    assert steps._record()['model_call_progress']['status'] == 'cancelled'
    assert len(steps._record()['model_calls']) == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_and_report_actionable_524(monkeypatch):
    steps = streamed_candidate([gateway_error(), gateway_error(), approved_review])
    async def wait(_):
        pass
    monkeypatch.setattr('application.onboarding.setup_generation.asyncio.sleep', wait)
    with pytest.raises(RuntimeError, match='524.*120.*继续生成'):
        await steps.deviation()
    assert len(steps.generator.llm_service.calls) == 2
    assert len(steps._record()['model_calls']) == 2
    assert steps._record()['model_call_progress']['status'] == 'failed'


@pytest.mark.asyncio
async def test_budget_enforced_failure_never_launches_another_paid_request():
    from application.ai.usage_context import current_usage
    steps = streamed_candidate([])

    async def budgeted_stream(prompt, config):
        steps.generator.llm_service.calls.append((prompt, config))
        current_usage().metadata['budget_enforced'] = True
        raise gateway_error()
        yield ''

    steps.generator.llm_service.stream_generate = budgeted_stream
    with pytest.raises(RuntimeError, match='524'):
        await steps.deviation()
    assert len(steps.generator.llm_service.calls) == 1
    assert len(steps._record()['model_calls']) == 1


def test_structured_http_status_beats_timeout_text_for_auth_and_explicit_no_retry():
    assert is_retryable_llm_error(gateway_error()) is True
    assert is_retryable_llm_error(gateway_error(401)) is False
    assert is_retryable_llm_error(gateway_error(retryable=False)) is False
