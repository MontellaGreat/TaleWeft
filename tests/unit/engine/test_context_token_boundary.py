import pytest

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.context_budget_models import BudgetAllocation, ContextSlot, PriorityTier
from application.engine.services.context_builder import ContextBuilder
from application.workflows.auto_novel_generation_workflow import assemble_chapter_bundle_context_text
from domain.ai.services.token_budget import conservative_token_count


def test_mixed_token_estimate_adds_language_counts_without_weighting_twice():
    allocator = ContextBudgetAllocator.__new__(ContextBudgetAllocator)
    allocator._token_counter = None
    assert allocator.estimate_tokens('中' * 100 + 'a' * 100) == 92
    assert allocator.estimate_tokens('a') == 1
    assert allocator.estimate_tokens('') == 0


def test_final_serialization_bounds_headers_and_retains_high_priority_content():
    allocation = BudgetAllocation(total_budget=120, slots={
        'anchor': ContextSlot(name='anchor', tier=PriorityTier.T0_CRITICAL,
                              content='FACT LOCK: author identity', priority=100),
        'recall': ContextSlot(name='recall', tier=PriorityTier.T3_SACRIFICIAL,
                              content='中文Mixed' * 100, tokens=1),
    })
    result = allocation.get_final_context()
    assert 'FACT LOCK: author identity' in result
    assert len(result.encode('utf-8')) <= 120
    assert allocation.used_tokens == len(result.encode('utf-8'))
    assert allocation.remaining_tokens == 120 - allocation.used_tokens
    assert allocation.serialization_tokens > 0
    assert allocation.get_final_context() == result


def test_mandatory_closing_directives_are_budgeted_or_block_generation():
    allocation = BudgetAllocation(total_budget=1000, expired_foreshadows=['必须揭晓的伏笔'])
    result = allocation.get_final_context()
    assert '必须揭晓的伏笔' in result
    assert allocation.used_tokens == conservative_token_count(result)
    allocation.total_budget = 1
    with pytest.raises(ValueError, match='Mandatory closing instructions'):
        allocation.get_final_context()


def test_zero_budget_does_not_leave_t0_content_in_final_context():
    allocation = BudgetAllocation(total_budget=0, slots={
        'anchor': ContextSlot(name='anchor', tier=PriorityTier.T0_CRITICAL, content='正文'),
    })
    assert allocation.get_final_context() == ''
    assert allocation.used_tokens == 0
    assert allocation.slots['anchor'].tokens == 0


def test_t3_reclaim_mutates_t2_content_instead_of_only_its_accounting(monkeypatch):
    allocator = ContextBudgetAllocator.__new__(ContextBudgetAllocator)
    allocator._token_counter = len
    slots = {
        'recent': ContextSlot(name='recent', tier=PriorityTier.T2_DYNAMIC, content='R' * 2000),
        'recall': ContextSlot(name='recall', tier=PriorityTier.T3_SACRIFICIAL, content='V' * 200),
    }
    for slot in slots.values():
        slot.tokens = len(slot.content)
    monkeypatch.setattr(allocator, '_collect_all_slots', lambda *_args: slots)
    monkeypatch.setattr(allocator, '_estimate_total_chapters', lambda _novel: 100)
    allocator._phase_thresholds = {'developing': 0.25, 'converging': 0.75, 'finale': 0.9}
    allocator.T1_BUDGET_RATIO = 0
    allocator.T2_BUDGET_RATIO = 0.99
    allocator.T3_BUDGET_RATIO = 0.01
    result = allocator.allocate('novel', 1, '', total_budget=1000)
    assert len(slots['recent'].content) <= 950
    assert 'T3 最低保障' in ' '.join(result.compression_log)
    assert result.t2_allocated == len(slots['recent'].content)
    assert len(result.get_final_context()) <= 1000
    assert result.budget_measure == 'model_tokenizer'


def structured_builder(monkeypatch):
    allocator = ContextBudgetAllocator.__new__(ContextBudgetAllocator)
    allocator._token_counter = None
    allocator._phase_thresholds = {'developing': 0.25, 'converging': 0.75, 'finale': 0.9}
    slots = {
        'anchor': ContextSlot(name='anchor', tier=PriorityTier.T0_CRITICAL,
                              content='AUTHOR FACT LOCK', priority=100),
        'recent': ContextSlot(name='recent', tier=PriorityTier.T2_DYNAMIC,
                              content='近期正文' * 3000, priority=100),
        'pending_foreshadowings': ContextSlot(name='pending', tier=PriorityTier.T1_COMPRESSIBLE,
                                            content='已过期: 必须揭晓的真相', priority=100),
    }
    for slot in slots.values():
        slot.tokens = allocator.estimate_tokens(slot.content)
    monkeypatch.setattr(allocator, '_collect_all_slots', lambda *_args: slots)
    monkeypatch.setattr(allocator, '_estimate_total_chapters', lambda _novel: 100)
    builder = ContextBuilder.__new__(ContextBuilder)
    builder.budget_allocator = allocator
    monkeypatch.setattr(builder, '_build_layer2_smart_retrieval', lambda **_kwargs: '检索Bible' * 10000)
    return builder


def test_real_workflow_layer_serialization_includes_bible_and_closing_directives_in_budget(monkeypatch):
    builder = structured_builder(monkeypatch)
    payload = builder.build_structured_context('novel', 1, 'outline', max_tokens=4096)
    actual = assemble_chapter_bundle_context_text(payload)
    assert len(actual.encode('utf-8')) <= 4096
    assert payload['token_usage']['total'] == len(actual.encode('utf-8'))
    assert payload['token_usage']['limit'] == 4096
    assert payload['budget_measure'] == 'utf8_upper_bound'
    assert 'AUTHOR FACT LOCK' in actual
    assert '强制剧情收束令' in actual
    assert '必须揭晓的真相' in actual
    assert sum(payload['token_usage'][layer] for layer in ('layer1', 'layer2', 'layer3')) + payload['token_usage']['serialization'] == payload['token_usage']['total']


@pytest.mark.asyncio
async def test_context_http_response_exposes_measure_and_exact_serialized_budget(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from interfaces.api.dependencies import get_context_builder
    from interfaces.api.v1.engine.context_intelligence import router

    builder = structured_builder(monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_context_builder] = lambda: builder
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/v1/novels/novel/context/retrieve', json={
            'chapter_number': 1, 'outline': 'outline', 'max_tokens': 4096,
        })
    assert response.status_code == 200, response.text
    payload = response.json()
    actual = assemble_chapter_bundle_context_text({
        f'layer{layer}_text': payload[f'layer{layer}']['content'] for layer in (1, 2, 3)
    })
    assert payload['budget_measure'] == 'utf8_upper_bound'
    assert payload['token_usage']['limit'] == 4096
    assert payload['token_usage']['total'] == len(actual.encode('utf-8')) <= 4096
