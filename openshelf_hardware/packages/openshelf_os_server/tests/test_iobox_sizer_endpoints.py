from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from openshelf_os.iobox_sizer.models import IOBoxSizerMeasurement


def _measurement(detected: bool = True) -> IOBoxSizerMeasurement:
    return IOBoxSizerMeasurement(
        detected=detected,
        mode="brightness",
        width_px=20 if detected else None,
        height_px=10 if detected else None,
        center_x=50 if detected else None,
        center_y=60 if detected else None,
        frame_width=1280,
        frame_height=720,
        depth_mm=140.0,
        depth_delta_mm=12.0,
        sensor_index=0,
        sensor_channel=6,
        duration_ms=25,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.anyio
async def test_sizer_status_endpoint_reports_disabled_by_default(client: AsyncClient) -> None:
    response = await client.get("/api/iobox/sizer/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False


@pytest.mark.anyio
async def test_sizer_measure_unavailable_returns_503(client: AsyncClient) -> None:
    with patch(
        "openshelf_os_server.routers.iobox_sizer.measure_iobox_size",
        AsyncMock(side_effect=RuntimeError("not connected")),
    ):
        response = await client.post("/api/iobox/sizer/measure")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "iobox_sizer_unavailable"


@pytest.mark.anyio
async def test_sizer_manual_measurement_returns_current_result(client: AsyncClient) -> None:
    with patch(
        "openshelf_os_server.routers.iobox_sizer.measure_iobox_size",
        AsyncMock(return_value=_measurement()),
    ):
        response = await client.post("/api/iobox/sizer/measure")

    assert response.status_code == 200
    assert response.json()["measurement"]["detected"] is True


@pytest.mark.anyio
async def test_sizer_measurement_can_include_no_detection(client: AsyncClient) -> None:
    with patch(
        "openshelf_os_server.routers.iobox_sizer.measure_iobox_size",
        AsyncMock(return_value=_measurement(detected=False)),
    ):
        response = await client.post("/api/iobox/sizer/measure")

    assert response.status_code == 200
    assert response.json()["measurement"]["detected"] is False


@pytest.mark.anyio
async def test_sizer_config_validation_rejects_duplicate_tof_channels(client: AsyncClient) -> None:
    config_response = await client.get("/api/iobox/sizer/config")
    assert config_response.status_code == 200
    config = config_response.json()
    config["tof"]["channels"] = [6, 6]

    response = await client.put("/api/iobox/sizer/config", json=config)

    assert response.status_code == 422
