import asyncio
from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/settings/security-audit", tags=["security"])


@router.get("")
async def read_audit(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    return await asyncio.to_thread(request.app.state.security_audit_log.read, limit)
