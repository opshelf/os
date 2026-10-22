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
from openshelf_os_server.core.import_jobs import ImportJobError, ImportJobService, ImportRowInput, notify_command_result
from openshelf_os_server.database.models import ImportJob, ImportJobUnit, RobotCommand, StorageBin, StorageBinItem, StorageBinWorkflow
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.routers import import_jobs as import_jobs_router
from openshelf_os_server.routers.auth_deps import RELAY_SERVICE_TOKEN_SHA256_ENV

RELAY_SERVICE_TOKEN = "os_service.test-relay-token-with-at-least-32-characters"
DIMENSIONS = {"width": 50.0, "height": 60.0, "length": 70.0}


def tray(cell=0, rows=1, columns=2, height=100):
    return StorageBinSettings.model_validate({
        "barcode": f"90000000000{cell}",
        "home_location": {"side": "front_module", "shelf_idx": 0, "cell_idx": cell},
        "inner_dimensions": {"width": 200, "length": 160, "height": height},
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


async def fill_and_return(service, bins, job, workflow):
    if workflow.state == "retrieving_bin":
        await bins.transition(workflow, "awaiting_operator")
    step = await service.next_step(job)
    names = []
    for unit in step["units"]:
        try:
            await service.place_in_tray(job, unit["id"], unit["planned_dimensions"]["height"])
        except ImportJobError as exc:
            # The kiosk offers every item that fits; the operator stops when the tray is full.
            if exc.code == "tray_full":
                break
            raise
        names.append(unit["name"])
    returning = await service.begin_tray_return(job)
    await bins.complete_return(returning)
    await service.apply_tray_return(returning)
    await service.settle(job)
    return names


@pytest.mark.anyio
async def test_small_items_go_to_a_tray_their_footprint_fits(mock_mode, trays):
    # The tester's file: two items for 2x4 compartments (48.5 x 79 mm), two for 1x2 (99 x 160 mm).
    # The 1x2 is homed first: both trays take two of the group, so the tighter 2x4 must win.
    trays[:] = [tray(0, rows=1, columns=2, height=160), tray(1, rows=2, columns=4, height=160)]
    job_id = await create([
        row("MT 3", small=True, height=90, length=40, width=40),
        row("MT 4", small=True, height=90, length=37, width=37),
        row("MT 5", small=True, height=130, length=65, width=65),
        row("MT 6", small=True, height=150, length=74, width=74),
    ])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        small_tray = await db.get(StorageBin, workflow.bin_id)
        assert small_tray.compartment_layout == {"rows": 2, "columns": 4}
        large = (await units(job_id))[2]
        await bins.transition(workflow, "awaiting_operator")
        with pytest.raises(ImportJobError) as refused:
            await service.place_in_tray(job, large.id, 130)
        assert refused.value.code == "unit_wrong_tray"
        assert await fill_and_return(service, bins, job, workflow) == ["MT 3", "MT 4"]

        assert [unit["name"] for unit in (await service.next_step(job))["units"]] == ["MT 5", "MT 6"]
        workflow = await service.start_tray(job, None)
        assert (await db.get(StorageBin, workflow.bin_id)).compartment_layout == {"rows": 1, "columns": 2}
        assert await fill_and_return(service, bins, job, workflow) == ["MT 5", "MT 6"]
        assert job.state == "completed"
    stored = await units(job_id)
    assert [unit.status for unit in stored] == ["loaded"] * 4
    assert stored[3].dimensions == {"width": 74.0, "length": 74.0, "height": 150.0}


@pytest.mark.anyio
async def test_small_items_take_the_tightest_tray_before_the_one_that_takes_most(mock_mode, trays):
    # The tester's file: nine items for 2x4 compartments, two for 1x2. The ninth must take a
    # second 2x4 instead of sharing a 1x2 with a larger item, which then needs a third trip.
    trays[:] = [
        tray(0, rows=2, columns=4, height=160),
        tray(1, rows=2, columns=4, height=160),
        tray(2, rows=1, columns=2, height=160),
        tray(3, rows=1, columns=2, height=160),
    ]
    job_id = await create(
        [row(f"S{index}", small=True, height=85, length=40, width=40) for index in range(1, 10)]
        + [row(f"L{index}", small=True, height=120, length=65, width=65) for index in range(1, 3)]
    )
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        trips = []
        while job.state == "active":
            workflow = await service.start_tray(job, None)
            layout = (await db.get(StorageBin, workflow.bin_id)).compartment_layout
            trips.append((layout["columns"], await fill_and_return(service, bins, job, workflow)))
    assert trips == [
        (4, [f"S{index}" for index in range(1, 9)]),
        (4, ["S9"]),
        (2, ["L1", "L2"]),
    ]


@pytest.mark.anyio
async def test_a_tighter_tray_holds_back_only_the_items_it_has_room_for(mock_mode, trays):
    # The 2x3 has one free compartment, so only S1 waits for it; S2 rides along with L1 instead
    # of costing a third trip.
    trays[:] = [tray(0, rows=2, columns=3, height=160), tray(1, rows=1, columns=2, height=160)]
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(
            await create([row(f"P{index}", small=True, height=85, length=40, width=40) for index in range(1, 6)])
        )
        workflow = await service.start_tray(job, None)
        assert await fill_and_return(service, bins, job, workflow) == [f"P{index}" for index in range(1, 6)]
        job = await service.get(
            await create([
                row("L1", small=True, height=120, length=80, width=80),
                row("S1", small=True, height=85, length=40, width=40),
                row("S2", small=True, height=85, length=40, width=40),
            ])
        )
        trips = []
        while job.state == "active":
            workflow = await service.start_tray(job, None)
            layout = (await db.get(StorageBin, workflow.bin_id)).compartment_layout
            trips.append((layout["columns"], await fill_and_return(service, bins, job, workflow)))
    assert trips == [(2, ["L1", "S2"]), (3, ["S1"])]


async def import_trips(service, bins, db, rows):
    """Run an import of `rows` to the end, as (compartment columns, names placed) per trip."""
    job = await service.get(await create(rows))
    trips = []
    while job.state == "active":
        workflow = await service.start_tray(job, None)
        layout = (await db.get(StorageBin, workflow.bin_id)).compartment_layout
        trips.append((layout["columns"], await fill_and_return(service, bins, job, workflow)))
    return trips


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("order", "expected"),
    [("LAB", [(2, ["L", "A"]), (3, ["B"])]), ("ABL", [(2, ["A", "L"]), (3, ["B"])])],
)
async def test_a_tighter_tray_keeps_its_last_compartment_for_an_item_only_it_takes(mock_mode, trays, order, expected):
    # The 2x3 has one free compartment: it must hold B, which only it takes, not A, which the
    # shallower 1x2 also takes; otherwise B is left with no tray, whatever the file order.
    trays[:] = [tray(0, rows=2, columns=3, height=160), tray(1, rows=1, columns=2, height=100)]
    items = {
        "L": row("L", small=True, height=90, length=80, width=80),
        "A": row("A", small=True, height=85, length=40, width=40),
        "B": row("B", small=True, height=130, length=40, width=40),
    }
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        prefill = [row(f"P{index}", small=True, height=130, length=40, width=40) for index in range(1, 6)]
        assert await import_trips(service, bins, db, prefill) == [(3, [f"P{index}" for index in range(1, 6)])]
        assert await import_trips(service, bins, db, [items[name] for name in order]) == expected


@pytest.mark.anyio
async def test_an_item_waits_for_the_tray_its_own_trip_fetches(mock_mode, trays):
    # Q fits all three trays and P only the tall 2x4, which has one free compartment. Q must go
    # to the 2x2, so P keeps the 2x4's compartment instead of losing it to Q's trip.
    trays[:] = [
        tray(0, rows=1, columns=2, height=100),
        tray(1, rows=2, columns=2, height=100),
        tray(2, rows=2, columns=4, height=160),
    ]
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        tall = [row(f"T{index}", small=True, height=130, length=40, width=40) for index in range(1, 8)]
        assert await import_trips(service, bins, db, tall) == [(4, [f"T{index}" for index in range(1, 8)])]
        wide = [row(f"W{index}", small=True, height=85, length=70, width=80) for index in range(1, 4)]
        assert await import_trips(service, bins, db, wide) == [(2, ["W1", "W2", "W3"])]
        assert await import_trips(service, bins, db, [
            row("L", small=True, height=90, length=80, width=80),
            row("Q", small=True, height=85, length=40, width=40),
            row("P", small=True, height=130, length=40, width=40),
        ]) == [(2, ["L"]), (2, ["Q"]), (4, ["P"])]


@pytest.mark.anyio
async def test_a_row_with_only_a_width_keeps_it_in_the_stored_item(mock_mode, trays):
    # 1x2 compartments are 99 x 160 mm: a width-only row must keep its own width and fall back
    # to the compartment's length, instead of taking the compartment's footprint for both.
    trays[:] = [tray(0, rows=1, columns=2)]
    job_id = await create([row("Narrow", small=True, width=90)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        unit = (await units(job_id))[0]
        await service.place_in_tray(job, unit.id, 90)
        returning = await service.begin_tray_return(job)
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)
    stored = (await units(job_id))[0]
    assert stored.status == "loaded"
    assert stored.dimensions == {"width": 90.0, "length": 160.0, "height": 90.0}


@pytest.mark.anyio
async def test_a_failed_delivery_stays_visible_when_no_pending_item_fits_the_tray(mock_mode, trays):
    # 2x4 compartments are 48.5 x 79 mm under a 160 mm height, so only the tall unit fits them.
    trays[:] = [tray(0, rows=2, columns=4, height=160), tray(1, rows=1, columns=2)]
    job_id = await create([
        row("Tall", small=True, height=120, length=40, width=40),
        row("Wide", small=True, height=90, length=74, width=74),
    ])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        assert (await db.get(StorageBin, workflow.bin_id)).compartment_layout == {"rows": 2, "columns": 4}
        await bins.transition(workflow, "failed")
        tall = (await units(job_id))[0]
        await service.skip_unit(job, tall.id)
        step = await service.next_step(job)
        # The delivery failure still needs an operator decision: retry, or return the tray.
        assert step["kind"] == "tray" and step["phase"] == "failed"
        assert [unit["name"] for unit in step["units"]] == ["Wide"]


@pytest.mark.anyio
async def test_a_small_item_no_compartment_takes_is_stored_on_its_own(mock_mode, trays):
    # 2x4 compartments are 48.5 x 79 mm: a 90 mm-wide item fits none, so it is not skipped but
    # offered as a single item, with the reason, and only it may skip the tray.
    trays[:] = [tray(0, rows=2, columns=4)]
    job_id = await create([
        row("Too wide", small=True, length=90, width=90),
        row("Vial", small=True, length=40, width=40),
    ])
    async with AsyncSessionLocal() as db:
        await BinStorageService(db).sync_config()  # as at startup
        service = ImportJobService(db)
        job = await service.get(job_id)
        step = await service.next_step(job)
        assert step["kind"] == "unit" and step["unit"]["name"] == "Too wide"
        assert step["reason"] == (
            "Too wide (90×90 mm) does not fit any multi-item bin compartment, so it is stored on its own."
        )
        with pytest.raises(ImportJobError) as refused:
            await service.start_tray(job, None)
        assert refused.value.code == "not_current"
        first, vial = await units(job_id)
        await service.begin_unit(job, first.id, {"width": 90, "length": 90, "height": 60})
        with pytest.raises(ImportJobError) as refused:
            await service.begin_unit(job, vial.id, DIMENSIONS)
        assert refused.value.code == "small_unit"
    assert [unit.status for unit in await units(job_id)] == ["in_progress", "pending"]


@pytest.mark.anyio
async def test_a_failed_delivery_yields_to_a_next_small_item_no_compartment_takes(mock_mode, trays):
    # Try again acts on the next item; a later item the tray takes must not keep the failed step.
    trays[:] = [tray(0, rows=2, columns=4)]
    job_id = await create([
        row("Vial", small=True, length=40, width=40),
        row("Beaker", small=True, length=90, width=90),
        row("Swab", small=True, length=40, width=40),
    ])
    async with AsyncSessionLocal() as db:
        await BinStorageService(db).sync_config()
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "failed")
        vial, beaker, _ = await units(job_id)
        await service.skip_unit(job, vial.id)
        step = await service.next_step(job)
        assert step["kind"] == "unit" and step["unit"]["name"] == "Beaker"
        assert step["reason"]
        await service.begin_unit(job, beaker.id, {"width": 90, "length": 90, "height": 60})
        # Measuring it dismisses the failed delivery: its store step shows, then a new fetch for Swab.
        step = await service.next_step(job)
        assert step["kind"] == "unit" and step["unit"]["name"] == "Beaker" and step["unit"]["stage"] == "measured"
        await service.skip_unit(job, beaker.id)
        step = await service.next_step(job)
        assert step["kind"] == "tray" and step["phase"] == "start"
        assert [unit["name"] for unit in step["units"]] == ["Swab"]
    assert [unit.status for unit in await units(job_id)] == ["skipped", "skipped", "pending"]


@pytest.mark.anyio
async def test_no_free_tray_refuses_the_fetch_and_leaves_the_skip_to_the_operator(mock_mode, trays):
    trays[:] = [tray(0, rows=1, columns=2)]
    job_id = await create([row(f"Vial {n}", small=True, length=40, width=40) for n in range(3)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        for unit in (await units(job_id))[:2]:
            await service.place_in_tray(job, unit.id, 30)
        returning = await service.begin_tray_return(job)
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)

        step = await service.next_step(job)
        assert step["phase"] == "start"
        assert step["reason"] == "No multi-item bin with a free compartment fits this item; skip it to continue"
        with pytest.raises(ImportJobError) as refused:
            await service.start_tray(job, None)
        assert refused.value.code == "no_tray_free"
    assert [unit.status for unit in await units(job_id)] == ["loaded", "loaded", "pending"]


@pytest.mark.anyio
async def test_an_empty_tray_return_shows_returning_until_the_tray_is_home(mock_mode, trays):
    job_id = await create([row(f"Vial {n}", small=True, length=40, width=40) for n in range(2)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        await service.skip_unit(job, (await units(job_id))[0].id)
        # On hardware the robot moves the tray after the request; it stays outside until then.
        returning = await service.begin_tray_return(job)
        step = await service.next_step(job)
        assert step["phase"] == "returning" and step["units"] == []
        await bins.complete_return(returning)
        await service.apply_tray_return(returning)
        await service.settle(job)
        step = await service.next_step(job)
        assert step["phase"] == "start" and step["reason"] is None


@pytest.mark.anyio
async def test_an_item_measured_too_tall_leaves_the_tray_instead_of_being_skipped(mock_mode, trays):
    trays[:] = [tray(0, rows=2, columns=4)]
    job_id = await create([row("Vial", small=True, length=40, width=40)])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        bins = BinStorageService(db)
        job = await service.get(job_id)
        workflow = await service.start_tray(job, None)
        await bins.transition(workflow, "awaiting_operator")
        unit = (await units(job_id))[0]
        placed = await service.place_in_tray(job, unit.id, 130)
        assert placed.status == "pending" and placed.item_id is None
        assert service.notice == (
            "Vial is taller than this bin's compartments (at most 100 mm). "
            "It is stored on its own after this bin."
        )
        # Nothing left for this tray, yet the empty tray can still go back before the next item.
        step = await service.next_step(job)
        assert step["phase"] == "filling" and step["units"] == []
        returning = await service.begin_tray_return(job)
        if returning is not None:
            await bins.complete_return(returning)
            await service.apply_tray_return(returning)
        await service.settle(job)
        assert (await db.get(StorageBin, workflow.bin_id)).status == "at_home"
        step = await service.next_step(job)
        assert step["kind"] == "unit" and step["unit"]["planned_dimensions"]["height"] == 130


@pytest.mark.anyio
async def test_no_free_cell_refuses_the_measure_instead_of_skipping(mock_mode):
    job_id = await create([row(barcode="ACE-1")])
    async with AsyncSessionLocal() as db:
        service = ImportJobService(db)
        job = await service.get(job_id)
        with pytest.raises(ImportJobError) as refused:
            await service.begin_unit(job, (await units(job_id))[0].id, {"width": 5000, "length": 5000, "height": 5000})
        assert refused.value.code == "no_cell_fits"
    assert (await units(job_id))[0].status == "pending"
