from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openshelf_os.iobox_sizer.models import IOBoxSizerMeasurement, IOBoxSizerMeasurementError
from openshelf_os_server.database.models import IOBoxSizerMeasurement as IOBoxSizerMeasurementRow


def build_config_snapshot(config) -> dict[str, Any]:
	return {
		"detection": config.detection.model_dump(mode="json"),
		"calibration": config.calibration.model_dump(mode="json"),
		"tof": {
			"channels": config.tof.channels,
			"timing_budget_ms": config.tof.timing_budget_ms,
			"read_timeout_s": config.tof.read_timeout_s,
		},
	}


async def record_measurement(
	db: AsyncSession,
	*,
	trigger: str,
	observer_mode: bool,
	config_snapshot: dict[str, Any] | None,
	upca: str | None = None,
	item_name: str | None = None,
	user_id: int | None = None,
	request_correlation_id: str | None = None,
	command_payload: dict[str, Any] | None = None,
	human_dimensions: dict[str, Any] | None = None,
	human_dimensions_source: str | None = None,
	measurement: IOBoxSizerMeasurement | None = None,
	error: IOBoxSizerMeasurementError | None = None,
) -> IOBoxSizerMeasurementRow:
	row = IOBoxSizerMeasurementRow(
		trigger=trigger,
		observer_mode=observer_mode,
		upca=upca,
		item_name=item_name,
		user_id=user_id,
		request_correlation_id=request_correlation_id,
		command_payload=command_payload,
		human_dimensions=human_dimensions,
		human_dimensions_source=human_dimensions_source,
		measurement=measurement.model_dump(mode="json") if measurement is not None else None,
		detected=measurement.detected if measurement is not None else False,
		success=error is None,
		error_type=error.error_type if error is not None else None,
		error_message=error.error_message if error is not None else None,
		duration_ms=measurement.duration_ms if measurement is not None else error.duration_ms if error is not None else None,
		config_snapshot=config_snapshot,
		debug_image_path=measurement.debug_image_path if measurement is not None else None,
	)
	db.add(row)
	await db.commit()
	await db.refresh(row)
	return row


async def list_measurements(
	db: AsyncSession,
	*,
	limit: int,
	offset: int,
	upca: str | None = None,
	trigger: str | None = None,
) -> list[IOBoxSizerMeasurementRow]:
	statement = select(IOBoxSizerMeasurementRow).order_by(IOBoxSizerMeasurementRow.created_at.desc())
	if upca:
		statement = statement.where(IOBoxSizerMeasurementRow.upca == upca)
	if trigger:
		statement = statement.where(IOBoxSizerMeasurementRow.trigger == trigger)
	statement = statement.limit(limit).offset(offset)
	result = await db.execute(statement)
	return list(result.scalars().all())
