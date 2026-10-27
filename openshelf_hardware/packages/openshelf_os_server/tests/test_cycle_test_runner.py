from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from openshelf_os.exceptions import ToolError
from openshelf_os.operating_mode import OperatingMode
from openshelf_os_server.core import cycle_test, robot as robot_core
from openshelf_os_server.database.models import Item
from openshelf_os_server.database.session import AsyncSessionLocal
from test_calibration_test_queue import build_grid, build_worker, patch_worker


@pytest.mark.asyncio
async def test_clear_run_is_refused_outside_development_and_mock(client, monkeypatch) -> None:
	# The run deletes every item when asked to clear; a production device must keep its inventory.
	async with AsyncSessionLocal() as db:
		db.add(Item(barcode="KEEP-1", name="keep", dimensions={"width": 10, "height": 10, "length": 10}))
		await db.commit()
	monkeypatch.setattr(cycle_test, "get_operating_mode", lambda: OperatingMode.production)

	resp = await client.post("/api/calibration/cycle-test", json={"mode": "store_retrieve", "clear": True})

	assert resp.status_code == 409
	assert resp.json()["detail"]["code"] == "cycle_test_mode"
	async with AsyncSessionLocal() as db:
		assert (await db.execute(select(Item).where(Item.barcode == "KEEP-1"))).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_run_stops_moving_after_the_first_failing_cell(client, monkeypatch) -> None:
	# A failed cell may leave the tool in an unknown state; the run must not go on to the next cell.
	grid = build_grid()
	pick = AsyncMock(side_effect=ToolError("gripper lost the tray"))
	worker = build_worker(grid, pick)
	worker.get_status_snapshot = AsyncMock(return_value=None)
	monkeypatch.setattr(robot_core, "robot_worker", worker)
	monkeypatch.setattr(cycle_test, "get_robot_worker", AsyncMock(return_value=worker))
	monkeypatch.setattr(cycle_test, "HARDWARE_FAULT_SETTLE_S", 0)
	manager = cycle_test.CycleTestManager()
	monkeypatch.setattr(cycle_test, "_manager", manager)

	with patch_worker(worker)[0], patch_worker(worker)[1], patch(
		"openshelf_os_server.routers.calibration._raise_motor_error",
		AsyncMock(side_effect=lambda *_: (_ for _ in ()).throw(RuntimeError("motor error"))),
	):
		resp = await client.post("/api/calibration/cycle-test", json={"mode": "quick", "target": "F:0"})
		assert resp.status_code == 200
		await manager.wait()

	snapshot = (await client.get("/api/calibration/cycle-test")).json()
	assert snapshot["state"] == "failed"
	assert pick.await_count == 1
	assert [r["cell"] for r in snapshot["results"]] == ["F:0:0"]
	assert snapshot["results"][0]["passed"] is False


@pytest.mark.asyncio
async def test_create_item_skips_past_many_leftover_test_items(client) -> None:
	# Retrieve runs leave their items behind; a later run must still get a fresh barcode, not give up.
	dims = {"width": 10, "height": 10, "length": 10}
	async with AsyncSessionLocal() as db:
		db.add_all(
			Item(barcode=f"{cycle_test.BARCODE_PREFIX}{n:06d}", name="old", dimensions=dims) for n in range(1, 151)
		)
		await db.commit()
	runner = object.__new__(cycle_test.CycleTestRunner)
	runner.barcode_counter = None
	runner.client = AsyncMock()
	runner.client.post.return_value.status_code = 200

	barcode, error = await runner.create_item({"width": 10, "height": 10, "length": 10})

	assert error is None
	assert barcode == f"{cycle_test.BARCODE_PREFIX}000151"
