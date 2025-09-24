import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from perpetual_os1_server.core.printer import generate_random_upca
from perpetual_os1_server.core.sizes import (
    ItemInformation,
    _coerce_dimensions,
    get_size_db,
    SizeDatabase,
    add_item_to_json,
    remove_item_from_json,
    get_item_in_json_format,
    get_storage_service
)
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
    use_json: bool = False,
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Get size data from database or JSON for backward compatibility."""
    _check_storage_permission(current_user, "storage:read")

    if use_json:
        # Return original JSON format for hardware compatibility
        return get_size_db()

    # Get from database and convert to JSON format
    storage_service = get_storage_service(session)
    items = await storage_service.get_all_items()

    # Convert to legacy JSON format
    json_data = {}
    for item in items:
        item_info = ItemInformation(
            size=_coerce_dimensions(item.dimensions),
            data={
                "name": item.name,
                "weight": float(item.weight) if item.weight else 0,
                "metadata": item.item_metadata or {},
                "shelf_idx": "",
                "cell_idx": ""
            }
        )
        json_data[item.upca] = item_info

    # Create legacy SizeDatabase object
    legacy_db = SizeDatabase()
    legacy_db.items = json_data
    return legacy_db


@router.put("/")
async def add_size_item(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db),
    use_json: bool = False,
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Add size item to database and sync to JSON."""
    _check_storage_permission(current_user, "storage:write")

    upca, request_item = _extract_item_information(payload)
    if not upca:
        raise HTTPException(status_code=422, detail="UPCA is required")

    if use_json:
        add_item_to_json(upca, request_item.data, request_item.size)
        return get_size_db()

    storage_service = get_storage_service(session)

    try:
        await storage_service.create_item(
            upca=upca,
            name=request_item.data.get("name", "Unknown"),
            dimensions=request_item.size.model_dump(),
            weight=request_item.data.get("weight"),
            metadata=request_item.data.get("metadata", {})
        )

        await storage_service.sync_to_json()

        items = await storage_service.get_all_items()
        json_data: Dict[str, ItemInformation] = {}
        for item in items:
            item_info = ItemInformation(
                size=_coerce_dimensions(item.dimensions),
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": "",
                    "cell_idx": ""
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        logging.error(f"Database operation failed for item {upca}: {str(e)}")
        try:
            add_item_to_json(upca, request_item.data, request_item.size)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed for item {upca}: {str(json_e)}")
            raise RuntimeError(f"Both database and JSON operations failed for item {upca}") from json_e


@router.post("/")
async def add_item(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db),
    use_json: bool = False,
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Add item with optional provided UPCA to database and sync to JSON."""
    _check_storage_permission(current_user, "storage:write")

    upca, request_item = _extract_item_information(payload)
    if not upca:
        upca = generate_random_upca()

    if use_json:
        add_item_to_json(upca, request_item.data, request_item.size)
        return get_size_db()

    storage_service = get_storage_service(session)

    try:
        await storage_service.create_item(
            upca=upca,
            name=request_item.data.get("name", "Unknown"),
            dimensions=request_item.size.model_dump(),
            weight=request_item.data.get("weight"),
            metadata=request_item.data.get("metadata", {})
        )

        await storage_service.sync_to_json()

        items = await storage_service.get_all_items()
        json_data: Dict[str, ItemInformation] = {}
        for item in items:
            item_info = ItemInformation(
                size=_coerce_dimensions(item.dimensions),
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": "",
                    "cell_idx": ""
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        logging.error(f"Database operation failed for new item: {str(e)}")
        try:
            add_item_to_json(upca, request_item.data, request_item.size)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed for new item: {str(json_e)}")
            raise RuntimeError("Both database and JSON operations failed for new item") from json_e


@router.delete("/{upca}")
async def remove_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    use_json: bool = False,
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> SizeDatabase:
    """Remove item from database and sync to JSON."""
    _check_storage_permission(current_user, "storage:delete")

    if use_json:
        # Use original JSON method for hardware compatibility
        remove_item_from_json(upca)
        return get_size_db()

    # Use database with fallback
    storage_service = get_storage_service(session)

    try:
        # Delete from database
        success = await storage_service.delete_item(upca)

        if success:
            # Sync to JSON for hardware compatibility
            await storage_service.sync_to_json()

        # Return updated data
        items = await storage_service.get_all_items()
        json_data: Dict[str, ItemInformation] = {}
        for item in items:
            item_info = ItemInformation(
                size=_coerce_dimensions(item.dimensions),
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": "",
                    "cell_idx": ""
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        logging.error(f"Database operation failed to remove item {upca}: {str(e)}")
        # Fallback to JSON if database fails
        try:
            remove_item_from_json(upca)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed to remove item {upca}: {str(json_e)}")
            raise RuntimeError(f"Both database and JSON operations failed to remove item {upca}") from json_e
