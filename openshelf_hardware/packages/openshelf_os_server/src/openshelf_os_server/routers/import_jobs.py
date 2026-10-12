"""Inventory import jobs created from the operator portal.

The relay forwards a mapped CSV here. The job stays active until it is
cancelled; loading the units is the kiosk's job.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openshelf_os_server.database.models import Item
from openshelf_os_server.database.session import get_db
from openshelf_os_server.errors import api_error
from openshelf_os_server.routers.auth_deps import require_relay_or_permissions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import-jobs", tags=["import"])

MAX_UNITS = 5000

_jobs: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


class ImportRow(BaseModel):
	model_config = ConfigDict(extra="ignore")

	row_number: int | None = None
	name: str = Field(min_length=1, max_length=255)
	quantity: int = Field(default=1, ge=1)
	small: bool = False
	barcode: str | None = Field(default=None, max_length=255)
	expires_at: str | None = None
	access_level: str | None = None
	tare_weight: float | None = None
	net_weight: float | None = None
	full_net_weight: float | None = None
	metadata: dict[str, str] = Field(default_factory=dict)


class ImportCreate(BaseModel):
	model_config = ConfigDict(extra="ignore")

	filename: str | None = None
	rows: list[ImportRow] = Field(min_length=1)


class ImportDimensions(BaseModel):
	width: float = Field(gt=0)
	height: float = Field(gt=0)
	length: float = Field(gt=0)


class BeginImportUnit(BaseModel):
	dimensions: ImportDimensions


class PlaceImportUnit(BaseModel):
	unit_id: int
	height: float = Field(gt=0)


def clear_import_jobs() -> None:
	"""Drop in-memory jobs. Tests use this so one case cannot leave a job running."""
	_jobs.clear()


def _now() -> str:
	return datetime.now(UTC).isoformat()


async def _publish_import_job_updated() -> None:
	"""Tell an open kiosk to reread the active import. The screen follows that read."""
	payload = {"kind": "import_job_updated"}
	try:
		from openshelf_os.common import OperatingMode
		from openshelf_os_server.core import robot as robot_core
		from openshelf_os_server.core.mock_robot import get_mock_robot_runtime
		from openshelf_os_server.settings import get_operating_mode

		if get_operating_mode() == OperatingMode.mock:
			await get_mock_robot_runtime().publish_event(payload)
			return
		worker = robot_core.robot_worker
		if worker is not None:
			await worker.publish_event(payload)
	except Exception as exc:
		logger.warning("import job event was not published: %s", exc)


def _open_job() -> dict[str, Any] | None:
	for job in _jobs.values():
		if job["state"] in {"active", "cancelling"}:
			return job
	return None


def _public(job: dict[str, Any], *, units: bool) -> dict[str, Any]:
	payload = {
		"id": job["id"],
		"state": job["state"],
		"filename": job["filename"],
		"cancel_source": job["cancel_source"],
		"counts": dict(job["counts"]),
		"created_at": job["created_at"],
		"updated_at": job["updated_at"],
		"completed_at": job["completed_at"],
		"skipped": list(job["skipped"]),
		"in_progress": list(job["in_progress"]),
		# The kiosk replaces the page while this step is present on an open job.
		"step": _step(job),
	}
	if units:
		payload["units"] = list(job["units"])
	return payload


def _empty_step(kind: str, **extra: Any) -> dict[str, Any]:
	step = {
		"kind": kind,
		"phase": None,
		"unit": None,
		"units": None,
		"workflow": None,
		"error": None,
	}
	step.update(extra)
	return step


def _step(job: dict[str, Any]) -> dict[str, Any] | None:
	if job["state"] not in {"active", "cancelling"}:
		return None
	if job.get("tray_phase") is not None:
		return _empty_step(
			"tray",
			phase=job["tray_phase"],
			units=[unit for unit in job["units"] if unit["status"] in {"pending", "in_progress"}],
		)
	current = next((unit for unit in job["units"] if unit["status"] == "in_progress"), None)
	if current is not None:
		return _empty_step("unit", phase="start", unit=current)
	pending = next((unit for unit in job["units"] if unit["status"] == "pending"), None)
	if pending is None:
		return _empty_step("waiting", units=[])
	return _empty_step("unit", phase="start", unit=pending)


def _recount(job: dict[str, Any]) -> None:
	units = job["units"]
	job["counts"] = {
		"total": len(units),
		"pending": sum(unit["status"] == "pending" for unit in units),
		"in_progress": sum(unit["status"] == "in_progress" for unit in units),
		"loaded": sum(unit["status"] == "loaded" for unit in units),
		"skipped": sum(unit["status"] == "skipped" for unit in units),
	}
	job["skipped"] = [unit for unit in units if unit["status"] == "skipped"]
	job["in_progress"] = [unit for unit in units if unit["status"] == "in_progress"]
	job["updated_at"] = _now()


def _finish_if_done(job: dict[str, Any]) -> None:
	if job["state"] != "active":
		return
	if job["units"] and all(unit["status"] in {"loaded", "skipped"} for unit in job["units"]):
		job["state"] = "completed"
		job["completed_at"] = _now()
		job["tray_phase"] = None


def _find_unit(job: dict[str, Any], unit_id: int) -> dict[str, Any]:
	for unit in job["units"]:
		if unit["id"] == unit_id:
			return unit
	raise api_error(404, "not_found", "Import unit not found")


def _result(job: dict[str, Any], unit: dict[str, Any] | None, notice: str | None = None) -> dict[str, Any]:
	return {"job": _public(job, units=False), "unit": unit, "notice": notice}


def _unit(position: int, row: ImportRow, offset: int) -> dict[str, Any]:
	barcode = row.barcode if offset == 0 and row.barcode else f"IMP{uuid.uuid4().hex[:10].upper()}"
	return {
		"id": position,
		"position": position,
		"row_number": row.row_number,
		"barcode": barcode,
		"name": row.name,
		"small": row.small,
		"access_level_id": None,
		"expires_at": row.expires_at,
		"status": "pending",
		"stage": None,
		"error": None,
		"request_id": None,
		"dimensions": None,
		"item_id": None,
		"bin_id": None,
		"compartment": None,
	}


def _build_job(body: ImportCreate) -> dict[str, Any]:
	units: list[dict[str, Any]] = []
	position = 1
	for row in body.rows:
		for offset in range(row.quantity):
			units.append(_unit(position, row, offset))
			position += 1
	total = len(units)
	stamp = _now()
	return {
		"id": str(uuid.uuid4()),
		"state": "active",
		"filename": body.filename,
		"cancel_source": None,
		"counts": {
			"total": total,
			"pending": total,
			"in_progress": 0,
			"loaded": 0,
			"skipped": 0,
		},
		"created_at": stamp,
		"updated_at": stamp,
		"completed_at": None,
		"skipped": [],
		"in_progress": [],
		"units": units,
	}


async def _reject_known_barcodes(session: AsyncSession, rows: list[ImportRow]) -> None:
	seen: set[str] = set()
	duplicates: list[dict[str, Any]] = []
	for row in rows:
		barcode = row.barcode
		if not barcode:
			continue
		if barcode in seen:
			duplicates.append({"row": row.row_number, "field": "barcode", "barcode": barcode})
		seen.add(barcode)
	if duplicates:
		raise api_error(
			409,
			"barcodes_exist",
			"Some barcodes already exist on this machine",
			errors=duplicates,
		)
	if not seen:
		return
	result = await session.execute(select(Item.barcode).where(Item.barcode.in_(seen)))
	existing = set(result.scalars().all())
	if not existing:
		return
	raise api_error(
		409,
		"barcodes_exist",
		"Some barcodes already exist on this machine",
		errors=[
			{"row": row.row_number, "field": "barcode", "barcode": row.barcode}
			for row in rows
			if row.barcode in existing
		],
	)


@router.post("")
async def create_import_job(
	body: ImportCreate,
	session: AsyncSession = Depends(get_db),
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Create an import job from mapped CSV rows."""
	total = sum(row.quantity for row in body.rows)
	if total > MAX_UNITS:
		raise api_error(422, "invalid_rows", f"An import can load at most {MAX_UNITS} items")
	await _reject_known_barcodes(session, body.rows)
	async with _lock:
		if _open_job() is not None:
			raise api_error(409, "import_active", "Another import is still running")
		job = _build_job(body)
		_jobs[job["id"]] = job
	await _publish_import_job_updated()
	return {"job": _public(job, units=False)}


@router.get("/active")
async def active_import_job(
	_: Any = Depends(require_relay_or_permissions("storage:read")),
) -> dict[str, Any]:
	"""The import that is still running, or `{job: null}` when the machine is idle."""
	job = _open_job()
	return {"job": None if job is None else _public(job, units=False)}


@router.get("/{job_id}")
async def read_import_job(
	job_id: str,
	units: bool = Query(False),
	_: Any = Depends(require_relay_or_permissions("storage:read")),
) -> dict[str, Any]:
	"""Read one import job. `units=true` includes every unit, not only the active ones."""
	job = _jobs.get(job_id)
	if job is None:
		raise api_error(404, "not_found", "Import job not found")
	return {"job": _public(job, units=units)}


@router.post("/{job_id}/cancel")
async def cancel_import_job(
	job_id: str,
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Stop an import. Units that have not started stay unloaded."""
	async with _lock:
		job = _jobs.get(job_id)
		if job is None:
			raise api_error(404, "not_found", "Import job not found")
		if job["state"] == "active":
			job["state"] = "cancelled"
			job["cancel_source"] = "relay"
			job["completed_at"] = _now()
			job["updated_at"] = job["completed_at"]
			await _publish_import_job_updated()
	return {"job": _public(job, units=False)}


async def note_unit_stored(barcode: str) -> None:
	"""Mark the measured import unit loaded after a mock store of its barcode."""
	async with _lock:
		job = _open_job()
		if job is None:
			return
		unit = next(
			(
				candidate
				for candidate in job["units"]
				if candidate["barcode"] == barcode and candidate["status"] == "in_progress"
			),
			None,
		)
		if unit is None:
			return
		unit["status"] = "loaded"
		unit["stage"] = "placed"
		_recount(job)
		_finish_if_done(job)
	await _publish_import_job_updated()


def _require_open(job_id: str) -> dict[str, Any]:
	job = _jobs.get(job_id)
	if job is None:
		raise api_error(404, "not_found", "Import job not found")
	if job["state"] not in {"active", "cancelling"}:
		raise api_error(409, "import_closed", "This import is no longer running")
	return job


@router.post("/{job_id}/units/{unit_id}/begin")
async def begin_import_unit(
	job_id: str,
	unit_id: int,
	body: BeginImportUnit,
	session: AsyncSession = Depends(get_db),
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Record the measured size and leave the unit ready to label and store."""
	dimensions = body.dimensions.model_dump()
	async with _lock:
		job = _require_open(job_id)
		unit = _find_unit(job, unit_id)
		if unit["status"] not in {"pending", "in_progress"}:
			raise api_error(409, "unit_handled", "This item was already handled")
		unit["status"] = "in_progress"
		unit["stage"] = "measured"
		unit["dimensions"] = dimensions
		unit["request_id"] = unit["request_id"] or str(uuid.uuid4())
		existing = (
			await session.execute(select(Item).where(Item.barcode == unit["barcode"]))
		).scalar_one_or_none()
		if existing is None:
			session.add(
				Item(
					barcode=unit["barcode"],
					name=unit["name"],
					dimensions=dimensions,
					weight=350.0,
					item_metadata={},
				)
			)
			await session.commit()
		_recount(job)
		return _result(job, unit)


@router.post("/{job_id}/units/{unit_id}/skip")
async def skip_import_unit(
	job_id: str,
	unit_id: int,
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Set an item aside. The import finishes when every item is loaded or skipped."""
	async with _lock:
		job = _require_open(job_id)
		unit = _find_unit(job, unit_id)
		if unit["status"] in {"loaded", "skipped"}:
			raise api_error(409, "unit_handled", "This item was already handled")
		unit["status"] = "skipped"
		unit["stage"] = None
		_recount(job)
		_finish_if_done(job)
	await _publish_import_job_updated()
	return _result(job, unit)


@router.post("/{job_id}/tray/start")
async def start_import_tray(
	job_id: str,
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Start filling a tray with the items still waiting."""
	async with _lock:
		job = _require_open(job_id)
		job["tray_phase"] = "filling"
		_recount(job)
		return _result(job, None)


@router.post("/{job_id}/tray/place")
async def place_import_unit(
	job_id: str,
	body: PlaceImportUnit,
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Record that the operator put one measured item into the tray."""
	async with _lock:
		job = _require_open(job_id)
		unit = _find_unit(job, body.unit_id)
		if unit["status"] not in {"pending", "in_progress"}:
			raise api_error(409, "unit_handled", "This item was already handled")
		dimensions = dict(unit["dimensions"] or {})
		dimensions["height"] = body.height
		unit["dimensions"] = dimensions
		unit["status"] = "in_progress"
		unit["stage"] = "placed"
		job["tray_phase"] = "filling"
		_recount(job)
		return _result(job, unit)


@router.post("/{job_id}/tray/return")
async def return_import_tray(
	job_id: str,
	_: Any = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
	"""Treat items placed in the tray as loaded and leave the tray step."""
	async with _lock:
		job = _require_open(job_id)
		for unit in job["units"]:
			if unit["stage"] == "placed":
				unit["status"] = "loaded"
		job["tray_phase"] = None
		_recount(job)
		_finish_if_done(job)
	await _publish_import_job_updated()
	return _result(job, None)
