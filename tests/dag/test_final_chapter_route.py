from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from interfaces.api.v1.engine.dag import dag_routes as routes


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [True, False])
async def test_explicit_final_chapter_choice_reaches_execution_manager(monkeypatch, value):
    start = AsyncMock(return_value={'status': 'running'})
    monkeypatch.setattr(routes, 'get_execution_manager', lambda: SimpleNamespace(start=start))
    await routes.run_dag('book', routes.RunDAGRequest(chapter_number=8, is_final_chapter=value))
    assert start.call_args.kwargs['is_final_chapter'] is value


@pytest.mark.parametrize('value', ['false', 'true', 0, 1])
def test_final_chapter_choice_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        routes.RunDAGRequest(is_final_chapter=value)
