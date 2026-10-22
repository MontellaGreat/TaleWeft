"""Author-managed signed data packages and immutable quality rule policy."""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from application.audit.services.anti_ai_audit import AntiAIAuditor
from application.audit.services.quality_rule_models import QualityRuleConfig
from infrastructure.ai.quality_rule_store import QualityRuleStore, RuleVersionConflict
from infrastructure.ai.signed_plugins import SignedPluginStore, SignedPluginPackage, Permission, PluginRejectedError

router = APIRouter(prefix='/extensions', tags=['签名插件与质量规则'])


class TrustKeyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=40, max_length=48)
    permissions: list[Permission] = Field(max_length=2)


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    package: SignedPluginPackage
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class RulePublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: QualityRuleConfig
    expected_version: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=500)


class RuleRollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: str = Field(min_length=1, max_length=100)


class ScanPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=100000)


async def _call(operation):
    try:
        return await asyncio.to_thread(operation)
    except RuleVersionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (PluginRejectedError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get('/plugins')
async def plugins():
    return await _call(lambda: {'keys': SignedPluginStore().keys(), 'packages': SignedPluginStore().list_packages()})


@router.post('/plugins/keys')
async def trust_key(request: TrustKeyRequest):
    return await _call(lambda: SignedPluginStore().trust(**request.model_dump()))


@router.delete('/plugins/keys/{signer_id}')
async def revoke_key(signer_id: str):
    await _call(lambda: SignedPluginStore().revoke(signer_id))
    return {'status': 'revoked'}


@router.post('/plugins/verify')
async def verify_plugin(request: SignedPluginPackage):
    return await _call(lambda: SignedPluginStore().verify(request))


@router.post('/plugins/install')
async def install_plugin(request: InstallRequest):
    return await _call(lambda: SignedPluginStore().install(request.package, expected_digest=request.expected_digest))


@router.delete('/plugins/{plugin_id}')
async def uninstall_plugin(plugin_id: str):
    await _call(lambda: SignedPluginStore().uninstall(plugin_id))
    return {'status': 'uninstalled'}


@router.get('/quality-rules')
async def quality_rules():
    return await _call(lambda: QualityRuleStore().overview())


@router.post('/quality-rules')
async def publish_rules(request: RulePublishRequest):
    return await _call(lambda: QualityRuleStore().publish(request.config,
        expected_version=request.expected_version, note=request.note))


@router.post('/quality-rules/versions/{version_id}/rollback')
async def rollback_rules(version_id: str, request: RuleRollbackRequest):
    return await _call(lambda: QualityRuleStore().rollback(version_id, expected_version=request.expected_version))


@router.get('/quality-rules/snapshots/{digest}')
async def rule_snapshot(digest: str):
    return await _call(lambda: QualityRuleStore().snapshot(digest))


@router.post('/quality-rules/scan')
async def rule_preview(request: ScanPreviewRequest):
    from dataclasses import asdict
    return await _call(lambda: asdict(AntiAIAuditor(rule_provider=lambda: QualityRuleStore().effective()).scan_chapter('preview', request.content)))
