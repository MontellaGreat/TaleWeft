"""Canonical, versioned creation contract editor."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from application.core.services.creation_contract_service import CreationContractService, ContractConflict
from application.onboarding.setup_edit_guard import SetupEditConflict
from infrastructure.persistence.database.connection import get_database

router = APIRouter(prefix='/novels', tags=['creation-contract'])


def get_contract_database():
    return get_database()


class CreationContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    locked_genre: str
    locked_world_preset: str
    locked_story_structure: str
    locked_pacing_control: str
    locked_writing_style: str
    locked_special_requirements: str
    target_chapters: int = Field(ge=0, le=100000)
    target_words_per_chapter: int = Field(ge=500, le=20000)
    style_notes: str
    premise_lock: str = Field(max_length=24000)


class SaveCreationContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: str = Field(min_length=1)
    contract: CreationContract
    confirm_market_profile: bool = False


@router.get('/{novel_id}/creation-contract')
def get_creation_contract(novel_id: str, db=Depends(get_contract_database)):
    try:
        return CreationContractService(db).get(novel_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.put('/{novel_id}/creation-contract')
def save_creation_contract(novel_id: str, request: SaveCreationContract, db=Depends(get_contract_database)):
    try:
        return CreationContractService(db).save(novel_id, version=request.version,
            contract=request.contract.model_dump(), confirm_market_profile=request.confirm_market_profile)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except SetupEditConflict as exc:
        raise HTTPException(409, detail={'code': 'SETUP_AUDIT_INCOMPLETE', 'reason': str(exc)}) from exc
    except ContractConflict as exc:
        raise HTTPException(409, detail={'code': 'CREATION_CONTRACT_CONFLICT', 'reason': str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
