"""Semantic receipts are bound to the exact input, every text leaf and review dimension."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest


def service():
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview
    return SemanticAntiAIReview


def clean_report(payload, *, kind="story", context=None):
    request = json.loads(service().build_prompt(payload, kind=kind, context=context).user)
    return {"review_version": request["review_version"], "passed": True, "input_hash": request["input_hash"], "issues": [],
            "coverage": [{"text_id": leaf["text_id"]}
                         for leaf in request["texts"]],
            "checks": {category: True for category in request["required_checks"]}}


def test_setup_and_style_rules_have_explicit_roles_and_no_global_key_exemptions():
    payload = {"style": "禁止使用“嘴角上扬”，用动作推进。", "voice_profile": {"label": "冷硬的低语"}}
    prompt = service().build_prompt(payload, kind="setup", context={"instructions_paths": ["$.style"]})
    request = json.loads(prompt.user)
    assert request["kind"] == "setup"
    assert {leaf["path"]: leaf["role"] for leaf in request["texts"]} == {
        "$.style": "instructions", "$.voice_profile.label": "setup"}
    assert "禁词" in prompt.system and "事实" in prompt.system
    assert "故事正文" in prompt.system
    assert service().validate_report(clean_report(payload, kind="setup"), payload, kind="setup")["passed"] is True


def test_valid_rejection_preserves_exact_evidence_and_actionable_feedback():
    body = "这是命运给他的启示，也是一场灵魂深处的洗礼。"
    report = clean_report(body)
    report.update(passed=False, issues=[{"path": "$", "evidence": "一场灵魂深处的洗礼",
        "reason": "空泛抒情替角色总结感受，缺少此刻可观察细节。", "suggestion": "保留事件事实，改用既有动作表现余波。",
        "category": "vague_lyricism"}])
    actual = service().validate_report(report, body)
    assert actual["issues"] == report["issues"]
    assert actual["input_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert actual["status"] == "completed"


@pytest.mark.parametrize("fault", ["passed_string", "empty_failure", "issues_on_pass", "stale_input",
    "missing_coverage", "stale_coverage", "duplicate_coverage", "missing_check", "false_check",
    "invented_evidence", "empty_reason", "empty_suggestion", "unknown_category", "unknown_path", "failed_status"])
def test_invalid_model_receipts_are_rejected(fault):
    body = {"description": "渡口的老人把钥匙压在账本下。", "voice": "句子短，常用反问。"}
    report = clean_report(body)
    issue = {"path": "$.description", "evidence": "把钥匙压在账本下", "reason": "表达重复。",
             "suggestion": "删去重复的描述，保留动作与物件。", "category": "mechanical_structure"}
    if fault == "passed_string": report["passed"] = "true"
    elif fault == "empty_failure": report["passed"] = False
    elif fault == "issues_on_pass": report["issues"] = [issue]
    elif fault == "stale_input": report["input_hash"] = "0" * 64
    elif fault == "missing_coverage": report["coverage"].pop()
    elif fault == "stale_coverage": report["coverage"][0]["input_hash"] = "0" * 64
    elif fault == "duplicate_coverage": report["coverage"].append(copy.deepcopy(report["coverage"][0]))
    elif fault == "missing_check": report["checks"].pop(next(iter(report["checks"])))
    elif fault == "false_check": report["checks"][next(iter(report["checks"]))] = False
    elif fault == "failed_status": report["status"] = "failed"
    else:
        report.update(passed=False, issues=[issue])
        if fault == "invented_evidence": issue["evidence"] = "原文从未存在的句子"
        elif fault == "empty_reason": issue["reason"] = " "
        elif fault == "empty_suggestion": issue["suggestion"] = ""
        elif fault == "unknown_category": issue["category"] = "plot_rewrite"
        elif fault == "unknown_path": issue["path"] = "$.missing"
    with pytest.raises(ValueError):
        service().validate_report(report, body)


@pytest.mark.asyncio
async def test_review_calls_injected_real_provider_interface_once_and_validates_return():
    body = "他把钥匙压在账本下。"
    calls = []
    class Provider:
        async def generate(self, prompt, config):
            calls.append((prompt, config))
            return SimpleNamespace(content=json.dumps(clean_report(body), ensure_ascii=False))
    result = await service().review(body, llm=Provider())
    assert result["passed"] is True and len(calls) == 1
    # Some otherwise functional OpenAI gateways reject native JSON mode.
    # The prompt still demands JSON and the receipt validator stays mandatory.
    assert calls[0][1].response_format is None


@pytest.mark.asyncio
async def test_caller_explicit_native_format_is_preserved():
    from domain.ai.services.llm_service import GenerationConfig
    config = GenerationConfig(response_format={"type": "json_object"})
    body = "他关上门。"
    class Provider:
        async def generate(self, prompt, received):
            assert received is config
            assert received.response_format == {"type": "json_object"}
            return SimpleNamespace(content=json.dumps(clean_report(body)))
    assert (await service().review(body, llm=Provider(), config=config))["passed"]


@pytest.mark.asyncio
async def test_missing_or_failed_provider_never_falls_back_to_clean_report():
    with pytest.raises(ValueError, match="模型|provider"):
        await service().review("他开了门。")
    async def broken(_prompt):
        raise RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service().review("他开了门。", invoke=broken)


@pytest.mark.parametrize("payload", ["", "  ", {}, {"empty": " "}, {"invalid": float("nan")}])
def test_empty_or_non_json_input_cannot_receive_semantic_approval(payload):
    with pytest.raises(ValueError):
        service().build_prompt(payload)
