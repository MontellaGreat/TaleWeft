"""Exercise the real SDK decoder, including tolerant union parsing on Python 3.14.

Provider mocks bypassed the old SDK's attempt to mutate typing.Union, so both
Responses modes failed after HTTP 200 despite otherwise valid adapter logic.
"""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.openai_provider import OpenAIProvider


def _response():
    # Gateways commonly omit optional metadata. The SDK's tolerant decoder
    # must still construct the nested discriminated unions correctly.
    return {"id": "resp-test", "object": "response", "created_at": 1,
        "model": "gpt-test", "status": "completed",
        "output": [{"id": "msg-test", "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "雾港灯亮", "annotations": []}]}],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_sdk_decodes_responses_without_mutating_typing_unions(streaming):
    paths = []

    async def serve(request):
        paths.append(request.url.path)
        if streaming:
            events = [
                {"type": "response.output_text.delta", "delta": "雾港灯亮", "output_index": 0,
                 "content_index": 0, "item_id": "msg-test"},
                {"type": "response.completed", "response": _response()},
            ]
            body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return httpx.Response(200, json=_response())

    provider = OpenAIProvider(Settings(api_key="offline-test", base_url="https://offline.invalid/v1",
        default_model="gpt-test", endpoint_kind="responses"))
    await provider.async_client.close()
    provider.async_client = AsyncOpenAI(api_key="offline-test", base_url="https://offline.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(serve)), max_retries=0)
    try:
        prompt = Prompt(system="协议回归测试", user="四字回复")
        config = GenerationConfig(model="gpt-test", max_tokens=128)
        if streaming:
            text = "".join([piece async for piece in provider.stream_generate(prompt, config)])
        else:
            text = (await provider.generate(prompt, config)).content
        assert text == "雾港灯亮"
        assert paths == ["/v1/responses"]
    finally:
        await provider.async_client.close()

