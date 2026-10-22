"""Adversarial checks of durable generation gates, with zero provider traffic."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.core.services.chapter_derivation_readiness import (
    ChapterDerivationsNotReady, assert_previous_derivations_ready,
)
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def book_db(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'adversarial-readiness.sqlite'))
    db.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES('book','验收','作者','测试','adversarial-ready')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,revision) VALUES('one','book',1,'第一章','新事实',2)")
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


def snapshot(db, identity, status, timestamp, branch='main'):
    db.execute("INSERT INTO chapter_evolution_snapshots(snapshot_id,novel_id,branch_id,chapter_number,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (identity, 'book', branch, 1, status, timestamp, timestamp))
    db.commit()


def test_successful_replay_keeps_stale_history_without_permanent_generation_block(book_db):
    snapshot(book_db, 'old', 'stale', '2026-10-16T01:00:00+00:00')
    snapshot(book_db, 'new', 'active', '2026-10-16T02:00:00+00:00')
    assert_previous_derivations_ready(book_db, 'book', 2)


def test_suffix_with_only_invalidated_snapshot_remains_blocked(book_db):
    snapshot(book_db, 'old', 'stale', '2026-10-16T01:00:00+00:00')
    with pytest.raises(ChapterDerivationsNotReady):
        assert_previous_derivations_ready(book_db, 'book', 2)


def test_old_active_snapshot_does_not_hide_latest_invalidated_snapshot(book_db):
    snapshot(book_db, 'old-active', 'active', '2026-10-16T01:00:00+00:00')
    snapshot(book_db, 'new-stale', 'stale', '2026-10-16T02:00:00+00:00')
    with pytest.raises(ChapterDerivationsNotReady):
        assert_previous_derivations_ready(book_db, 'book', 2)


def test_other_worldline_staleness_does_not_block_main(book_db):
    snapshot(book_db, 'main-active', 'active', '2026-10-16T01:00:00+00:00')
    snapshot(book_db, 'alternate-stale', 'stale', '2026-10-16T02:00:00+00:00', 'alternate')
    assert_previous_derivations_ready(book_db, 'book', 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('policy_name', ['DIRECT', 'FULL_INTERACTIVE'])
async def test_invocation_rechecks_current_derivations_even_for_previously_frozen_prompt(book_db, monkeypatch, streaming, policy_name):
    from application.ai_invocation.dtos import InvocationPolicy, InvocationSession
    from application.ai_invocation.services import AttemptService
    from application.engine.rules.prose_prompt_hooks import ProsePromptHooks
    from domain.ai.value_objects.prompt import Prompt

    hooks = ProsePromptHooks(book_db)
    context = {'novel_id': 'book', 'chapter_number': 2}
    # The author reviewed a prompt while the preceding chapter was ready.
    prompt, evidence = hooks.compose(Prompt(system='具体清楚', user='继续写下一章'), context)
    session = InvocationSession(id='reviewed', operation='chapter.generate', node_key='chapter',
        policy=getattr(InvocationPolicy, policy_name), context=context,
        metadata={'anti_ai_prompt': evidence})
    frozen = SimpleNamespace(prompt=prompt)
    # They then edited chapter 1 before accepting/resuming the old session.
    book_db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json,status) VALUES('pending-one','one','book',1,2,?,'pending')",
        (json.dumps({'content': '新事实'}),))
    book_db.commit()
    provider = SimpleNamespace(generate=AsyncMock(side_effect=AssertionError('Provider must not be reached')),
        stream_generate=Mock(side_effect=AssertionError('Streaming provider must not be reached')))
    monkeypatch.setattr(AttemptService, 'prose_prompt_hook_factory', staticmethod(lambda: hooks))
    service = AttemptService(provider)
    with pytest.raises(ChapterDerivationsNotReady):
        if streaming:
            await service.generate_streaming(session=session, prompt_snapshot=frozen)
        else:
            await service.generate(session=session, prompt_snapshot=frozen)
    provider.generate.assert_not_called()
    provider.stream_generate.assert_not_called()
