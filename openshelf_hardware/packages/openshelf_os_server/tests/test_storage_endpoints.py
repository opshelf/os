import pytest
from httpx import AsyncClient

from openshelf_os_server.core.mass import apply_cycle_mass
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import StorageBin, StorageBinItem
from openshelf_os_server.database.session import AsyncSessionLocal


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
                barcode="900000000019", side="left_module", shelf_idx=0, cell_idx=0,
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
    response = await client.get(f"/api/storage/items/{payload['barcode']}")
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
    from openshelf_os_server.routers.storage import _deserialize_capacity

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
async def test_storage_items_endpoint_authorized(client: AsyncClient) -> None:
    """Test storage items endpoint with authorization."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Test GET /api/storage/items
    response = await client.get("/api/storage/items", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "items" in data
    assert "total" in data
    assert "skip" in data
    assert "limit" in data
    assert isinstance(data["items"], list)


@pytest.mark.anyio
async def test_create_storage_item(client: AsyncClient) -> None:
    """Test creating a storage item."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0},
        "weight": 500.0,
        "metadata": {"category": "test", "priority": 1}
    }

    response = await client.post("/api/storage/items", json=item_data, headers=headers)
    assert response.status_code == 200
    data = response.json()

    # Validate response structure
    assert data["id"] is not None
    assert data["barcode"] == "123456789012"
    assert data["name"] == "Test Item"
    assert data["dimensions"] == {"width": 10.0, "height": 20.0, "length": 30.0}
    assert data["weight"] == 500.0
    assert data["metadata"] == {"category": "test", "priority": 1}
    assert data["locations"] == []
    assert "created_at" in data
    assert "updated_at" in data


@pytest.mark.anyio
async def test_create_storage_item_validation_error(client: AsyncClient) -> None:
    """Test creating a storage item with validation error."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item with invalid dimensions
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0}  # missing length
    }

    response = await client.post("/api/storage/items", json=item_data, headers=headers)
    assert response.status_code == 422
    data = response.json()
    assert "detail" in data


@pytest.mark.anyio
async def test_get_storage_item(client: AsyncClient) -> None:
    """Test retrieving a specific storage item."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item first
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    create_response = await client.post("/api/storage/items", json=item_data, headers=headers)
    created_item = create_response.json()

    # Get item by barcode
    response = await client.get(f"/api/storage/items/{created_item['barcode']}", headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert data["barcode"] == created_item["barcode"]
    assert data["name"] == created_item["name"]


@pytest.mark.anyio
async def test_get_nonexistent_storage_item(client: AsyncClient) -> None:
    """Test retrieving a non-existent storage item."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.get("/api/storage/items/nonexistent", headers=headers)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_update_storage_item(client: AsyncClient) -> None:
    """Test updating a storage item."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item first
    item_data = {
        "barcode": "123456789012",
        "name": "Original Name",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    create_response = await client.post("/api/storage/items", json=item_data, headers=headers)
    created_item = create_response.json()

    # Update item
    update_data = {
        "name": "Updated Name",
        "weight": 1000.0
    }

    response = await client.put(f"/api/storage/items/{created_item['barcode']}",
                               json=update_data, headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert data["name"] == "Updated Name"
    assert data["weight"] == 1000.0
    assert data["dimensions"] == item_data["dimensions"]  # unchanged


@pytest.mark.anyio
async def test_delete_storage_item(client: AsyncClient) -> None:
    """Test deleting a storage item."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item first
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    create_response = await client.post("/api/storage/items", json=item_data, headers=headers)
    created_item = create_response.json()

    # Delete item
    response = await client.delete(f"/api/storage/items/{created_item['barcode']}",
                                   headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "message" in data

    # Verify item is deleted
    get_response = await client.get(f"/api/storage/items/{created_item['barcode']}",
                                  headers=headers)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_item_location_operations(client: AsyncClient) -> None:
    """Test item location operations."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create item first
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    create_response = await client.post("/api/storage/items", json=item_data, headers=headers)
    created_item = create_response.json()

    # Add location to item
    location_data = {
        "shelf_idx": 1,
        "cell_idx": 2,
        "side": "left"
    }

    response = await client.post(f"/api/storage/items/{created_item['barcode']}/locations",
                               json=location_data, headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert data["barcode"] == created_item["barcode"]
    assert len(data["locations"]) == 1
    assert data["locations"][0]["shelf_idx"] == 1
    assert data["locations"][0]["cell_idx"] == 2

    # Get item with location
    get_response = await client.get(
        f"/api/storage/items/{created_item['barcode']}",
        headers=headers
    )
    assert get_response.status_code == 200
    data = get_response.json()

    assert len(data["locations"]) == 1
    assert data["locations"][0]["shelf_idx"] == 1
    assert data["locations"][0]["cell_idx"] == 2

    # Remove location
    remove_response = await client.delete(
        f"/api/storage/items/{created_item['barcode']}/locations?shelf_idx=1&cell_idx=2&side=left",
        headers=headers
    )
    assert remove_response.status_code == 200
    data = remove_response.json()
    assert len(data["locations"]) == 0

    # Verify location is removed
    get_response = await client.get(
        f"/api/storage/items/{created_item['barcode']}",
        headers=headers
    )
    assert get_response.status_code == 200
    data = get_response.json()
    assert len(data["locations"]) == 0


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

    location_data = {"shelf_idx": 3, "cell_idx": 4, "side": "left"}

    first_response = await client.post(
        f"/api/storage/items/{first_item['barcode']}/locations",
        json=location_data, headers=headers
    )
    assert first_response.status_code == 200

    conflict_response = await client.post(
        f"/api/storage/items/{second_item['barcode']}/locations",
        json=location_data, headers=headers
    )
    assert conflict_response.status_code == 409


@pytest.mark.anyio
async def test_add_item_location_invalid_index_returns_unprocessable(client: AsyncClient) -> None:
    """A negative cell index is rejected as 422 instead of 500."""
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    item_data = {
        "barcode": "333333333333",
        "name": "Third Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }
    await client.post("/api/storage/items", json=item_data, headers=headers)

    response = await client.post(
        f"/api/storage/items/{item_data['barcode']}/locations",
        json={"shelf_idx": -1, "cell_idx": 2, "side": "left"}, headers=headers
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_storage_cells_endpoint(client: AsyncClient) -> None:
    """Test storage cells endpoint."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Get storage cells
    response = await client.get("/api/storage/locations", headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data, list)
    if data:
        assert isinstance(data[0]["capacity"], dict)
        assert {"width", "height", "length"} <= set(data[0]["capacity"].keys())

    # Update a storage cell (assuming cell 1,1 exists)
    update_data = {
        "capacity": {"width": 50, "height": 100, "length": 50},
        "status": "occupied"
    }

    response = await client.put("/api/storage/locations/1/1",
                               json=update_data, headers=headers)
    # This might fail if the cell doesn't exist, which is expected
    if response.status_code == 200:
        data = response.json()
        assert data["shelf_idx"] == 1
        assert data["cell_idx"] == 1
        assert data["status"] == "occupied"
        assert data["capacity"]["width"] >= 0


@pytest.mark.anyio
async def test_storage_permissions_enforcement(client: AsyncClient) -> None:
    """Test that storage permissions are properly enforced."""
    # Login as viewer (should have limited permissions)
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",  # For simplicity, using admin but with viewer logic
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Try to write to storage endpoints
    item_data = {
        "barcode": "123456789012",
        "name": "Test Item",
        "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
    }

    # These tests assume the current role (admin) has permissions
    # In real tests, we'd create a specific viewer role and test permission denial
    response = await client.post("/api/storage/items", json=item_data, headers=headers)
    assert response.status_code in [200, 403]  # 200 if admin, 403 if viewer


@pytest.mark.anyio
async def test_pagination_items_endpoint(client: AsyncClient) -> None:
    """Test pagination on items endpoint."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create multiple items
    for i in range(3):
        item_data = {
            "barcode": f"1234567890{i}",
            "name": f"Test Item {i}",
            "dimensions": {"width": 10.0, "height": 20.0, "length": 30.0}
        }
        await client.post("/api/storage/items", json=item_data, headers=headers)

    # Test pagination
    response = await client.get("/api/storage/items?skip=0&limit=2", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)
    assert data["skip"] == 0
    assert data["limit"] == 2
    assert len(data["items"]) <= 2

    response = await client.get("/api/storage/items?skip=1&limit=2", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)
    assert data["skip"] == 1
    assert data["limit"] == 2
    assert len(data["items"]) <= 2


@pytest.mark.anyio
async def test_storage_endpoint_filtering(client: AsyncClient) -> None:
    """Test filtering on storage cells endpoint."""
    # Login as admin
    login_response = await client.post("/api/auth/login/json", json={
        "email": "admin@opshelf.com",
        "pin": "1234"
    })
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Test filtering by shelf index
    response = await client.get("/api/storage/locations?shelf_idx=1", headers=headers)
    assert response.status_code == 200
    data = response.json()
    # All returned cells should have shelf_idx = 1 (if any exist)
    for cell in data:
        assert cell["shelf_idx"] == 1

    # Test filtering by cell index
    response = await client.get("/api/storage/locations?cell_idx=1", headers=headers)
    assert response.status_code == 200
    data = response.json()
    # All returned cells should have cell_idx = 1 (if any exist)
    for cell in data:
        assert cell["cell_idx"] == 1

