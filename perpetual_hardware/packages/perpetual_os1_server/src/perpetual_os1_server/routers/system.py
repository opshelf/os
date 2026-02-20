from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from perpetual_os1.common import OperatingMode
from perpetual_os1_server.settings import get_operating_mode
from perpetual_os1_server.routers.auth_deps import get_current_user

router = APIRouter(tags=["system"])

MODE_FILE = Path("data/.mode")


class ModeResponse(BaseModel):
	mode: str


class ModeUpdateRequest(BaseModel):
	mode: str


class ModeUpdateResponse(BaseModel):
	mode: str
	message: str


@router.get("/mode", response_model=ModeResponse, dependencies=[Depends(get_current_user)])
async def get_mode() -> ModeResponse:
	mode = get_operating_mode()
	return ModeResponse(mode=mode.value)


@router.put("/mode", response_model=ModeUpdateResponse, dependencies=[Depends(get_current_user)])
async def set_mode(request: ModeUpdateRequest) -> ModeUpdateResponse:
	raw = request.mode.strip().lower()

	valid_modes = {m.value for m in OperatingMode}
	if raw not in valid_modes:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid mode: {raw!r}. Must be one of: {', '.join(sorted(valid_modes))}",
		)

	current = get_operating_mode()
	if raw == current.value:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=f"Already in {raw} mode",
		)

	MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
	MODE_FILE.write_text(raw, encoding="utf-8")

	return ModeUpdateResponse(
		mode=raw,
		message="Mode written. Restart required for changes to take effect.",
	)
