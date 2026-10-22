"""Candidate voice checks must use the actual body and preserve author metrics."""
import json
from types import SimpleNamespace

import pytest

from application.analyst.services import llm_voice_analysis_service as analysis_module
from application.analyst.services.llm_voice_analysis_service import LLMVoiceAnalysisService, STYLE_DIMENSIONS
from application.analyst.services.voice_drift_service import VoiceDriftService


@pytest.mark.asyncio
async def test_same_chapter_rewritten_body_gets_new_analysis(monkeypatch):
    calls = []

    class Provider:
        async def generate(self, prompt, config):
            calls.append(prompt)
            return SimpleNamespace(content=json.dumps({key: 0.3 if prompt == "old body" else 0.9 for key in STYLE_DIMENSIONS}))

    monkeypatch.setattr(analysis_module, "get_prompt_gateway", lambda: SimpleNamespace(
        render=lambda _contract, variables: SimpleNamespace(prompt=variables["content"])))
    monkeypatch.setattr(analysis_module, "generation_config_from_profile", lambda *_: object())
    monkeypatch.setattr(analysis_module, "ensure_trace", lambda **_: None)
    service = LLMVoiceAnalysisService(Provider())
    original = await service.analyze_chapter_style("novel", 7, "old body")
    rewritten = await service.analyze_chapter_style("novel", 7, "new body")
    cached = await service.analyze_chapter_style("novel", 7, "new body")
    assert calls == ["old body", "new body"]
    assert original["pacing"] == 0.3 and rewritten["pacing"] == cached["pacing"] == 0.9
    assert original["_meta"]["content_hash"] != rewritten["_meta"]["content_hash"]


@pytest.mark.parametrize("raw", ["not json", "{}", "[]", '{"pacing": 0.9}',
    json.dumps({key: float("nan") for key in STYLE_DIMENSIONS}),
    json.dumps({key: True for key in STYLE_DIMENSIONS}),
    json.dumps({key: 2.0 for key in STYLE_DIMENSIONS})])
def test_invalid_voice_dimensions_cannot_turn_into_neutral_scores(raw):
    service = LLMVoiceAnalysisService(object())
    with pytest.raises(ValueError):
        service._parse_style_json(raw)


@pytest.mark.asyncio
async def test_provider_failure_does_not_cache_fake_neutral_vector(monkeypatch):
    class Provider:
        async def generate(self, *_):
            raise RuntimeError("provider offline")

    monkeypatch.setattr(analysis_module, "get_prompt_gateway", lambda: SimpleNamespace(
        render=lambda *_: SimpleNamespace(prompt="prompt")))
    monkeypatch.setattr(analysis_module, "generation_config_from_profile", lambda *_: object())
    monkeypatch.setattr(analysis_module, "ensure_trace", lambda **_: None)
    service = LLMVoiceAnalysisService(Provider())
    with pytest.raises(RuntimeError, match="不可用"):
        await service.analyze_chapter_style("novel", 7, "candidate body")
    assert service._cache == {}


def test_statistical_candidate_score_never_overwrites_formal_scores(monkeypatch):
    writes = []
    score_repo = SimpleNamespace(upsert=lambda **kw: writes.append(kw))
    fingerprint_repo = SimpleNamespace(get_by_novel=lambda *_: {"sample_count": 20})
    service = VoiceDriftService(score_repo, fingerprint_repo)
    monkeypatch.setattr(service, "_cosine_similarity", lambda *_: 0.9)
    monkeypatch.setattr(service, "_check_drift_alert", lambda *_: (_ for _ in ()).throw(AssertionError("candidate changed history")))
    result = service.score_chapter("novel", 7, "候选正文有实际动作。", persist=False)
    assert result["similarity_score"] == 0.9 and not result["drift_alert"]
    assert writes == []


@pytest.mark.asyncio
async def test_llm_candidate_score_never_updates_baseline_or_formal_scores(monkeypatch):
    writes, updates = [], []

    async def analyze(*_):
        return {key: 0.9 for key in STYLE_DIMENSIONS}

    async def update(*_):
        updates.append(True)

    llm_voice = SimpleNamespace(analyze_chapter_style=analyze, compute_drift_score=lambda *_: 0.9)
    service = VoiceDriftService(SimpleNamespace(upsert=lambda **kw: writes.append(kw)), object(),
                                llm_voice_service=llm_voice, use_llm_mode=True)
    monkeypatch.setattr(service, "_get_or_init_baseline", lambda *_: {"baseline": {"pacing": 0.9}})
    monkeypatch.setattr(service, "_update_baseline", update)
    result = await service.score_chapter_async("novel", 10, "候选正文。", persist=False)
    assert result["similarity_score"] == 0.9 and not result["drift_alert"]
    assert writes == updates == []
