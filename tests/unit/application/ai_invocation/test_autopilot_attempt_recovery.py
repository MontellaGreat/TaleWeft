"""Real persisted attempts must explain interrupted managed model requests."""
import asyncio

import pytest

from application.ai_invocation.autopilot.intents import AutopilotInvocationIntent
from application.ai_invocation.autopilot.orchestrator import AutopilotInvocationOrchestrator
from application.ai_invocation.dtos import InvocationPolicy, InvocationSpec
from application.ai_invocation.prompt_assembler import CPMSPromptAssembler
from application.ai_invocation.services import AttemptService
from application.ai_invocation.spec_service import InMemoryInvocationSpecRepository, InvocationSpecService
from application.ai_invocation.variable_hub import InMemoryVariableHubRepository, VariableResolver
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
    SqliteInvocationAttemptRepository, SqliteInvocationSessionRepository,
)
from tests.unit.application.ai_invocation.test_autopilot_orchestrator_publish import (
    _Publisher, _Registry, _TemplateEngine,
)


@pytest.fixture
def persisted(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'attempts.sqlite'))
    yield db, SqliteInvocationSessionRepository(db), SqliteInvocationAttemptRepository(db)
    db.close_all(skip_checkpoint=True)


def orchestration(provider, persisted):
    _, sessions, attempts = persisted
    publisher = _Publisher()
    orchestrator = AutopilotInvocationOrchestrator(
        spec_service=InvocationSpecService(InMemoryInvocationSpecRepository([
            InvocationSpec(operation='autopilot.test', node_key='node', prompt_node_version_id='node-v1',
                default_policy=InvocationPolicy.DIRECT)])),
        variable_resolver=VariableResolver(InMemoryVariableHubRepository()),
        prompt_assembler=CPMSPromptAssembler(registry=_Registry(), template_engine=_TemplateEngine()),
        llm_service=provider, attempt_service=AttemptService(provider), publisher=publisher,
        session_repository=sessions, attempt_repository=attempts,
    )
    intent = AutopilotInvocationIntent(novel_id='novel', stage='planning', operation='autopilot.test',
        node_key='node', policy_hint=InvocationPolicy.DIRECT)
    return orchestrator, publisher, intent


@pytest.mark.asyncio
async def test_running_attempt_is_durable_before_provider_finishes(persisted):
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        async def generate(self, *_):
            started.set()
            await release.wait()
            return GenerationResult(content='generated', token_usage=TokenUsage(input_tokens=1, output_tokens=1))

    orchestrator, publisher, intent = orchestration(Provider(), persisted)
    task = asyncio.create_task(orchestrator.request(intent))
    try:
        await asyncio.wait_for(started.wait(), 2)
        session_id = publisher.events[0][1]['active_invocation_session_id']
        stored = persisted[1].get(session_id)
        assert stored.status.value == 'generating'
        assert len(stored.attempts) == 1
        assert persisted[2].get(stored.attempts[0]).status.value == 'running'
        assert publisher.events[-1][1]['active_invocation_status'] == 'generating'
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_provider_failure_or_cancellation_retains_terminal_attempt_and_session(persisted, cancelled):
    class Provider:
        async def generate(self, *_):
            raise asyncio.CancelledError() if cancelled else RuntimeError('upstream returned 502')

    orchestrator, publisher, intent = orchestration(Provider(), persisted)
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await orchestrator.request(intent)
    session_id = publisher.events[0][1]['active_invocation_session_id']
    stored = persisted[1].get(session_id)
    assert stored.status.value == ('cancelled' if cancelled else 'failed')
    assert len(stored.attempts) == 1
    attempt = persisted[2].get(stored.attempts[0])
    assert attempt.status.value == 'failed'
    assert attempt.prompt_snapshot == stored.prompt_snapshot
    if not cancelled:
        assert '502' in attempt.error
    assert publisher.events[-1][1]['active_invocation_status'] == stored.status.value
    assert publisher.events[-1][1]['autopilot_pause_reason'] == 'ai_invocation_retry_required'
