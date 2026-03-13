import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from perpetual_os1.laser_scan import ScanAnalysisResult, ScanBounds
from perpetual_os1.storage import StorageModuleSide


def _mock_worker(result: ScanAnalysisResult):
	robot = SimpleNamespace(
		_grid=SimpleNamespace(get_side=lambda side: []),
		scan_storage=AsyncMock(return_value=result),
	)

	return SimpleNamespace(
		_robot=robot,
		_current_command=None,
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
	)


@pytest.mark.asyncio
async def test_start_scan_and_status_complete(client) -> None:
	result = ScanAnalysisResult(
		run_id="scan-run",
		side=StorageModuleSide.LEFT,
		bounds=ScanBounds(x_min=0.0, x_max=20.0, y_min=0.0, y_max=20.0),
		step_mm=10.0,
		samples_per_point=3,
		settle_ms=25,
		surface_depth_mm=10.0,
		depth_threshold_mm=5.0,
		total_points=9,
		valid_points=9,
		raw_scan_path="data/scans/scan-run/depth.npy",
		metadata_path="data/scans/scan-run/metadata.json",
		shelves=[],
		created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
	)
	worker = _mock_worker(result)

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		response = await client.post(
			"/api/calibration/scan",
			json={
				"side": "left_module",
				"x_min": 0.0,
				"x_max": 20.0,
				"y_min": 0.0,
				"y_max": 20.0,
				"step_mm": 10.0,
			},
		)

		assert response.status_code == 200
		assert response.json()["state"] == "running"

		await asyncio.sleep(0)

		status_response = await client.get("/api/calibration/scan/status")
		assert status_response.status_code == 200
		assert status_response.json()["state"] == "completed"
		assert status_response.json()["result"]["run_id"] == "scan-run"
