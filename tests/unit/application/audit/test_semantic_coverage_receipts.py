"""Large setup reviews acknowledge short text IDs without copying 90 SHA-256s."""
import copy
import json

import pytest

from application.audit.services.semantic_anti_ai import SemanticAntiAIReview as Review


def receipt(payload):
    request = json.loads(Review.build_prompt(payload, kind="setup").user)
    return {"review_version": request["review_version"], "passed": True,
            "input_hash": request["input_hash"], "checks": {key: True for key in Review.CHECKS},
            "coverage": [{"text_id": f"t{index:04d}"} for index in range(1, len(request["texts"]) + 1)],
            "issues": []}


def test_ninety_fields_use_unique_ids_and_preserve_full_content_binding():
    payload = {"locations": [{"name": f"渡口{i}", "description": f"编号{i}的渡口铺着石板。",
                              "relation": "渡船往返"} for i in range(30)]}
    request = json.loads(Review.build_prompt(payload, kind="setup").user)
    assert request["review_version"] == Review.VERSION
    assert [text["text_id"] for text in request["texts"]] == [f"t{i:04d}" for i in range(1, 91)]
    assert len(request["texts"]) == 90
    result = Review.validate_report(receipt(payload), payload, kind="setup")
    assert result["passed"] is True
    assert {row["path"]: row["input_hash"] for row in result["coverage"]} == {
        path: Review.input_hash(text) for path, text in Review.text_leaves(payload).items()}
    assert Review.validate_report(result, payload, kind="setup") == result


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "bad_type", "wrong_hash",
                                   "wrong_path", "stale_input", "missing_version"])
def test_index_receipts_fail_closed_on_incomplete_or_mismatched_evidence(fault):
    payload = {"name": "渡口", "description": "木牌挂在门旁。"}
    report = receipt(payload)
    if fault == "missing": report["coverage"].pop()
    elif fault == "duplicate": report["coverage"][1] = copy.deepcopy(report["coverage"][0])
    elif fault == "unknown": report["coverage"][0]["text_id"] = "t9999"
    elif fault == "bad_type": report["coverage"][0]["text_id"] = 1
    elif fault == "wrong_hash": report["coverage"][0]["input_hash"] = "0" * 64
    elif fault == "wrong_path": report["coverage"][0]["path"] = "$.other"
    elif fault == "stale_input": report["input_hash"] = "0" * 64
    elif fault == "missing_version": report.pop("review_version")
    with pytest.raises(ValueError):
        Review.validate_report(report, payload, kind="setup")


def test_legacy_receipt_still_requires_exact_per_field_hashes():
    payload = {"name": "渡口", "description": "木牌挂在门旁。"}
    prompt = Review.build_prompt(payload, kind="setup", version="semantic-style-v1")
    request = json.loads(prompt.user)
    assert request["review_version"] == "semantic-style-v1"
    assert all("text_id" not in text for text in request["texts"])
    report = {"passed": True, "input_hash": request["input_hash"], "issues": [],
              "checks": {key: True for key in Review.CHECKS},
              "coverage": [{"path": text["path"], "input_hash": text["input_hash"]} for text in request["texts"]]}
    assert Review.validate_report(report, payload, kind="setup")["passed"] is True
    report["coverage"][0]["input_hash"] = "0" * 64
    with pytest.raises(ValueError, match="哈希"):
        Review.validate_report(report, payload, kind="setup")


def test_new_prompt_cannot_accept_old_protocol_response():
    payload = {"name": "渡口"}
    report = receipt(payload)
    report["review_version"] = "semantic-style-v1"
    with pytest.raises(ValueError):
        Review.validate_report(report, payload, kind="setup", version="semantic-style-v2")


def test_real_style_rejection_survives_short_receipts_without_any_auto_approval():
    payload = {"description": "这是一场灵魂深处的洗礼。"}
    report = receipt(payload)
    report.update(passed=False, issues=[{"path": "$.description", "category": "vague_lyricism",
        "evidence": "灵魂深处的洗礼", "reason": "抽象概括替代可观察细节。", "suggestion": "保留事实，用原有动作呈现。"}])
    actual = Review.validate_report(report, payload, kind="setup")
    assert actual["passed"] is False and actual["issues"] == report["issues"]
    with pytest.raises(ValueError, match="哈希"):
        Review.validate_report(report, {"description": "改成另一段内容。"}, kind="setup")
