from __future__ import annotations
from openshelf_os_server.documentation import response_docs

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from openshelf_os_server.database.models import Telemetry
from openshelf_os_server.database.session import AsyncSessionLocal

router = APIRouter(tags=["telemetry"])


class TelemetryResponse(BaseModel):
	cycle_count: int
	motor_movement_seconds: float


@router.get(
    "/telemetry",
    response_model=TelemetryResponse,
    summary="Read cumulative device telemetry",
    responses=response_docs(),
)
async def get_telemetry() -> TelemetryResponse:
	"""Read cumulative device telemetry.

	Public counters for robot cycles and motor movement time in seconds. Does not start
	hardware sampling or reset counters.
	"""
	async with AsyncSessionLocal() as session:
		result = await session.execute(select(Telemetry).where(Telemetry.id == 1))
		row = result.scalar_one_or_none()

	if row is None:
		return TelemetryResponse(cycle_count=0, motor_movement_seconds=0.0)

	return TelemetryResponse(
		cycle_count=row.cycle_count,
		motor_movement_seconds=row.motor_movement_seconds,
	)
