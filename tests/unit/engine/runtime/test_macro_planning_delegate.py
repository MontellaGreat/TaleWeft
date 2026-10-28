from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy
from engine.runtime import macro_planning_delegate as macro


@pytest.mark.asyncio
async def test_autopilot_backfills_bible_inputs_before_compiling_macro_call(monkeypatch):
    events = []
    intent = None

    class Planning:
        def _get_bible_context(self, novel_id):
            events.append(("bible", novel_id))
            return {"characters": [{"name": "陈禾"}]}

        def build_quick_macro_variables(self, context, target_chapters):
            events.append(("planning", context, target_chapters))
            return {"planning_depth": "compact", "rec_parts": 1}

    class Orchestrator:
        async def request(self, submitted):
            nonlocal intent
            intent = submitted
            events.append(("request", submitted))
            return submitted

    monkeypatch.setattr(macro, "_backfill_autopilot_setup_variables",
                        lambda novel_id: events.append(("backfill", novel_id)))
    monkeypatch.setattr("application.ai_invocation.contracts.ensure_invocation_contract",
                        lambda *args: events.append(("contract", *args[:2])))
    monkeypatch.setattr(macro, "_write_macro_variables",
                        lambda **kwargs: events.append(("write", kwargs["novel_id"])))
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda: object())
    monkeypatch.setattr("infrastructure.ai.generation_profiles.generation_config_from_profile",
                        lambda _: None)
    monkeypatch.setattr("application.ai_invocation.autopilot.factory.get_or_create_autopilot_orchestrator",
                        lambda _: Orchestrator())

    novel = SimpleNamespace(novel_id=SimpleNamespace(value="book"), auto_approve_mode=True)
    await macro._request_macro_invocation(
        SimpleNamespace(planning_service=Planning()), novel, target_chapters=12,
    )

    assert events[0] == ("backfill", "book")
    assert [event[0] for event in events].index("backfill") < [event[0] for event in events].index("planning")
    assert intent.policy_hint is InvocationPolicy.DIRECT
    assert intent.context["target_chapters"] == 12
