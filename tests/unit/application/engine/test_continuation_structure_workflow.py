"""Continuation planning uses the real API, single writer and structure journal."""
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer, RAW
from application.engine.services import autopilot_control as control
from application.engine.services import continuation_proposal_service as continuation
from application.blueprint.services.continuous_planning_service import ContinuousPlanningService
from application.snapshot.services.data_change_journal import DataChangeJournal
from domain.novel.entities.novel import NovelStage
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from interfaces.api.v1.engine import autopilot_routes as routes
from interfaces.api.v1.blueprint import story_structure
from engine.runtime import macro_planning_delegate


STRUCTURE = [{"title": "第一部", "volumes": [{"title": "第一卷", "acts": [{"title": "第一幕"}]}]}]


@pytest.fixture
def setup(daemon_writer, monkeypatch):
    db = daemon_writer[0]
    db.execute("UPDATE novels SET current_stage='completed',autopilot_status='completed',target_chapters=1,auto_approve_mode=1 WHERE id='novel'")
    monkeypatch.setattr(routes, "get_novel_repository", lambda: SqliteNovelRepository(db))
    monkeypatch.setattr(routes, "_get_shared_state_for_novel", lambda nid: None)
    monkeypatch.setattr(story_structure, "get_database", lambda: db)
    from application.engine.services import novel_stop_signal
    monkeypatch.setattr(novel_stop_signal, "publish_start_signal", lambda *a, **k: False)
    api = FastAPI()
    api.include_router(routes.router)
    api.include_router(story_structure.router)
    planning = ContinuousPlanningService(StoryNodeRepository(db.db_path), None, None)
    return db, api, planning


async def approved(client, db):
    response = await client.post("/autopilot/novel/continuation-proposals", json={"target_chapters": 3})
    assert response.status_code == 200, response.text
    proposal = response.json()
    response = await client.post(f"/autopilot/novel/continuation-proposals/{proposal['proposal_id']}/approve", json={})
    assert response.status_code == 200, response.text
    command = response.json()
    control.process_pending_commands(db=db)
    return proposal, command


async def prepared(client, db, planning):
    proposal, command = await approved(client, db)
    state = continuation.prepare_structure(proposal["proposal_id"], "novel", copy.deepcopy(STRUCTURE), planning_service=planning, db=db)
    return state, command


def tokens(state):
    review = state["structure_review"]
    return {"expected_hash": review["base_hash"], "proposal_hash": review["proposal_hash"], "run_epoch": review["run_epoch"]}


@pytest.mark.asyncio
async def test_api_writer_waits_for_author_before_atomic_structure_and_act_planning(setup):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        assert state["status"] == "awaiting_structure_review"
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "paused_for_review"
        assert db.fetch_one("SELECT COUNT(*) AS n FROM story_nodes WHERE novel_id='novel'")["n"] == 0
        for endpoint in ("resume", "start"):
            response = await client.post(f"/autopilot/novel/{endpoint}", json={})
            assert response.status_code == 409
        fetched = await client.get(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}")
        assert fetched.json()["structure_review"]["proposal_id"] == state["structure_change_id"]
        response = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/structure/confirm", json=tokens(state))
        assert response.status_code == 200, response.text
        result = continuation.get(state["proposal_id"], "novel", db)
        assert result["status"] == "applied" and result["structure_confirmed_at"]
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "act_planning"
        assert db.fetch_one("SELECT COUNT(*) AS n FROM story_nodes WHERE novel_id='novel'")["n"] == 3
        assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW
        repeated = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/structure/confirm", json=tokens(state))
        assert repeated.status_code == 409


@pytest.mark.asyncio
async def test_waiting_review_survives_daemon_tick_and_blocks_unrelated_macro_apply(setup):
    db, api, planning = setup
    from engine.runtime.novel_lifecycle import process_novel
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        novel = SqliteNovelRepository(db).get_by_id(NovelId("novel"))
        assert novel.auto_approve_mode is True
        ended = []
        host = SimpleNamespace(_begin_novel_execution=lambda nid: True, _is_still_running=lambda novel: True,
            _end_novel_execution=lambda nid: ended.append(nid.value))
        await process_novel(host, novel)
        assert ended == ["novel"]
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "paused_for_review"
        nodes = planning._flatten_structure_to_nodes("novel", copy.deepcopy(STRUCTURE))
        unrelated = DataChangeJournal(db).propose_macro("novel", nodes, allow_running=True)
        response = await client.post(f"/novels/novel/data-changes/{unrelated['proposal_id']}/confirm", json={
            "expected_hash": unrelated["base_hash"], "proposal_hash": unrelated["proposal_hash"], "run_epoch": unrelated["run_epoch"]})
        assert response.status_code == 409
        assert continuation.get(state["proposal_id"], "novel", db)["status"] == "awaiting_structure_review"
        assert db.fetch_one("SELECT COUNT(*) AS n FROM story_nodes WHERE novel_id='novel'")["n"] == 0


@pytest.mark.asyncio
async def test_generic_confirmation_of_bound_review_advances_the_same_state_machine(setup):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        response = await client.post(f"/novels/novel/data-changes/{state['structure_change_id']}/confirm", json=tokens(state))
        assert response.status_code == 200, response.text
        assert continuation.get(state["proposal_id"], "novel", db)["status"] == "applied"
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "act_planning"


@pytest.mark.asyncio
async def test_author_can_confirm_during_macro_tick_without_stale_lifecycle_save(setup, monkeypatch):
    db, api, planning = setup
    from engine.runtime import novel_lifecycle
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        proposal, _ = await approved(client, db)
        novel = SqliteNovelRepository(db).get_by_id(NovelId("novel"))
        async def macro(host, working):
            state = continuation.prepare_structure(proposal["proposal_id"], "novel", copy.deepcopy(STRUCTURE), planning_service=planning, db=db)
            working.current_stage = NovelStage.PAUSED_FOR_REVIEW
            response = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/structure/confirm", json=tokens(state))
            assert response.status_code == 200, response.text
        monkeypatch.setattr(novel_lifecycle, "run_macro_planning", macro)
        host = SimpleNamespace(_begin_novel_execution=lambda nid: True, _is_still_running=lambda novel: True,
            _end_novel_execution=lambda nid: None, _update_shared_state=lambda *a, **k: None)
        # No save method is provided: the tick must not save its stale paused object.
        await novel_lifecycle.process_novel(host, novel)
        assert continuation.get(proposal["proposal_id"], "novel", db)["status"] == "applied"
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "act_planning"


def test_schema_upgrade_never_treats_legacy_early_applied_as_confirmed(setup):
    db, _, _ = setup
    old_sql = continuation.TABLE_SQL.split("    base_run_epoch")[0].rstrip().rstrip(",") + ")"
    db.execute(old_sql)
    db.execute("""INSERT INTO autopilot_continuation_proposals
        (proposal_id,novel_id,mode,base_stage,base_target_chapters,proposed_target_chapters,
        completed_chapters,from_chapter,plan_json,status,created_at) VALUES
        ('legacy','novel','continue','completed',1,3,1,2,'{}','applied','today')""")
    continuation.ensure_schema(db)
    continuation.ensure_schema(db)
    proposal = continuation.get("legacy", "novel", db)
    assert proposal["status"] == "legacy_unverified"
    assert proposal["structure_confirmed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["author_edit", "epoch", "hash", "writer_failure"])
async def test_conflict_or_writer_fault_preserves_review_state_and_structure(setup, fault):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        request = tokens(state)
        if fault == "author_edit":
            db.execute("UPDATE chapters SET content='作者编辑' WHERE id='chapter'")
        elif fault == "epoch":
            db.execute("UPDATE novels SET autopilot_run_epoch=autopilot_run_epoch+1 WHERE id='novel'")
        elif fault == "hash":
            request["proposal_hash"] = "0" * 64
        else:
            db.execute("CREATE TRIGGER fail_continuation_apply BEFORE UPDATE OF status ON autopilot_continuation_proposals WHEN NEW.status='applied' BEGIN SELECT RAISE(ABORT,'injected structure commit failure'); END")
        response = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/structure/confirm", json=request)
        assert response.status_code == 409
        assert continuation.get(state["proposal_id"], "novel", db)["status"] == "awaiting_structure_review"
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "paused_for_review"
        assert db.fetch_one("SELECT COUNT(*) AS n FROM story_nodes WHERE novel_id='novel'")["n"] == 0


@pytest.mark.asyncio
async def test_rejected_review_cannot_be_applied_through_generic_structure_endpoint(setup):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        old_epoch = state["run_epoch"]
        response = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/reject")
        assert response.status_code == 200
        response = await client.post(f"/novels/novel/data-changes/{state['structure_change_id']}/confirm", json=tokens(state))
        assert response.status_code == 409
        assert continuation.get(state["proposal_id"], "novel", db)["status"] == "rejected"
        assert db.fetch_one("SELECT current_stage,target_chapters FROM novels WHERE id='novel'") == {"current_stage": "completed", "target_chapters": 1}
        assert db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id='novel'")["autopilot_run_epoch"] == old_epoch + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["rejected", "superseded"])
async def test_rejected_or_superseded_approval_command_never_starts_planning(setup, fault):
    db, api, _ = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        proposal = (await client.post("/autopilot/novel/continuation-proposals", json={"target_chapters": 3})).json()
        command = (await client.post(f"/autopilot/novel/continuation-proposals/{proposal['proposal_id']}/approve", json={})).json()
        if fault == "rejected":
            await client.post(f"/autopilot/novel/continuation-proposals/{proposal['proposal_id']}/reject")
        else:
            db.execute("UPDATE novels SET autopilot_run_epoch=autopilot_run_epoch+1 WHERE id='novel'")
        control.process_pending_commands(db=db)
        assert control.get_command(command["command_id"], db)["state"] == ("failed" if fault == "rejected" else "superseded")
        assert continuation.get(proposal["proposal_id"], "novel", db)["status"] != "applied"
        assert db.fetch_one("SELECT current_stage FROM novels WHERE id='novel'")["current_stage"] == "completed"


@pytest.mark.asyncio
async def test_real_macro_delegate_produces_dry_run_even_in_auto_approve_mode(setup, monkeypatch):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        proposal, _ = await approved(client, db)
        monkeypatch.setattr(macro_planning_delegate, "_consume_pending_macro_plan", lambda *a, **k: {"success": True, "structure": copy.deepcopy(STRUCTURE)})
        host = SimpleNamespace(_is_still_running=lambda novel: True, _update_shared_state=lambda *a, **k: None,
                               planning_service=planning)
        novel = SqliteNovelRepository(db).get_by_id(NovelId("novel"))
        assert novel.auto_approve_mode is True
        await macro_planning_delegate.run_macro_planning(host, novel)
        assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
        assert continuation.get(proposal["proposal_id"], "novel", db)["status"] == "awaiting_structure_review"
        assert db.fetch_one("SELECT COUNT(*) AS n FROM story_nodes WHERE novel_id='novel'")["n"] == 0


@pytest.mark.asyncio
async def test_approval_rejects_stale_epoch_and_review_can_be_abandoned_after_stop(setup):
    db, api, planning = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        state, _ = await prepared(client, db, planning)
        stop = control.submit_command("novel", "stop", {}, db=db)
        control.process_pending_commands(db=db)
        assert control.get_command(stop["command_id"], db)["state"] == "completed"
        confirmed = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/structure/confirm", json=tokens(state))
        assert confirmed.status_code == 409
        rejected = await client.post(f"/autopilot/novel/continuation-proposals/{state['proposal_id']}/reject")
        assert rejected.status_code == 200, rejected.text
        assert db.fetch_one("SELECT current_stage,target_chapters FROM novels WHERE id='novel'") == {"current_stage": "completed", "target_chapters": 1}
        proposal = (await client.post("/autopilot/novel/continuation-proposals", json={"target_chapters": 3})).json()
        db.execute("UPDATE novels SET autopilot_run_epoch=autopilot_run_epoch+1 WHERE id='novel'")
        stale = await client.post(f"/autopilot/novel/continuation-proposals/{proposal['proposal_id']}/approve", json={})
        assert stale.status_code == 409
