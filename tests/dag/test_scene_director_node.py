"""SceneDirector DAG adapter regression and failure propagation checks."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import NodeDefinition, NodeStatus
from application.engine.dag.nodes.execution_nodes import BeatNode, SceneNode, WriterNode
from application.engine.dag.chapter_services import DAGChapterServices
from application.engine.dag.plan.schema import ChapterExecutionPlan, PlanAtomSpec, PlanningEnvelope
from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis
from domain.ai.value_objects.prompt import Prompt
from tests.dag.test_execution_integrity import edge, graph, node_factory
from tests.unit.application.services.test_context_builder import _make_builder
from tests.unit.engine.test_prose_prompt_hooks import hook_db


@pytest.mark.asyncio
async def test_scene_node_uses_real_async_director_service(monkeypatch):
    prompts = []

    def render(key, variables):
        prompts.append((key, variables))
        return Prompt(system="scene direction", user=variables["outline"])

    monkeypatch.setattr("application.engine.services.scene_director_service.render_required_prompt", render)
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=
        '{"characters":["林舟"],"locations":["码头"],"pov":"林舟",'
        '"atg":{"nodes":[{"location_id":"码头","is_entry_point":true}],"visit_sequence":["码头"]}}')))
    result = await SceneNode().execute({"outline": "  林舟到达码头  "},
        {"shared_state": {"chapter_number": 3, "llm_service": llm}})

    assert result.status == NodeStatus.SUCCESS, result.error
    llm.generate.assert_awaited_once()
    assert prompts[0][1]["outline"] == "Chapter 3\n林舟到达码头"
    analysis = result.outputs["scene_analysis"]
    assert isinstance(analysis, dict) and analysis["characters"] == ["林舟"]
    assert analysis["action_transition_graph"]["nodes"][0]["is_entry_point"] is True


@pytest.mark.asyncio
async def test_scene_node_reuses_injected_director_and_context_chapter():
    director = SimpleNamespace(analyze=AsyncMock(return_value=SceneDirectorAnalysis(pov="林舟")))
    result = await SceneNode().execute({"outline": "梗概"}, {
        "chapter_number": " 4 ", "scene_director_service": director,
        "shared_state": {"chapter_number": 8},
    })
    assert result.status == NodeStatus.SUCCESS, result.error
    director.analyze.assert_awaited_once_with(chapter_number=4, outline="梗概")
    assert result.outputs["scene_analysis"]["pov"] == "林舟"


@pytest.mark.parametrize("outline", [None, "", " \n ", {}, []])
@pytest.mark.asyncio
async def test_scene_node_rejects_empty_or_non_text_outline_before_service(outline):
    director = SimpleNamespace(analyze=AsyncMock())
    inputs = {"outline": outline}
    assert not SceneNode().validate_inputs(inputs)
    result = await SceneNode().execute(inputs, {"chapter_number": 1, "scene_director_service": director})
    assert result.status == NodeStatus.ERROR and "outline" in result.error
    director.analyze.assert_not_called()


@pytest.mark.parametrize("chapter_number", [None, 0, -1, True, 1.5, "invalid", "0"])
@pytest.mark.asyncio
async def test_scene_node_rejects_invalid_chapter_before_service(chapter_number):
    director = SimpleNamespace(analyze=AsyncMock())
    result = await SceneNode().execute({"outline": "梗概"},
        {"chapter_number": chapter_number, "scene_director_service": director})
    assert result.status == NodeStatus.ERROR and "chapter_number" in result.error
    director.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_scene_node_provider_resolution_failure_is_not_success(monkeypatch):
    def unavailable(_context):
        raise RuntimeError("model provider unavailable")
    monkeypatch.setattr("application.engine.dag.registry.resolve_llm_service", unavailable)
    result = await SceneNode().execute({"outline": "梗概"}, {"chapter_number": 1})
    assert result.status == NodeStatus.ERROR
    assert "model provider unavailable" in result.error
    assert result.outputs == {"scene_analysis": {}}


@pytest.mark.asyncio
async def test_scene_node_propagates_director_failure():
    director = SimpleNamespace(analyze=AsyncMock(side_effect=RuntimeError("director failed")))
    result = await SceneNode().execute({"outline": "梗概"},
        {"chapter_number": 1, "shared_state": {"scene_director_service": director}})
    assert result.status == NodeStatus.ERROR and "director failed" in result.error


@pytest.mark.parametrize("raw", [
    "not json", "{}", '{"unknown_field":"value"}', '["not","a","dict"]',
    '{"characters":[],"performance_notes":[]}', '{"atg":{}}',
    '{"characters":[" "],"atg":{"nodes":[{}],"visit_sequence":[" "]}}',
])
@pytest.mark.asyncio
async def test_scene_node_blocks_empty_analysis_from_tolerant_parser(monkeypatch, raw):
    monkeypatch.setattr("application.engine.services.scene_director_service.render_required_prompt",
        lambda _key, variables: Prompt(system="scene direction", user=variables["outline"]))
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content=raw)))
    result = await SceneNode().execute({"outline": "梗概"}, {"chapter_number": 1, "llm_service": llm})
    assert result.status == NodeStatus.ERROR
    assert "未返回有效分析" in result.error


@pytest.mark.asyncio
async def test_scene_node_failure_blocks_downstream_beat_node(node_factory):
    downstream = node_factory("scene_beats", lambda *_: pytest.fail("Invalid scene analysis must block beats"))
    director = SimpleNamespace(analyze=AsyncMock(return_value=SceneDirectorAnalysis()))
    result = await DAGEngine().run(graph([NodeDefinition(id="scene", type="exec_scene"), downstream],
        [edge("scene", "scene_beats")]), {"outline": "梗概", "chapter_number": 1,
        "scene_director_service": director})
    assert result.status == "error"
    assert result.node_results["scene"].status == NodeStatus.ERROR
    assert result.node_results["scene_beats"].status == NodeStatus.BYPASSED


def plan():
    return ChapterExecutionPlan(envelope=PlanningEnvelope(target_chapter_words=5000), atoms=[
        PlanAtomSpec(id="one", intent="林舟在码头取得钥匙"),
        PlanAtomSpec(id="two", intent="林舟沿石阶进入库房，锁上门"),
    ])


def scene():
    return SceneDirectorAnalysis.model_validate({
        "characters": ["林舟"], "locations": ["码头", "库房"], "pov": "林舟",
        "emotional_state": "警惕", "performance_notes": ["压住袖口"],
        "action_transition_graph": {
            "nodes": [{"location_id": "码头", "is_entry_point": True, "initial_props": ["钥匙"]},
                      {"location_id": "库房", "initial_props": ["账本"]}],
            "transitions": [{"source_location": "码头", "target_location": "库房",
                             "required_action": "沿石阶进入库房", "trigger_characters": ["林舟"]}],
            "visit_sequence": ["码头", "库房"],
        },
    })


async def execute_beats(monkeypatch, path, raw, chapter_plan=None):
    state = {"novel_id": "one", "chapter_number": 1, "outline": "林舟进库房",
             "target_chapter_words": 5000, "chapter_plan_json": (chapter_plan or plan()).model_dump(mode="json")}
    inputs = {**state, "scene_analysis": raw}
    if path == "production":
        # This production adapter branch only projects a validated execution
        # plan, so no database/model dependency is needed for this test.
        svc = DAGChapterServices.__new__(DAGChapterServices)
        result = await svc.execute_node(BeatNode(), inputs, {"shared_state": state})
    else:
        builder = _make_builder()
        monkeypatch.setattr("application.engine.dag.nodes.execution_nodes._dag_context_builder", lambda: builder)
        result = await BeatNode().execute(inputs, {"chapter_number": 1, "novel_id": "one"})
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("as_dict", [False, True])
async def test_scene_metadata_and_atg_are_consumed_by_both_beat_adapters(monkeypatch, path, as_dict):
    analysis = scene()
    result = await execute_beats(monkeypatch, path, analysis.model_dump(mode="json") if as_dict else analysis)
    assert result.status == NodeStatus.SUCCESS, result.error
    beats = result.outputs["beats"]
    assert [beat["location_id"] for beat in beats] == ["码头", "库房"]
    assert all(beat["pov"] == "林舟" and beat["cast_refs"] == ["林舟"] for beat in beats)
    assert beats[0]["location_refs"] == ["码头"] and beats[0]["prop_refs"] == ["钥匙"]
    assert beats[1]["prop_refs"] == ["账本"] and "沿石阶进入库房" in beats[1]["must_include"]
    assert beats[0]["entity_manifest"]["scene_director"]["emotional_state"] == "警惕"
    assert beats[1]["entity_manifest"]["scene_director"]["performance_notes"] == ["压住袖口"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("raw", [
    None, {}, SceneDirectorAnalysis(), SceneDirectorAnalysis().model_dump(mode="json"),
    {"characters": [" "], "locations": ["\n"], "performance_notes": ["\t"],
     "pov": " ", "emotional_state": " "},
    {"action_transition_graph": {}},
    {"action_transition_graph": {"nodes": [{}], "visit_sequence": [" "]}},
    SceneDirectorAnalysis.model_validate({"action_transition_graph": {}}),
    {"action_transition_graph": {"transitions": [{}]}},
    {"action_transition_graph": {"transitions": [{"source_location": " ", "target_location": "\n",
        "required_action": "\t", "trigger_characters": [" "]}]}},
])
async def test_optional_empty_scene_analysis_keeps_existing_beat_generation(monkeypatch, path, raw):
    result = await execute_beats(monkeypatch, path, raw)
    assert result.status == NodeStatus.SUCCESS, result.error
    assert [beat["description"] for beat in result.outputs["beats"]] == [atom.intent for atom in plan().atoms]
    assert all(beat["location_id"] == "" for beat in result.outputs["beats"])
    assert all("scene_director" not in (beat.get("entity_manifest") or {})
               for beat in result.outputs["beats"])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("raw", [
    [], "not an object", {"unknown_field": "ignored"}, {"characters": "林舟"},
    {"pov": {}}, {"action_transition_graph": "not a graph"},
    {"action_transition_graph": {"nodes": [{"location_id": []}]}},
    {"action_transition_graph": {"transitions": [{"source_location": "码头", "target_location": "库房",
        "required_action": "沿石阶进入库房", "trigger_characters": ["林舟"]}]}},
    {"action_transition_graph": {"transitions": [{"target_location": "库房", "required_action": "走到库房"}]}},
    {"action_transition_graph": {"transitions": [{"source_location": "码头"}]}},
    {"action_transition_graph": {"transitions": [{"required_action": "走到库房"}]}},
    {"action_transition_graph": {"transitions": [{"trigger_characters": ["林舟"]}]}},
    {"action_transition_graph": {"nodes": [{"initial_props": ["钥匙"]}]}},
    {"action_transition_graph": {"nodes": [{"is_entry_point": True}]}},
])
async def test_invalid_scene_objects_never_silently_disappear(monkeypatch, path, raw):
    if path == "production":
        with pytest.raises(ValueError):
            await execute_beats(monkeypatch, path, raw)
    else:
        result = await execute_beats(monkeypatch, path, raw)
        assert result.status == NodeStatus.ERROR and result.error
        assert result.outputs["beats"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_template", [False, True])
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_scene_to_beats_to_writer_sends_the_bound_metadata(hook_db, monkeypatch, custom_template, path):
    builder = _make_builder()
    monkeypatch.setattr("application.engine.dag.nodes.execution_nodes._dag_context_builder", lambda: builder)
    if custom_template:
        monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "自定义正文规则", "user": "请写正文"})
    director = SimpleNamespace(analyze=AsyncMock(return_value=scene()))
    seen = []

    class Provider:
        async def generate(self, prompt, _config):
            seen.append(prompt)
            return "林舟按住钥匙，沿石阶进入库房，把账本收入怀中。"

    svc = DAGChapterServices.__new__(DAGChapterServices)
    services = SimpleNamespace(db=hook_db, handles=lambda kind: path == "production" and kind == "exec_beat",
                               execute_node=svc.execute_node)
    nodes = [NodeDefinition(id="scene", type="exec_scene"), NodeDefinition(id="beat", type="exec_beat"),
             NodeDefinition(id="writer", type="exec_writer")]
    result = await DAGEngine().run(graph(nodes, [
        edge("scene", "beat", source_port="scene_analysis", target_port="scene_analysis"),
        edge("beat", "writer", source_port="beats", target_port="beats"),
    ]), {"novel_id": "one", "chapter_number": 1, "outline": "林舟进入库房",
        "target_chapter_words": 5000, "chapter_plan_json": plan().model_dump(mode="json"),
        "scene_director_service": director, "llm_service": Provider(),
        "services": services})
    assert result.status == "completed", result.node_results
    assert len(seen) == 1
    prompt_text = seen[0].system + seen[0].user
    assert all(value in prompt_text for value in ["码头", "库房", "沿石阶进入库房", "压住袖口", "警惕", "scene_director"])
    assert result.node_results["writer"].execution_receipt.executed


@pytest.mark.asyncio
async def test_scene_atg_without_sequence_uses_entry_location_first(monkeypatch):
    analysis = scene()
    analysis.action_transition_graph.visit_sequence = []
    analysis.action_transition_graph.nodes.reverse()
    result = await execute_beats(monkeypatch, "production", analysis)
    assert [beat["location_id"] for beat in result.outputs["beats"]] == ["码头", "库房"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_scene_defaults_preserve_explicit_per_beat_plan_metadata(monkeypatch, path):
    chapter_plan = plan()
    chapter_plan.atoms[0].extensions = {"pov": "旁观者", "cast_refs": ["守卫"], "prop_refs": ["手环"],
                                        "must_include": ["交出通行证"]}
    result = await execute_beats(monkeypatch, path, scene(), chapter_plan)
    first, second = result.outputs["beats"]
    assert first["pov"] == "旁观者" and first["cast_refs"] == ["守卫"]
    assert first["prop_refs"] == ["手环", "钥匙"] and first["must_include"] == ["交出通行证"]
    assert second["pov"] == "林舟" and second["cast_refs"] == ["林舟"]
    assert first["entity_manifest"]["scene_director"]["action_transition_graph"]
    assert "action_transition_graph" not in second["entity_manifest"]["scene_director"]


def itinerary_scene(sequence):
    return SceneDirectorAnalysis.model_validate({"action_transition_graph": {
        "nodes": [{"location_id": location, "initial_props": [f"{location}道具"]}
                  for location in dict.fromkeys(sequence)],
        "transitions": [{"source_location": source, "target_location": target,
                         "required_action": f"从{source}步行至{target}",
                         "trigger_characters": [f"{source}{target}引路人"]}
                        for source, target in zip(sequence, sequence[1:])],
        "visit_sequence": sequence,
    }})


def itinerary_plan(count):
    return ChapterExecutionPlan(envelope=PlanningEnvelope(target_chapter_words=5000), atoms=[
        PlanAtomSpec(id=f"beat-{index}", intent=f"落实第{index + 1}段行动") for index in range(count)
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("sequence", [["A", "B", "C"], ["A", "B", "A"]])
async def test_atg_spans_keep_every_intermediate_obligation(monkeypatch, path, count, sequence):
    analysis = itinerary_scene(sequence)
    result = await execute_beats(monkeypatch, path, analysis, itinerary_plan(count))
    assert result.status == NodeStatus.SUCCESS, result.error
    beats = result.outputs["beats"]
    actions = [item for beat in beats for item in beat["must_include"]]
    cast = [item for beat in beats for item in beat["cast_refs"]]
    props = [item for beat in beats for item in beat["prop_refs"]]
    assert set(actions) == {edge.required_action for edge in analysis.action_transition_graph.transitions}
    assert set(cast) == {name for edge in analysis.action_transition_graph.transitions for name in edge.trigger_characters}
    assert set(props) == {f"{location}道具" for location in sequence}
    spans = [beat["entity_manifest"]["scene_visit_span"]["visit_sequence"] for beat in beats]
    reconstructed = spans[0] + [location for span in spans[1:] for location in span[1:]]
    assert reconstructed == sequence
    assert sum("action_transition_graph" in beat["entity_manifest"]["scene_director"] for beat in beats) == 1
    assert beats[0]["entity_manifest"]["scene_director"]["action_transition_graph"]["visit_sequence"] == sequence


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_explicit_plan_location_is_preserved_and_aligned_to_visit_span(monkeypatch, path):
    chapter_plan = itinerary_plan(2)
    chapter_plan.atoms[0].extensions = {"location_id": "B", "location_refs": ["B"]}
    result = await execute_beats(monkeypatch, path, itinerary_scene(["A", "B", "C"]), chapter_plan)
    assert result.status == NodeStatus.SUCCESS, result.error
    first, second = result.outputs["beats"]
    assert first["location_id"] == "B" and second["location_id"] == "C"
    assert first["entity_manifest"]["scene_visit_span"]["visit_sequence"] == ["A", "B"]
    assert first["must_include"] == ["从A步行至B"] and second["must_include"] == ["从B步行至C"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_valid_scene_metadata_with_empty_optional_graph_is_allowed(monkeypatch, path):
    result = await execute_beats(monkeypatch, path, {"pov": "林舟", "characters": ["林舟"],
                                                   "action_transition_graph": {}})
    assert result.status == NodeStatus.SUCCESS, result.error
    assert all(beat["pov"] == "林舟" and beat["cast_refs"] == ["林舟"] for beat in result.outputs["beats"])
    assert all("scene_visit_span" not in beat["entity_manifest"] for beat in result.outputs["beats"])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_auto_coordinates_obey_confirmed_anchor_bounds(monkeypatch, path):
    chapter_plan = itinerary_plan(3)
    chapter_plan.atoms[0].extensions = {"location_id": "C"}
    chapter_plan.atoms[2].extensions = {"location_id": "C"}
    result = await execute_beats(monkeypatch, path, itinerary_scene(["A", "B", "C"]), chapter_plan)
    assert result.status == NodeStatus.SUCCESS, result.error
    beats = result.outputs["beats"]
    assert [beat["location_id"] for beat in beats] == ["C", "C", "C"]
    assert beats[0]["entity_manifest"]["scene_visit_span"]["visit_sequence"] == ["A", "B", "C"]
    assert beats[0]["must_include"] == ["从A步行至B", "从B步行至C"]
    assert all(beat["entity_manifest"]["scene_visit_span"]["visit_sequence"] == ["C"] for beat in beats[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_repeated_location_anchor_respects_later_confirmed_location(monkeypatch, path):
    chapter_plan = itinerary_plan(3)
    for atom, location in zip(chapter_plan.atoms, ["A", "B", "C"]):
        atom.extensions = {"location_id": location}
    result = await execute_beats(monkeypatch, path, itinerary_scene(["A", "B", "A", "C"]), chapter_plan)
    assert result.status == NodeStatus.SUCCESS, result.error
    assert [beat["location_id"] for beat in result.outputs["beats"]] == ["A", "B", "C"]
    assert result.outputs["beats"][2]["must_include"] == ["从B步行至A", "从A步行至C"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
async def test_unique_multi_edge_passage_expands_abbreviated_explicit_visits(monkeypatch, path):
    analysis = itinerary_scene(["A", "B", "C"])
    analysis.action_transition_graph.visit_sequence = ["A", "C"]
    result = await execute_beats(monkeypatch, path, analysis, itinerary_plan(2))
    assert result.status == NodeStatus.SUCCESS, result.error
    assert result.outputs["beats"][1]["must_include"] == ["从A步行至B", "从B步行至C"]
    assert result.outputs["beats"][0]["entity_manifest"]["scene_director"]["resolved_visit_sequence"] == ["A", "B", "C"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("kind", ["branch", "disconnected", "unreachable_visit"])
async def test_ambiguous_or_inconsistent_transition_graph_cannot_invent_a_route(monkeypatch, path, kind):
    analysis = itinerary_scene(["A", "B", "C"])
    graph = analysis.action_transition_graph
    graph.nodes[0].is_entry_point = True
    if kind == "branch":
        graph.transitions.append(graph.transitions[0].model_copy(update={"target_location": "C"}))
        graph.visit_sequence = []
    elif kind == "disconnected":
        graph.transitions.pop()
        graph.visit_sequence = []
    else:
        graph.visit_sequence = ["C", "A"]
    if path == "production":
        with pytest.raises(ValueError, match="visit_sequence"):
            await execute_beats(monkeypatch, path, analysis, itinerary_plan(2))
    else:
        result = await execute_beats(monkeypatch, path, analysis, itinerary_plan(2))
        assert result.status == NodeStatus.ERROR and "visit_sequence" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("locations", [["X", "C"], ["C", "A"], ["A", "B"]])
async def test_conflicting_plan_locations_are_rejected_without_overwriting(monkeypatch, path, locations):
    chapter_plan = itinerary_plan(2)
    for atom, location in zip(chapter_plan.atoms, locations):
        atom.extensions = {"location_id": location}
    if path == "production":
        with pytest.raises(ValueError, match="已确认"):
            await execute_beats(monkeypatch, path, itinerary_scene(["A", "B", "C"]), chapter_plan)
    else:
        result = await execute_beats(monkeypatch, path, itinerary_scene(["A", "B", "C"]), chapter_plan)
        assert result.status == NodeStatus.ERROR and "已确认" in result.error
    assert [atom.extensions["location_id"] for atom in chapter_plan.atoms] == locations


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["direct", "production"])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("sequence", [["A", "B", "C"], ["A", "B", "A"]])
@pytest.mark.parametrize("explicit_first", [False, True])
@pytest.mark.parametrize("inferred_route", [False, True])
async def test_intermediate_atg_obligations_reach_real_writer_prompt(hook_db, monkeypatch, path, count, sequence, explicit_first, inferred_route):
    builder = _make_builder()
    monkeypatch.setattr("application.engine.dag.nodes.execution_nodes._dag_context_builder", lambda: builder)
    monkeypatch.setattr(WriterNode, "resolve_prompt", lambda *_: {"system": "正文规则", "user": "按计划写作"})
    seen = []
    class Provider:
        async def generate(self, prompt, _config):
            seen.append(prompt)
            return "人物取得道具，沿路步行进入下一地点。"
    svc = DAGChapterServices.__new__(DAGChapterServices)
    services = SimpleNamespace(db=hook_db, handles=lambda kind: path == "production" and kind == "exec_beat",
                               execute_node=svc.execute_node)
    analysis = itinerary_scene(sequence)
    if inferred_route:
        analysis.action_transition_graph.visit_sequence = []
        analysis.action_transition_graph.nodes.reverse()
        for location in analysis.action_transition_graph.nodes:
            location.is_entry_point = location.location_id == sequence[0]
    chapter_plan = itinerary_plan(count)
    if explicit_first:
        chapter_plan.atoms[0].extensions = {"location_id": "B"}
    result = await DAGEngine().run(graph([
        NodeDefinition(id="scene", type="exec_scene"), NodeDefinition(id="beat", type="exec_beat"),
        NodeDefinition(id="writer", type="exec_writer")], [
        edge("scene", "beat", source_port="scene_analysis", target_port="scene_analysis"),
        edge("beat", "writer", source_port="beats", target_port="beats"),
    ]), {"novel_id": "one", "chapter_number": 1, "outline": "沿访问顺序行动",
        "target_chapter_words": 5000, "chapter_plan_json": chapter_plan.model_dump(mode="json"),
        "scene_director_service": SimpleNamespace(analyze=AsyncMock(return_value=analysis)),
        "llm_service": Provider(), "services": services})
    assert result.status == "completed", result.node_results
    assert len(seen) == 1
    prompt_text = seen[0].system + seen[0].user
    for transition in analysis.action_transition_graph.transitions:
        assert transition.required_action in prompt_text
        assert all(character in prompt_text for character in transition.trigger_characters)
    assert all(f"{location}道具" in prompt_text for location in sequence)
    assert "scene_visit_span" in prompt_text and "action_transition_graph" in prompt_text
    assert prompt_text.count('"action_transition_graph"') == 1
    assert result.node_results["beat"].outputs["beats"][0]["entity_manifest"]["scene_director"]["resolved_visit_sequence"] == sequence
    if explicit_first:
        assert result.node_results["beat"].outputs["beats"][0]["location_id"] == "B"


@pytest.mark.asyncio
async def test_scene_node_rejects_transition_only_graph_before_downstream_writer(node_factory):
    downstream = node_factory("scene_beats", lambda *_: pytest.fail("Indeterminate ATG must block downstream"))
    analysis = SceneDirectorAnalysis.model_validate({"action_transition_graph": {"transitions": [
        {"source_location": "A", "target_location": "B", "required_action": "从A步行至B"}
    ]}})
    result = await DAGEngine().run(graph([NodeDefinition(id="scene", type="exec_scene"), downstream],
        [edge("scene", "scene_beats")]), {"outline": "场景梗概", "chapter_number": 1,
        "scene_director_service": SimpleNamespace(analyze=AsyncMock(return_value=analysis))})
    assert result.status == "error"
    assert result.node_results["scene"].status == NodeStatus.ERROR
    assert "缺少可确定访问顺序" in result.node_results["scene"].error
    assert result.node_results["scene_beats"].status == NodeStatus.BYPASSED
