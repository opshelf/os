from __future__ import annotations

import hashlib

import httpx
import pytest

from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.routers.auth_deps import RELAY_SERVICE_TOKEN_SHA256_ENV
from openshelf_os_server.routers.import_jobs import clear_import_jobs

RELAY_SERVICE_TOKEN = "os_service.test-relay-token-with-at-least-32-characters"


@pytest.fixture(autouse=True)
def _reset_import_jobs() -> None:
	clear_import_jobs()
	yield
	clear_import_jobs()


def _headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
	monkeypatch.setenv(
		RELAY_SERVICE_TOKEN_SHA256_ENV,
		hashlib.sha256(RELAY_SERVICE_TOKEN.encode("utf-8")).hexdigest(),
	)
	return {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}


@pytest.mark.asyncio
async def test_relay_can_create_and_cancel_an_import_job(
	unauthenticated_client: httpx.AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _ensure_developer_user()
	headers = _headers(monkeypatch)

	created = await unauthenticated_client.post(
		"/api/import-jobs",
		headers=headers,
		json={"filename": "stock.csv", "rows": [{"name": "Acetone", "quantity": 3}]},
	)
	assert created.status_code == 200, created.text
	job = created.json()["job"]
	assert job["state"] == "active"
	assert job["counts"]["total"] == 3
	assert job["counts"]["pending"] == 3
	assert job["step"]["kind"] == "unit"
	assert job["step"]["unit"]["name"] == "Acetone"

	second = await unauthenticated_client.post(
		"/api/import-jobs",
		headers=headers,
		json={"rows": [{"name": "Other"}]},
	)
	assert second.status_code == 409
	assert second.json()["detail"]["code"] == "import_active"

	active = await unauthenticated_client.get("/api/import-jobs/active", headers=headers)
	assert active.status_code == 200
	assert active.json()["job"]["id"] == job["id"]

	read = await unauthenticated_client.get(
		f"/api/import-jobs/{job['id']}", params={"units": "true"}, headers=headers
	)
	assert read.status_code == 200
	assert len(read.json()["job"]["units"]) == 3

	cancelled = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/cancel", headers=headers
	)
	assert cancelled.status_code == 200
	assert cancelled.json()["job"]["state"] == "cancelled"
	assert cancelled.json()["job"]["cancel_source"] == "relay"

	idle = await unauthenticated_client.get("/api/import-jobs/active", headers=headers)
	assert idle.status_code == 200
	assert idle.json() == {"job": None}

	missing = await unauthenticated_client.get(
		"/api/import-jobs/00000000-0000-4000-8000-999999999999", headers=headers
	)
	assert missing.status_code == 404
	assert missing.json()["detail"]["message"] == "Import job not found"


@pytest.mark.asyncio
async def test_import_job_requires_the_relay_or_a_user(
	unauthenticated_client: httpx.AsyncClient,
) -> None:
	idle = await unauthenticated_client.get("/api/import-jobs/active")
	assert idle.status_code == 401


@pytest.mark.asyncio
async def test_import_steps_measure_skip_and_finish(
	unauthenticated_client: httpx.AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _ensure_developer_user()
	headers = _headers(monkeypatch)
	created = await unauthenticated_client.post(
		"/api/import-jobs",
		headers=headers,
		json={"filename": "stock.csv", "rows": [{"name": "Acetone", "quantity": 2}]},
	)
	assert created.status_code == 200, created.text
	job = created.json()["job"]
	first = job["step"]["unit"]["id"]
	second = first + 1

	measured = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/units/{first}/begin",
		headers=headers,
		json={"dimensions": {"width": 10, "height": 20, "length": 30}},
	)
	assert measured.status_code == 200, measured.text
	body = measured.json()
	assert body["unit"]["status"] == "in_progress"
	assert body["unit"]["stage"] == "measured"
	assert body["job"]["step"]["unit"]["id"] == first

	skipped = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/units/{first}/skip",
		headers=headers,
	)
	assert skipped.status_code == 200, skipped.text
	assert skipped.json()["job"]["step"]["unit"]["id"] == second

	started = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/tray/start", headers=headers
	)
	assert started.status_code == 200, started.text
	assert started.json()["job"]["step"]["kind"] == "tray"

	placed = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/tray/place",
		headers=headers,
		json={"unit_id": second, "height": 20},
	)
	assert placed.status_code == 200, placed.text

	returned = await unauthenticated_client.post(
		f"/api/import-jobs/{job['id']}/tray/return", headers=headers
	)
	assert returned.status_code == 200, returned.text
	assert returned.json()["job"]["state"] == "completed"

	idle = await unauthenticated_client.get("/api/import-jobs/active", headers=headers)
	assert idle.json() == {"job": None}
