import asyncio
from types import SimpleNamespace

import httpx
import pytest

from openshelf_os_server.core import chemicals
from openshelf_os_server.core.chemicals import (
    ChemicalUnavailable, CommonChemistry, get_common_chemistry, normalize_cas,
)
from openshelf_os_server.main import app
from openshelf_os_server.routers.auth_deps import get_current_user


@pytest.mark.parametrize("value", ["64-17-5", "64175", " 64-17-5 "])
def test_normalize_cas(value):
    assert normalize_cas(value) == "64-17-5"


@pytest.mark.parametrize("value", ["64-17-4", "6417", "0-00-0", "ethanol"])
def test_invalid_cas(value):
    with pytest.raises(ValueError):
        normalize_cas(value)


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["eth", "1,2-dichloroethane"])
async def test_search_fallback_sanitization_and_cap(query):
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.headers["X-API-KEY"] == "fixture-key"
        if request.url.params["q"] == query:
            return httpx.Response(200, json={"count": 0, "results": []})
        return httpx.Response(200, json={"count": "8", "results": [
            {"rn": "64-17-5", "name": "<b>Eth</b>anol &amp; <svg>ignored</svg>", "image": "secret SVG"}
        ] * 8})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    result = await service.search(query, 2)
    assert result.count == 8
    assert len(result.results) == 3
    assert result.results[0].name == "Ethanol &"
    assert dict(requests[-1].url.params) == {"q": query + "*", "offset": "2", "size": "3"}
    assert "image" not in result.model_dump_json()
    assert await service.search(query, 2) == result
    assert await service.search(query + "*", 2) == result
    assert len(requests) == 2


@pytest.mark.anyio
async def test_cas_search_never_wildcards():
    queries = []

    def upstream(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"count": 0, "results": []})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    assert (await service.search("64175")).count == 0
    assert (await service.search("64-17-4")).count == 0
    assert queries == ["64-17-5"]


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["112-", "112-1", "112-12", "112-12-", "112–12–"])
async def test_incomplete_cas_never_contacts_upstream(query):
    queries = []

    def upstream(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"count": 0, "results": []})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    assert (await service.search(query)).count == 0
    assert queries == []


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["64-17*", "64-17-4*", "64-17-5*", "64175*", "*64-17-5", "64-*-5"])
async def test_numeric_wildcard_search_never_contacts_upstream(query):
    def upstream(request):
        pytest.fail("Numeric wildcard query must not contact upstream")

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    assert (await service.search(query)).model_dump() == {"count": 0, "results": []}


@pytest.mark.anyio
async def test_details_plain_text_and_source():
    service = CommonChemistry("fixture-key", httpx.MockTransport(lambda request: httpx.Response(200, json={
        "rn": "64-17-5", "name": "<i>Ethanol</i>", "image": "discard",
        "synonyms": ["Ethyl <b>alcohol</b>", "Ethyl alcohol", "<script>bad()</script>"],
    })))
    result = await service.detail("64175")
    assert result.model_dump() == {
        "name": "Ethanol", "cas_number": "64-17-5", "synonyms": ["Ethyl alcohol"],
        "source_url": "https://commonchemistry.cas.org/detail?cas_rn=64-17-5",
    }


@pytest.mark.anyio
async def test_concurrent_requests_survive_one_cancellation():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={"count": 1, "results": [{"rn": "64-17-5", "name": "Ethanol"}]})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    first = asyncio.create_task(service.search("ethanol"))
    await started.wait()
    second = asyncio.create_task(service.search("ethanol"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).count == 1
    assert calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status,payload", [(429, {}), (500, {}), (200, []), (200, {"count": "bad"})])
async def test_upstream_errors_are_recoverable(status, payload):
    calls = 0

    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json=payload, headers={"Retry-After": "60"})

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    with pytest.raises(ChemicalUnavailable) as error:
        await service.search("ethanol")
    assert str(error.value) == ""
    if status in {429, 500}:
        with pytest.raises(ChemicalUnavailable):
            await service.search("water")
        assert calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("path,payload", [
    ("search", {"count": "bad", "results": []}),
    ("search", {"count": 0}),
    ("search", {"count": 1, "results": {}}),
    ("search", {"count": 1, "results": [{"rn": "64-17-4", "name": "Ethanol"}]}),
    ("detail", {"rn": "64-17-5"}),
    ("detail", {"rn": "64-17-5", "name": "Ethanol", "synonyms": {}}),
    ("detail", {"rn": "64-17-5", "name": "Ethanol", "synonyms": [None]}),
])
async def test_malformed_response_is_not_cached_and_retry_recovers(monkeypatch, path, payload):
    now = 100.0
    monkeypatch.setattr(chemicals, "time", SimpleNamespace(monotonic=lambda: now))
    calls = 0
    chemical = {"rn": "64-17-5", "name": "Ethanol", "synonyms": ["Ethyl alcohol"]}
    healthy = {"count": 1, "results": [chemical]} if path == "search" else chemical

    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload if calls == 1 else healthy)

    service = CommonChemistry("fixture-key", httpx.MockTransport(upstream))
    lookup = service.search if path == "search" else service.detail
    query = "ethanol" if path == "search" else "64-17-5"
    with pytest.raises(ChemicalUnavailable):
        await lookup(query)
    assert not service._cache
    now += 6  # Retry after the short failure cooldown, well before the one-hour cache TTL.
    result = await lookup(query)
    if path == "search":
        assert result.results[0].name == "Ethanol"
    else:
        assert result.name == "Ethanol"
        assert result.synonyms == ["Ethyl alcohol"]
    assert await lookup(query) == result
    assert calls == 2


@pytest.mark.anyio
async def test_timeout_and_missing_key():
    def timeout(request):
        raise httpx.ReadTimeout("sensitive upstream message", request=request)

    for service in [CommonChemistry(""), CommonChemistry("fixture-key", httpx.MockTransport(timeout))]:
        with pytest.raises(ChemicalUnavailable) as error:
            await service.search("ethanol")
        assert str(error.value) == ""


@pytest.mark.anyio
async def test_cache_is_bounded():
    service = CommonChemistry("fixture-key", httpx.MockTransport(lambda request: httpx.Response(200, json={
        "count": 1, "results": [{"rn": "64-17-5", "name": "Ethanol"}],
    })))
    for i in range(270):
        await service.search(f"query{i}")
    assert len(service._cache) == 256


@pytest.mark.anyio
async def test_routes_authorization_and_availability(client, unauthenticated_client):
    for path in ["/api/chemicals/search?q=ethanol", "/api/chemicals/64-17-5"]:
        assert (await unauthenticated_client.get(path)).status_code == 401
    app.dependency_overrides[get_common_chemistry] = lambda: CommonChemistry("")
    try:
        response = await client.get("/api/chemicals/search?q=ethanol")
        assert response.status_code == 503
        assert response.json() == {"detail": {"message": "CAS search unavailable", "code": "service_unavailable"}}
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
