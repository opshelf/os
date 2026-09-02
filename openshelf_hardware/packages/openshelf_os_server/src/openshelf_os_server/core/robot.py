import logging
import os
from typing import Optional

import sentry_sdk

from openshelf_os import robot as r
from openshelf_os.storage import CellStatus
import openshelf_os.robot.worker as r_worker
from openshelf_os.common import (
    ConfigError,
    ConfigMissingError,
    ConfigValidationError,
    assert_config,
)

from openshelf_os_server.core.init_progress import (
    OVERALL_DEGRADED,
    OVERALL_FAILED,
    OVERALL_INITIALIZING,
    OVERALL_READY,
    get_init_tracker,
)
from openshelf_os_server.core.crash_video_recorder import (
    begin_motion_crash_artifacts,
    capture_motion_crash_artifacts,
)
from openshelf_os_server.core.printer_status import PrinterStatusProbeError, probe_printer_status
from openshelf_os_server.core.robot_events import RobotEventListener
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.bin_storage import BinStorageService, reserved_bin_locations
from openshelf_os_server.core.command_lifecycle import handle_worker_lifecycle
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_root_config

logger = logging.getLogger(__name__)

robot_worker: Optional[r_worker.RobotWorker] = None
robot_instance: Optional["r.Robot"] = None
event_listener: Optional[RobotEventListener] = None
robot_initialization_error: Optional[str] = None
robot_initialization_error_type: Optional[str] = None
CRITICAL_MOTOR_COMPONENTS = {"x_axis", "y_axis", "z_axis", "stage", "rotate", "door"}
BARCODE_CONNECT_TIMEOUT_S = 5.0


def _barcode_enabled() -> bool:
    return os.getenv("OPENSHELF_DISABLE_BARCODE", "false").lower() not in {"1", "true", "yes"}


def _printer_enabled() -> bool:
    try:
        return bool(get_root_config().printer.enabled)
    except Exception:
        return False


def _printer_device() -> str:
    return os.getenv("OPENSHELF_PRINTER_DEVICE", "/dev/usb/lp0")


async def _report_scanner_status(worker: r_worker.RobotWorker) -> None:
    if _barcode_enabled() is False:
        return
    tracker = get_init_tracker()
    try:
        connected = await worker.wait_scanner_connected(BARCODE_CONNECT_TIMEOUT_S)
    except Exception as exc:
        tracker.report("barcode", "failed", reason=str(exc))
        return
    if connected is True:
        tracker.report("barcode", "ready")
    else:
        tracker.report("barcode", "failed", reason=worker.describe_scanner_failure())


def _report_printer_status() -> None:
    if _printer_enabled() is False:
        return
    tracker = get_init_tracker()
    device = _printer_device()
    try:
        status = probe_printer_status(device)
    except PrinterStatusProbeError as exc:
        tracker.report("printer", "failed", reason=str(exc))
        return

    reason = status.failure_reason()
    if reason is not None:
        tracker.report("printer", "failed", reason=reason)
        return

    detail = f"{device} (paper near end)" if status.paper_near_end is True else device
    tracker.report("printer", "ready", detail=detail)


def validate_robot_config() -> None:
    try:
        config = get_root_config()
        assert_config(config, requirements=[
            "robot.tool.parameters.IDLE_POSITION",
            "robot.tool.parameters.PICKUP_POSITION_L",
            "robot.tool.parameters.PICKUP_POSITION_H",
        ])
        logger.info("Robot config validated successfully")
    except ConfigMissingError as err:
        logger.error("Config file not found: %s", err.path)
        raise
    except ConfigValidationError as err:
        logger.error("Config validation failed:\n%s", err)
        raise
    except ConfigError as err:
        logger.error("Config assertion failed: %s", err)
        raise


def _home_on_init_enabled() -> bool:
    try:
        return bool(get_root_config().robot_app.home_on_init)
    except Exception:
        return True


async def _has_active_bin_workflow() -> bool:
    """Read recovered durable bin state before configuring hardware that may move."""
    async with AsyncSessionLocal() as session:
        bin_storage = BinStorageService(session)
        return await bin_storage.active_workflow() is not None


async def init_robot_worker() -> None:
    """Bring up the robot in the background, reporting progress into the init tracker.

    This never raises: any failure is recorded as init state (tracker + error
    globals) so the background task that runs it cannot die silently and the
    server stays up regardless of hardware health.
    """
    global robot_worker, robot_instance, event_listener
    global robot_initialization_error, robot_initialization_error_type

    robot_initialization_error = None
    robot_initialization_error_type = None
    tracker = get_init_tracker()

    tracker.register_components(["config"])
    tracker.report("config", "configuring")
    try:
        logger.info("Validating robot configuration")
        validate_robot_config()
        tracker.report("config", "ready")
    except Exception as e:
        tracker.report("config", "failed", reason=str(e))
        set_robot_initialization_error(e)
        tracker.set_overall(OVERALL_FAILED, error=str(e))
        logger.warning("robot_config_validation_failed: %s", e, exc_info=True)
        _capture_init_exception(e)
        return

    try:
        logger.info("Getting Robot")
        robot = r.Robot()
        robot_instance = robot
        robot.set_init_progress_reporter(tracker.report)
        tracker.register_components(robot.get_init_component_names())
        post_steps = ["command_worker"]
        if _barcode_enabled() is True:
            post_steps.append("barcode")
        if _printer_enabled() is True:
            post_steps.append("printer")
        tracker.register_components(post_steps)
        tracker.set_overall(OVERALL_INITIALIZING)
    except Exception as e:
        robot_instance = None
        set_robot_initialization_error(e)
        tracker.set_overall(OVERALL_FAILED, error=str(e))
        logger.warning("robot_constructor_failed: %s", e, exc_info=True)
        _capture_init_exception(e)
        return

    try:
        has_active_bin_workflow = await _has_active_bin_workflow()
    except Exception as e:
        set_robot_initialization_error(e)
        tracker.set_overall(OVERALL_FAILED, error=str(e))
        logger.warning("bin_workflow_state_query_failed: %s", e, exc_info=True)
        _capture_init_exception(e)
        return

    home_on_init = _home_on_init_enabled() and not has_active_bin_workflow
    try:
        await robot.configure(isolate=True, home_on_init=home_on_init)
        logger.info("Built Robot")
    except Exception as e:
        set_robot_initialization_error(e)
        tracker.set_overall(OVERALL_FAILED, error=str(e))
        logger.warning("robot_configure_failed: %s", e, exc_info=True)
        _capture_init_exception(e)
        return

    worker: Optional[r_worker.RobotWorker] = None
    listener: Optional[RobotEventListener] = None
    try:
        tracker.report("command_worker", "configuring")
        async with AsyncSessionLocal() as session:
            storage_service = StorageService(session)
            occupied = await storage_service.get_all_occupied_cells()
            occupied_tuples = [(side, shelf_idx, cell_idx) for side, shelf_idx, cell_idx, _ in occupied]
            robot.sync_occupied_cells(occupied_tuples)
            reserved_locations = await reserved_bin_locations(session)
            robot.set_reserved_cell_locations(reserved_locations)
            for side, shelf_idx, cell_idx in reserved_locations:
                try:
                    robot.set_cell_status(side, shelf_idx, cell_idx, CellStatus.MAINTENANCE)
                except (ValueError, IndexError) as exc:
                    logger.warning(
                        "reserved_bin_home_not_in_grid: side=%s shelf=%d cell=%d error=%s",
                        side,
                        shelf_idx,
                        cell_idx,
                        exc,
                    )
            logger.info("Synced %d occupied cells from database", len(occupied_tuples))

        worker = r_worker.RobotWorker(
            robot,
            crash_artifact_provider=capture_motion_crash_artifacts,
            crash_artifact_begin_provider=begin_motion_crash_artifacts,
            command_lifecycle_callback=handle_worker_lifecycle,
        )
        failed = tracker.failed_components(degrading_only=True)
        failed_motor_components = [component for component in failed if component in CRITICAL_MOTOR_COMPONENTS]
        if home_on_init is False:
            if failed_motor_components:
                reason = "Startup failed for motion components: " + ", ".join(failed_motor_components)
            else:
                reason = "Automatic homing disabled on initialization"
            worker.set_needs_home(reason, axes=sorted(CRITICAL_MOTOR_COMPONENTS))
        elif failed_motor_components:
            reason = "Startup failed for motion components: " + ", ".join(failed_motor_components)
            worker.set_needs_home(reason, axes=failed_motor_components)
        logger.info("Got Instance")
        await worker.connect()
        logger.info("Robot worker connected")
        await _report_scanner_status(worker)
        await worker.start()
        logger.info("Worker started")

        listener = RobotEventListener(worker)
        await listener.start()
        logger.info("Robot event listener started")

        robot_worker = worker
        event_listener = listener
        tracker.report("command_worker", "ready")
        _report_printer_status()
    except Exception as e:
        tracker.report("command_worker", "failed", reason=str(e))
        if listener is not None:
            try:
                await listener.stop()
            except Exception as stop_error:
                logger.warning("robot_event_listener_cleanup_failed: %s", stop_error, exc_info=True)
        if worker is not None:
            try:
                await worker.shutdown()
            except Exception as shutdown_error:
                logger.warning("robot_worker_cleanup_failed: %s", shutdown_error, exc_info=True)
        set_robot_initialization_error(e)
        tracker.set_overall(OVERALL_FAILED, error=str(e))
        logger.warning("robot_worker_start_failed: %s", e, exc_info=True)
        _capture_init_exception(e)
        return

    failed = tracker.failed_components(degrading_only=True)
    if failed:
        tracker.set_overall(OVERALL_DEGRADED)
        logger.warning("robot_initialized_degraded: failed_components=%s", failed)
    else:
        tracker.set_overall(OVERALL_READY)
        logger.info("robot_initialized_ready")

    try:
        await robot_worker.get_status_snapshot(force_refresh=True)
    except Exception as e:
        logger.warning("initial_status_snapshot_failed: %s", e)


def _capture_init_exception(error: Exception) -> None:
    try:
        if sentry_sdk.is_initialized() is True:
            sentry_sdk.capture_exception(error)
    except Exception:
        pass


async def stop_robot_worker() -> None:
    global robot_worker, robot_instance, event_listener

    worker = robot_worker
    listener = event_listener
    shutdown_error: Optional[Exception] = None

    try:
        if worker is not None:
            try:
                await worker.shutdown()
            except Exception as error:
                shutdown_error = error
                logger.warning("robot_worker_shutdown_failed: %s", error, exc_info=True)

        if listener is not None:
            try:
                await listener.drain()
            except Exception as error:
                if shutdown_error is None:
                    shutdown_error = error
                logger.warning("robot_event_listener_drain_failed: %s", error, exc_info=True)
    finally:
        if listener is not None:
            try:
                await listener.stop()
                logger.info("Robot event listener stopped")
            except Exception as error:
                if shutdown_error is None:
                    shutdown_error = error
                logger.warning("robot_event_listener_stop_failed: %s", error, exc_info=True)

        robot_worker = None
        robot_instance = None
        event_listener = None

    if shutdown_error is not None:
        raise shutdown_error


async def get_robot_worker() -> r_worker.RobotWorker:
    if robot_worker is not None:
        return robot_worker

    raise ValueError("Robot Worker was Not Initialized")


def get_robot_instance() -> Optional["r.Robot"]:
    return robot_instance


def set_robot_initialization_error(error: Exception) -> None:
    global robot_initialization_error, robot_initialization_error_type

    robot_initialization_error = str(error)
    robot_initialization_error_type = type(error).__name__


def get_robot_health() -> dict[str, object]:
    tracker = get_init_tracker()
    snapshot = tracker.snapshot()
    overall = snapshot["phase"]
    failed = tracker.failed_components(degrading_only=True)

    if robot_worker is not None:
        status = "degraded" if failed else "ok"
        return {
            "status": status,
            "phase": overall,
            "failed_components": failed,
            "components": snapshot["components"],
        }

    if robot_initialization_error is not None:
        return {
            "status": "error",
            "phase": OVERALL_FAILED,
            "error": robot_initialization_error,
            "error_type": robot_initialization_error_type or "Error",
            "components": snapshot["components"],
        }

    if overall == OVERALL_INITIALIZING:
        return {
            "status": "initializing",
            "phase": overall,
            "components": snapshot["components"],
        }

    return {"status": "disabled", "phase": overall}
