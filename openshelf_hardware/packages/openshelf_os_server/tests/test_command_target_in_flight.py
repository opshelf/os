from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete, update

from openshelf_os_server.core.command_lifecycle import CommandTargetInFlightError, admit
from openshelf_os_server.database.models import RobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal


async def _clear_commands() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(RobotCommand))
		await session.commit()


async def _finish_commands() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(
			update(RobotCommand)
			.where(RobotCommand.status.in_(("queued", "running")))
			.values(status="succeeded")
		)
		await session.commit()


@pytest.fixture
async def clean_commands():
	await _clear_commands()
	yield
	await _clear_commands()


def _location(cell_idx: int = 0) -> dict[str, Any]:
	return {"side": "front_module", "shelf_idx": 0, "cell_idx": cell_idx}


def _retrieve(barcode: str, cell_idx: int = 0) -> dict[str, Any]:
	return {"command": "retrieve_item", "barcode": barcode, "location": _location(cell_idx)}


def _store(barcode: str, location: dict[str, Any] | None = None) -> dict[str, Any]:
	item: dict[str, Any] = {"barcode": barcode, "name": barcode}
	if location is not None:
		item["location"] = location
	return {"command": "store_item", "item": item}


@pytest.mark.anyio
async def test_second_retrieve_of_the_same_item_is_rejected_while_the_first_is_active(clean_commands) -> None:
	await admit(_retrieve("inflight-a"), request_id=None, correlation_id=None)

	with pytest.raises(CommandTargetInFlightError):
		await admit(_retrieve("inflight-a"), request_id=None, correlation_id=None)
	# The same cell is busy even when a client omits or mistypes the barcode.
	with pytest.raises(CommandTargetInFlightError):
		await admit(
			{"command": "retrieve_item", "location": _location()},
			request_id=None,
			correlation_id=None,
		)

	_, was_replay = await admit(_retrieve("inflight-b", cell_idx=1), request_id=None, correlation_id=None)
	assert was_replay is False

	await _finish_commands()
	_, was_replay = await admit(_retrieve("inflight-a"), request_id=None, correlation_id=None)
	assert was_replay is False


@pytest.mark.anyio
async def test_store_conflicts_by_barcode_and_requested_cell(clean_commands) -> None:
	await admit(_store("inflight-store", _location(2)), request_id=None, correlation_id=None)

	with pytest.raises(CommandTargetInFlightError):
		await admit(_store("inflight-store"), request_id=None, correlation_id=None)
	with pytest.raises(CommandTargetInFlightError):
		await admit(_retrieve("other-item", cell_idx=2), request_id=None, correlation_id=None)

	# Automatic allocation of a different item does not target a known cell.
	_, was_replay = await admit(_store("inflight-other"), request_id=None, correlation_id=None)
	assert was_replay is False
