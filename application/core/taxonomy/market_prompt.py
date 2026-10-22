"""Use the persisted project profile in planning and writing prompts."""
from __future__ import annotations

from dataclasses import replace
import json

from domain.ai.value_objects.prompt import Prompt
from .market_selection import profile_snapshot_hash, validate_market_preferences_for_generation


def load_verified_market_profile(db, novel_id):
    if not novel_id:
        return {}, ""
    row = db.fetch_one("SELECT generation_prefs_json FROM novels WHERE id=?", (novel_id,))
    if not row:
        return {}, ""
    raw = json.loads(row.get("generation_prefs_json") or "{}")
    if not isinstance(raw, dict):
        raise ValueError("小说生成偏好不是有效对象")
    snapshot = validate_market_preferences_for_generation(raw)
    labels = {"world_tone": "世界观基调", "story_structure": "剧情结构", "pacing_control": "节奏把控",
              "writing_style": "写作风格", "special_requirements": "特殊要求"}
    dimensions = snapshot.get("profile", {})
    lines = [f"{label}：{dimensions[key]}" for key, label in labels.items() if dimensions.get(key)]
    if not lines:
        return snapshot, ""
    header = ["【本项目最终写作画像】"]
    if snapshot.get("audience_mode"):
        header.append("目标读者频道：" + {"male": "男频", "female": "女频", "dual": "双频"}.get(snapshot["audience_mode"], snapshot["audience_mode"]))
    if snapshot.get("reader_contract"):
        lines.append("读者契约：" + snapshot["reader_contract"])
    return snapshot, "\n".join(header + lines)


def is_market_planning_operation(operation):
    return operation in {"setup.main_plot_options", "setup.plot_outline"} or str(operation).startswith("bible.setup.")


class MarketPromptHooks:
    def __init__(self, db=None):
        from infrastructure.persistence.database.connection import get_database
        self.db = db or get_database()

    def compose(self, prompt, context):
        from application.ai_invocation.dtos import prompt_hash
        snapshot, block = load_verified_market_profile(self.db, str((context or {}).get("novel_id") or ""))
        structured = bool(snapshot.get("canonical_genre_id") or snapshot.get("custom_market_profile"))
        if not structured:
            return prompt, {}
        if block not in prompt.system and block not in prompt.user:
            prompt = Prompt(system=prompt.system.rstrip() + "\n\n" + block, user=prompt.user)
        return prompt, {"profile_snapshot_hash": profile_snapshot_hash(snapshot), "prompt_hash": prompt_hash(prompt)}

    def freeze(self, snapshot, context):
        from application.ai_invocation.dtos import prompt_hash, stable_hash
        prompt, evidence = self.compose(snapshot.prompt, context)
        if evidence:
            snapshot = replace(snapshot, prompt=prompt, rendered_prompt_hash=prompt_hash(prompt),
                               composition_hash=stable_hash({"base": snapshot.composition_hash, "market_profile": evidence}))
        return snapshot, evidence

    def validate_frozen(self, prompt, context, evidence):
        from application.ai_invocation.dtos import prompt_hash
        snapshot, block = load_verified_market_profile(self.db, str((context or {}).get("novel_id") or ""))
        structured = bool(snapshot.get("canonical_genre_id") or snapshot.get("custom_market_profile"))
        if not structured and not evidence:
            return
        if (not evidence or evidence.get("profile_snapshot_hash") != profile_snapshot_hash(snapshot)
                or evidence.get("prompt_hash") != prompt_hash(prompt)
                or not block or (block not in prompt.system and block not in prompt.user)):
            raise ValueError("市场画像或提示词已变化，请重新编译并审阅后生成")
