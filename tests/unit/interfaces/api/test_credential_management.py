import asyncio

import httpx
import pytest
from fastapi import FastAPI

from interfaces.api.middleware.session_auth import SessionAuthMiddleware, SessionAuthority
from interfaces.api.v1.core import credential_management as routes

TOKEN = "credential-management-session-0123456789abcdef"
REF = "ppcred:v1:" + "a" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeService:
    def list_references(self):
        return [{"credential_ref": REF, "status": "active", "owners": [{"table": "llm_profiles", "record_id_sha256": "owner", "field": "api_key"}], "ciphertext_sha256": "hash", "size_bytes": 10, "created_at": "today", "updated_at": "today"}]

    def export_metadata(self):
        return {"format": "plotpilot-credential-refs-v1", "credentials": self.list_references(), "warning": "no secret"}

    def delete_reference(self, _reference):
        from application.ai.credential_management_service import CredentialReferenceInUse
        raise CredentialReferenceInUse(REF, self.list_references()[0]["owners"])


@pytest.mark.asyncio
async def test_credential_http_never_returns_secret_and_blocks_active_delete(monkeypatch):
    monkeypatch.setattr(routes, "CredentialManagementService", FakeService)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        listed = await client.get("/api/v1/settings/credentials", headers=AUTH)
        exported = await client.get("/api/v1/settings/credentials/export", headers=AUTH)
        deleted = await client.delete(f"/api/v1/settings/credentials/{REF}", headers=AUTH)
    assert listed.status_code == 200
    assert exported.status_code == 200
    assert deleted.status_code == 409
    assert "api_key" in deleted.text and "private" not in deleted.text
    assert "ppcred:v1:" in exported.text and "private-api-key" not in exported.text

