"""Chapter beat projection helpers.

This module is the single projection boundary from planning artifacts to the
runtime beat shape used by generation, shared state, and chapter summaries.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from application.engine.dag.plan.schema import ChapterExecutionPlan
from application.engine.dtos.scene_director_dto import ActionTransitionGraphPayload, SceneDirectorAnalysis
from application.engine.services.beat_models import Beat
from engine.pipeline.beat_contracts import serialize_beats_for_shared_state

logger = logging.getLogger(__name__)

OUTLINE_OBLIGATION_PREFIX = (
    "【章纲节选·须落实】以下要点必须写入正文（可合理扩写，不得跳过核心因果；"
    "人物姓名须与 Bible 一致）：\n"
)

FocusInferer = Callable[[str], str]
ExpansionHintBuilder = Callable[[str, int], List[str]]


def _string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _string(value: Any) -> str:
    return str(value).strip() if value not in (None, [], {}) else ""


def _default_function(intent: str, focus: str) -> str:
    text = intent or ""
    if any(k in text for k in ("突袭", "逼", "压制", "杀机", "遭遇")):
        return "pressure"
    if any(k in text for k in ("爆发", "震碎", "击败", "破局", "反制")):
        return "payoff"
    if any(k in text for k in ("发现", "徽记", "真相", "计划", "芯片")):
        return "reveal"
    if any(k in text for k in ("逃离", "离开", "转移", "赶往")):
        return "transition"
    return focus or "setup"


def _summarize_intent(intent: str, max_len: int = 48) -> str:
    """Short intent snippet for default contract fields (avoid repeating full paragraph)."""
    text = (intent or "").strip()
    if not text:
        return ""
    for sep in ("。", "！", "？", "；", ";", "\n"):
        idx = text.find(sep)
        if idx >= 8:
            text = text[: idx + 1]
            break
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip("，。；; ") + "…"
    return text


def _default_visible_action(intent: str) -> str:
    summary = _summarize_intent(intent)
    if not summary:
        return "用可见动作、对白或选择落实本拍意图"
    return f"用可见动作、对白或选择落实：{summary}"


def _default_delta(intent: str) -> str:
    summary = _summarize_intent(intent)
    if not summary:
        return "本拍结束时，读者必须看到局势发生明确变化"
    return f"本拍结束时，局势须因「{summary}」出现可感变化"


def beat_sheet_to_plan_json(beat_sheet: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Project a repository BeatSheet into the canonical plan input JSON."""
    if not beat_sheet:
        return None
    scenes_raw = getattr(beat_sheet, "scenes", None)
    if not scenes_raw:
        return None

    scenes: List[Dict[str, Any]] = []
    for scene in scenes_raw:
        scenes.append(
            {
                "title": getattr(scene, "title", "") or "",
                "goal": getattr(scene, "goal", "") or "",
                "estimated_words": getattr(scene, "estimated_words", None) or 600,
                "pov_character": getattr(scene, "pov_character", "") or "",
                "location": getattr(scene, "location", None),
                "tone": getattr(scene, "tone", None),
                "transition_from_prev": getattr(scene, "transition_from_prev", None),
            }
        )
    return {"scenes": scenes}


def beats_from_execution_plan(
    plan: ChapterExecutionPlan,
    *,
    outline: str,
    target_chapter_words: int,
    infer_focus: FocusInferer,
    build_expansion_hints: ExpansionHintBuilder,
) -> List[Beat]:
    """Project ``ChapterExecutionPlan.atoms`` into runtime Beats."""
    atoms = plan.atoms
    if not atoms:
        return []

    total_weight = sum(max(0.01, float(atom.weight)) for atom in atoms)
    mode = (plan.provenance or {}).get("mode", "")
    logger.info(
        "节拍投影（ChapterExecutionPlan）：%d 拍，provenance_mode=%s outline≈%d 字，整章目标 %d 字",
        len(atoms),
        mode,
        len((outline or "").strip()),
        target_chapter_words,
    )

    beats: List[Beat] = []
    for atom in atoms:
        intent = (atom.intent or "").strip()
        if not intent:
            continue

        share = max(0.01, float(atom.weight)) / total_weight
        target_words = max(1, int(target_chapter_words * share))
        ext = atom.extensions if isinstance(atom.extensions, dict) else {}

        raw_focus = ext.get("focus") or ext.get("type")
        focus = raw_focus.strip() if isinstance(raw_focus, str) and raw_focus.strip() else infer_focus(intent)
        function = _string(ext.get("function")) or _default_function(intent, focus)

        transition_raw = ext.get("transition_from_prev")
        transition = str(transition_raw).strip() if transition_raw else ""

        location_raw = ext.get("location_id")
        location_id = (
            str(location_raw).strip()
            if isinstance(location_raw, str) and location_raw.strip()
            else ""
        )
        location_refs = _string_list(ext.get("location_refs"))
        if not location_id and location_refs:
            location_id = location_refs[0]

        beats.append(
            Beat(
                description=intent,
                target_words=target_words,
                focus=focus,
                expansion_hints=build_expansion_hints(focus, target_words),
                scene_goal=intent,
                transition_from_prev=transition,
                location_id=location_id,
                function=function,
                pov=_string(ext.get("pov")),
                cast_refs=_string_list(ext.get("cast_refs")),
                location_refs=location_refs,
                prop_refs=_string_list(ext.get("prop_refs")),
                knowledge_refs=_string_list(ext.get("knowledge_refs")),
                visible_action=_string(ext.get("visible_action") or ext.get("active_action")) or _default_visible_action(intent),
                conflict=_string(ext.get("conflict")),
                delta=_string(ext.get("delta") or ext.get("required_delta")) or _default_delta(intent),
                handoff_to_next=_string(ext.get("handoff_to_next")),
                must_include=_string_list(ext.get("must_include")),
                must_not_include=_string_list(ext.get("must_not_include")),
            )
        )
    return beats


def planned_micro_beats_from_beats(beats: List[Any]) -> List[Dict[str, Any]]:
    """Serialize runtime Beats for /status, SSE, and chapter summary snapshots."""
    return serialize_beats_for_shared_state(beats)


def scene_director_for_beats(raw: Any) -> Optional[SceneDirectorAnalysis]:
    """Validate optional scene-node JSON before it reaches either beat adapter."""
    if raw is None or raw == {}:
        return None
    if isinstance(raw, SceneDirectorAnalysis):
        scene = raw
    else:
        if not isinstance(raw, dict):
            raise ValueError("scene_analysis 必须为场景分析对象")
        unknown = set(raw) - set(SceneDirectorAnalysis.model_fields)
        if unknown:
            raise ValueError(f"scene_analysis 包含未知字段: {sorted(unknown)}")
        scene = SceneDirectorAnalysis.model_validate(raw)

    # A default DTO is also used when no scene analysis was requested. Do not
    # turn that absence into a fabricated scene manifest or context filter.
    text_fields = (
        scene.characters, scene.locations, scene.action_types,
        scene.trigger_keywords, scene.performance_notes or [],
        [scene.emotional_state, scene.pov or ""],
    )
    graph = scene.action_transition_graph
    if graph is not None:
        for edge in graph.transitions:
            if (edge.source_location.strip() or edge.target_location.strip() or edge.required_action.strip()
                    or any(name.strip() for name in edge.trigger_characters)) and not (
                        edge.source_location.strip() and edge.target_location.strip()):
                raise ValueError("scene_analysis 的 ATG 转场边包含实际约束但缺少起点或终点")
        for node in graph.nodes:
            if not node.location_id.strip() and (node.is_entry_point or any(prop.strip() for prop in node.initial_props)):
                raise ValueError("scene_analysis 的 ATG 节点包含入口或道具约束但缺少地点")
    if graph is not None and any(
        edge.source_location.strip() and edge.target_location.strip() for edge in graph.transitions
    ) and not (
        any(node.location_id.strip() for node in graph.nodes)
        or any(item.strip() for item in graph.visit_sequence)
    ):
        raise ValueError("scene_analysis 的 ATG 只有转移边，缺少可确定访问顺序的地点或 visit_sequence")
    if any(item.strip() for values in text_fields for item in values):
        return scene
    if graph is not None and (
        any(node.location_id.strip() for node in graph.nodes)
        or any(item.strip() for item in graph.visit_sequence)
        or any(edge.source_location.strip() and edge.target_location.strip()
               for edge in graph.transitions)
    ):
        return scene
    return None


def scene_visit_sequence(graph: ActionTransitionGraphPayload) -> List[str]:
    """Use explicit visits, or infer one unambiguous walk from real edges."""
    nodes = list(dict.fromkeys(node.location_id.strip() for node in graph.nodes if node.location_id.strip()))
    entries = list(dict.fromkeys(node.location_id.strip() for node in graph.nodes
                                if node.is_entry_point and node.location_id.strip()))
    edges = set()
    for edge in graph.transitions:
        source, target = edge.source_location.strip(), edge.target_location.strip()
        if (source or target or edge.required_action.strip() or any(name.strip() for name in edge.trigger_characters)):
            if not source or not target:
                raise ValueError("场记 ATG 的转场边缺少起点或终点")
            edges.add((source, target))
    explicit = [location.strip() for location in graph.visit_sequence if location.strip()]
    if not edges:
        return explicit or list(dict.fromkeys(entries + nodes))
    if explicit:
        sequence = [explicit[0]]
        for source, target in zip(explicit, explicit[1:]):
            if source == target or (source, target) in edges:
                sequence.append(target)
                continue
            # A declared jump may abbreviate a unique multi-edge passage, but
            # choosing one branch of an ambiguous graph would invent a scene.
            reachable = {target}
            pending = [target]
            while pending:
                current = pending.pop()
                for start, end in edges:
                    if end == current and start not in reachable:
                        reachable.add(start)
                        pending.append(start)
            path = [source]
            while path[-1] != target:
                candidates = sorted(end for start, end in edges if start == path[-1] and end in reachable)
                if len(candidates) != 1 or candidates[0] in path:
                    break
                path.append(candidates[0])
            if path[-1] != target:
                raise ValueError(f"场记访问跳转 {source!r} → {target!r} 缺少唯一可说明路径，请补充 visit_sequence")
            sequence.extend(path[1:])
        return sequence
    sources = {source for source, _ in edges} - {target for _, target in edges}
    starts = entries or sorted(sources)
    if len(starts) != 1:
        raise ValueError("场记 ATG 无法确定唯一入口，请补充 visit_sequence")
    sequence = [starts[0]]
    remaining = set(edges)
    while remaining:
        outgoing = [(source, target) for source, target in remaining if source == sequence[-1]]
        if len(outgoing) != 1:
            raise ValueError("场记 ATG 存在分叉或不连通路径，请补充 visit_sequence")
        edge = outgoing[0]
        remaining.remove(edge)
        sequence.append(edge[1])
    if set(nodes) - set(sequence):
        raise ValueError("场记 ATG 包含未连通地点，请补充 visit_sequence")
    return sequence


def apply_scene_director_to_beats(beats: List[Any], scene_director: Any) -> None:
    """Carry scene metadata and ATG obligations into the writer's beat JSON."""
    scene = scene_director_for_beats(scene_director)
    if scene is None or not beats:
        return
    graph = scene.action_transition_graph
    domain_graph = graph.to_domain() if graph is not None else None
    sequence = []
    anchors = []
    if graph is not None:
        from application.engine.services.spatial_coherence import assign_visit_locations_to_beats, visit_anchor_indices
        sequence = scene_visit_sequence(graph)
        assign_visit_locations_to_beats(beats, sequence)
        anchors = visit_anchor_indices(beats, sequence)
        if len(beats) > 1 and anchors and anchors[-1] != len(sequence) - 1:
            raise ValueError("已确认的末拍地点与场记访问顺序终点不一致")

    # Keep the whole graph once for the writer, including constraints outside
    # a sampled anchor. Bind every adjacent transition in each covered span;
    # endpoint lookup alone would lose A -> B -> C when only two beats exist.
    metadata = scene.model_dump(mode="json", exclude={"action_transition_graph"})
    previous_index = 0
    for index, beat in enumerate(beats):
        if not beat.pov:
            beat.pov = (scene.pov or "").strip()
        if not beat.cast_refs:
            beat.cast_refs = [name.strip() for name in scene.characters if name.strip()]
        manifest = {**(beat.entity_manifest or {}), "scene_director": dict(metadata)}
        if graph is not None and index == 0:
            manifest["scene_director"]["action_transition_graph"] = graph.model_dump(mode="json")
            manifest["scene_director"]["resolved_visit_sequence"] = sequence
        new_locations = []
        if domain_graph is not None and sequence:
            end_index = anchors[index]
            if index == len(beats) - 1:
                end_index = len(sequence) - 1
            span = sequence[previous_index:end_index + 1]
            new_locations = span if index == 0 or previous_index == end_index else span[1:]
            transitions = [edge for source, target in zip(span, span[1:])
                           for edge in domain_graph.transitions
                           if edge.source_location == source and edge.target_location == target]
            manifest["scene_visit_span"] = {
                "visit_sequence": span,
                "transitions": [{"source_location": edge.source_location,
                                 "target_location": edge.target_location,
                                 "required_action": edge.required_action,
                                 "trigger_characters": list(edge.trigger_characters)}
                                for edge in transitions],
            }
            for location in new_locations:
                node = domain_graph.node(location)
                if node is not None:
                    beat.prop_refs = list(dict.fromkeys(list(beat.prop_refs) + list(node.initial_props)))
            for transition in transitions:
                if transition.required_action.strip():
                    beat.must_include = list(dict.fromkeys(list(beat.must_include) + [transition.required_action.strip()]))
                beat.cast_refs = list(dict.fromkeys(list(beat.cast_refs) + list(transition.trigger_characters)))
            previous_index = end_index
        if beat.location_id:
            beat.location_refs = list(dict.fromkeys([beat.location_id] + list(beat.location_refs) + new_locations))
        elif not beat.location_refs:
            beat.location_refs = [location.strip() for location in scene.locations if location.strip()]
        beat.entity_manifest = manifest


__all__ = [
    "OUTLINE_OBLIGATION_PREFIX",
    "beat_sheet_to_plan_json",
    "beats_from_execution_plan",
    "planned_micro_beats_from_beats",
    "scene_director_for_beats",
    "scene_visit_sequence",
    "apply_scene_director_to_beats",
]
