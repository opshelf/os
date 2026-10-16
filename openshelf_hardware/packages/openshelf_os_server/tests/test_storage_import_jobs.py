"""Import jobs: expansion, unit and tray steps, results, cancellation, and the router."""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from openshelf_os.common import OperatingMode
from openshelf_os.config.settings import StorageBinSettings
from openshelf_os_server.core import bin_storage, mock_robot
from openshelf_os_server.core import import_jobs as import_jobs_core
from openshelf_os_server.core.bin_storage import BinStorageService
from openshelf_os_server.core.import_jobs import ImportJobService, ImportRowInput, notify_command_result
from openshelf_os_server.database.models import ImportJob, ImportJobUnit, RobotCommand, StorageBin, StorageBinItem, StorageBinWorkflow
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.routers import import_jobs as import_jobs_router
from openshelf_os_server.routers.auth_deps import RELAY_SERVICE_TOKEN_SHA256_ENV

RELAY_SERVICE_TOKEN = "os_service.test-relay-token-with-at-least-32-characters"
DIMENSIONS = {"width": 50.0, "height": 60.0, "length": 70.0}


def tray(cell=0, rows=1, columns=2):
    return StorageBinSettings.model_validate({
        "barcode": f"90000000000{cell}",
        "home_location": {"side": "front_module", "shelf_idx": 0, "cell_idx": cell},
        "inner_dimensions": {"width": 200, "length": 160, "height": 100},
        "compartment_layout": {"rows": rows, "columns": columns},
    })


class FakeRuntime:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.occupied = False
        self.has_active_commands = AsyncMock(return_value=False)
        self.publish_status = AsyncMock()

    async def publish_event(self, payload: dict) -> None:
        self.events.append(payload)

    def set_bin_io_box_occupied(self, occupied: bool) -> None:
        self.occupied = occupied

    def is_bin_io_box_occupied(self) -> bool:
        return self.occupied


@pytest.fixture
def mock_mode(monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(import_jobs_core, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(mock_robot, "mock_robot_runtime", runtime)
    monkeypatch.setattr(import_jobs_router, "get_mock_robot_runtime", lambda: runtime)
    return runtime


@pytest.fixture
def trays(monkeypatch):
    entries = [tray()]
    monkeypatch.setattr(
        bin_storage, "get_root_config", lambda: SimpleNamespace(storage=SimpleNamespace(bins=entries))
    )
    return entries


def row(name="Acetone", **values):
    return ImportRowInput(name=name, **values)


async def create(rows, filename="stock.csv"):
    async with AsyncSessionLocal() as db:
        job = await ImportJobService(db).create(rows, filename)
        return job.id


async def units(job_id):
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.scalars(
                    select(ImportJobUnit).where(ImportJobUnit.job_id == job_id).order_by(ImportJobUnit.position)
                )
            ).all()
        )


async def job_state(job_id):
    async with AsyncSessionLocal() as db:
        return (await db.get(ImportJob, job_id)).state


@pytest.mark.anyio
async def test_reconcile_applies_results_that_arrived_without_an_event(mock_mode):
    job_id = await create([row(barcode="ACE-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        unit = await service.begin_unit(job, (await units(job_id))[0].id, DIMENSIONS)
        db.add(RobotCommand(
            command_id="import-reconcile-1", runtime="mock", request_id=unit.request_id,
            payload={"command": "store_item"}, status="running",
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        await service.reconcile(job)
        await db.refresh(unit)
        assert unit.stage == "storing"
        command = await db.get(RobotCommand, "import-reconcile-1")
        command.status = "succeeded"
        await db.commit()
        await service.reconcile(job)
        await db.refresh(unit)
        assert unit.status == "loaded"
        assert job.state == "completed"


@pytest.mark.anyio
async def test_relay_cancel_lets_the_measured_unit_finish(mock_mode):
    job_id = await create([row(barcode="ACE-1"), row("Later", barcode="LATER-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        unit = await service.begin_unit(job, (await units(job_id))[0].id, DIMENSIONS)
        request_id = unit.request_id
        await service.cancel(job, "relay")
        assert job.state == "cancelling" and job.cancel_source == "relay"
        assert (await service.next_step(job))["unit"]["barcode"] == "ACE-1"
    await notify_command_result(request_id, {"command": "store_item", "message": "success"})
    assert await job_state(job_id) == "cancelled"
    statuses = [unit.status for unit in await units(job_id)]
    assert statuses == ["loaded", "pending"]


@pytest.mark.anyio
async def test_stopping_hands_the_tray_to_the_standard_return(mock_mode, trays):
    job_id = await create([row("Vial", small=True, quantity=2)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        first = (await units(job_id))[0]
        placed = await service.place_in_tray(job, first.id, 30)
        await service.cancel(job, "kiosk")
        # Nothing is moving, so the import ends at once and leaves the tray outside.
        assert job.state == "cancelled"
        await db.refresh(workflow)
        assert workflow.state == "completed"
        assert await service.next_step(job) is None

        returning = await bins.begin_return("900000000000")
        assert returning.operation == "return"
        assert returning.resolution["candidate_item_ids"] == [placed.item_id]
        assert await bins.prepare_return(returning, [placed.item_id]) is True
        await bins.complete_return(returning)
        tray_row = await db.scalar(select(StorageBin))
        assert tray_row.status == "at_home"
        member = await db.scalar(select(StorageBinItem).where(StorageBinItem.item_id == placed.item_id))
        assert member is not None and member.compartment == 1
    assert [unit.status for unit in await units(job_id)] == ["loaded", "pending"]


@pytest.mark.anyio
async def test_stopping_during_tray_delivery_releases_the_tray_when_it_arrives(mock_mode, trays):
    job_id = await create([row("Vial", small=True)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await service.cancel(job, "relay")
        assert job.state == "cancelling"
        assert (await service.next_step(job))["phase"] == "fetching"
        await bins.transition(workflow, "awaiting_operator")
    await notify_command_result(workflow.id, {"command": "retrieve_item", "message": "success"})
    assert await job_state(job_id) == "cancelled"
    async with AsyncSessionLocal() as db:
        assert (await db.get(StorageBinWorkflow, workflow.id)).state == "completed"


@pytest.mark.anyio
async def test_stopping_waits_for_a_tray_return_that_is_moving(mock_mode, trays):
    job_id = await create([row("Vial", small=True)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        await service.place_in_tray(job, (await units(job_id))[0].id, 30)
        returning = await service.begin_tray_return(job)
        await service.cancel(job, "kiosk")
        assert job.state == "cancelling"
        assert (await service.next_step(job))["phase"] == "returning"
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)
        assert job.state == "cancelled"
    assert [unit.status for unit in await units(job_id)] == ["loaded"]


@pytest.mark.anyio
async def test_router_cancel_source_and_permissions(mock_mode, client, unauthenticated_client, monkeypatch):
    await _ensure_developer_user()
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, hashlib.sha256(RELAY_SERVICE_TOKEN.encode()).hexdigest())
    relay = {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}
    created = await client.post("/api/import-jobs", json={"rows": [{"name": "Acetone", "barcode": "ACE-9"}]})
    assert created.status_code == 200, created.text
    job_id = created.json()["job"]["id"]

    kiosk_only = await unauthenticated_client.post(f"/api/import-jobs/{job_id}/tray/start", headers=relay)
    assert kiosk_only.status_code == 401

    cancelled = await unauthenticated_client.post(f"/api/import-jobs/{job_id}/cancel", headers=relay)
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["state"] == "cancelled"
    assert cancelled.json()["job"]["cancel_source"] == "relay"
    again = await unauthenticated_client.post(f"/api/import-jobs/{job_id}/cancel", headers=relay)
    assert again.json()["job"]["state"] == "cancelled"

    missing = await client.get("/api/import-jobs/does-not-exist")
    assert missing.status_code == 404
    invalid = await client.post("/api/import-jobs", json={"rows": [{"name": "A", "net_weight": 5}]})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["errors"][0]["field"] == "weight"


@pytest.mark.anyio
async def test_a_tray_return_is_only_moved_once(mock_mode, trays):
    job_id = await create([row("Vial", small=True)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        unit = (await units(job_id))[0]
        await service.place_in_tray(job, unit.id, 30)
        first = await service.begin_tray_return(job)
        assert first is not None and first.state == "returning_bin"
        # A stale retry that still sees the unit placed must not start a second motion.
        stale = await db.get(ImportJobUnit, unit.id)
        stale.stage = "placed"
        await db.commit()
        assert await service.begin_tray_return(job) is None
        await db.refresh(stale)
        assert stale.stage == "returning" and stale.request_id == first.id
