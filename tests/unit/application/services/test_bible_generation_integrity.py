"""Adversarial checks for legacy Bible empty output and false completion."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, HTTPException

from application.world.services.auto_bible_generator import AutoBibleGenerator
from application.world.worldbuilding_schema import WORLDBUILDING_DIMENSION_DEFS
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from interfaces.api.v1.world import bible


@pytest.fixture
def rendered_prompt(monkeypatch):
    from infrastructure.ai import prompt_registry
    monkeypatch.setattr(prompt_registry, "get_prompt_registry", lambda: SimpleNamespace(
        get_node=lambda *a, **kw: None,
        render_to_prompt=lambda *a, **kw: Prompt(system="Bible system", user="Bible user"),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{}", '{"characters": [], "locations": [], "style": " "}'])
async def test_empty_bible_object_is_retried_and_never_replaced_with_default_characters(rendered_prompt, content):
    provider = Mock(generate=AsyncMock(return_value=GenerationResult(content, None)))
    generator = AutoBibleGenerator(provider, Mock())
    with pytest.raises(ValueError, match="空对象或空内容"):
        await generator._generate_bible_data("premise", 10)
    assert provider.generate.await_count == 3
    assert not generator.bible_service.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("array_key,item_type", [("characters", "character"), ("locations", "location")])
@pytest.mark.parametrize("content", ["", " ", "{}", "[]", "empty", None])
async def test_stream_empty_or_unusable_content_never_emits_done(array_key, item_type, content):
    async def stream(*_args):
        if content is not None:
            yield content
    generator = AutoBibleGenerator(SimpleNamespace(stream_generate=stream), Mock())
    emitted = []
    with pytest.raises(ValueError):
        async for event in generator._stream_json_items(Prompt("s", "u"), array_key, item_type):
            emitted.append(event)
    assert all(event["type"] != "done" for event in emitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("array_key,item_type", [("characters", "character"), ("locations", "location")])
@pytest.mark.parametrize("partial", [False, True])
async def test_provider_failure_after_partial_items_is_still_a_failure(array_key, item_type, partial):
    async def stream(*_args):
        if partial:
            yield json.dumps({array_key: [{"name": "First"}]})
        raise RuntimeError("connection interrupted")
    generator = AutoBibleGenerator(SimpleNamespace(stream_generate=stream), Mock())
    emitted = []
    with pytest.raises(RuntimeError, match="connection interrupted"):
        async for event in generator._stream_json_items(Prompt("s", "u"), array_key, item_type):
            emitted.append(event)
    assert all(event["type"] != "done" for event in emitted)
    assert len([event for event in emitted if event["type"] == item_type]) == int(partial)


@pytest.mark.asyncio
@pytest.mark.parametrize("array_key,item_type", [("characters", "character"), ("locations", "location")])
async def test_stream_valid_result_emits_each_item_once_before_done(array_key, item_type):
    payload = json.dumps({array_key: [{"name": "First"}, {"name": "Second"}]})
    async def stream(*_args):
        for offset in range(0, len(payload), 3):
            yield payload[offset:offset + 3]
    generator = AutoBibleGenerator(SimpleNamespace(stream_generate=stream), Mock())
    emitted = [event async for event in generator._stream_json_items(Prompt("s", "u"), array_key, item_type)]
    assert [event["content"]["name"] for event in emitted if event["type"] == item_type] == ["First", "Second"]
    assert emitted[-1] == {"type": "done", "count": 2}


@pytest.mark.asyncio
async def test_empty_style_stream_is_failure(rendered_prompt):
    async def stream(*_args):
        yield " \n"
    generator = AutoBibleGenerator(SimpleNamespace(stream_generate=stream), Mock())
    with pytest.raises(ValueError, match="空内容"):
        _ = [event async for event in generator._stream_style("premise", 10)]


@pytest.mark.asyncio
async def test_bible_creation_failure_is_not_swallowed():
    service = Mock()
    service.bible_repository.get_by_novel_id.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await AutoBibleGenerator(Mock(), service)._save_to_bible("book", {"style": "style"})
    service.add_style_note.assert_not_called()


def complete_world():
    return {key: {field: "完整有效的世界观设定内容" * 3 for field in definition["fields"]}
            for key, definition in WORLDBUILDING_DIMENSION_DEFS.items()}


@pytest.mark.asyncio
async def test_worldbuilding_storage_failure_and_incomplete_dimensions_are_not_success():
    world_service = Mock()
    world_service.update_worldbuilding.side_effect = RuntimeError("world write failed")
    generator = AutoBibleGenerator(Mock(), Mock(), worldbuilding_service=world_service)
    with pytest.raises(RuntimeError, match="world write failed"):
        await generator._save_worldbuilding("book", complete_world())
    world_service.reset_mock()
    with pytest.raises(ValueError, match="完整字段"):
        await generator._save_worldbuilding("book", {"core_rules": {"power_system": "短"}})
    world_service.update_worldbuilding.assert_not_called()


@pytest.fixture
def legacy_sse(monkeypatch):
    from interfaces.api import dependencies
    from application.core.taxonomy import market_prompt
    novel = SimpleNamespace(id="book", title="Book", premise="Premise", target_chapters=10)
    monkeypatch.setattr(dependencies, "get_novel_service", lambda: SimpleNamespace(get_novel=lambda _: novel))
    monkeypatch.setattr(market_prompt, "load_verified_market_profile", lambda *_: ({}, ""))
    monkeypatch.setattr(bible, "_BIBLE_SETUP_NODE_BY_STAGE", {})
    monkeypatch.setattr(bible, "get_bible_runtime_settings", lambda: SimpleNamespace(
        worldbuilding_field_emit_delay_seconds=0, worldbuilding_dimension_emit_delay_seconds=0))
    monkeypatch.setattr(bible, "clear_bible_generation_state", Mock())
    monkeypatch.setattr(bible, "record_bible_generation_failure", Mock())
    service = Mock()
    service.get_bible_by_novel.return_value = SimpleNamespace(style_notes=[], characters=[], locations=[])
    generator = AutoBibleGenerator(Mock(), service)
    generator._load_worldbuilding = Mock(return_value={})
    generator._load_characters = Mock(return_value=[])
    knowledge = SimpleNamespace(generate_and_save=AsyncMock())
    return generator, knowledge


async def sse_body(generator, knowledge, stage):
    return "".join([event async for event in bible._sse_bible_generator("book", stage, generator, knowledge)])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["characters", "locations"])
async def test_empty_legacy_stage_stops_without_done_or_knowledge(legacy_sse, stage):
    generator, knowledge = legacy_sse
    async def stream(*_args, **_kwargs):
        yield {"type": "done", "count": 0}
    setattr(generator, f"_stream_generate_{stage}", stream)
    body = await sse_body(generator, knowledge, stage)
    assert "event: error" in body
    assert "event: done" not in body
    assert f'"phase": "{stage}_done"' not in body
    knowledge.generate_and_save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,item_type,save_method", [
    ("characters", "character", "add_character"), ("locations", "location", "add_location"),
])
async def test_legacy_item_storage_failure_stops_without_success(legacy_sse, stage, item_type, save_method):
    generator, knowledge = legacy_sse
    async def stream(*_args, **_kwargs):
        content = {"name": "First", "description": "A valid generated entry", "type": "place"}
        yield {"type": item_type, "index": 0, "content": content}
        yield {"type": "done", "count": 1}
    setattr(generator, f"_stream_generate_{stage}", stream)
    getattr(generator.bible_service, save_method).side_effect = RuntimeError("storage unavailable")
    body = await sse_body(generator, knowledge, stage)
    assert "storage unavailable" in body
    assert "event: error" in body and "event: done" not in body
    knowledge.generate_and_save.assert_not_called()
    bible.record_bible_generation_failure.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_style_save_failure_stops_world_and_knowledge(legacy_sse):
    generator, knowledge = legacy_sse
    async def stream(*_args, **_kwargs):
        yield {"type": "done", "style": "Style"}
    generator._stream_style = stream
    generator._stream_worldbuilding_full = Mock()
    generator.bible_service.add_style_note.side_effect = RuntimeError("style write failed")
    body = await sse_body(generator, knowledge, "all")
    assert "style write failed" in body and "event: done" not in body
    generator._stream_worldbuilding_full.assert_not_called()
    knowledge.generate_and_save.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_world_save_failure_stops_following_stages(legacy_sse):
    generator, knowledge = legacy_sse
    async def style(*_args, **_kwargs):
        yield {"type": "done", "style": "Style"}
    async def world(*_args, **_kwargs):
        yield {"type": "dimension", "key": "core_rules", "content": complete_world()["core_rules"]}
    generator._stream_style = style
    generator._stream_worldbuilding_full = world
    generator._save_worldbuilding = AsyncMock(side_effect=RuntimeError("world write failed"))
    body = await sse_body(generator, knowledge, "all")
    assert "world write failed" in body and "event: done" not in body
    assert '"phase": "characters"' not in body
    knowledge.generate_and_save.assert_not_called()
    bible.record_bible_generation_failure.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_location_graph_has_distinct_ids_and_parent_before_child(legacy_sse):
    generator, knowledge = legacy_sse
    async def stream(*_args, **_kwargs):
        payload = [{"id": "child", "name": "Child", "description": "child place", "parent_id": "parent"},
                   {"id": "parent", "name": "Parent", "description": "parent place"},
                   {"name": "No ID A", "description": "first place"},
                   {"name": "No ID B", "description": "second place"}]
        for index, item in enumerate(payload):
            yield {"type": "location", "index": index, "content": item}
        yield {"type": "done", "count": len(payload)}
    generator._stream_generate_locations = stream
    body = await sse_body(generator, knowledge, "locations")
    assert "event: done" in body and "event: error" not in body
    stored = [call.kwargs for call in generator.bible_service.add_location.call_args_list]
    assert len(stored) == 4 and len({item["location_id"] for item in stored}) == 4
    assert [item["name"] for item in stored].index("Parent") < [item["name"] for item in stored].index("Child")
    assert next(item for item in stored if item["name"] == "Child")["parent_id"] == "parent"


@pytest.mark.asyncio
async def test_unknown_stage_is_rejected_before_tasks_or_mutation(legacy_sse):
    generator, knowledge = legacy_sse
    background = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await bible.generate_bible("book", background, "unknown", generator, knowledge)
    assert exc.value.status_code == 422
    assert not background.tasks
    with pytest.raises(HTTPException) as exc:
        await bible.generate_bible_stream("book", "unknown", generator, knowledge)
    assert exc.value.status_code == 422
    with pytest.raises(ValueError, match="Unknown stage"):
        await generator.generate_and_save("book", "premise", 10, stage="unknown")
    assert not generator.bible_service.mock_calls
    body = await sse_body(generator, knowledge, "unknown")
    assert "event: error" in body and "event: done" not in body
    bible.clear_bible_generation_state.assert_not_called()
