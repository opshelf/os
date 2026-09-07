"""Authenticated chemical reference lookups; optional upstream availability."""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.chemicals import (
    ChemicalDetail, ChemicalNotFound, ChemicalSearch, ChemicalUnavailable,
    CommonChemistry, get_common_chemistry, normalize_cas,
)
from .auth_deps import require_permissions

router = APIRouter(tags=["chemicals"], dependencies=[Depends(require_permissions("storage:read"))])


@router.get("/search", response_model=ChemicalSearch)
async def search_chemicals(
    q: str = Query(min_length=2, max_length=255),
    offset: int = Query(default=0, ge=0, le=10000),
    service: CommonChemistry = Depends(get_common_chemistry),
) -> ChemicalSearch:
    if len(q.strip()) < 2:
        raise HTTPException(422, "Enter at least two characters")
    try:
        return await service.search(q, offset)
    except ChemicalUnavailable:
        raise HTTPException(503, "CAS search unavailable", headers={"Retry-After": "5"}) from None


@router.get("/{cas_number}", response_model=ChemicalDetail)
async def chemical_detail(
    cas_number: str,
    service: CommonChemistry = Depends(get_common_chemistry),
) -> ChemicalDetail:
    try:
        cas_number = normalize_cas(cas_number)
    except ValueError:
        raise HTTPException(422, "Invalid CAS number") from None
    try:
        return await service.detail(cas_number)
    except ChemicalNotFound:
        raise HTTPException(404, "Chemical not found") from None
    except ChemicalUnavailable:
        raise HTTPException(503, "CAS search unavailable", headers={"Retry-After": "5"}) from None
