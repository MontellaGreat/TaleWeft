"""Recover only the proven voice-field loss of a successful audited save."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import (
    AdoptionCommitStatus, ContinuationRef, InvocationPolicy, InvocationSession,
    InvocationSessionStatus, PromptSnapshot,
)
from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
from application.onboarding.setup_generation import SetupInvocationSteps
from application.audit.services import anti_ai_audit
from application.world.services.bible_setup_output_bindings import bible_setup_output_bindings
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
from tests.dag.test_setup_dag_adversarial import adapter_services, setup_db  # noqa: F401
from tests.dag.test_setup_generation_gates import FixtureLLM, approved_review


async def old_voice_loss(adapter_services, setup_db, monkeypatch):
    bible, world, novel, invocation = adapter_services
    bible._unified_char_repo = SqliteUnifiedCharacterRepository(setup_db)
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(llm_service=FixtureLLM([approved_review]),
        bible_service=bible, worldbuilding_service=world)
    steps.novel, steps.unit, steps.run_id = novel, 'characters', 'original-voice-run'
    steps.repos, steps.base_context = invocation._repositories(), steps._base()
    binding_id = 'voice-resume-output'
    steps.repos['variable_hub'].set_bindings(binding_id, 'bible-characters',
        bible_setup_output_bindings('bible-characters'), direction='output')
    snapshot = PromptSnapshot(prompt=Prompt(system='Return characters JSON.', user='Generate a bridge keeper.'),
        node_key='bible-characters', node_version_id='fixture-v1', output_binding_set_id=binding_id,
        asset_link_set_id='', input_binding_set_id='', variable_snapshot_hash='fixture',
        template_hash='fixture', composition_hash='fixture', rendered_prompt_hash='fixture')
    steps.session = InvocationSession(id='voice-resume-session', operation='bible.setup.characters',
        node_key='bible-characters', policy=InvocationPolicy.FULL_INTERACTIVE,
        context={'novel_id': 'novel'}, continuation=ContinuationRef(handler_key='bible_characters'),
        prompt_snapshot=snapshot)
    steps._install_candidate({'characters': [{'id': 'hero', 'name': 'Lin',
        'description': 'The bridge keeper counts fuel before opening the gate.',
        'voice_profile': {'style': 'Economical dialogue.', 'sentence_pattern': 'Short clauses.',
                          'speech_tempo': 'Measured pace.'}}]}, purpose='fixture_generated')
    steps.validate('validate')
    await steps.deviation()
    await steps.anti_ai()
    assert claim_execution_lease('novel', 'setup', steps.run_id, 120, setup_db)
    async def interrupted_readback():
        return {}  # Reproduce a process ending after durable commit, before verification.
    steps._verify_unit = interrupted_readback
    try:
        await steps.commit()
    finally:
        del steps._verify_unit
        release_execution_lease('novel', 'setup', steps.run_id, setup_db)
    setup_db.execute("UPDATE unified_characters SET voice_style='',sentence_pattern='',speech_tempo='' WHERE id='hero'")
    setup_db.commit()
    checkpoint = steps.export_checkpoint()
    restored = SetupInvocationSteps('novel', 'characters')
    restored.generator, restored.repos, restored.novel = steps.generator, invocation._repositories(), novel
    restored.restore_checkpoint(checkpoint)
    restored.run_id = 'resume-voice-run'
    return restored


@pytest.mark.asyncio
async def test_old_successful_commit_recovers_voice_and_repeated_readback_without_model_calls(adapter_services, setup_db, monkeypatch):
    steps = await old_voice_loss(adapter_services, setup_db, monkeypatch)
    calls = deepcopy(steps._record()['model_calls'])
    assert await steps.validate_resume() is True
    assert claim_execution_lease('novel', 'setup', steps.run_id, 120, setup_db)
    try:
        for _ in range(2):
            assert (await steps._verify_unit())['saved_data_verified'] is True
    finally:
        release_execution_lease('novel', 'setup', steps.run_id, setup_db)
    character = steps.generator.bible_service.get_bible_by_novel('novel').characters[0]
    assert character.voice_profile == steps.payload['characters'][0]['voice_profile']
    assert steps._record()['verified'] is True
    assert steps._record()['persistence_repair']['kind'] == 'legacy_character_voice_loss'
    assert steps.repos['session'].get(steps.session.id).status == InvocationSessionStatus.COMPLETED
    assert steps._record()['model_calls'] == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['author_edit', 'partial_voice', 'extra_character', 'base',
    'predecessor', 'no_lease', 'wrong_lease', 'decision_payload', 'commit_failed', 'commit_session',
    'commit_result', 'decision_session', 'decision_attempt', 'decision_audits', 'review_attempt'])
async def test_voice_recovery_cannot_overwrite_edits_or_accept_unproven_commit(adapter_services, setup_db, monkeypatch, tamper):
    steps = await old_voice_loss(adapter_services, setup_db, monkeypatch)
    repo = steps.repos['adoption']
    record = steps._record()
    if tamper in {'commit_session', 'decision_session', 'decision_attempt'}:
        other_session = deepcopy(steps.session)
        other_session.id = 'unrelated'
        steps.repos['session'].save(other_session)
        other_attempt = deepcopy(steps.attempt)
        other_attempt.id = other_attempt.session_id = 'unrelated'
        steps.repos['attempt'].save(other_attempt)
    if tamper == 'author_edit':
        setup_db.execute("UPDATE unified_characters SET description='Author changed the motive.' WHERE id='hero'")
    elif tamper == 'partial_voice':
        setup_db.execute("UPDATE unified_characters SET voice_style='Author voice.' WHERE id='hero'")
    elif tamper == 'extra_character':
        setup_db.execute("INSERT INTO unified_characters(id,novel_id,name) VALUES ('extra','novel','Other')")
    elif tamper == 'base':
        steps.novel.premise = 'New premise.'
    elif tamper == 'predecessor':
        steps.approved['style'] = 'An absent predecessor.'
    elif tamper == 'review_attempt':
        attempt = steps.repos['attempt'].get(record['audits']['deviation']['attempt_id'])
        attempt.content = '{}'
        steps.repos['attempt'].save(attempt)
    elif tamper.startswith('decision'):
        decision = repo.get_decision(record['decision_id'])
        if tamper == 'decision_payload':
            decision.accepted_content = '{}'
        elif tamper == 'decision_audits':
            decision.metadata['audits'] = {}
        else:
            # Identity columns are immutable on repository upsert.
            column = 'session_id' if tamper == 'decision_session' else 'attempt_id'
            setup_db.execute(f'UPDATE ai_adoption_decisions SET {column}=? WHERE id=?', ('unrelated', decision.id))
        if tamper in {'decision_payload', 'decision_audits'}:
            repo.save_decision(decision)
    elif tamper.startswith('commit'):
        commit = repo.get_commit_for_decision(record['decision_id'])
        if tamper == 'commit_failed':
            commit.status = AdoptionCommitStatus.FAILED
        elif tamper == 'commit_session':
            setup_db.execute('UPDATE ai_adoption_commits SET session_id=? WHERE id=?', ('unrelated', commit.id))
        else:
            commit.result['continuation']['characters'][0]['voice_profile'] = {'style': 'Different accepted voice.'}
        if tamper != 'commit_session':
            repo.save_commit(commit)
    before = asdict(steps.generator.bible_service.get_bible_by_novel('novel'))
    owner = 'unrelated-owner' if tamper == 'wrong_lease' else steps.run_id
    if tamper != 'no_lease':
        assert claim_execution_lease('novel', 'setup', owner, 120, setup_db)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await steps._verify_unit()
    finally:
        release_execution_lease('novel', 'setup', owner, setup_db)
    assert asdict(steps.generator.bible_service.get_bible_by_novel('novel')) == before
    assert not steps._record().get('verified')


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['character', 'relationship', 'base', 'lease', 'before_snapshot_base'])
async def test_voice_repair_writer_compare_rejects_interleaved_changes(adapter_services, setup_db, monkeypatch, race):
    steps = await old_voice_loss(adapter_services, setup_db, monkeypatch)
    repository = steps.generator.bible_service.bible_repository
    original = setup_db.transaction
    changed = False
    def change():
        nonlocal changed
        if changed:
            return
        changed = True
        if race == 'character':
            setup_db.execute("UPDATE unified_characters SET description='Concurrent author revision.' WHERE id='hero'")
        elif race == 'relationship':
            setup_db.execute("INSERT INTO unified_character_relationships(id,character_id,target_name,relation,description) VALUES ('tie','hero','Other','Ally','New tie.')")
        elif race == 'lease':
            release_execution_lease('novel', 'setup', steps.run_id, setup_db)
        else:
            setup_db.execute("UPDATE novels SET premise='Concurrent premise.' WHERE id='novel'")
            steps.novel.premise = 'Concurrent premise.'
        setup_db.commit()
    @contextmanager
    def race_at_writer():
        change()
        with original() as conn:
            yield conn
    if race == 'before_snapshot_base':
        prior = repository.restore_missing_character_voices
        def raced(*args, **kwargs):
            change()
            return prior(*args, **kwargs)
        monkeypatch.setattr(repository, 'restore_missing_character_voices', raced)
    else:
        monkeypatch.setattr(setup_db, 'transaction', race_at_writer)
    assert claim_execution_lease('novel', 'setup', steps.run_id, 120, setup_db)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await steps._verify_unit()
    finally:
        release_execution_lease('novel', 'setup', steps.run_id, setup_db)
    assert changed
    assert steps.generator.bible_service.get_bible_by_novel('novel').characters[0].voice_profile == {}
    assert not steps._record().get('verified')
