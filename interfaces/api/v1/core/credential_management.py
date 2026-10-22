"""Credential reference lifecycle API.  No endpoint returns secret material."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.ai.credential_management_service import (
    CredentialManagementService,
    CredentialReferenceInUse,
)
from infrastructure.security.credential_store import CredentialUnavailable

router = APIRouter(prefix="/settings/credentials", tags=["settings"])


class CredentialDeleteRequest(BaseModel):
    credential_ref: str = Field(min_length=42, max_length=42)


@router.get("")
def list_credentials():
    return {"format": "plotpilot-credential-refs-v1", "credentials": CredentialManagementService().list_references()}


@router.get("/export")
def export_credentials():
    return CredentialManagementService().export_metadata()


@router.delete("/{credential_ref}")
def delete_credential(credential_ref: str):
    try:
        return CredentialManagementService().delete_reference(credential_ref)
    except CredentialReferenceInUse as exc:
        raise HTTPException(status_code=409, detail={
            "code": "credential_in_use",
            "message": str(exc),
            "credential_ref": exc.reference,
            "owners": exc.owners,
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CredentialUnavailable as exc:
        raise HTTPException(status_code=422, detail="凭据引用无效或凭据文件不可用") from exc

