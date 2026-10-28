import json
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
from application.onboarding.setup_edit_guard import SetupEditConflict, assert_setup_editable, setup_write_scope
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def guard_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "setup-guard.sqlite"))
    db.execute("""CREATE TABLE setup_dag_runs (
        id TEXT PRIMARY KEY, novel_id TEXT, stage TEXT, status TEXT,
        snapshot_json TEXT, started_at TEXT)""")
    db.commit()
    monkeypatch.setattr(connection, "get_database", lambda *_args, **_kwargs: db)
    yield db
    db.close_all(skip_checkpoint=True)


def run(db, stage="worldbuilding", status="running", version=2, run_id="run", started="2026-10-24T01:00:00"):
    db.execute("INSERT INTO setup_dag_runs VALUES (?,?,?,?,?,?)",
               (run_id, "novel", stage, status, json.dumps({"workflow_version": version}), started))
    db.commit()


@pytest.mark.parametrize("state", ["running", "error", "cancelled", "interrupted"])
def test_unfinished_audited_run_keeps_edits_locked_after_restart(guard_db, state):
    run(guard_db, status=state)
    with pytest.raises(SetupEditConflict, match="尚未完成"):
        assert_setup_editable("novel", "worldbuilding", db=guard_db)
    assert_setup_editable("other-book", "worldbuilding", db=guard_db)


def test_legacy_and_latest_completed_runs_allow_edits(guard_db):
    run(guard_db, version=1)
    assert_setup_editable("novel", "worldbuilding", db=guard_db)
    run(guard_db, status="completed", run_id="new", started="2026-10-24T02:00:00")
    assert_setup_editable("novel", "worldbuilding", db=guard_db)


@pytest.mark.parametrize("running_stage,locked", [
    ("characters", ("worldbuilding", "characters")),
    ("locations", ("worldbuilding", "characters", "locations")),
    ("plot_outline", ("worldbuilding", "characters", "locations", "plot_outline")),
])
def test_dependency_material_cannot_change_during_unfinished_run(guard_db, running_stage, locked):
    run(guard_db, stage=running_stage)
    for stage in locked:
        with pytest.raises(SetupEditConflict):
            assert_setup_editable("novel", stage, db=guard_db)
    if running_stage == "characters":
        assert_setup_editable("novel", "locations", db=guard_db)


def test_internal_scope_needs_current_exact_lease_and_cannot_escape_stage(guard_db):
    run(guard_db)
    assert claim_execution_lease("novel", "setup", "resumed-run", 120, guard_db)
    # Resuming can use an old invocation session with a new execution run.
    with setup_write_scope("novel", "worldbuilding", "resumed-run"):
        assert_setup_editable("novel", "worldbuilding", db=guard_db)
        release_execution_lease("novel", "setup", "resumed-run", guard_db)
        assert claim_execution_lease("novel", "setup", "another-run", 120, guard_db)
        with pytest.raises(SetupEditConflict, match="租约已失效"):
            assert_setup_editable("novel", "worldbuilding", db=guard_db)
    with setup_write_scope("novel", "characters", "another-run"):
        with pytest.raises(SetupEditConflict):
            assert_setup_editable("novel", "worldbuilding", db=guard_db)
    with pytest.raises(SetupEditConflict):
        assert_setup_editable("novel", "worldbuilding", db=guard_db)


def test_worldbuilding_save_and_empty_create_return_conflict_before_mutating(guard_db):
    from interfaces.api.v1.world import worldbuilding_routes as api
    run(guard_db, status="cancelled")
    service = SimpleNamespace(update_worldbuilding=lambda **_: pytest.fail("wrote locked material"),
                              create_worldbuilding=lambda *_: pytest.fail("replaced locked material"))
    for call in (lambda: api.update_worldbuilding("novel", api.UpdateWorldbuildingRequest(), service),
                 lambda: api.create_worldbuilding("novel", service)):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "SETUP_AUDIT_INCOMPLETE"


@pytest.mark.asyncio
async def test_whole_bible_put_compares_effective_sections_before_guarding(guard_db, monkeypatch):
    from application.world.dtos.bible_dto import CharacterDTO, StyleNoteDTO
    from interfaces.api.v1.world import bible as api
    run(guard_db, stage="characters", status="interrupted")
    current = SimpleNamespace(characters=[CharacterDTO("c", "角色", "描述", [], gender="女")],
                              world_settings=[], locations=[], timeline_notes=[],
                              style_notes=[StyleNoteDTO("s", "文风", "克制")])
    writes = []
    service = SimpleNamespace(get_bible_by_novel=lambda _: current,
                              update_bible=lambda **kwargs: writes.append(kwargs) or current)
    monkeypatch.setattr(api, "_sync_bible_variable_hub", lambda *_: None)
    monkeypatch.setattr("application.engine.services.state_bootstrap.refresh_narrative_contract_in_shared_state", lambda *_: None)
    request = api.BulkUpdateBibleRequest(
        characters=[{"id": "c", "name": "角色", "description": "描述"}],
        style_notes=[{"id": "s", "category": "文风", "content": "克制"}],
        timeline_notes=[{"id": "t", "event": "事件", "time_point": "昨天", "description": "笔记"}],
    )
    await api.bulk_update_bible("novel", request, service)
    assert len(writes) == 1  # Omitted optional gender preserves its old value.
    request.characters[0].name = "改名"
    with pytest.raises(HTTPException) as error:
        await api.bulk_update_bible("novel", request, service)
    assert error.value.status_code == 409
    assert len(writes) == 1


def test_clearing_location_parent_counts_as_edit():
    from application.world.dtos.bible_dto import LocationDTO
    from interfaces.api.v1.world import bible as api
    assert api._section_changed([api.LocationData(id="child", name="子", description="", location_type="场景")],
                                [LocationDTO("child", "子", "", "场景", parent_id="parent")])


def test_plot_outline_manual_save_is_locked(guard_db):
    from interfaces.api.v1.engine import generation as api
    run(guard_db, stage="plot_outline", status="error")
    with pytest.raises(HTTPException) as error:
        api.save_plot_outline("novel", None, SimpleNamespace(get_novel=lambda _: SimpleNamespace(target_chapters=10)))
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_bible_create_cannot_replace_locked_collections(guard_db):
    from interfaces.api.v1.world import bible as api
    run(guard_db, stage="characters")
    with pytest.raises(HTTPException) as error:
        await api.create_bible("novel", api.CreateBibleRequest(bible_id="existing", novel_id="novel"),
                               SimpleNamespace(create_bible=lambda *_: pytest.fail("replaced locked collections")))
    assert error.value.status_code == 409


@pytest.mark.parametrize("name,stage", [("bible_worldbuilding_handler", "worldbuilding"),
                                       ("bible_characters_handler", "characters"),
                                       ("bible_locations_handler", "locations")])
def test_direct_continuations_cannot_bypass_the_wizard_gate(guard_db, name, stage):
    from application.world.services import bible_setup_continuation as continuation
    run(guard_db, stage=stage)
    context = SimpleNamespace(session=SimpleNamespace(context={"novel_id": "novel"}))
    with pytest.raises(SetupEditConflict):
        getattr(continuation, name)(context)


def test_generic_invocation_commit_cannot_write_outputs_past_locked_continuation(guard_db, monkeypatch):
    from application.ai_invocation.dtos import AdoptionDecision, ContinuationRef, InvocationPolicy, InvocationSession
    from application.ai_invocation.services import AdoptionCommitService
    from application.world.services.bible_setup_continuation import register_bible_setup_continuations
    run(guard_db, stage="characters")
    register_bible_setup_continuations()
    session = InvocationSession(id="session", operation="bible.setup.characters", node_key="bible-characters",
                                policy=InvocationPolicy.FULL_INTERACTIVE, context={"novel_id": "novel"},
                                continuation=ContinuationRef(handler_key="bible_characters"))
    decision = AdoptionDecision(id="decision", session_id="session", attempt_id="attempt", accepted_content="{}",
                                commit_variable_outputs=True, metadata={"source": "setup_dag", "setup_dag_run_id": "run"})
    service = AdoptionCommitService()
    monkeypatch.setattr(service, "_commit_variable_outputs", lambda **_: pytest.fail("bypassed audit into variable hub"))
    result = service.commit(session=session, decision=decision)
    assert result.status.value == "failed"
    assert session.status.value == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["engine", "background", "alias", "stream"])
async def test_legacy_bible_generation_entries_reject_locked_setup_before_starting(guard_db, entry):
    from fastapi import BackgroundTasks
    from interfaces.api.v1.core import novels
    from interfaces.api.v1.engine import generation
    from interfaces.api.v1.world import bible
    run(guard_db, stage="characters", status="interrupted")
    calls = []

    async def generate_and_save(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    generator = SimpleNamespace(generate_and_save=generate_and_save)
    service = SimpleNamespace(get_novel=lambda _: SimpleNamespace(title="Novel", premise="Premise", target_chapters=10))
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        if entry == "engine":
            await generation.generate_bible("novel", generator, service)
        elif entry == "background":
            await bible.generate_bible("novel", tasks, "all", generator, None)
        elif entry == "alias":
            await novels.generate_bible_alias("novel", tasks, "all", generator, None, service)
        else:
            await bible.generate_bible_stream("novel", "all", generator, None)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "SETUP_AUDIT_INCOMPLETE"
    assert not calls and not tasks.tasks


@pytest.mark.asyncio
async def test_direct_legacy_generator_cannot_start_while_setup_is_locked(guard_db):
    from application.world.services.auto_bible_generator import AutoBibleGenerator
    run(guard_db, stage="characters", status="interrupted")
    # No model or service is configured: the audit lock must precede either.
    with pytest.raises(SetupEditConflict):
        await AutoBibleGenerator.__new__(AutoBibleGenerator).generate_and_save("novel", "Premise", 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["engine", "background", "alias", "stream"])
@pytest.mark.parametrize("historical", ["none", "legacy", "completed"])
async def test_legacy_generation_entries_preserve_unlocked_book_behavior(guard_db, entry, historical):
    from fastapi import BackgroundTasks
    from interfaces.api.v1.core import novels
    from interfaces.api.v1.engine import generation
    from interfaces.api.v1.world import bible
    if historical != "none":
        run(guard_db, status="interrupted" if historical == "legacy" else "completed",
            version=1 if historical == "legacy" else 2)
    calls = []

    async def generate_and_save(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    generator = SimpleNamespace(generate_and_save=generate_and_save)
    service = SimpleNamespace(get_novel=lambda _: SimpleNamespace(title="Novel", premise="Premise", target_chapters=10))
    tasks = BackgroundTasks()
    if entry == "engine":
        assert (await generation.generate_bible("novel", generator, service)).success
        assert len(calls) == 1
    elif entry == "background":
        assert (await bible.generate_bible("novel", tasks, "all", generator, None))["novel_id"] == "novel"
        assert len(tasks.tasks) == 1
    elif entry == "alias":
        assert (await novels.generate_bible_alias("novel", tasks, "all", generator, None, service))["novel_id"] == "novel"
        assert len(tasks.tasks) == 1
    else:
        response = await bible.generate_bible_stream("novel", "all", generator, None)
        assert response.media_type == "text/event-stream"


def legacy_generator(monkeypatch, writes):
    from application.core.taxonomy import market_prompt
    from application.world.services.auto_bible_generator import AutoBibleGenerator
    monkeypatch.setattr(market_prompt, "load_verified_market_profile", lambda *_: None)
    generator = AutoBibleGenerator.__new__(AutoBibleGenerator)
    generator.bible_service = SimpleNamespace(get_bible_by_novel=lambda _: SimpleNamespace(),
        add_style_note=lambda **row: writes.append(row), add_character=lambda **row: writes.append(row),
        add_location=lambda **row: writes.append(row))
    generator.worldbuilding_service = None
    generator.triple_repository = None
    generator._load_worldbuilding = lambda _: {}
    generator._load_characters = lambda _: []
    return generator


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["all", "worldbuilding", "characters", "locations"])
async def test_legacy_task_crossing_model_await_rechecks_new_v2_lock_before_saving(guard_db, monkeypatch, stage):
    writes = []
    generator = legacy_generator(monkeypatch, writes)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def model(*args, **kwargs):
        entered.set()
        await resume.wait()
        return {"style": "Late unreviewed style", "characters": [{"name": "Late", "role": "hero", "description": "candidate"}],
                "locations": [{"name": "Late", "description": "candidate"}]}

    generator._generate_bible_data = generator._generate_worldbuilding_and_style = model
    generator._generate_characters = generator._generate_locations = model
    task = asyncio.create_task(generator.generate_and_save("novel", "Premise", 10, stage))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        run(guard_db, stage="plot_outline")  # A later workflow freezes every predecessor.
        resume.set()
        with pytest.raises(SetupEditConflict):
            await task
        assert writes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["worldbuilding", "characters", "locations"])
async def test_internal_legacy_writer_with_exact_live_scope_can_save_its_own_stage(guard_db, monkeypatch, stage):
    writes = []
    generator = legacy_generator(monkeypatch, writes)

    async def model(*args, **kwargs):
        return {"style": "Audited style", "characters": [{"name": "Audited", "role": "hero", "description": "candidate"}],
                "locations": [{"name": "Audited", "description": "candidate"}]}

    generator._generate_worldbuilding_and_style = generator._generate_characters = generator._generate_locations = model
    run(guard_db, stage=stage)
    assert claim_execution_lease("novel", "setup", "owner", 120, guard_db)
    with setup_write_scope("novel", stage, "owner"):
        await generator.generate_and_save("novel", "Premise", 10, stage)
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_legacy_sse_crossing_model_yield_rechecks_new_v2_lock_before_style_save(guard_db, monkeypatch):
    from interfaces.api import dependencies
    from interfaces.api.v1.world import bible
    from application.core.taxonomy import market_prompt
    writes = []
    service = SimpleNamespace(get_bible_by_novel=lambda _: SimpleNamespace(),
                              add_style_note=lambda **row: writes.append(row))
    novel = SimpleNamespace(title="Novel", premise="Premise", target_chapters=10)
    monkeypatch.setattr(dependencies, "get_novel_service", lambda: SimpleNamespace(get_novel=lambda _: novel))
    monkeypatch.setattr(market_prompt, "load_verified_market_profile", lambda *_: None)
    monkeypatch.setattr(bible, "clear_bible_generation_state", lambda *_: None)
    monkeypatch.setattr(bible, "record_bible_generation_failure", lambda *_: None)

    async def style(*args, **kwargs):
        run(guard_db, stage="worldbuilding")
        await asyncio.sleep(0)
        yield {"type": "done", "style": "Late unaudited style"}

    generator = SimpleNamespace(bible_service=service, _stream_style=style)
    messages = [message async for message in bible._sse_bible_generator("novel", "all", generator, None)]
    assert not writes
    assert any("event: error" in message and "尚未完成" in message for message in messages)
    assert not any("event: done" in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_legacy_sync_api_maps_late_audit_conflict_but_preserves_unknown_error(guard_db, provider_failure):
    from application.onboarding.setup_edit_guard import assert_setup_bible_generation_editable
    from interfaces.api.v1.engine import generation
    entered, resume = asyncio.Event(), asyncio.Event()

    async def delayed_generation(*args, **kwargs):
        entered.set()
        await resume.wait()
        if provider_failure:
            raise RuntimeError("provider disconnected")
        assert_setup_bible_generation_editable("novel")
        return {}

    generator = SimpleNamespace(generate_and_save=delayed_generation)
    service = SimpleNamespace(get_novel=lambda _: SimpleNamespace(title="Novel", target_chapters=10))
    task = asyncio.create_task(generation.generate_bible("novel", generator, service))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        run(guard_db, stage="worldbuilding")
        resume.set()
        with pytest.raises(HTTPException) as error:
            await task
        assert error.value.status_code == (500 if provider_failure else 409)
        if provider_failure:
            assert "provider disconnected" in error.value.detail
        else:
            assert error.value.detail["code"] == "SETUP_AUDIT_INCOMPLETE"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
