import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from openshelf_os_server.core.bin_storage import BinStorageService, BinWorkflowConflict
from openshelf_os_server.database.models import (
    Base,
    Item,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
)


def _upgrade(connection):
    path = (
        Path(__file__).parents[1]
        / "src/openshelf_os_server/alembic/versions/b5c6d7e8f9a0_independent_tray_returns.py"
    )
    spec = importlib.util.spec_from_file_location("tray_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()


def test_upgrade_preserves_contents_and_removes_global_claim_columns():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        sa.Table("items", metadata, sa.Column("id", sa.Integer, primary_key=True))
        bins = sa.Table(
            "storage_bins",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("status", sa.String),
            sa.Column("gross_weight", sa.Float),
        )
        workflows = sa.Table(
            "storage_bin_workflows",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("bin_id", sa.Integer, sa.ForeignKey("storage_bins.id")),
            sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
            sa.Column("state", sa.String),
            sa.Column("operation", sa.String),
            sa.Column("resolution", sa.JSON),
            sa.Column("history", sa.JSON),
            sa.Column("compartment", sa.Integer),
            sa.Column("lock_key", sa.String, unique=True),
            sa.Column("recovery_claim", sa.String),
        )
        metadata.create_all(connection)
        connection.execute(bins.insert().values(id=1, status="quarantined", gross_weight=500))
        connection.execute(
            workflows.insert().values(
                id="old",
                bin_id=1,
                item_id=1,
                state="recovery_required",
                operation="retrieve",
                lock_key="active",
                resolution={"candidate_compartments": {"1": 2}},
            )
        )
        _upgrade(connection)
        migrated = sa.Table("storage_bin_workflows", sa.MetaData(), autoload_with=connection)
        assert "lock_key" not in migrated.c and "recovery_claim" not in migrated.c
        assert migrated.c.item_id.nullable
        row = connection.execute(sa.select(migrated)).mappings().one()
        assert row["state"] == "failed"
        assert row["resolution"] == {"candidate_compartments": {"1": 2}}
        assert connection.execute(sa.select(bins.c.status, bins.c.gross_weight)).one() == (
            "outside",
            None,
        )
        connection.execute(
            migrated.insert().values(
                id="return", bin_id=1, item_id=None, state="awaiting_operator", operation="return"
            )
        )


@pytest.fixture
async def legacy_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    workflows = metadata.tables["storage_bin_workflows"]
    workflows.c.item_id.nullable = False
    workflows.append_column(sa.Column("lock_key", sa.String))
    workflows.append_column(sa.Column("recovery_claim", sa.String))
    workflows.append_constraint(sa.UniqueConstraint("lock_key"))
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_legacy_return(engine, operation, resolution, state, *, physical=True):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all(
            Item(
                id=i,
                upca=f"12345678900{i}",
                name=f"Item {i}",
                dimensions={"width": 1, "height": 1, "length": 1},
                enrollment_weight_pending=True,
                enrollment_storage_completed=False,
            )
            for i in [1, 2, 3]
        )
        session.add(
            StorageBin(
                id=1,
                barcode="900000000001",
                side="left_module",
                shelf_idx=0,
                cell_idx=0,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                compartment_layout={"rows": 1, "columns": 3},
                max_items=3,
                status="checked_out",
                gross_weight=100,
            )
        )
        await session.flush()
        session.add(StorageBinItem(bin_id=1, item_id=2, compartment=2))
        if operation in {"retrieve", "restore"}:
            session.add(StorageBinItem(bin_id=1, item_id=1, compartment=1))
        session.add(
            StorageBinWorkflow(
                id="legacy",
                bin_id=1,
                item_id=1,
                compartment=1,
                operation=operation,
                state=state,
                resolution=resolution,
                history=[{"state": "returning_bin"}]
                + ([{"state": "physical_return_completed"}] if physical else []),
                error="Legacy inventory finalization interrupted",
            )
        )
        await session.commit()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state", ["returning_bin", "recovery_required", "retry_claimed", "reconcile_claimed"]
)
@pytest.mark.parametrize(
    "operation,resolution,expected_ids",
    [
        ("store", None, {1, 2}),
        ("store", {"target_item_in_bin": True}, {1, 2}),
        ("store", {"target_item_in_bin": False}, {2}),
        ("retrieve", None, {2}),
        ("retrieve", {"target_item_in_bin": False}, {2}),
        ("retrieve", {"target_item_in_bin": True}, {1, 2}),
        (
            "restore",
            {
                "candidate_item_ids": [1, 2, 3],
                "candidate_compartments": {"1": 1, "2": 2, "3": 3},
                "confirmed_item_ids": [2, 3],
            },
            {2, 3},
        ),
        (
            "restore",
            {
                "candidate_item_ids": [1, 2],
                "candidate_compartments": {"1": 1, "2": 2},
                "confirmed_item_ids": [],
            },
            set(),
        ),
    ],
)
async def test_upgrade_finalizes_legacy_physical_success_without_repeated_motion(
    legacy_engine, monkeypatch, operation, resolution, expected_ids, state
):
    await _seed_legacy_return(legacy_engine, operation, resolution, state)
    async with legacy_engine.begin() as connection:
        await connection.run_sync(_upgrade)
    enqueue = AsyncMock()
    monkeypatch.setattr(BinStorageService, "enqueue_return", enqueue)
    async with AsyncSession(legacy_engine, expire_on_commit=False) as session:
        service = BinStorageService(session)
        workflow = await session.get(StorageBinWorkflow, "legacy")
        assert workflow.operation == "return"
        assert workflow.state == "returning_bin"
        assert workflow.resolution["physical_return_completed"] is True
        assert set(workflow.resolution["confirmed_item_ids"]) == expected_ids
        assert {"state": "physical_return_completed"} in workflow.history
        if operation == "restore":
            for key, value in resolution.items():
                assert workflow.resolution[key] == value
        await service.recover_interrupted_workflows()
        tray = await session.get(StorageBin, 1)
        assert workflow.state == "completed"
        assert workflow.error is None
        assert tray.status == "at_home"
        assert tray.gross_weight is None
        members = list(await session.scalars(sa.select(StorageBinItem)))
        assert {(member.item_id, member.compartment) for member in members} == {
            (item_id, item_id) for item_id in expected_ids
        }
        for item_id in expected_ids:
            item = await session.get(Item, item_id)
            assert item.enrollment_storage_completed
            assert not item.enrollment_weight_pending
        history = list(workflow.history)
        await service.recover_interrupted_workflows()
        await service.complete_return(workflow)
        assert not await service.prepare_return(workflow, sorted(expected_ids))
        assert workflow.history == history
        assert len(list(await session.scalars(sa.select(StorageBinItem)))) == len(expected_ids)
        with pytest.raises(BinWorkflowConflict, match="already in the system"):
            await service.begin_return(tray.barcode)
    enqueue.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "operation,state,physical,expected_state",
    [
        ("store", "returning_bin", False, "failed"),
        ("retrieve", "recovery_required", False, "failed"),
        ("restore", "returning_bin", False, "cancelled"),
        ("store", "completed", True, "completed"),
        ("retrieve", "reconciled", True, "reconciled"),
        ("restore", "completed", True, "completed"),
        ("restore", "reconciled", True, "reconciled"),
    ],
)
async def test_upgrade_does_not_revive_terminal_or_unconfirmed_physical_returns(
    legacy_engine, operation, state, physical, expected_state
):
    await _seed_legacy_return(legacy_engine, operation, None, state, physical=physical)
    async with legacy_engine.begin() as connection:
        await connection.run_sync(_upgrade)
    async with AsyncSession(legacy_engine, expire_on_commit=False) as session:
        service = BinStorageService(session)
        await service.recover_interrupted_workflows()
        workflow = await session.get(StorageBinWorkflow, "legacy")
        tray = await session.get(StorageBin, 1)
        assert workflow.state == expected_state
        assert workflow.operation == operation
        assert workflow.resolution is None
        assert tray.status == "outside"
        assert await session.scalar(sa.select(sa.func.count()).select_from(StorageBinItem)) == (
            1 if operation == "store" else 2
        )


@pytest.mark.anyio
@pytest.mark.parametrize("conflict", ["capacity", "compartment"])
async def test_upgrade_keeps_failed_legacy_inventory_finalization_reserved(legacy_engine, conflict):
    await _seed_legacy_return(
        legacy_engine, "retrieve", {"target_item_in_bin": True}, "recovery_required"
    )
    async with legacy_engine.begin() as connection:
        if conflict == "capacity":
            await connection.execute(sa.update(StorageBin).values(max_items=1))
        else:
            await connection.execute(sa.update(StorageBinWorkflow).values(compartment=3))
        await connection.run_sync(_upgrade)
    async with AsyncSession(legacy_engine, expire_on_commit=False) as session:
        service = BinStorageService(session)
        for _ in range(2):
            await service.recover_interrupted_workflows()
            workflow = await session.get(StorageBinWorkflow, "legacy")
            assert workflow.state == "returning_bin"
            assert workflow.resolution["physical_return_completed"] is True
            assert workflow.error
            tray = await session.get(StorageBin, 1)
            assert tray.status == "outside"
            assert (await service.begin_return(tray.barcode)).id == workflow.id
            assert not await service.prepare_return(workflow, [1, 2])
        assert set(await session.scalars(sa.select(StorageBinItem.item_id))) == {1, 2}
