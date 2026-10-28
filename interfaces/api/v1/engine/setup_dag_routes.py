"""Setup DAG adapter: existing CPMS invocations, validation and real commits."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from application.engine.dag.runtime import DAGRuntimeConflict
from application.onboarding.setup_dag import SetupDAGManager
from application.onboarding.setup_generation import SetupInvocationSteps

router = APIRouter(prefix='/dag', tags=['新书 DAG 工作流'])
_manager = None


def get_setup_manager():
    global _manager
    if _manager is None:
        from infrastructure.persistence.database.connection import get_database
        _manager = SetupDAGManager(get_database(), SetupInvocationSteps)
    return _manager


async def shutdown_setup_jobs():
    if _manager is not None:
        await _manager.shutdown()


class SetupRunRequest(BaseModel):
    stage: Literal['worldbuilding', 'characters', 'locations', 'plot_outline']
    restart: bool = False


class SetupStopRequest(BaseModel):
    run_id: str


def setup_error(exc):
    if isinstance(exc, DAGRuntimeConflict):
        return HTTPException(409, detail={'code': 'SETUP_RUNTIME_CONFLICT', 'reason': str(exc)})
    if isinstance(exc, LookupError):
        return HTTPException(404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, detail=str(exc))
    return HTTPException(503, detail={'code': 'SETUP_UNAVAILABLE', 'reason': str(exc)})


@router.get('/{novel_id}/setup')
async def setup_overview(novel_id: str):
    try:
        return await get_setup_manager().overview(novel_id)
    except Exception as exc:
        raise setup_error(exc) from exc


@router.post('/{novel_id}/setup/run')
async def run_setup(novel_id: str, request: SetupRunRequest):
    try:
        return await get_setup_manager().start(novel_id, request.stage, restart=request.restart)
    except Exception as exc:
        raise setup_error(exc) from exc


@router.get('/{novel_id}/setup/runs/{run_id}')
async def setup_run_status(novel_id: str, run_id: str):
    try:
        return await get_setup_manager().status(novel_id, run_id)
    except Exception as exc:
        raise setup_error(exc) from exc


@router.post('/{novel_id}/setup/stop')
async def stop_setup(novel_id: str, request: SetupStopRequest):
    try:
        return await get_setup_manager().stop(novel_id, request.run_id)
    except Exception as exc:
        raise setup_error(exc) from exc
