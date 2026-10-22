from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.analyst.services.tension_scoring_service import TensionScoringService
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.tension_dimensions import UNEVALUATED
from application.ai.tension_scoring_contract import (
    TensionScoringLlmPayload,
    parse_tension_scoring_llm_response,
    tension_scoring_payload_to_domain,
    tension_scoring_response_format,
    tension_scoring_openai_function_tool,
)


def test_tension_strict_schema_requires_all_six_fields_and_closes_root():
    contract = tension_scoring_response_format()['json_schema']
    schema = contract['schema']
    assert contract['strict'] is True
    assert schema.get('additionalProperties') is False
    assert set(schema['required']) == set(TensionScoringLlmPayload.model_fields)
    assert all('default' not in item for item in schema['properties'].values())
    assert tension_scoring_openai_function_tool()['function']['parameters'] == schema


def test_tension_wire_schema_does_not_change_legacy_parser_or_domain():
    tension_scoring_response_format()
    payload, errors = parse_tension_scoring_llm_response(
        '{"plot_tension":"70分", "emotional_tension":60, "pacing_tension":50, "unexpected":"legacy extra"}'
    )
    assert not errors
    assert payload.plot_justification == ''
    assert payload.plot_tension == 70
    assert tension_scoring_payload_to_domain(payload).composite_score != UNEVALUATED
    ordinary_schema = TensionScoringLlmPayload.model_json_schema(mode='validation')
    assert ordinary_schema['properties']['plot_justification']['default'] == ''


def test_tension_wire_schema_closes_nested_objects_arrays_and_definitions(monkeypatch):
    original = {
        'type': 'object',
        'properties': {
            'notes': {'type': 'array', 'items': {'type': 'object', 'properties': {'text': {'type': 'string', 'default': ''}}}},
            'maybe': {'anyOf': [{'type': 'null'}, {'$ref': '#/$defs/Evidence'}], 'default': None},
        },
        '$defs': {'Evidence': {'type': 'object', 'properties': {'reason': {'type': 'string'}}}},
    }
    monkeypatch.setattr(TensionScoringLlmPayload, 'model_json_schema', lambda **kwargs: original)
    schema = tension_scoring_response_format()['json_schema']['schema']
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == {'notes', 'maybe'}
    item = schema['properties']['notes']['items']
    assert item['additionalProperties'] is False
    assert item['required'] == ['text']
    assert 'default' not in item['properties']['text']
    definition = schema['$defs']['Evidence']
    assert definition['additionalProperties'] is False
    assert definition['required'] == ['reason']
    assert schema['properties']['maybe']['anyOf'][0] == {'type': 'null'}
    assert 'default' not in schema['properties']['maybe']
    assert 'additionalProperties' not in original
    assert original['properties']['notes']['items']['properties']['text']['default'] == ''


class _Gateway:
    def render(self, contract, variables):
        return SimpleNamespace(prompt=Prompt(system="score tension", user=variables["content"]))


@pytest.mark.asyncio
async def test_tension_scoring_parse_failure_uses_neutral_score(monkeypatch):
    monkeypatch.setattr(
        "application.analyst.services.tension_scoring_service.get_prompt_gateway",
        lambda: _Gateway(),
    )
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content="不是 JSON")))

    dims = await TensionScoringService(llm).score_chapter(
        chapter_content="这一章有足够正文用于评分。",
        chapter_number=3,
    )

    assert dims.composite_score == 50.0


@pytest.mark.asyncio
async def test_tension_scoring_llm_failure_stays_unevaluated(monkeypatch):
    monkeypatch.setattr(
        "application.analyst.services.tension_scoring_service.get_prompt_gateway",
        lambda: _Gateway(),
    )
    llm = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("provider down")))

    dims = await TensionScoringService(llm).score_chapter(
        chapter_content="这一章有足够正文用于评分。",
        chapter_number=3,
    )

    assert dims.composite_score == UNEVALUATED
