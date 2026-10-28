"""Binding publication uses the actual queued SQLite writer, with no provider."""
from pathlib import Path

import pytest

from application.ai_invocation.dtos import (
    AdoptionDecision, InvocationPolicy, InvocationSession,
    InvocationSessionStatus, PromptSnapshot, VariableBinding,
)
from application.ai_invocation.services import AdoptionCommitService
from application.ai_invocation.variable_hub import InMemoryVariableHubRepository
from application.engine.services import persistence_queue as queue_module
from application.engine.services.persistence_queue import PersistenceQueue, register_persistence_handlers
from application.world.services.bible_setup_output_bindings import BIBLE_SETUP_WORLD_NODE, OUTPUT_BINDING_SET_BY_NODE
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository


@pytest.fixture
def queued_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    db = DatabaseConnection(str(tmp_path / "bindings.sqlite"))
    monkeypatch.setattr(connection, "_db_instance", db)
    monkeypatch.setattr(connection, "_db_instances_by_path", {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", queue)
    register_persistence_handlers()
    queue.start_consumer()
    yield db
    queue.stop_consumer()
    db.close_all(skip_checkpoint=True)
    queue.get_queue().close()
    queue.get_queue().join_thread()


def session_and_decision():
    session = InvocationSession(
        id="session", operation="bible.setup.worldbuilding", node_key=BIBLE_SETUP_WORLD_NODE,
        policy=InvocationPolicy.DIRECT, status=InvocationSessionStatus.AWAITING_COMMIT,
        context={"novel_id": "book"},
        prompt_snapshot=PromptSnapshot(
            prompt=Prompt(system="test", user="test"), node_key=BIBLE_SETUP_WORLD_NODE,
            node_version_id="test", asset_link_set_id="", input_binding_set_id="",
            output_binding_set_id=OUTPUT_BINDING_SET_BY_NODE[BIBLE_SETUP_WORLD_NODE],
            variable_snapshot_hash="", template_hash="", composition_hash="", rendered_prompt_hash="",
        ),
    )
    decision = AdoptionDecision(
        id="decision", session_id="session", attempt_id="attempt", accepted_by="system",
        accepted_content='{"style":"克制","worldbuilding":{"core_rules":{"rule":"守约"}}}',
    )
    return session, decision


def test_queued_bindings_preserve_versions_active_set_and_replace_aliases(queued_db):
    repo = SqliteVariableHubRepository(queued_db)
    first = VariableBinding(alias="first", variable_key="test.first")
    second = VariableBinding(alias="second", variable_key="test.second")
    repo.set_bindings("first-set", "test-node", [first], direction="output")
    repo.set_bindings("second-set", "test-node", [second], direction="output")
    repo.set_bindings("first-set", "test-node", [second], direction="output")
    assert [binding.alias for binding in repo.get_output_bindings("first-set", "test-node")] == ["second"]
    assert queued_db.fetch_all(
        "SELECT id,version_number,is_active FROM cpms_variable_binding_sets WHERE node_key='test-node' ORDER BY version_number"
    ) == [
        {"id": "first-set", "version_number": 1, "is_active": 1},
        {"id": "second-set", "version_number": 2, "is_active": 0},
    ]


def test_bible_adoption_publishes_and_writes_outputs_through_real_writer(queued_db):
    repo = SqliteVariableHubRepository(queued_db)
    session, decision = session_and_decision()
    commit = AdoptionCommitService(variable_hub_repository=repo).commit(session=session, decision=decision)
    assert commit.status.value == "succeeded"
    step = next(step for step in commit.steps if step.name == "commit_variable_outputs")
    assert step.result["skipped"] is False
    assert repo.get_output_bindings(OUTPUT_BINDING_SET_BY_NODE[BIBLE_SETUP_WORLD_NODE], BIBLE_SETUP_WORLD_NODE)
    assert repo.get_value("worldbuilding.style", "novel_id:book").value == "克制"


def test_bible_binding_publication_failure_blocks_adoption():
    class BrokenBindings(InMemoryVariableHubRepository):
        def set_bindings(self, *args, **kwargs):
            raise RuntimeError("injected binding commit failure")

    session, decision = session_and_decision()
    commit = AdoptionCommitService(variable_hub_repository=BrokenBindings()).commit(session=session, decision=decision)
    assert commit.status.value == "blocked"
    assert session.status == InvocationSessionStatus.BLOCKED
    step = next(step for step in commit.steps if step.name == "commit_variable_outputs")
    assert step.result["reason"] == "output_bindings_unavailable"
    assert "injected binding commit failure" in step.result["error"]


def test_bible_binding_read_failure_cannot_report_success():
    class UnreadableBindings(InMemoryVariableHubRepository):
        def get_output_bindings(self, *args, **kwargs):
            raise RuntimeError("injected binding read failure")

    session, decision = session_and_decision()
    repo = UnreadableBindings()
    commit = AdoptionCommitService(variable_hub_repository=repo).commit(session=session, decision=decision)
    assert commit.status.value == "blocked"
    step = next(step for step in commit.steps if step.name == "commit_variable_outputs")
    assert step.result["reason"] == "output_bindings_unavailable"
    assert repo.bindings == {}
