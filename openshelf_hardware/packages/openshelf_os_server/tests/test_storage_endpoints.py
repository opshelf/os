import pytest
from httpx import AsyncClient

from openshelf_os_server.core.mass import apply_cycle_mass
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import StorageBin, StorageBinItem
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers.storage import _deserialize_capacity


@pytest.mark.anyio
@pytest.mark.parametrize("in_bin", [False, True])
async def test_upsert_preserves_cycle_zero_and_allows_net_correction(client: AsyncClient, in_bin: bool) -> None:
    payload = {"barcode": "123456789019", "name": "Bottle"}
    response = await client.post("/api/storage/items", json={
        **payload, "weight": 350, "tare_weight": 100, "full_net_weight": 500,
    })
    assert response.status_code == 200, response.text
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).get_item(payload["barcode"])
        assert item is not None
        apply_cycle_mass(item, 0, stored=True, command_id="zero-cycle")
        if in_bin:
            bin_row = StorageBin(
                barcode="900000000019", side="front_module", shelf_idx=0, cell_idx=0,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=2, status="at_home", active=True,
            )
            db.add(bin_row)
            await db.flush()
            db.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await db.commit()

    response = await client.post("/api/storage/items", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["weight"] == 0
    assert response.json()["tare_weight"] == 100
    assert response.json()["full_net_weight"] == 500

    for invalid_weight in (0, -1):
        response = await client.post("/api/storage/items", json={**payload, "weight": invalid_weight})
        assert response.status_code == 422, response.text
    response = await client.post("/api/storage/items/lookup",
        json={"barcode": payload['barcode']})
    assert response.json()["weight"] == 0

    response = await client.post("/api/storage/items", json={**payload, "net_weight": 250})
    assert response.status_code == 200, response.text
    assert response.json()["weight"] == 350
    assert response.json()["net_weight"] == 250
    assert response.json()["full_net_weight"] == 500

    response = await client.post("/api/storage/items", json={**payload, "weight": None, "net_weight": 250})
    assert response.status_code == 422, response.text
    response = await client.post("/api/storage/items", json={**payload, "weight": None})
    assert response.status_code == 200, response.text
    assert response.json()["weight"] is None
    response = await client.post("/api/storage/items", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["weight"] is None


@pytest.mark.parametrize("raw_capacity", ["10cm x 20cm x 30cm", "1x2xbad", "{bad", "{"])
def test_deserialize_capacity_tolerates_invalid_strings(raw_capacity: str) -> None:
    capacity = _deserialize_capacity(raw_capacity)

    assert capacity.width == 0.0
    assert capacity.height == 0.0
    assert capacity.length == 0.0

@pytest.mark.anyio
async def test_storage_items_endpoint_unauthorized(unauthenticated_client: AsyncClient) -> None:
    """Test storage endpoints without authorization."""
    # Test GET /api/storage/items without auth
    response = await unauthenticated_client.get("/api/storage/items")
    assert response.status_code == 401

    # Test POST /api/storage/items without auth
    response = await unauthenticated_client.post("/api/storage/items", json={
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    })
    assert response.status_code == 401


@pytest.mark.anyio
async def test_add_item_location_occupied_returns_conflict(client: AsyncClient) -> None:
    """Assigning an already-occupied cell returns 409 instead of 500."""
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    first_item = {
        "barcode": "111111111111",
        "name": "First Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    second_item = {
        "barcode": "222222222222",
        "name": "Second Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    await client.post("/api/storage/items", json=first_item, headers=headers)
    await client.post("/api/storage/items", json=second_item, headers=headers)

    location_data = {"shelf_idx": 3, "cell_idx": 4, "side": "front"}

    first_response = await client.post(
        "/api/storage/items/locations/add",
        json={**location_data, "barcode": first_item['barcode']}, headers=headers
    )
    assert first_response.status_code == 200

    conflict_response = await client.post(
        "/api/storage/items/locations/add",
        json={**location_data, "barcode": second_item['barcode']}, headers=headers
    )
    assert conflict_response.status_code == 409

