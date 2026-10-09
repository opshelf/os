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
from openshelf_os_server.core.bin_storage import BinStorageService, BinWorkflowConflict
from openshelf_os_server.core.import_jobs import (
    ImportJobError,
    ImportJobService,
    ImportRowInput,
    generate_barcode,
    notify_command_result,
)
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import (
    AccessLevel,
    ImportJob,
    ImportJobUnit,
    Item,
    RobotCommand,
    StorageBin,
    StorageBinItem,
)
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


def test_generated_barcodes_are_upc_a_with_a_valid_check_digit():
    for _ in range(20):
        barcode = generate_barcode()
        assert len(barcode) == 12 and barcode.isdigit() and barcode[0] == "2"
        digits = [int(digit) for digit in barcode]
        assert (sum(digits[0:11:2]) * 3 + sum(digits[1:11:2]) + digits[11]) % 10 == 0


@pytest.mark.anyio
async def test_rows_expand_in_order_with_label_and_generated_barcodes(mock_mode):
    job_id = await create([
        row("Acetone", sku="ACE-1", quantity=3, metadata={"Supplier": "Acme"}),
        row("Ethanol", barcode="0123456789", sku="ETH-1"),
        row("Gloves", small=True, quantity=2),
    ])
    loaded = await units(job_id)
    assert [unit.name for unit in loaded] == ["Acetone"] * 3 + ["Ethanol"] + ["Gloves"] * 2
    assert [unit.position for unit in loaded] == list(range(6))
    assert loaded[0].barcode == "ACE-1"
    assert loaded[3].barcode == "0123456789"
    generated = [loaded[1].barcode, loaded[2].barcode, loaded[4].barcode, loaded[5].barcode]
    assert len(set(generated)) == 4 and all(len(code) == 12 for code in generated)
    assert all(unit.sku == "ACE-1" for unit in loaded[:3])
    assert loaded[0].unit_metadata == {"Supplier": "Acme"}
    assert [unit.small for unit in loaded] == [False] * 4 + [True] * 2


@pytest.mark.anyio
async def test_create_rejects_existing_barcodes_invalid_rows_and_a_second_job(mock_mode):
    async with AsyncSessionLocal() as db:
        await StorageService(db).create_item("DUP-1", "Existing", DIMENSIONS)
    with pytest.raises(ImportJobError) as existing:
        await create([row(barcode="DUP-1"), row(sku="NEW-1")])
    assert existing.value.status_code == 409
    assert existing.value.code == "barcodes_exist"
    assert [entry["barcode"] for entry in existing.value.errors] == ["DUP-1"]

    with pytest.raises(ImportJobError) as invalid:
        await create([
            row(barcode="A", row_number=2),
            row(barcode="A", row_number=3),
            row(access_level="Level 99", row_number=4),
            row(expires_at="31/12/2026", row_number=5),
            row(net_weight=10, row_number=6),
        ])
    assert invalid.value.status_code == 422
    assert {(entry["row"], entry["field"]) for entry in invalid.value.errors} == {
        (3, "barcode"), (4, "access_level"), (5, "expires_at"), (6, "weight")
    }

    with pytest.raises(ImportJobError) as too_many:
        await create([row(quantity=3000), row(quantity=2001)])
    assert too_many.value.code == "too_many_units"

    await create([row()])
    with pytest.raises(ImportJobError) as second:
        await create([row("Other")])
    assert second.value.code == "import_active"


@pytest.mark.anyio
async def test_access_levels_resolve_by_name_or_id(mock_mode):
    async with AsyncSessionLocal() as db:
        level = await db.scalar(select(AccessLevel).where(AccessLevel.name == "Level 2"))
        if level is None:
            level = AccessLevel(name="Level 2")
            db.add(level)
            await db.commit()
        level_id = level.id
    job_id = await create([row(access_level="level 2"), row(access_level=str(level_id)), row(access_level=None)])
    assert [unit.access_level_id for unit in await units(job_id)] == [level_id, level_id, None]


@pytest.mark.anyio
async def test_unit_is_loaded_when_its_store_command_succeeds(mock_mode):
    job_id = await create([row(sku="ACE-1"), row("Ethanol", sku="ETH-1")])
    first, second = await units(job_id)
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        step = await service.next_step(job)
        assert step["kind"] == "unit" and step["unit"]["id"] == first.id
        with pytest.raises(ImportJobError) as out_of_order:
            await service.begin_unit(job, second.id, DIMENSIONS)
        assert out_of_order.value.code == "not_current"
        unit = await service.begin_unit(job, first.id, DIMENSIONS)
        assert unit.status == "in_progress" and unit.stage == "measured"
        assert unit.request_id == f"import-{job_id}-0-1"
        item = await db.get(Item, unit.item_id)
        assert item.barcode == "ACE-1"
        assert item.dimensions == DIMENSIONS
        assert item.item_metadata["sku"] == "ACE-1"
        request_id = unit.request_id
        # The measured unit stays the kiosk's step until it is stored.
        assert (await service.next_step(job))["unit"]["id"] == first.id

    await notify_command_result(request_id, {"command": "store_item", "message": "success"})
    loaded = (await units(job_id))[0]
    assert loaded.status == "loaded" and loaded.stage is None
    assert mock_mode.events[-1]["kind"] == "import_job_updated"
    assert mock_mode.events[-1]["payload"]["counts"]["loaded"] == 1
    assert await job_state(job_id) == "active"


@pytest.mark.anyio
async def test_failed_store_keeps_the_unit_with_a_fresh_request_id(mock_mode):
    job_id = await create([row(sku="ACE-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        unit = await service.begin_unit(job, (await units(job_id))[0].id, DIMENSIONS)
        request_id = unit.request_id
    await notify_command_result(
        request_id, {"command": "store_item", "message": "failed", "error": "No suitable storage location found"}
    )
    unit = (await units(job_id))[0]
    assert unit.status == "in_progress" and unit.stage == "measured"
    assert unit.error == "No suitable storage location found"
    assert unit.request_id == f"import-{job_id}-0-2"


@pytest.mark.anyio
async def test_reconcile_applies_results_that_arrived_without_an_event(mock_mode):
    job_id = await create([row(sku="ACE-1")])
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
async def test_unit_is_auto_skipped_when_no_cell_fits(mock_mode):
    job_id = await create([row(sku="HUGE-1"), row("Next", sku="NEXT-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        unit = await service.begin_unit(job, (await units(job_id))[0].id, {"width": 900, "height": 10, "length": 10})
        assert unit.status == "skipped"
        assert unit.skip_reason == "No free storage cell fits this item"
        assert unit.item_id is None
        assert await StorageService(db).get_item("HUGE-1") is None
        assert (await service.next_step(job))["unit"]["barcode"] == "NEXT-1"


@pytest.mark.anyio
async def test_skip_records_the_reason_and_deletes_the_measured_item(mock_mode):
    job_id = await create([row(sku="ACE-1"), row("Other", sku="OTH-1")])
    first, second = await units(job_id)
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        await service.begin_unit(job, first.id, DIMENSIONS)
        with pytest.raises(ImportJobError):
            await service.skip_unit(job, first.id, "other", "  ")
        skipped = await service.skip_unit(job, first.id, "damaged", "label torn")
        assert skipped.skip_reason == "Damaged: label torn"
        assert await StorageService(db).get_item("ACE-1") is None
        await service.skip_unit(job, second.id, "not_present")
        assert job.state == "completed"


@pytest.mark.anyio
async def test_relay_cancel_lets_the_measured_unit_finish(mock_mode):
    job_id = await create([row(sku="ACE-1"), row("Later", sku="LATER-1")])
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
async def test_kiosk_stop_releases_an_unsubmitted_unit(mock_mode):
    job_id = await create([row(sku="ACE-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        await service.begin_unit(job, (await units(job_id))[0].id, DIMENSIONS)
        await service.cancel(job, "kiosk")
        assert job.state == "cancelled"
        assert await StorageService(db).get_item("ACE-1") is None
    unit = (await units(job_id))[0]
    assert unit.status == "pending" and unit.item_id is None


@pytest.mark.anyio
async def test_small_units_fill_one_tray_and_load_on_return(mock_mode, trays):
    job_id = await create([
        row("Vial", small=True, quantity=2),
        row("Bottle", sku="BOT-1"),
        row("Pipette", small=True),
    ])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        step = await service.next_step(job)
        # Small units of one access level form one group, across the normal unit between them.
        assert step["kind"] == "tray" and step["phase"] == "start"
        assert [unit["name"] for unit in step["units"]] == ["Vial", "Vial", "Pipette"]
        workflow = await service.start_tray(job, None)
        assert workflow.operation == "batch_store"
        assert (await service.next_step(job))["phase"] == "fetching"
        await bins.transition(workflow, "awaiting_operator")
        assert (await service.next_step(job))["phase"] == "filling"

        vial_one, vial_two, _, pipette = await units(job_id)
        first = await service.place_in_tray(job, vial_one.id, 40)
        second = await service.place_in_tray(job, vial_two.id, 40)
        assert (first.compartment, second.compartment) == (1, 2)
        assert first.stage == "placed"
        with pytest.raises(ImportJobError) as full:
            await service.place_in_tray(job, pipette.id, 40)
        assert full.value.code == "tray_full"

        returning = await service.begin_tray_return(job)
        assert returning.operation == "return" and returning.state == "returning_bin"
        assert (await service.next_step(job))["phase"] == "returning"
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        statuses = [unit.status for unit in await units(job_id)]
        assert statuses == ["loaded", "loaded", "pending", "pending"]
        members = (await db.scalars(select(StorageBinItem.compartment))).all()
        assert sorted(members) == [1, 2]
        tray_row = await db.scalar(select(StorageBin))
        assert tray_row.status == "at_home"
        # The normal unit comes next, then the next tray for the remaining small unit.
        assert (await service.next_step(job))["unit"]["name"] == "Bottle"


@pytest.mark.anyio
async def test_too_tall_small_unit_is_skipped_and_no_tray_skips_the_group(mock_mode, trays):
    job_id = await create([row("Vial", small=True, quantity=2)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        first, second = await units(job_id)
        tall = await service.place_in_tray(job, first.id, 150)
        assert tall.status == "skipped"
        assert tall.skip_reason == "Too tall for the tray (at most 100 mm)"
        assert await StorageService(db).get_item(first.barcode) is None
        await service.skip_unit(job, second.id, "not_present")
        # The empty tray still has to go back before the job ends.
        assert job.state == "active"
        returning = await service.begin_tray_return(job)
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)
        assert job.state == "completed"

    trays.clear()
    other = await create([row("Swab", small=True)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(other)
        assert await service.start_tray(job, None) is None
        assert job.state == "completed"
    assert (await units(other))[0].skip_reason == "No tray has a free compartment for this access level"


@pytest.mark.anyio
async def test_stray_tray_scan_cannot_cancel_the_import_tray(mock_mode, trays):
    job_id = await create([row("Vial", small=True)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        with pytest.raises(BinWorkflowConflict, match="import screen"):
            await bins.begin_return("900000000000")
        await db.refresh(workflow)
        with pytest.raises(BinWorkflowConflict, match="import screen"):
            await bins.close(workflow)
        await db.refresh(workflow)
        assert workflow.state == "awaiting_operator"


@pytest.mark.anyio
async def test_cancel_during_a_tray_waits_for_its_return(mock_mode, trays):
    job_id = await create([row("Vial", small=True, quantity=2)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        first = (await units(job_id))[0]
        await service.place_in_tray(job, first.id, 30)
        await service.cancel(job, "kiosk")
        assert job.state == "cancelling"
        step = await service.next_step(job)
        assert step["kind"] == "tray" and step["phase"] == "filling"
        assert [unit["status"] for unit in step["units"]] == ["in_progress"]
        returning = await service.begin_tray_return(job)
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)
        assert job.state == "cancelled"
    assert [unit.status for unit in await units(job_id)] == ["loaded", "pending"]


@pytest.mark.anyio
async def test_router_flow_for_relay_and_kiosk(mock_mode, trays, client, unauthenticated_client, monkeypatch):
    await _ensure_developer_user()
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, hashlib.sha256(RELAY_SERVICE_TOKEN.encode()).hexdigest())
    relay = {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}

    created = await unauthenticated_client.post(
        "/api/import-jobs",
        headers=relay,
        json={"filename": "stock.csv", "rows": [
            {"row_number": 2, "sku": "ACE-1", "name": "Acetone", "quantity": 1},
            {"row_number": 3, "name": "Vial", "quantity": 1, "small": True},
        ]},
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["job"]["id"]
    assert created.json()["job"]["counts"] == {"total": 2, "pending": 2, "in_progress": 0, "loaded": 0, "skipped": 0}

    duplicate = await unauthenticated_client.post(
        "/api/import-jobs", headers=relay, json={"rows": [{"name": "X"}]}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "import_active"

    active = await client.get("/api/import-jobs/active")
    step = active.json()["job"]["step"]
    assert step["kind"] == "unit" and step["unit"]["barcode"] == "ACE-1"

    begun = await client.post(
        f"/api/import-jobs/{job_id}/units/{step['unit']['id']}/begin", json={"dimensions": DIMENSIONS}
    )
    assert begun.status_code == 200, begun.text
    request_id = begun.json()["unit"]["request_id"]
    await notify_command_result(request_id, {"command": "store_item", "message": "success"})

    tray_step = (await client.get("/api/import-jobs/active")).json()["job"]["step"]
    assert tray_step["kind"] == "tray" and tray_step["phase"] == "start"
    started = await client.post(f"/api/import-jobs/{job_id}/tray/start")
    assert started.status_code == 200, started.text
    assert started.json()["job"]["step"]["phase"] == "filling"
    assert mock_mode.occupied is True
    vial_id = tray_step["units"][0]["id"]
    placed = await client.post(f"/api/import-jobs/{job_id}/tray/place", json={"unit_id": vial_id, "height": 30})
    assert placed.status_code == 200, placed.text
    assert placed.json()["unit"]["compartment"] == 1

    returned = await client.post(f"/api/import-jobs/{job_id}/tray/return")
    assert returned.status_code == 200, returned.text
    assert returned.json()["job"]["state"] == "completed"
    assert returned.json()["job"]["counts"]["loaded"] == 2
    assert mock_mode.occupied is False

    read = await unauthenticated_client.get(f"/api/import-jobs/{job_id}?units=true", headers=relay)
    assert read.status_code == 200
    assert [unit["status"] for unit in read.json()["job"]["units"]] == ["loaded", "loaded"]
    assert (await client.get("/api/import-jobs/active")).json() == {"job": None}
    assert any(event["kind"] == "import_job_updated" for event in mock_mode.events)


@pytest.mark.anyio
async def test_router_cancel_source_and_permissions(mock_mode, client, unauthenticated_client, monkeypatch):
    await _ensure_developer_user()
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, hashlib.sha256(RELAY_SERVICE_TOKEN.encode()).hexdigest())
    relay = {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}
    created = await client.post("/api/import-jobs", json={"rows": [{"name": "Acetone", "sku": "ACE-9"}]})
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
