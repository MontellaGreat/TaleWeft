import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, InvocationSessionStatus, VariableBinding, VariablePlan
from application.ai_invocation.variable_hub import VariableValue, VariableWrite
from application.onboarding.setup_edit_guard import SetupEditConflict, assert_setup_variable_writes
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def variable_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "variable-guard.sqlite"))
    db.execute("CREATE TABLE setup_dag_runs (id TEXT,novel_id TEXT,stage TEXT,status TEXT,snapshot_json TEXT,started_at TEXT)")
    db.execute("INSERT INTO setup_dag_runs VALUES ('run','book','characters','interrupted',?, '2026-10-24')",
               (json.dumps({'workflow_version': 2}),))
    db.commit()
    monkeypatch.setattr(connection, "get_database", lambda *_args, **_kwargs: db)
    yield db
    db.close_all(skip_checkpoint=True)


class Variables:
    def __init__(self, db, bindings=(), current=None):
        self._db = db
        self.bindings = list(bindings)
        self.writes = []
        self.current = current or {}

    def get_value(self, key, context):
        value = self.current.get((key, context))
        return VariableValue(key, value, context) if (key, context) in self.current else None

    def set_value(self, write):
        self.writes.append(write)
        return VariableValue(write.key, write.value, write.context_key)

    def get_bindings(self, *_):
        return self.bindings

    get_output_bindings = get_bindings


@pytest.mark.parametrize("key", ["novel.premise", "novel.setup.genre_label", "worldbuilding.content", "characters.list",
                                 "novel.worldbuilding", "novel.worldbuilding.core_rules", "novel.style.guide",
                                 "novel.characters.list", "novel.characters.protagonist"])
def test_setup_hub_inputs_and_aliases_are_locked_even_after_interrupt(variable_db, key):
    with pytest.raises(SetupEditConflict):
        assert_setup_variable_writes([VariableWrite(key, 'changed', 'novel_id:book')], db=variable_db)
    assert_setup_variable_writes([VariableWrite(key, 'changed', 'novel_id:legacy-book')], db=variable_db)


@pytest.mark.parametrize("key", ["novel.locations.list", "novel.plot.outline", "novel.plot.stage_plan",
                                 "novel.plot.main_story_overview", "novel.plot.expected_ending", "novel.plot.core_conflict"])
def test_outline_run_locks_compatible_location_and_outline_aliases(variable_db, key):
    variable_db.execute("UPDATE setup_dag_runs SET stage='plot_outline'")
    variable_db.commit()
    with pytest.raises(SetupEditConflict):
        assert_setup_variable_writes([VariableWrite(key, 'changed', 'novel_id:book')], db=variable_db)


def test_global_fallback_cannot_change_inputs_of_a_locked_book(variable_db):
    with pytest.raises(SetupEditConflict):
        assert_setup_variable_writes([VariableWrite('worldbuilding.content', {'new': 'value'})], db=variable_db)


def test_unrelated_prompt_values_and_equal_values_remain_usable(variable_db):
    repo = Variables(variable_db, current={('novel.premise', 'novel_id:book'): 'same premise'})
    assert_setup_variable_writes([VariableWrite('prompt.temperature', 0.5, 'novel_id:book'),
                                  VariableWrite('novel.premise', 'same premise', 'novel_id:book')], repository=repo)


def test_internal_prepare_can_materialize_frozen_inputs_only_with_live_lease(variable_db):
    from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
    from application.onboarding.setup_edit_guard import setup_write_scope
    writes = [VariableWrite('novel.setup.genre_label', '', 'novel_id:book'),
              VariableWrite('worldbuilding.content', {'frozen': 'input'}, 'novel_id:book')]
    assert claim_execution_lease('book', 'setup', 'resumed-run', 120, variable_db)
    with setup_write_scope('book', 'characters', 'resumed-run'):
        assert_setup_variable_writes(writes, db=variable_db)
        release_execution_lease('book', 'setup', 'resumed-run', variable_db)
        with pytest.raises(SetupEditConflict):
            assert_setup_variable_writes(writes, db=variable_db)


def bindings():
    return [VariableBinding(alias='setting', variable_key='prompt.setting'),
            VariableBinding(alias='cast', variable_key='characters.list', value_type='list')]


@pytest.mark.asyncio
async def test_invocation_variable_batch_rejects_before_any_value_is_written(variable_db, monkeypatch):
    from interfaces.api.v1.engine import ai_invocation_routes as api
    repo = Variables(variable_db, bindings())
    session = InvocationSession(id='s', operation='custom', node_key='custom', policy=InvocationPolicy.FULL_INTERACTIVE,
                                status=InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW, context={'novel_id': 'book'})
    spec = SimpleNamespace(input_binding_set_id='input', node_key='custom')
    monkeypatch.setattr(api, '_repositories', lambda: {'session': SimpleNamespace(get=lambda _: session),
                                                      'spec': SimpleNamespace(get=lambda *_: spec), 'variable_hub': repo})
    monkeypatch.setattr(api, '_session_input_bindings', lambda *_: bindings())
    with pytest.raises(HTTPException) as error:
        await api.update_invocation_variables('s', api.VariableUpdateRequest(values={'setting': 'new', 'cast': []}))
    assert error.value.status_code == 409
    assert repo.writes == []


def test_create_invocation_materialization_preflights_the_whole_batch(variable_db):
    from interfaces.api.v1.engine import ai_invocation_routes as api
    repo = Variables(variable_db, bindings())
    spec = SimpleNamespace(input_binding_set_id='input', node_key='custom')
    with pytest.raises(HTTPException) as error:
        api._materialize_request_variables_before_invoke(
            {'variable_hub': repo, 'spec': SimpleNamespace(get=lambda *_: spec)},
            api.InvocationCreateRequest(operation='setup.custom', node_key='custom', context={'novel_id': 'book'},
                                        variables={'setting': 'new', 'cast': []}))
    assert error.value.status_code == 409
    assert repo.writes == []


def test_resolved_input_materialization_preflights_the_whole_batch(variable_db):
    from application.ai_invocation.input_materialization import materialize_input_variables
    repo = Variables(variable_db, bindings())
    session = InvocationSession(id='s', operation='custom', node_key='custom', policy=InvocationPolicy.FULL_INTERACTIVE,
                                context={'novel_id': 'book'})
    with pytest.raises(SetupEditConflict):
        materialize_input_variables(variable_hub_repository=repo, session=session,
            spec=SimpleNamespace(input_binding_set_id='input', node_key='custom'),
            variable_plan=VariablePlan(aliases={'setting': 'new', 'cast': []}))
    assert repo.writes == []


def test_generic_output_binding_cannot_bypass_audits_or_partially_write(variable_db):
    from application.ai_invocation.services import AdoptionCommitService
    repo = Variables(variable_db, bindings())
    service = AdoptionCommitService(variable_hub_repository=repo)
    session = SimpleNamespace(operation='custom', prompt_snapshot=SimpleNamespace(output_binding_set_id='output', node_key='custom'),
                               context={'novel_id': 'book'}, id='s', metadata={}, node_key='custom')
    decision = SimpleNamespace(accepted_content=json.dumps({'setting': 'new', 'cast': []}), attempt_id='a')
    with pytest.raises(SetupEditConflict):
        service._commit_variable_outputs(session=session, decision=decision, commit_id='c')
    assert repo.writes == []


def test_missing_required_output_prevents_partial_write(variable_db):
    from application.ai_invocation.services import AdoptionCommitService
    repo = Variables(variable_db, [VariableBinding(alias='setting', variable_key='prompt.setting'),
                                  VariableBinding(alias='required', variable_key='prompt.required', required=True)])
    session = SimpleNamespace(operation='custom', prompt_snapshot=SimpleNamespace(output_binding_set_id='output', node_key='custom'),
                               context={'novel_id': 'legacy-book'}, id='s', metadata={}, node_key='custom')
    result = AdoptionCommitService(variable_hub_repository=repo)._commit_variable_outputs(session=session,
        decision=SimpleNamespace(accepted_content='{"setting":"new"}', attempt_id='a'), commit_id='c')
    assert result['blocked'] is True
    assert repo.writes == []


@pytest.mark.asyncio
async def test_public_historical_backfill_is_blocked_before_any_book_is_changed(variable_db):
    from interfaces.api.v1.workbench import llm_control as api
    with pytest.raises(HTTPException) as error:
        await api.backfill_variable_hub(api.VariableHubBackfillRequest())
    assert error.value.status_code == 409
