"""Freeze existing Anti-AI rules into prose prompts before review/provider use.

Each call owns its character vectors and budget. No global novel state or
manuscript is stored in receipts. Core rules are protected; an input that
cannot fit fails explicitly rather than silently losing facts or constraints.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
from uuid import uuid4

from application.engine.rules.rule_parser import RuleParser
from application.engine.rules.character_state_vector import CharacterStateVectorManager
from application.engine.rules.allowlist_manager import get_allowlist_manager
from domain.ai.services.token_budget import estimate_text_tokens
from domain.ai.value_objects.prompt import Prompt
from application.core.taxonomy.market_prompt import load_verified_market_profile

logger = logging.getLogger(__name__)
LAYER_KEYS = ("L1_positive_framing", "L2_protocol_rules", "L3_allowlist_scenes", "L4_state_vector", "L5_context_quota")
HOOK_VERSION = "prose-prompt-v1"


def _hash(prompt):
    from application.ai_invocation.dtos import prompt_hash
    return prompt_hash(prompt)


class ProsePromptHooks:
    def __init__(self, db=None):
        from infrastructure.persistence.database.connection import get_database
        self.db = db or get_database()

    def validate_current_market_profile(self, evidence, context=None):
        """An approved prompt cannot silently retain an obsolete project profile."""
        snapshot, block = load_verified_market_profile(self.db, str((context or {}).get("novel_id") or ""))
        from application.core.taxonomy.market_selection import profile_snapshot_hash
        expected = profile_snapshot_hash(snapshot) if block else None
        if not isinstance(evidence, dict) or evidence.get("market_profile_hash") != expected:
            raise RuntimeError("市场画像已变化，请重新编译并审阅提示词后生成")

    def compose(self, prompt: Prompt, context=None):
        context = dict(context or {})
        novel_id = str(context.get("novel_id") or "")
        market_snapshot, profile_block = load_verified_market_profile(self.db, novel_id)
        if profile_block and profile_block not in prompt.system and profile_block not in prompt.user:
            prompt = Prompt(system=prompt.system.rstrip() + "\n\n" + profile_block, user=prompt.user)
        policy_scene = "default"
        if novel_id:
            from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
            policy_scene = ChapterRewriteCandidates(self.db).get_policy(novel_id).get("scene_type", "default")
        scene_type = str(context.get("scene_type") or policy_scene)
        if scene_type not in get_allowlist_manager().list_scene_types():
            raise ValueError("未配置该场景的去味规则")
        budget = context.get("anti_ai_input_budget_tokens", 32000)
        if type(budget) is not int or budget <= 0:
            raise ValueError("去味输入预算必须为正整数")
        # The deterministic parser contains the actual P1-P5 and replacement
        # rules. Several old CPMS subnodes contain only an instruction to
        # generate rules; injecting that system stub does not inject rules.
        vectors = CharacterStateVectorManager()
        parser = RuleParser(state_vector_manager=vectors)
        protocol = parser.build_behavior_protocol_block(scene_type="default")
        allowlist = get_allowlist_manager().generate_allowlist_block(scene_type)
        core = "【去味执行协议 · " + HOOK_VERSION + "】\n" + protocol + "\n\n" + allowlist
        protected_tokens = estimate_text_tokens(prompt.system + prompt.user + core)
        if protected_tokens > budget:
            raise ValueError(f"正文输入及去味核心协议超出预算（估算 {protected_tokens}/{budget} tokens）；请缩减上下文或增加输入预算")
        selected, source, state_error = [], "none", None
        if novel_id:
            try:
                from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
                rows = [vars(item) for item in SqliteUnifiedCharacterRepository(self.db).list_by_novel(novel_id)]
                source = "unified_characters"
                if not rows:
                    rows = [dict(row) for row in self.db.fetch_all("SELECT * FROM bible_characters WHERE novel_id=?", (novel_id,))]
                    source = "bible_characters"
                names = context.get("character_names") or context.get("cast_names") or []
                names = {str(name) for name in names} if isinstance(names, (list, tuple, set)) else set()
                text = prompt.system + "\n" + prompt.user
                selected = [row for row in rows if row.get("name") and (row["name"] in names or row["name"] in text)][:7]
            except Exception as exc:
                state_error = str(exc)
        locks, used_names = [], []
        remaining = min(1500, budget - protected_tokens)
        for row in selected:
            name = row["name"]
            state_is_current = not context.get("chapter_number") or int(row.get("last_updated_chapter") or 0) < int(context["chapter_number"])
            data = {"physical_state": "沿用已提交正文，不推测恢复", "emotional_baseline": str(row.get("mental_state_reason") or row.get("mental_state") or "未提供") if state_is_current else "沿用本章上文，不引入后续章节状态"}
            voice = str(row.get("voice_style") or row.get("sentence_pattern") or "")
            if voice or row.get("verbal_tic"):
                data["voice_print"] = {"description": voice, "common_expressions": [str(row["verbal_tic"])] if row.get("verbal_tic") else []}
            if row.get("idle_behavior"):
                data["nervous_habit"] = {"primary": str(row["idle_behavior"])}
            vectors.update_from_bible(name, data)
            vector = vectors.get_or_create(name)
            vector.voice_print = vector.voice_print if "voice_print" in data else None
            vector.nervous_habit = vector.nervous_habit if "nervous_habit" in data else None
            vector.reaction_pattern = None
            block = parser.build_character_state_lock_block([name])
            count = estimate_text_tokens(block)
            if count <= remaining:
                locks.append(block)
                used_names.append(name)
                remaining -= count
        character_block = "\n\n".join(locks)
        composed = Prompt(system=prompt.system.rstrip() + "\n\n" + core + ("\n\n" + character_block if character_block else ""), user=prompt.user)
        actual = estimate_text_tokens(composed.system + composed.user)
        if actual > budget:
            raise ValueError("去味上下文组装后超出输入预算")
        states = {key: {"status": "applied", "applied": True} for key in LAYER_KEYS}
        states["L4_state_vector"] = {"status": "applied" if locks else "unavailable", "applied": bool(locks),
            "source": source, "character_count": len(used_names), "skipped_character_count": len(selected) - len(used_names),
            "reason": state_error or (None if locks else "本次上下文未匹配到角色锚点")}
        evidence = {"hook_version": HOOK_VERSION, "prompt_hash": _hash(composed),
            "rule_version": hashlib.sha256(core.encode()).hexdigest(), "scene_type": scene_type,
            "input_token_estimate": actual, "input_budget_tokens": budget,
            "budget_mode": "protected_core_and_whole_character_blocks", "layers": states}
        if profile_block:
            from application.core.taxonomy.market_selection import profile_snapshot_hash
            evidence["market_profile_hash"] = profile_snapshot_hash(market_snapshot)
        return composed, evidence

    def freeze(self, snapshot, context):
        from application.ai_invocation.dtos import prompt_hash, stable_hash
        prompt, evidence = self.compose(snapshot.prompt, context)
        return replace(snapshot, prompt=prompt, rendered_prompt_hash=prompt_hash(prompt),
            composition_hash=stable_hash({"base": snapshot.composition_hash, "anti_ai": evidence})), evidence


def archive_prompt_use(db, evidence, *, context=None, job_id="", status="completed", error=None):
    """One durable receipt after an actual provider attempt, without prompt text."""
    context = dict(context or {})
    receipt = {**evidence, "run_id": uuid4().hex, "job_id": job_id, "status": status, "error": error}
    try:
        with db.transaction() as writer:
            writer.execute("""INSERT INTO anti_ai_prompt_runs
                (run_id,novel_id,chapter_number,job_id,status,prompt_hash,rule_version,evidence_json,error)
                VALUES (?,?,?,?,?,?,?,?,?)""", (receipt["run_id"], str(context.get("novel_id") or ""), context.get("chapter_number"),
                job_id,status,evidence["prompt_hash"],evidence["rule_version"],json.dumps(receipt,ensure_ascii=False),error))
        receipt["persistence_status"] = "committed"
    except Exception as exc:
        receipt.update(persistence_status="failed", archive_error=str(exc))
        logger.warning("去味提示词执行回执未归档 job=%s: %s", job_id, exc)
    return receipt


def prompt_evidence_matches(prompt, evidence):
    return bool(evidence and evidence.get("hook_version") == HOOK_VERSION and evidence.get("prompt_hash") == _hash(prompt))


async def generate_direct_prose(provider, prompt, config, *, context, db=None, evidence_sink=None, content_transform=None):
    """Non-stream providers still consume the real L1-L5 frozen prompt."""
    if not str(context.get("novel_id") or "").strip() or type(context.get("chapter_number")) is not int or context["chapter_number"] <= 0:
        raise ValueError("正文缺少有效小说或章节号，无法绑定去味回执")
    hooks = ProsePromptHooks(db)
    from application.core.services.chapter_derivation_readiness import assert_previous_derivations_ready
    assert_previous_derivations_ready(hooks.db, context["novel_id"], context["chapter_number"])
    composed, evidence = hooks.compose(prompt, context)
    job_id = str(context.get("job_id") or uuid4().hex)
    if context.get("dag_injections"):
        evidence["dag_injections"] = context["dag_injections"]
    status, error = "failed", None
    try:
        result = await provider.generate(composed, config)
        body = result if isinstance(result, str) else getattr(result, "content", None)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("LLM returned empty or invalid content")
        if content_transform is not None:
            body = content_transform(body)
            if not isinstance(body, str) or not body.strip():
                raise ValueError("正文清洗后为空或无效")
            if isinstance(result, str):
                result = body
            else:
                from copy import copy
                result = copy(result)
                result.content = body
        evidence["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        from application.ai_invocation.prose_execution_evidence import archive_prose_scan_receipt
        evidence["anti_ai_scan"] = archive_prose_scan_receipt(
            hooks.db,
            novel_id=context.get("novel_id"),
            chapter_number=context.get("chapter_number"),
            attempt_id=job_id,
            content=body,
            scene_type=evidence.get("scene_type", "default"),
        )
        status = "completed"
        return result
    except BaseException as exc:
        import asyncio
        status, error = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", str(exc)
        raise
    finally:
        receipt = archive_prompt_use(hooks.db, evidence, context=context,
            job_id=job_id, status=status, error=error)
        receipt["anti_ai_scan"] = dict(evidence.get("anti_ai_scan") or {})
        if evidence_sink is not None:
            evidence_sink.clear()
            evidence_sink.update(receipt)
        if status == "completed" and receipt.get("persistence_status") != "committed":
            raise RuntimeError("去味提示词执行回执未持久化，正文不可采纳")
