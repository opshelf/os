from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from hardware.tof import ToFSensorBus
from hardware.uvc_camera import UvcCamera
from openshelf_os.config.models import IOBoxSizerConfig
from openshelf_os.iobox_sizer.models import IOBoxSizerMeasurement
from openshelf_os.iobox_sizer.service import IOBoxSizerService
from openshelf_os.runtime_state import RuntimeStatePaths
from openshelf_os_server.core.camera import init_sizer_camera_manager, unregister_camera_manager
from openshelf_os_server.database.models import Item, ItemSizerMeasurement
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.settings import get_root_config

logger = get_logger(__name__)


@dataclass
class IOBoxSizerRuntime:
	config: IOBoxSizerConfig
	service: IOBoxSizerService | None = None
	available: bool = False
	last_error: str | None = None
	last_error_type: str | None = None
	last_measurement_time: str | None = None


_runtime: IOBoxSizerRuntime | None = None
_measure_lock = asyncio.Lock()
_reload_lock = asyncio.Lock()


def _make_service(config: IOBoxSizerConfig) -> IOBoxSizerService:
	camera = UvcCamera(
		device=config.camera.device,
		width=config.camera.width,
		height=config.camera.height,
		fps=config.camera.fps,
		flip_horizontal=config.camera.flip_horizontal,
		buffer_size=config.camera.buffer_size,
	)
	tof_bus = ToFSensorBus(
		ftdi_url=config.tof.ftdi_url,
		mux_address=config.tof.mux_address,
		sensor_address=config.tof.sensor_address,
		channels=config.tof.channels,
		timing_budget_ms=config.tof.timing_budget_ms,
		read_timeout_s=config.tof.read_timeout_s,
		distance_mode=config.tof.distance_mode,
		roi_width=config.tof.roi_width,
		roi_height=config.tof.roi_height,
		roi_center=config.tof.roi_center,
	)
	return IOBoxSizerService(config=config, camera=camera, tof_bus=tof_bus)


async def init_iobox_sizer() -> None:
	global _runtime

	root_config = get_root_config().robot.io.sizer
	config = root_config.model_copy(deep=True) if hasattr(root_config, "model_copy") else deepcopy(root_config)
	config.detection.blank_frame_path = str(
		RuntimeStatePaths.current().resolve_mode_owned_path(
			config.detection.blank_frame_path,
			relative_to=Path.cwd(),
		)
	)
	_runtime = IOBoxSizerRuntime(config=config)

	if config.enabled is False:
		logger.info("iobox_sizer_disabled")
		return

	logger.info("iobox_sizer_initializing", channels=config.tof.channels)
	service = _make_service(config)

	try:
		await asyncio.to_thread(service.start)
	except Exception as exc:
		try:
			await asyncio.to_thread(service.close)
		except Exception as close_exc:
			logger.warning(
				"iobox_sizer_cleanup_failed",
				error=str(close_exc),
				error_type=type(close_exc).__name__,
				exc_info=True,
			)
		_runtime.available = False
		_runtime.last_error = str(exc)
		_runtime.last_error_type = type(exc).__name__
		_runtime.service = None
		logger.warning(
			"iobox_sizer_unavailable",
			error=str(exc),
			error_type=type(exc).__name__,
			exc_info=True,
		)
		return

	_runtime.service = service
	_runtime.available = True
	_runtime.last_error = None
	_runtime.last_error_type = None
	await init_sizer_camera_manager(service.camera)
	logger.info("iobox_sizer_initialized", channels=config.tof.channels)


async def stop_iobox_sizer() -> None:
	global _runtime

	service = None
	async with _measure_lock:
		runtime = _runtime
		_runtime = None
		if runtime is not None:
			service = runtime.service
		if service is not None:
			try:
				await asyncio.to_thread(service.close)
			except Exception as exc:
				logger.warning(
					"iobox_sizer_shutdown_failed",
					error=str(exc),
					error_type=type(exc).__name__,
					exc_info=True,
				)

	await unregister_camera_manager("iobox_sizer")


async def reload_iobox_sizer_config() -> None:
	async with _reload_lock:
		await stop_iobox_sizer()
		await init_iobox_sizer()


def get_iobox_sizer_status() -> dict[str, Any]:
	config = get_root_config().robot.io.sizer
	runtime = _runtime
	return {
		"enabled": config.enabled,
		"available": bool(runtime.available) if runtime is not None else False,
		"observer_mode_enabled": config.observer_mode_enabled,
		"measure_on_store": config.measure_on_store,
		"camera": config.camera.model_dump(mode="json"),
		"tof_channels": config.tof.channels,
		"last_error": runtime.last_error if runtime is not None else None,
		"last_error_type": runtime.last_error_type if runtime is not None else None,
		"last_measurement_time": runtime.last_measurement_time if runtime is not None else None,
	}


def get_iobox_sizer_or_error() -> IOBoxSizerService:
	if _runtime is None or _runtime.service is None or _runtime.available is False:
		raise RuntimeError("IO box sizer is disabled or unavailable")

	return _runtime.service


async def measure_iobox_size() -> IOBoxSizerMeasurement:
	async with _measure_lock:
		runtime = _runtime
		service = get_iobox_sizer_or_error()
		measurement_task = asyncio.create_task(asyncio.to_thread(service.measure))
		logger.info("iobox_sizer_measurement_started")
		try:
			measurement = await asyncio.shield(measurement_task)
		except asyncio.CancelledError:
			try:
				await measurement_task
			except Exception as exc:
				if runtime is not None:
					runtime.last_error = str(exc)
					runtime.last_error_type = type(exc).__name__
				logger.warning(
					"iobox_sizer_measurement_failed",
					error=str(exc),
					error_type=type(exc).__name__,
					exc_info=True,
				)
			raise
		except Exception as exc:
			if runtime is not None:
				runtime.last_error = str(exc)
				runtime.last_error_type = type(exc).__name__
			logger.warning(
				"iobox_sizer_measurement_failed",
				error=str(exc),
				error_type=type(exc).__name__,
				exc_info=True,
			)
			raise

		if runtime is not None:
			runtime.last_measurement_time = measurement.timestamp.isoformat()
		logger.info(
			"iobox_sizer_measurement_completed",
			detected=measurement.detected,
			duration_ms=measurement.duration_ms,
		)
		return measurement


def _consume_measurement_task_result(task: asyncio.Task[IOBoxSizerMeasurement]) -> None:
	try:
		task.result()
	except asyncio.CancelledError:
		logger.warning("iobox_sizer_observer_capture_cancelled")
	except Exception as exc:
		logger.warning(
			"iobox_sizer_observer_capture_background_failed",
			error=str(exc),
			error_type=type(exc).__name__,
			exc_info=True,
		)


async def record_store_sizer_measurement(
	*,
	upca: str | None,
	item_name: str | None,
	user_id: int | None,
	request_correlation_id: str | None,
	command_payload: dict[str, Any] | None,
	human_dimensions: dict[str, Any] | None,
	human_dimensions_source: str | None,
	measurement: IOBoxSizerMeasurement | None,
	error: Exception | None,
	error_type: str | None = None,
	error_message: str | None = None,
	should_raise: bool = False,
) -> None:
	try:
		auto_dimensions = None
		measurement_timestamp = None
		raw_measurement = None
		detected = False

		if measurement is not None:
			detected = measurement.detected
			measurement_timestamp = measurement.timestamp
			raw_measurement = measurement.model_dump(mode="json")

			if measurement.dimensions_mm is not None:
				auto_dimensions = measurement.dimensions_mm.model_dump(mode="json")

		resolved_error_type = error_type
		resolved_error_message = error_message
		if error is not None:
			resolved_error_type = resolved_error_type or type(error).__name__
			resolved_error_message = resolved_error_message or str(error)

		async with AsyncSessionLocal() as session:
			item_id = None
			if upca is not None:
				result = await session.execute(select(Item.id).where(Item.upca == upca))
				item_id = result.scalar_one_or_none()

			session.add(
				ItemSizerMeasurement(
					item_id=item_id,
					upca=upca,
					item_name=item_name,
					user_id=user_id,
					request_correlation_id=request_correlation_id,
					manual_dimensions=human_dimensions,
					manual_dimensions_source=human_dimensions_source,
					auto_dimensions=auto_dimensions,
					raw_measurement=raw_measurement,
					command_payload=command_payload,
					detected=detected,
					error_type=resolved_error_type,
					error_message=resolved_error_message,
					measurement_timestamp=measurement_timestamp,
				)
			)
			await session.commit()
	except Exception as exc:
		logger.warning(
			"iobox_sizer_observer_measurement_persist_failed",
			upca=upca,
			error=str(exc),
			error_type=type(exc).__name__,
			exc_info=True,
		)
		if should_raise is True:
			raise


async def capture_store_observer_measurement(
	*,
	upca: str | None,
	item_name: str | None,
	user_id: int | None,
	request_correlation_id: str | None,
	command_payload: dict[str, Any] | None,
	human_dimensions: dict[str, Any] | None,
	human_dimensions_source: str | None,
) -> None:
	config = get_root_config().robot.io.sizer
	if config.enabled is False or config.observer_mode_enabled is False or config.measure_on_store is False:
		logger.info("iobox_sizer_observer_capture_skipped", reason="disabled")
		return

	logger.info("iobox_sizer_observer_capture_started", upca=upca)

	measurement_task = asyncio.create_task(measure_iobox_size())

	try:
		measurement = await asyncio.wait_for(
			asyncio.shield(measurement_task),
			timeout=config.store_measurement_timeout_s,
		)
		logger.info("iobox_sizer_observer_capture_completed", upca=upca, detected=measurement.detected)
		await record_store_sizer_measurement(
			upca=upca,
			item_name=item_name,
			user_id=user_id,
			request_correlation_id=request_correlation_id,
			command_payload=command_payload,
			human_dimensions=human_dimensions,
			human_dimensions_source=human_dimensions_source,
			measurement=measurement,
			error=None,
		)
	except asyncio.TimeoutError as exc:
		measurement_task.add_done_callback(_consume_measurement_task_result)
		logger.warning(
			"iobox_sizer_observer_capture_timed_out",
			upca=upca,
			error=str(exc),
			error_type=type(exc).__name__,
		)
		await record_store_sizer_measurement(
			upca=upca,
			item_name=item_name,
			user_id=user_id,
			request_correlation_id=request_correlation_id,
			command_payload=command_payload,
			human_dimensions=human_dimensions,
			human_dimensions_source=human_dimensions_source,
			measurement=None,
			error=exc,
			error_type="TimeoutError",
			error_message=f"Timed out after {config.store_measurement_timeout_s}s",
		)
		return
	except Exception as exc:
		logger.warning(
			"iobox_sizer_observer_capture_failed",
			upca=upca,
			error=str(exc),
			error_type=type(exc).__name__,
		)
		await record_store_sizer_measurement(
			upca=upca,
			item_name=item_name,
			user_id=user_id,
			request_correlation_id=request_correlation_id,
			command_payload=command_payload,
			human_dimensions=human_dimensions,
			human_dimensions_source=human_dimensions_source,
			measurement=None,
			error=exc,
		)
