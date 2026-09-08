from types import SimpleNamespace

import pytest
from sqlalchemy import select

from openshelf_os.config.settings import StorageBinSettings
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.bin_storage import BinStorageService, BinWorkflowConflict
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import StorageBin, StorageBinItem, StorageBinWorkflow
from openshelf_os_server.database.session import AsyncSessionLocal


def tray(columns=4, rows=2, cell=0, width=200, length=160):
    return StorageBinSettings.model_validate({
        "barcode": f"90000000000{cell}",
        "home_location": {"side": "left_module", "shelf_idx": 0, "cell_idx": cell},
        "inner_dimensions": {"width": width, "length": length, "height": 100},
        "compartment_layout": {"rows": rows, "columns": columns},
    })


@pytest.fixture
def configured(monkeypatch):
    entries = [tray()]
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: SimpleNamespace(storage=SimpleNamespace(bins=entries)))
    return entries


async def item(db, number, width=40, length=70, height=90):
    return await StorageService(db).create_item(
        f"123456789{number:03}", f"Bottle {number}",
        {"width": width, "length": length, "height": height},
    )


async def finish(service, workflow, present):
    workflow.resolution = {"target_item_in_bin": present}
    await service.transition(workflow, "returning_bin")
    await service.complete_return(workflow)
    assert workflow.state == "completed"


@pytest.mark.anyio
async def test_slots_persist_across_sessions_and_reuse_first_vacancy(configured):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        first, second = await item(db, 1), await item(db, 2)
        for target, slot in [(first, 1), (second, 2)]:
            workflow = await service.begin("store", target, None)
            assert workflow.compartment == slot
            assert await service.item_bin(target.id) is None
            await finish(service, workflow, True)
        first_id, second_id = first.id, second.id
        workflow_id = workflow.id
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        stored = await db.get(StorageBinWorkflow, workflow_id)
        assert stored.compartment == 2
        payload = await service.serialize_workflow(stored)
        assert payload["bin"]["compartment_layout"] == {"rows": 2, "columns": 4}
        assert payload["bin"]["compartment_dimensions"] == {"width": 50, "length": 80, "height": 100}
        assert {entry["id"]: entry["compartment"] for entry in payload["bin"]["items"]} == {first_id: 1, second_id: 2}
        first = await StorageService(db).get_item("123456789001")
        retrieve = await service.begin("retrieve", first, None)
        assert retrieve.compartment == 1
        await finish(service, retrieve, False)
        third = await item(db, 3)
        replacement = await service.begin("store", third, None)
        assert replacement.compartment == 1
        await finish(service, replacement, True)
        assert await db.scalar(select(StorageBinItem.compartment).where(StorageBinItem.item_id == second_id)) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("compartmentalized", [True, False])
async def test_serialization_keeps_contents_coherent_during_retrieval(
    configured, monkeypatch, compartmentalized
):
    if not compartmentalized:
        configured[:] = [configured[0].model_copy(update={"compartment_layout": None})]
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        second, first = await item(db, 2), await item(db, 1)
        for target in (second, first):
            workflow = await service.begin("store", target, None)
            await finish(service, workflow, True)
        retrieve = await service.begin("retrieve", second, None)
        retrieve.resolution = {"target_item_in_bin": False}
        await service.transition(retrieve, "returning_bin")
        bin_id, workflow_id = retrieve.bin_id, retrieve.id
        expected = [
            {"id": first.id, "upca": first.upca, "name": first.name,
             "compartment": 2 if compartmentalized else None},
            {"id": second.id, "upca": second.upca, "name": second.name,
             "compartment": 1 if compartmentalized else None},
        ]

    async with AsyncSessionLocal() as reader, AsyncSessionLocal() as writer:
        service = BinStorageService(reader)
        row = await reader.get(StorageBin, bin_id)
        retrieve = await writer.get(StorageBinWorkflow, workflow_id)
        execute = reader.execute
        completed = False

        async def complete_retrieval_after_read(*args, **kwargs):
            nonlocal completed
            result = await execute(*args, **kwargs)
            if not completed:
                completed = True
                # Complete the real workflow after a read has observed the old contents.
                await BinStorageService(writer).complete_return(retrieve)
                assert retrieve.state == "completed"
            return result

        monkeypatch.setattr(reader, "execute", complete_retrieval_after_read)
        payload = await service.serialize_bin(row)
        assert completed
        assert payload["items"] == expected
        assert payload["item_count"] == 2
        assert payload["used_volume"] == 2 * 40 * 70 * 90
        assert payload["available_volume"] == 200 * 160 * 100 - payload["used_volume"]

        current = await service.serialize_bin(row)
        assert current["items"] == expected[:1]
        assert current["item_count"] == 1
        assert current["used_volume"] == 40 * 70 * 90
        assert current["available_volume"] == 200 * 160 * 100 - current["used_volume"]


@pytest.mark.anyio
@pytest.mark.parametrize("width,length,height,fits", [(50,80,100,True),(80,50,100,True),(51,80,100,False),(40,70,101,False)])
async def test_fit_is_per_compartment_and_allows_rotation(configured, width, length, height, fits):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        target = await item(db, 1, width, length, height)
        assert (await service.allocate_bin(target) is not None) is fits


@pytest.mark.anyio
async def test_full_slots_cannot_share_even_with_spare_volume(configured):
    configured[:] = [tray(columns=2, rows=1)]
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        for number in (1, 2):
            target = await item(db, number, 1, 1, 1)
            workflow = await service.begin("store", target, None)
            assert workflow.compartment == number
            await finish(service, workflow, True)
        assert await service.allocate_bin(await item(db, 3, 1, 1, 1)) is None


@pytest.mark.anyio
async def test_selects_compatible_cell_layout(configured):
    configured[:] = [tray(), tray(columns=2, rows=2, cell=1)]
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        small = await item(db, 1)
        assert (await service.allocate_bin(small)).cell_idx == 0
        large = await item(db, 2, 90, 70)
        assert (await service.allocate_bin(large)).cell_idx == 1


@pytest.mark.anyio
@pytest.mark.parametrize("occupied", [True, False])
async def test_relayout_refuses_nonempty_or_checked_out_tray(configured, occupied):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        workflow = await service.begin("store", await item(db, 1), None)
        if occupied:
            await finish(service, workflow, True)
        configured[:] = [tray(columns=2, rows=4)]
        with pytest.raises(ValueError, match="Empty and return"):
            await service.sync_config()
        await db.rollback()
        row = await db.scalar(select(StorageBin))
        assert row.compartment_layout == {"rows": 2, "columns": 4}


@pytest.mark.anyio
async def test_empty_tray_can_adopt_layout_without_reusing_old_restore_candidates(configured):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        target = await item(db, 1)
        old = await service.begin("store", target, None)
        await service.reconcile(old, resolution="quarantined", target_item_in_bin=None)
        restore = await service.begin_unavailable_return(configured[0].barcode)
        restore.resolution = {**restore.resolution, "confirmed_item_ids": []}
        await service.reconcile(restore, resolution="returned", target_item_in_bin=None)
        configured[:] = [tray(columns=2, rows=4)]
        await service.sync_config()
        newer = await service.begin("store", await item(db, 2, 20, 20), None)
        await service.reconcile(newer, resolution="quarantined", target_item_in_bin=None)
        restored = await service.begin_unavailable_return(configured[0].barcode)
        assert restored.resolution["candidate_item_ids"] == [newer.item_id]
        assert restored.resolution["candidate_compartments"] == {str(newer.item_id): 1}


@pytest.mark.anyio
async def test_quarantined_store_restores_reserved_compartment(configured):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        original = await service.begin("store", await item(db, 1), None)
        await finish(service, original, True)
        uncertain = await service.begin("store", await item(db, 2), None)
        assert uncertain.compartment == 2
        await service.reconcile(uncertain, resolution="quarantined", target_item_in_bin=None)
        restore = await service.begin_unavailable_return(configured[0].barcode)
        payload = await service.serialize_workflow(restore)
        assert [entry["compartment"] for entry in payload["contents"]] == [1, 2]
        restore.resolution = {**restore.resolution, "confirmed_item_ids": [original.item_id, uncertain.item_id]}
        await service.transition(restore, "returning_bin")
        await service.complete_return(restore)
        assert restore.state == "completed"
        assert await db.scalar(select(StorageBinItem.compartment).where(StorageBinItem.item_id == uncertain.item_id)) == 2


@pytest.mark.anyio
async def test_duplicate_restore_positions_rejected_before_motion(configured):
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        await service.sync_config()
        first = await service.begin("store", await item(db, 1), None)
        await service.reconcile(first, resolution="quarantined", target_item_in_bin=None)
        restore = await service.begin_unavailable_return(configured[0].barcode)
        await service.reconcile(restore, resolution="returned", target_item_in_bin=None)
        second = await service.begin("store", await item(db, 2), None)
        await service.reconcile(second, resolution="quarantined", target_item_in_bin=None)
        restore = await service.begin_unavailable_return(configured[0].barcode)
        restore.resolution = {**restore.resolution, "confirmed_item_ids": [first.item_id, second.item_id]}
        worker = SimpleNamespace(needs_home=lambda: False)
        with pytest.raises(BinWorkflowConflict, match="same compartment"):
            await service.enqueue_return(restore, worker)
        assert await service.item_bin(first.item_id) is None
