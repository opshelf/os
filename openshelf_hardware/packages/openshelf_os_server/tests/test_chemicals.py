import asyncio
from types import SimpleNamespace

import httpx
import pytest

from openshelf_os_server.core.chemicals import (
    CommonChemistry, get_common_chemistry,
)
from openshelf_os_server.main import app
from openshelf_os_server.routers.auth_deps import get_current_user


@pytest.mark.anyio
async def test_routes_authorization_and_availability(client, unauthenticated_client):
    for path in ["/api/chemicals/search?q=ethanol", "/api/chemicals/64-17-5"]:
        assert (await unauthenticated_client.get(path)).status_code == 401
    app.dependency_overrides[get_common_chemistry] = lambda: CommonChemistry("")
    try:
        response = await client.get("/api/chemicals/search?q=ethanol")
        assert response.status_code == 503
        assert response.json() == {"detail": {"message": "CAS search unavailable", "code": "chemical_unavailable"}}
        assert response.headers.get("Retry-After") == "5"
        assert (await client.get("/api/chemicals/64-17-4")).status_code == 422
        assert (await client.get("/api/chemicals/search?q=%20%20")).status_code == 422
        assert (await client.get("/api/chemicals/search?q=ethanol&offset=-1")).status_code == 422
    finally:
        app.dependency_overrides.pop(get_common_chemistry, None)


@pytest.mark.anyio
async def test_routes_require_storage_permission(client):
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=SimpleNamespace(permissions=[]))
    try:
        for path in ["/api/chemicals/search?q=ethanol", "/api/chemicals/64-17-5"]:
            assert (await client.get(path)).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.anyio
async def test_routes_success_and_not_found(client):
    def upstream(request):
        if request.url.path.endswith("detail"):
            return httpx.Response(404)
        return httpx.Response(200, json={"count": 1, "results": [{"rn": "64-17-5", "name": "Ethanol"}]})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    app.dependency_overrides[get_common_chemistry] = lambda: service
    try:
        response = await client.get("/api/chemicals/search?q=ethanol")
        assert response.status_code == 200
        assert response.json() == {"count": 1, "results": [{"name": "Ethanol", "cas_number": "64-17-5"}]}
        assert (await client.get("/api/chemicals/64-17-5")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_common_chemistry, None)


@pytest.mark.anyio
async def test_live_like_slow_response_within_deadline():
    async def upstream(request):
        await asyncio.sleep(3.1)
        return httpx.Response(200, json={"count": 1, "results": [{"rn": "64-17-5", "name": "Ethanol"}]})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    assert (await service.search("64-17-5")).results[0].name == "Ethanol"
