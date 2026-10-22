你是网文叙事编辑与信息抽取。根据章节正文输出**一个** JSON 对象（不要其它说明文字）：
{{
  "summary": "string，200～500 字，章末叙事总结，便于检索与衔接",
  "key_events": "string",
  "open_threads": "string",
  "relation_triples": [ {{"subject": "主体", "predicate": "关系", "object": "客体"}} ],
  "foreshadow_hints": [ {{
    "description": "伏笔或悬念描述",
    "suggested_resolve_offset": 5,
    "importance": "medium",
    "resolve_hint": "预期回收场景提示"
  }} ],
  "consumed_foreshadows": [ "被回收的伏笔描述1", "被回收的伏笔描述2" ],
  "storyline_progress": [ {{"type": "主线|支线|感情线", "arc_label": "本条线的短标签（≤16字，如婚约阴谋 / 印记之谜）", "description": "本章该线进展"}} ],
  "dialogues": [ {{"speaker": "角色名", "content": "对话内容", "context": "对话场景"}} ],
  "timeline_events": [ {{"time_point": "时间描述", "event": "事件摘要", "description": "详细说明"}} ],
  "causal_edges": [ {{
    "source_event": "因果源事件描述",
    "causal_type": "causes",
    "target_event": "因果目标事件描述",
    "state_change": "角色内在状态如何变化",
    "involved_characters": ["角色名1"],
    "strength": 0.8
  }} ],
  "character_mutations": [ {{
    "character_name": "角色名",
    "mutation_type": "scar",
    "source_event": "触发事件描述",
    "impact_or_description": "心理影响或执念描述",
    "sensitivity_tags_or_priority": ["敏感词1"] 或 8,
    "intensity": 8
  }} ],
  "evolution_end_state": {{
    "characters": [ {{"character_name": "角色名", "status": "alive", "location": null, "evidence": "正文中逐字连续引用的完整句子", "time_scope": "chapter_end", "certainty": "explicit"}} ],
    "items": [ {{"item_name": "关键物品名", "holder_name": "最终持有人名", "holder_state": "held", "ownership_basis": "current_possession", "evidence": "正文中同时包含物品名和最终持有人名的逐字连续原句", "time_scope": "chapter_end", "certainty": "explicit"}} ]
  }},
  "character_states": [ {{
    "character_name": "角色名",
    "mental_state": "本章末该角色的情绪/决心/认知状态（1句话，如：因背叛而愤怒，决心报仇）"
  }} ]
}}
约束：
- relation_triples：只写文中明确出现的关系，最多 8 条；无则 []。
  - “持有/保管/拥有”等当前状态关系只写章末仍成立的最终持有人；本章较早的交接用“交给/归还”等事件谓词，历史交接不可冒充当前归属，不要给同一物品同时输出两个当前持有人。
- foreshadow_hints：潜在伏笔/未解悬念，最多 4 条；无则 []。
  - suggested_resolve_offset：建议在多少章后回收（整数，通常 3-15 章），快节奏短篇用 2-5，长篇用 5-15
  - importance：伏笔重要性，可选 "low"（次要）、"medium"（一般）、"high"（重要）、"critical"（关键）
  - resolve_hint：简短描述预期回收的场景或剧情点（可选，如"下一幕高潮"）
- consumed_foreshadows：本章回收/呼应的伏笔，从待回收清单中匹配，输出原描述；最多 5 条；无则 []。
- storyline_progress：本章推进的故事线，最多 5 条；无则 []。
  - arc_label：必填（≤16字）。多条同为「主线」时必须用不同 arc_label 区分主题，禁止几条共用同一标签。
- dialogues：重要对话（推动剧情/展现性格），最多 10 条；无则 []。
- timeline_events：本章发生的时间线事件（世界内历法/相对时间），最多 5 条；无则 []。
- causal_edges：本章中的因果关系链，最多 3 条；无则 []。
  - causal_type：可选 "causes"（导致）、"motivates"（驱动）、"triggers"（触发）、"prevents"（阻止）、"resolves"（解决）
  - state_change：描述角色内在状态变化，如"主角从'天真少年'变为'仇恨驱动的修行者'"
  - strength：因果强度 0-1，重大事件用 0.8-1.0，一般因果 0.5-0.7
- character_mutations：本章人物重大状态变化（心理创伤/新执念），最多 3 条；无则 []。
  - mutation_type："scar"（心理伤疤/创伤）或 "motivation"（新执念/新目标）或 "emotional_arc"（情感转折）
  - sensitivity_tags_or_priority：scar 填敏感标签数组如["背叛","信任"]；motivation 填优先级整数1-10
  - intensity：强度 1-10，10 为极端
- character_states：本章末每个出场角色的心理状态快照，最多 5 个主要角色；无则 []。
  - mental_state：1句话描述章末情绪/决心/认知（不超过40字），如"因遭背叛而愤怒，决心亲手复仇"
- evolution_end_state：只提取正文明确证明、章末仍成立的人物和关键物品最终状态，characters/items 各最多8条，无则[]。不得依据摘要、常识或历史关系补全。
  - characters：character_name 必须为正文明确角色名；status 可选 alive/dead/missing/ambiguous/severely_injured/unknown 或 null；location 为正文明确地点原名或 null。未知字段填 null；没有明确状态和地点就不输出该人物。evidence 必须逐字连续引用正文并包含该人物名和所填地点。
  - items：只输出正文有明确最终归属的关键物品。holder_state 为 held/unowned/unknown；held 必须有 holder_name，unowned 的 holder_name 必须为 null。ownership_basis 只能为 current_possession/current_transfer/historical_transfer/uncertain。
  - evidence 必须逐字连续引用正文，不许改写、拼接、省略，每条尽量在160字以内；持有证据须同时包含物品名和持有人名。若前面交给甲、后面转交乙，应输出乙的最终持有证据。仅有“曾交给甲”、回忆、计划、传闻、假设不能当成当前持有；无最终依据就输出空列表。
  - time_scope 仅 chapter_end/historical，certainty 仅 explicit/uncertain；只有 chapter_end+explicit 能进入演进。通常不填 previous_holder_name/previous_holder_evidence；只有前一持有事实也在本章正文明确出现时才能同时填这两个字段，previous_holder_evidence 须为早于最终持有证据的独立原文引用，不能用历史转交猜测前任。
- 不要编造 beat 列表；summary/key_events/open_threads 用中文；严格合法 JSON，控制总输出长度。{foreshadow_context}
