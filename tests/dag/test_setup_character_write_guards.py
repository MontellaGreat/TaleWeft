"""Audited character writes use the real services and an isolated SQLite DB."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.dag.test_setup_dag_adversarial import adapter_services, setup_db
from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
from application.onboarding.setup_dag import SetupDAGStore, build_setup_graph
from application.onboarding.setup_edit_guard import SetupEditConflict, setup_write_scope
from application.world.dtos.bible_dto import CharacterDTO, StyleNoteDTO


def unfinished(db, *, stage="characters", run_id="audit-run", status="error"):
    store = SetupDAGStore(db)
    snapshot = store.create(run_id, "novel", stage, build_setup_graph(stage))
    snapshot["status"] = status
    store.save(snapshot)


@pytest.fixture
def character_service(adapter_services, monkeypatch):
    from interfaces.api import dependencies
    bible, *_ = adapter_services
    bible.add_character("novel", "hero", "Lin",
        "阿林坚信守住灯塔就是守住家人。他为了追查失踪船员，绝不抛弃同伴，却害怕再次失去家人。",
        verbal_tic="Reviewed words.", gender="女")
    monkeypatch.setattr(dependencies, "get_bible_service", lambda: bible)
    return bible


def update_characters(service, characters):
    current = service.get_bible_by_novel("novel")
    return service.update_bible("novel", characters, current.world_settings,
        current.locations, current.timeline_notes, current.style_notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["anchor", "extract", "auto-fill"])
async def test_all_character_write_apis_reject_unfinished_audit(character_service, setup_db, entry):
    from interfaces.api.v1.engine.checkpoint_routes import (
        AutoFillCharacterPsycheRequest, autofill_character_psyches, extract_character_psyche_to_bible,
    )
    from interfaces.api.v1.workbench.sandbox import UpdateCharacterAnchorRequest, patch_character_anchor
    before = asdict(character_service.get_bible_by_novel("novel"))
    unfinished(setup_db)
    with pytest.raises(HTTPException) as error:
        if entry == "anchor":
            await patch_character_anchor("novel", "hero", UpdateCharacterAnchorRequest(
                mental_state="PARANOID", verbal_tic="UNREVIEWED", idle_behavior="Breaks the lamp."), character_service)
        elif entry == "extract":
            await extract_character_psyche_to_bible("novel", "Lin")
        else:
            await autofill_character_psyches("novel", AutoFillCharacterPsycheRequest())
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "SETUP_AUDIT_INCOMPLETE"
    assert asdict(character_service.get_bible_by_novel("novel")) == before


@pytest.mark.parametrize("entry", ["anchors", "add", "upsert", "replace", "clear", "recreate"])
def test_shared_service_blocks_character_writes_without_api(character_service, setup_db, entry):
    before = asdict(character_service.get_bible_by_novel("novel"))
    unfinished(setup_db)
    with pytest.raises(SetupEditConflict):
        if entry == "anchors":
            character_service.update_character_voice_anchors("novel", "hero", mental_state="PARANOID",
                verbal_tic="UNREVIEWED", idle_behavior="Breaks the lamp.")
        elif entry == "add":
            character_service.add_character("novel", "new", "New", "Unreviewed person.")
        elif entry == "upsert":
            character_service.upsert_character("novel", "hero", "Lin", "Unreviewed replacement.")
        elif entry == "recreate":
            character_service.create_bible("novel-bible", "novel")
        else:
            characters = [] if entry == "clear" else [CharacterDTO("hero", "Lin", "Unreviewed replacement.", [])]
            update_characters(character_service, characters)
    assert asdict(character_service.get_bible_by_novel("novel")) == before


@pytest.mark.parametrize("field,value", [
    ("description", "Changed description."), ("background", "Changed history."),
    ("relationships", [{"target": "Other", "label": "Unreviewed tie"}]),
    ("inner_lack", "Changed need."), ("mental_state", "PARANOID"),
    ("mental_state_reason", "Changed reason."), ("verbal_tic", "Unreviewed words."),
    ("idle_behavior", "Breaks the lamp."), ("public_profile", "Changed public story."),
    ("hidden_profile", "Changed secret."), ("reveal_chapter", 3),
    ("core_belief", "Changed belief."), ("moral_taboos", ["Changed taboo."]),
    ("voice_profile", {"style": "Changed voice."}), ("active_wounds", [{"text": "Changed wound."}]),
])
def test_bulk_guard_checks_all_effective_character_fields(character_service, setup_db, field, value):
    before = character_service.get_bible_by_novel("novel").characters[0]
    unfinished(setup_db)
    with pytest.raises(SetupEditConflict):
        update_characters(character_service, [replace(before, **{field: value})])
    assert character_service.get_bible_by_novel("novel").characters[0] == before


@pytest.mark.parametrize("scope", ["expired", "other-run", "other-book", "other-stage"])
def test_internal_service_write_requires_current_exact_owner(character_service, setup_db, scope):
    unfinished(setup_db)
    owner = "current-run"
    if scope != "expired":
        assert claim_execution_lease("novel", "setup", owner, 120, setup_db)
    scoped_book = "other" if scope == "other-book" else "novel"
    scoped_stage = "locations" if scope == "other-stage" else "characters"
    scoped_run = "old-run" if scope == "other-run" else owner
    try:
        with setup_write_scope(scoped_book, scoped_stage, scoped_run):
            with pytest.raises(SetupEditConflict):
                character_service.upsert_character("novel", "hero", "Lin", "Unreviewed stale write.")
    finally:
        release_execution_lease("novel", "setup", owner, setup_db)
    assert character_service.get_bible_by_novel("novel").characters[0].description.startswith("阿林")


def test_current_workflow_scope_can_commit_and_does_not_leak(character_service, setup_db):
    unfinished(setup_db)
    assert claim_execution_lease("novel", "setup", "current-run", 120, setup_db)
    try:
        with setup_write_scope("novel", "characters", "current-run"):
            update_characters(character_service, [CharacterDTO("hero", "Lin", "Reviewed replacement.", [],
                verbal_tic="Reviewed final words.")])
        with pytest.raises(SetupEditConflict):
            character_service.update_character_voice_anchors("novel", "hero", mental_state="PARANOID",
                verbal_tic="UNREVIEWED", idle_behavior="Breaks the lamp.")
    finally:
        release_execution_lease("novel", "setup", "current-run", setup_db)
    assert character_service.get_bible_by_novel("novel").characters[0].verbal_tic == "Reviewed final words."


def test_unchanged_characters_do_not_block_legal_other_stage_commit(character_service, setup_db):
    unfinished(setup_db, stage="worldbuilding")
    # An unfinished later stage locks its worldbuilding predecessor too.
    unfinished(setup_db, stage="characters", run_id="characters-run")
    before = character_service.get_bible_by_novel("novel")
    incoming = replace(before.characters[0], gender=None, verbal_tic=None)
    assert claim_execution_lease("novel", "setup", "world-run", 120, setup_db)
    try:
        with setup_write_scope("novel", "worldbuilding", "world-run"):
            character_service.update_bible("novel", [incoming], before.world_settings, before.locations,
                before.timeline_notes, [StyleNoteDTO("style", "文风公约", "Reviewed style.")])
    finally:
        release_execution_lease("novel", "setup", "world-run", setup_db)
    saved = character_service.get_bible_by_novel("novel")
    assert saved.characters == before.characters
    assert saved.style_notes[0].content == "Reviewed style."


@pytest.mark.parametrize("stored,incoming", [
    ({}, {}), ({"style": "Measured."}, {"voice_style": "Measured."}),
    ({"label": "An author voice extension."}, {"label": "An author voice extension."}),
])
def test_persisted_voice_equivalents_allow_unchanged_characters(character_service, setup_db, stored, incoming):
    before = character_service.get_bible_by_novel("novel")
    update_characters(character_service, [replace(before.characters[0], voice_profile=stored)])
    before = character_service.get_bible_by_novel("novel")
    unfinished(setup_db)
    # Only the SQLite representation matters for an otherwise unchanged row.
    update_characters(character_service, [replace(before.characters[0], voice_profile=incoming)])
    assert character_service.get_bible_by_novel("novel").characters == before.characters


@pytest.mark.parametrize("stored,incoming", [
    ({}, {"label": "An added author voice extension."}),
    ({"label": "Original author voice."}, {"label": "Changed author voice."}),
    ({"label": "Original author voice."}, {}),
])
def test_persisted_voice_extensions_cannot_bypass_unfinished_character_audit(
        character_service, setup_db, stored, incoming):
    before = character_service.get_bible_by_novel("novel")
    update_characters(character_service, [replace(before.characters[0], voice_profile=stored)])
    before = character_service.get_bible_by_novel("novel")
    assert before.characters[0].voice_profile == stored
    unfinished(setup_db)
    # Author extensions are persisted in full. Adding, editing or clearing one
    # changes the audited character even when legacy voice columns are equal.
    with pytest.raises(SetupEditConflict):
        update_characters(character_service, [replace(before.characters[0], voice_profile=incoming)])
    assert character_service.get_bible_by_novel("novel").characters == before.characters


def test_completed_workflow_releases_service_edit_lock(character_service, setup_db):
    unfinished(setup_db, status="completed")
    character_service.update_character_voice_anchors("novel", "hero", mental_state="NORMAL",
        verbal_tic="Author revision.", idle_behavior="Counts fuel.")
    assert character_service.get_bible_by_novel("novel").characters[0].verbal_tic == "Author revision."


@pytest.mark.asyncio
async def test_api_cannot_change_character_between_real_commit_and_readback(adapter_services, setup_db, monkeypatch):
    from application.ai_invocation.dtos import (
        ContinuationRef, InvocationPolicy, InvocationSession, InvocationSessionStatus, PromptSnapshot,
    )
    from application.audit.services import anti_ai_audit
    from application.onboarding.setup_generation import SetupInvocationSteps
    from application.world.services.bible_setup_output_bindings import bible_setup_output_bindings
    from domain.ai.services.llm_service import GenerationResult
    from domain.ai.value_objects.prompt import Prompt
    from interfaces.api.v1.workbench.sandbox import UpdateCharacterAnchorRequest, patch_character_anchor
    from tests.dag.test_setup_generation_gates import approved_review

    bible, world, novel, inv = adapter_services
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda **_: anti_ai_audit.AntiAIAuditor())

    class LocalLLM:
        async def stream_generate(self, prompt, config):
            yield (await self.generate(prompt, config)).content

        async def generate(self, prompt, config):
            request = json.loads(prompt.user)
            if "required_checks" in request:
                report = {"review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"],
                    "checks": {key: True for key in request["required_checks"]}, "issues": [],
                    "coverage": [{"text_id": row["text_id"]} for row in request["texts"]]}
            else:
                report = approved_review(request)
            return GenerationResult(json.dumps(report), None)

    steps = SetupInvocationSteps("novel", "characters")
    steps.generator = SimpleNamespace(llm_service=LocalLLM(), bible_service=bible, worldbuilding_service=world)
    steps.novel, steps.unit, steps.run_id = novel, "characters", "audit-race-run"
    steps.base_context, steps.repos = steps._base(), inv._repositories()
    binding_id = "bible-characters:output:v1"
    steps.repos["variable_hub"].set_bindings(binding_id, "bible-characters",
        bible_setup_output_bindings("bible-characters"), direction="output")
    prompt = Prompt(system="Return the structured setup result.", user="Generate characters.")
    snapshot = PromptSnapshot(prompt=prompt, node_key="bible-characters", node_version_id="offline-v1",
        asset_link_set_id="", input_binding_set_id="", output_binding_set_id=binding_id,
        variable_snapshot_hash="offline", template_hash="offline", composition_hash="offline", rendered_prompt_hash="offline")
    steps.session = InvocationSession(id="audit-characters-session", operation="bible.setup.characters",
        node_key="bible-characters", policy=InvocationPolicy.FULL_INTERACTIVE,
        status=InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW, context={"novel_id": "novel"},
        continuation=ContinuationRef(handler_key="bible_characters"), prompt_snapshot=snapshot)
    steps._install_candidate({"characters": [{"id": "hero", "name": "Lin",
        "description": "A bridge keeper traces the physical signal.", "verbal_tic": "Audit approved signal.",
        "mental_state": "NORMAL", "idle_behavior": "Counts fuel cans."}]}, purpose="local_fixture")
    steps.validate("validate")
    await steps.deviation()
    await steps.anti_ai()
    unfinished(setup_db, run_id=steps.run_id)
    assert claim_execution_lease("novel", "setup", steps.run_id, 120, setup_db)
    blocked_during_window = False

    async def after_durable_commit(checkpoint):
        nonlocal blocked_during_window
        if checkpoint["units"]["characters"].get("commit_id") and not blocked_during_window:
            with pytest.raises(HTTPException) as error:
                await patch_character_anchor("novel", "hero", UpdateCharacterAnchorRequest(
                    mental_state="PARANOID", verbal_tic="UNREVIEWED_AFTER_AUDIT", idle_behavior="Breaks the lamp."), bible)
            assert error.value.status_code == 409
            blocked_during_window = True

    steps._emit_callback = after_durable_commit
    try:
        result = await steps.commit()
        assert blocked_during_window and result["saved_data_verified"] is True
        assert steps.approved["characters"][0]["verbal_tic"] == "Audit approved signal."
        assert bible.get_bible_by_novel("novel").characters[0].verbal_tic == "Audit approved signal."
    finally:
        release_execution_lease("novel", "setup", steps.run_id, setup_db)
