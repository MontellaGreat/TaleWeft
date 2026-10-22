"""Probe configured C/R providers with short, real, isolated calls.

No protocol fallback, model substitution, source writes or credential logging.
The four requests use the application's existing LLMProviderFactory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

from live_novel_fullflow import ROOT, CheckFailed, copy_configuration, require, write_json


def safe_failure(error):
    chain, current, status, locations, attributes = [], error, None, [], []
    while current is not None and len(chain) < 5:
        chain.append(type(current).__name__)
        trace = current.__traceback__
        while trace is not None:
            locations.append({'file': Path(trace.tb_frame.f_code.co_filename).name,
                'function': trace.tb_frame.f_code.co_name, 'line': trace.tb_lineno})
            trace = trace.tb_next
        if isinstance(current, AttributeError):
            attributes.append({'name': current.name if isinstance(current.name, str) and current.name.isidentifier() else None,
                'object_type': type(current.obj).__name__})
        candidate = getattr(current, 'status_code', None)
        if isinstance(candidate, int):
            status = candidate
        current = current.__cause__ or current.__context__
    value = str(error).casefold()
    category = 'provider_failure'
    for name, markers in (
        ('endpoint_unsupported', ('unknown endpoint', 'unsupported endpoint', 'responses api unsupported', '404')),
        ('model_unavailable', ('model_not_found', 'invalid_model', 'model does not exist')),
        ('authentication_failed', ('401', '403', 'invalid_api_key', 'authentication')),
        ('timeout', ('timeout', 'timed out')),
        ('empty_or_truncated', ('empty', 'incomplete', 'length', 'max_output_tokens')),
        ('invalid_parameter', ('invalid parameter', 'unsupported parameter', '400')),
    ):
        if any(marker in value for marker in markers):
            category = name
            break
    return {'error_types': chain, 'http_status': status, 'category': category,
        'error_locations': locations[-8:], 'attribute_errors': attributes}


async def probe(directory, timeout, endpoint_kind='both', mode='both'):
    # Environment must be set before importing application.paths or any service.
    os.environ['PLOTPILOT_PROD_DATA_DIR'] = str(directory / 'data')
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(ROOT))
    import openai
    from application.ai.llm_control_service import LLMControlService
    from domain.ai.services.llm_service import GenerationConfig
    from domain.ai.value_objects.prompt import Prompt
    from infrastructure.ai.provider_factory import LLMProviderFactory

    control = LLMControlService()
    factory = LLMProviderFactory(control)
    active = control.resolve_active_profile()
    require(active is not None and active.protocol == 'openai', 'Saved active profile must use OpenAI compatible protocol')
    results = []
    for endpoint in (('chat', 'responses') if endpoint_kind == 'both' else (endpoint_kind,)):
        profile = active.model_copy(update={'endpoint_kind': endpoint,
            'preset_key': 'custom-openai-compatible' if endpoint == 'chat' else 'custom-openai-responses',
            'use_legacy_chat_completions': endpoint == 'chat'})
        for streaming in ((False, True) if mode == 'both' else (mode == 'streaming',)):
            record = {'endpoint_kind': endpoint, 'streaming': streaming, 'model_changed': False,
                'provider_factory': 'LLMProviderFactory.create_from_profile', 'requested_paths': [], 'responses': [],
                'sdk_version': openai.__version__, 'python_version': sys.version.split()[0]}
            started, provider = time.monotonic(), None
            try:
                provider = factory.create_from_profile(profile)
                # Add passive HTTP event observers. Persist only endpoint kind
                # and integer status: never URL authority/query/headers/body.
                async def request_observer(request):
                    path = request.url.path.rstrip('/')
                    record['requested_paths'].append('/chat/completions' if path.endswith('/chat/completions') else '/responses' if path.endswith('/responses') else 'unexpected_path')

                async def response_observer(response):
                    record['responses'].append(response.status_code)

                provider._http_client.event_hooks.setdefault('request', []).append(request_observer)
                provider._http_client.event_hooks.setdefault('response', []).append(response_observer)
                prompt = Prompt(system='这是简短的协议连通测试。', user='只回复“雾港灯亮”，不要添加其他文字。')
                config = GenerationConfig(model=profile.model, max_tokens=1024,
                    reasoning_effort=profile.reasoning_effort, thinking_budget_tokens=profile.thinking_budget_tokens)
                content, chunks = '', 0
                if streaming:
                    async def consume():
                        nonlocal content, chunks
                        async for piece in provider.stream_generate(prompt, config):
                            content += piece
                            chunks += 1
                    await asyncio.wait_for(consume(), timeout=timeout)
                else:
                    response = await asyncio.wait_for(provider.generate(prompt, config), timeout=timeout)
                    content = response.content
                require(bool(content.strip()), 'Provider returned an empty probe body')
                expected_path = '/chat/completions' if endpoint == 'chat' else '/responses'
                require(bool(record['requested_paths']) and all(p == expected_path for p in record['requested_paths']), 'Explicit protocol selection changed endpoint unexpectedly')
                record.update(status='passed_real_provider', chars=len(content), chunks=chunks,
                    content_hash=hashlib.sha256(content.encode('utf-8')).hexdigest(), fallback_observed=False)
            except Exception as error:
                record.update(status='failed_real_provider', **safe_failure(error))
                expected_path = '/chat/completions' if endpoint == 'chat' else '/responses'
                record['fallback_observed'] = any(p != expected_path for p in record['requested_paths'])
            finally:
                record['elapsed_seconds'] = round(time.monotonic() - started, 3)
                if provider is not None:
                    await provider.async_client.close()
                results.append(record)
                write_json(directory / 'protocol-result.json', {'status': 'running', 'calls': results})
                print(json.dumps(record, ensure_ascii=False), flush=True)
    final = {'status': 'passed_real_provider' if all(r['status'] == 'passed_real_provider' for r in results) else 'failed_real_provider',
        'calls': results, 'source_database_modified': False,
        'scope': 'Short provider protocol probes only; this does not replace novel full-flow acceptance.'}
    write_json(directory / 'protocol-result.json', final)
    return 0 if final['status'] == 'passed_real_provider' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--endpoint-kind', choices=('both', 'chat', 'responses'), default='both')
    parser.add_argument('--mode', choices=('both', 'nonstream', 'streaming'), default='both')
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    require(directory.is_relative_to(ROOT) and not directory.exists(), 'Choose a fresh project-local probe directory')
    directory.mkdir(parents=True)
    copy_configuration(args.source_db.resolve(), directory / 'data' / 'plotpilot.db')
    return asyncio.run(probe(directory, args.timeout, args.endpoint_kind, args.mode))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
