"""Conservative, reviewed runtime usage descriptions for the prompt plaza."""
from infrastructure.ai import prompt_keys as keys


# Reviewed against execute()/service calls, not merely NodeMeta.cpms_node_key.
_REFERENCE_ONLY = {
    keys.CONTEXT_BLUEPRINT, keys.CONTEXT_FORESHADOW, keys.CONTEXT_MEMORY,
    keys.CONTEXT_DEBT, keys.VOICE_STYLE_ANALYSIS, keys.VOICE_DRIFT,
    keys.FORESHADOW_CHECK, keys.KG_INFERENCE, keys.CIRCUIT_BREAKER,
    keys.REVIEW_GATEWAY, keys.CONDITION_GATEWAY, keys.RETRY_GATEWAY,
    keys.MACRO_PLANNING,
}
_RUNTIME = {
    keys.BIBLE_ALL, keys.BIBLE_WORLDBUILDING, keys.BIBLE_CHARACTERS,
    keys.BIBLE_LOCATIONS, keys.BIBLE_STYLE_CONVENTION,
    keys.CHAPTER_GENERATION_MAIN, keys.CHAPTER_GENERATION_BASIC, keys.CHAPTER_PROSE_GENERATION,
    keys.CHAPTER_NARRATIVE_SYNC, keys.CHAPTER_STATE_EXTRACTION, keys.CHAPTER_SUMMARIZER,
    keys.CHAPTER_BRIDGE_EXTRACT, keys.CHAPTER_BRIDGE_CHECK, keys.CHAPTER_BRIDGE_FIX,
    keys.CHAPTER_END_EVIDENCE_REPAIR, keys.SCENE_GENERATION, keys.SCENE_DIRECTOR,
    keys.SCRIPT_GENERATION, keys.PROSE_FROM_SCRIPT, keys.DIALOGUE_GENERATION,
    keys.PROP_EVENT_EXTRACTION, keys.REVIEW_CHARACTER_CONSISTENCY,
    keys.REVIEW_TIMELINE_CONSISTENCY, keys.REVIEW_STORYLINE_CONSISTENCY,
    keys.REVIEW_FORESHADOWING_USAGE, keys.REVIEW_IMPROVEMENT_SUGGESTIONS,
    keys.CHAPTER_AI_REVIEW, keys.MEMORY_EXTRACTION, keys.EMOTION_LEDGER_EXTRACTION,
    keys.BEAT_SHEET_DECOMPOSITION, keys.OUTLINE_BEAT_PARTITION, keys.BEAT_COT_BRIDGE,
    keys.PLANNING_QUICK_MACRO, keys.PLANNING_PRECISE_MACRO,
    keys.PLANNING_PRECISE_VOLUME, keys.PLANNING_PRECISE_REPAIR, keys.PLANNING_ACT,
    keys.PLANNING_CHAPTER_PREPLAN, keys.CONTINUOUS_PLANNING_NEXT_ACT,
    keys.PLANNING_MAIN_PLOT_OPTION, keys.PLANNING_PLOT_OUTLINE,
    keys.SUMMARY_CHECKPOINT, keys.SUMMARY_ACT, keys.SUMMARY_VOLUME, keys.SUMMARY_PART,
    keys.STYLE_ANALYSIS, keys.VOICE_BASELINE_ANALYSIS, keys.VOICE_REWRITE,
    keys.TENSION_SCORING, keys.TENSION_ANALYSIS_DIAGNOSIS, keys.KNOWLEDGE_INITIAL,
    keys.WORKFLOW_CHAPTER_GENERATION, keys.AUTOPILOT_STREAM_BEAT,
    keys.BEAT_FOCUS_INSTRUCTIONS, keys.LIFECYCLE_PHASE_DIRECTIVES,
    keys.REFACTOR_PROPOSAL_MACRO, keys.ANTI_AI_BEHAVIOR_PROTOCOL,
    keys.ANTI_AI_ALLOWLIST_EXPLAIN, keys.ANTI_AI_CHARACTER_STATE_LOCK,
    keys.ANTI_AI_FINALE_ENHANCEMENT,
}


def get_prompt_usage(node_key: str, *, is_builtin: bool) -> dict:
    if node_key == keys.ANTI_AI_CHAPTER_AUDIT:
        return {"status": "runtime", "label": "已接入语义审稿", "description": "新书与正文语义审稿均读取本模板的当前激活版本作为附加文风规范，并记录实际版本与原文。固定审稿核心继续约束完整覆盖、原文证据和输出格式；修改不替换这些核心规则。"}
    if node_key == keys.CLICHE_SCAN:
        return {"status": "reference_only", "label": "复用共享审稿规范", "description": "本模板未作为独立模型请求执行；当前 DAG 的 AI 味审计复用固定语义审稿核心及 anti-ai-chapter-audit 的附加规范。请编辑 anti-ai-chapter-audit 调整共享模型审稿规则，本地与尾段检查仍独立执行。"}
    if node_key == keys.CHAPTER_AFTERMATH:
        return {"status": "mixed", "label": "按路径生效", "description": "旧托管调用规格可使用本模板；当前 DAG 叙事同步委托独立处理管线，不能仅凭 DAG 关联确认本模板被调用。"}
    if node_key == keys.ANTI_AI_MID_GENERATION_REFRESH:
        return {"status": "limited", "label": "生成前片段", "description": "DAG 读取本模板提供刷新约束片段；当前正文非流式路径仅在生成前注入，不执行 Token 级中途拦截。"}
    if node_key in _REFERENCE_ONLY:
        return {"status": "reference_only", "label": "仅关联说明", "description": "该 DAG 节点执行固定逻辑或委托其他服务，当前未读取本模板。DAG 关联表示用途，不表示保存后会影响执行。"}
    if node_key in _RUNTIME:
        return {"status": "runtime", "label": "已接入调用路径", "description": "已有生成、提取或审稿服务读取本模板的当前激活版本；仅在对应功能被执行时使用，DAG 节点覆盖配置可能优先生效。"}
    if not is_builtin:
        return {"status": "unbound", "label": "未自动接入", "description": "新建或导入提示词不会自动用于正文生成或审稿。需由调用规格、工作流或业务功能明确引用。"}
    return {"status": "available", "label": "待核对调用路径", "description": "已收录模板；不能据此确认运行时使用。"}
