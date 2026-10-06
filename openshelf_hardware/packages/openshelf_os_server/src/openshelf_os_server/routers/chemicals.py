"""Authenticated chemical reference lookups; optional upstream availability."""
from openshelf_os_server.documentation import response_docs
from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.chemicals import (
    ChemicalDetail, ChemicalSearch, CommonChemistry, get_common_chemistry, normalize_cas,
)
from .auth_deps import require_permissions

router = APIRouter(tags=["chemicals"], dependencies=[Depends(require_permissions("storage:read"))])


@router.get(
    "/search",
    response_model=ChemicalSearch,
    summary="Search CAS Common Chemistry",
    responses=response_docs(errors=(401, 403, 422, 503)),
)
async def search_chemicals(
    q: str = Query(min_length=2, max_length=255),
    offset: int = Query(default=0, ge=0, le=10000),
    service: CommonChemistry = Depends(get_common_chemistry),
) -> ChemicalSearch:
    """Search CAS Common Chemistry.

    Searches the configured CAS service by name or identifier. `q` must contain at least two
    non-whitespace characters; `offset` pages the upstream result set. Requires
    authentication. Upstream failure returns `503` with `Retry-After: 5`.
    """
    if len(q.strip()) < 2:
        raise HTTPException(422, "Enter at least two characters")
    return await service.search(q, offset)


@router.get(
    "/{cas_number}",
    response_model=ChemicalDetail,
    summary="Read a chemical by CAS number",
    responses=response_docs(errors=(401, 403, 404, 422, 503)),
)
async def chemical_detail(
    cas_number: str,
    service: CommonChemistry = Depends(get_common_chemistry),
) -> ChemicalDetail:
    """Read a chemical by CAS number.

    Looks up one CAS registry number. Invalid numbers return `422`, absent records `404`,
    and upstream unavailability `503` with `Retry-After: 5`. Requires authentication.
    """
    try:
        cas_number = normalize_cas(cas_number)
    except ValueError:
        raise HTTPException(422, "Invalid CAS number") from None
    return await service.detail(cas_number)
