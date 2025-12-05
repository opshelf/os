from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from perpetual_os1_server.core.printer import generate_random_upca
from perpetual_os1_server.core.sizes import (
    ItemInformation,
    _coerce_dimensions,
    SizeDatabase,
    build_size_database_from_items,
)
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.session import get_db
from typing import Any, Dict, Mapping, Optional

from ..database.models import User
from ..core.permissions import has_permission
from .auth_deps import get_current_user_optional

router = APIRouter(tags=["sizes"])

# Apply authorization to specific endpoints instead of globally


def _check_storage_permission(current_user: Optional[User], permission: str) -> None:
    """Enforce storage permissions when an authenticated user is present."""
    if current_user is None:
        # Allow anonymous access for backward compatibility with hardware tests.
        return

    if not has_permission(current_user.role.permissions, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {permission}",
        )


def _extract_item_information(payload: Dict[str, Any]) -> tuple[Optional[str], ItemInformation]:
    if "item" in payload and isinstance(payload["item"], Mapping):
        item_body = payload["item"]
        size_section = item_body.get("size") or {}
        data_section = dict(item_body.get("data") or {})
    else:
        size_section = payload.get("size") or payload.get("dimensions") or {}
        base_data = payload.get("data")
        data_section = dict(base_data) if isinstance(base_data, Mapping) else {}

        if "name" in payload and payload["name"] is not None:
            data_section["name"] = payload["name"]
        else:
            data_section.setdefault("name", "Unknown")

        if "weight" in payload:
            data_section["weight"] = payload["weight"]
        else:
            data_section.setdefault("weight", None)

        if "metadata" in payload:
            data_section["metadata"] = payload.get("metadata") or {}
        else:
            data_section.setdefault("metadata", {})

    data_section.setdefault("metadata", {})
    return payload.get("upca"), ItemInformation(
        size=_coerce_dimensions(size_section),
        data=data_section,
    )


@router.get("/")
async def get_size_data(
    session: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Get size data from the database as legacy SizeDatabase payload."""
    _check_storage_permission(current_user, "storage:read")

    storage_service = StorageService(session)
    items = await storage_service.get_all_items()
    return build_size_database_from_items(items)


@router.put("/")
async def add_size_item(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Add size item to the database and return legacy view."""
    _check_storage_permission(current_user, "storage:write")

    upca, request_item = _extract_item_information(payload)
    if not upca:
        raise HTTPException(status_code=422, detail="UPCA is required")

    storage_service = StorageService(session)

    await storage_service.create_item(
        upca=upca,
        name=request_item.data.get("name", "Unknown"),
        dimensions=request_item.size.model_dump(),
        weight=request_item.data.get("weight"),
        metadata=request_item.data.get("metadata", {})
    )

    items = await storage_service.get_all_items()
    return build_size_database_from_items(items)


@router.post("/")
async def add_item(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Add item with optional provided UPCA to database and return legacy view."""
    _check_storage_permission(current_user, "storage:write")

    upca, request_item = _extract_item_information(payload)
    if not upca:
        upca = generate_random_upca()

    storage_service = StorageService(session)

    await storage_service.create_item(
        upca=upca,
        name=request_item.data.get("name", "Unknown"),
        dimensions=request_item.size.model_dump(),
        weight=request_item.data.get("weight"),
        metadata=request_item.data.get("metadata", {})
    )

    items = await storage_service.get_all_items()
    return build_size_database_from_items(items)


@router.delete("/{upca}")
async def remove_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Remove item from database and return legacy view."""
    _check_storage_permission(current_user, "storage:delete")

    storage_service = StorageService(session)

    await storage_service.delete_item(upca)
    items = await storage_service.get_all_items()
    return build_size_database_from_items(items)
