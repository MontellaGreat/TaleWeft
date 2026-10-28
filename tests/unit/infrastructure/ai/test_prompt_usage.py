from infrastructure.ai.prompt_usage import get_prompt_usage


def test_unbound_custom_prompt_is_explicitly_not_automatically_used():
    usage = get_prompt_usage("custom-user-template", is_builtin=False)
    assert usage["status"] == "unbound"
    assert "不会自动" in usage["description"]


def test_metadata_only_gateway_does_not_claim_its_prompt_is_executed():
    usage = get_prompt_usage("circuit-breaker", is_builtin=True)
    assert usage["status"] == "reference_only"
    assert "固定逻辑" in usage["description"]


def test_semantic_review_uses_cpms_supplement_without_replacing_fixed_core():
    usage = get_prompt_usage("anti-ai-chapter-audit", is_builtin=True)
    assert usage["status"] == "runtime"
    assert "固定" in usage["description"]
    assert "附加" in usage["description"]


def test_writer_prompt_is_an_active_runtime_template():
    assert get_prompt_usage("workflow-chapter-generation", is_builtin=True)["status"] == "runtime"


def test_unreviewed_builtin_is_not_assumed_to_be_used():
    assert get_prompt_usage("new-unverified-builtin", is_builtin=True)["status"] == "available"
