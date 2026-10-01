# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import *
from .models import LaserAutoCalibrationError


class SnapshotRunnerMixin:
    async def _refresh_scan_side_state(
        self,
        side_name: StorageModuleSideName,
        recorder: Any,
        bounds: ScanBounds,
    ) -> None:
        side_state = self._side_states.setdefault(side_name, _SideState())
        side_state.bounds = bounds
        side_state.run_id = recorder.run_id

        if recorder.metadata_path.exists() is True:
            metadata = self._read_json(recorder.metadata_path)
            side_state.run_id = str(metadata.get("run_id", recorder.run_id))
            side_state.shelf_edges = [
                LaserAutoCalibrationShelfEdge(
                    side=StorageModuleSide(side_name),
                    x_mm=float(row["x_mm"]),
                    y_mm=float(row["y_mm"]),
                )
                for row in metadata.get("shelf_edges", [])
            ]
            side_state.candidates = _build_candidate_rows(
                side_name,
                list(metadata.get("candidates", [])),
                side_state.shelf_edges,
            )
            side_state.cells = _build_cell_rows(side_name, list(metadata.get("fine_scans", [])))
            side_state.live_shelf_count = max(side_state.live_shelf_count, len(side_state.shelf_edges))
            side_state.live_candidate_count = max(side_state.live_candidate_count, len(side_state.candidates))
            side_state.live_detected_cell_count = max(side_state.live_detected_cell_count, len(side_state.cells))

        progress_payload: dict[str, Any] = {}

        if recorder.progress_path.exists() is True:
            progress_payload = self._read_json(recorder.progress_path)

        await self._sync_snapshot(
            run_id=side_state.run_id,
            current_side=side_name,
            bounds=bounds,
            stage_detail={
                **self._snapshot.stage_detail,
                "side": side_name,
                "scan_progress": progress_payload.get("progress", {}),
                "scan_phase": progress_payload.get("phase"),
            },
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        return __import__("json").loads(path.read_text(encoding="utf-8"))

    def _should_preserve_cancelling_snapshot(self) -> bool:
        return self._abort_requested is True or self._snapshot.current_stage == "cancelling"

    def _should_abort_work(self) -> bool:
        return self._abort_requested is True or self._snapshot.current_stage == "cancelling"

    def _raise_if_abort_requested(self) -> None:
        if self._should_abort_work() is True:
            raise asyncio.CancelledError()

    def _should_ignore_status_update(self) -> bool:
        return self._snapshot.state not in ACTIVE_SESSION_STATES

    def _build_scan_status_callback(self, side_name: StorageModuleSideName, recorder: Any, bounds: ScanBounds):
        async def did_receive_status(message: str) -> None:
            if self._should_preserve_cancelling_snapshot() is True or self._should_ignore_status_update() is True:
                return

            current_stage = self._snapshot.current_stage
            stage_detail = dict(self._snapshot.stage_detail)
            stage_detail["side"] = side_name
            stage_detail["log"] = message
            side_state = self._side_states.setdefault(side_name, _SideState())

            shelf_match = SCAN_SHELF_SCOUT_RE.search(message)
            shelf_hole_match = SCAN_SHELF_HOLE_RE.search(message)
            progress_sample_match = SCAN_PROGRESS_SAMPLE_RE.search(message)
            shelf_edges_match = SHELF_EDGES_RE.search(message)
            coarse_candidates_match = COARSE_CANDIDATES_RE.search(message)
            fine_match = SCAN_FINE_RE.search(message)
            fine_retry_match = SCAN_FINE_RETRY_RE.search(message)
            fine_detected_cells_match = FINE_DETECTED_CELLS_RE.search(message)

            if message.startswith("phase=coarse-scan") is True or shelf_match is not None:
                current_stage = "shelf-scout"

            if shelf_match is not None:
                stage_detail.update(
                    {
                        "phase": "shelf-scout",
                        "scout_idx": int(shelf_match.group("idx")),
                        "scout_count": int(shelf_match.group("count")),
                        "x_position": float(shelf_match.group("x")),
                    }
                )

            if progress_sample_match is not None:
                laser_distance_mm: float | None = None

                if progress_sample_match.group("depth") != "none":
                    laser_distance_mm = float(progress_sample_match.group("depth"))

                stage_detail.update(
                    {
                        "laser_sample_label": progress_sample_match.group("label"),
                        "laser_sample_idx": int(progress_sample_match.group("sample_idx")),
                        "laser_sample_count": int(progress_sample_match.group("sample_count")),
                        "laser_sample_x_mm": float(progress_sample_match.group("x")),
                        "laser_sample_y_mm": float(progress_sample_match.group("y")),
                        "laser_distance_mm": laser_distance_mm,
                    }
                )

            if message.startswith("phase=coarse-detect") is True or shelf_hole_match is not None:
                current_stage = "coarse-detect"

            if shelf_hole_match is not None:
                side_state.live_shelf_count = max(
                    side_state.live_shelf_count,
                    int(shelf_hole_match.group("shelf_count")),
                )
                stage_detail.update(
                    {
                        "phase": "coarse-detect",
                        "shelf_idx": int(shelf_hole_match.group("shelf_idx")),
                        "shelf_count": int(shelf_hole_match.group("shelf_count")),
                        "offset_idx": int(shelf_hole_match.group("offset_idx")),
                        "offset_count": int(shelf_hole_match.group("offset_count")),
                        "y_position": float(shelf_hole_match.group("y")),
                    }
                )

            if shelf_edges_match is not None:
                side_state.live_shelf_count = max(
                    side_state.live_shelf_count,
                    int(shelf_edges_match.group("count")),
                )

            if coarse_candidates_match is not None:
                side_state.live_candidate_count = max(
                    side_state.live_candidate_count,
                    int(coarse_candidates_match.group("count")),
                )

            if fine_detected_cells_match is not None:
                side_state.live_detected_cell_count = max(
                    side_state.live_detected_cell_count,
                    int(fine_detected_cells_match.group("count")),
                )

            if fine_match is not None or fine_retry_match is not None:
                current_stage = "fine-scan"
                active_match = fine_match if fine_match is not None else fine_retry_match

                if active_match is not None:
                    side_state.live_candidate_count = max(
                        side_state.live_candidate_count,
                        int(active_match.group("count")),
                    )
                    stage_detail.update(
                        {
                            "phase": "fine-scan",
                            "candidate_idx": int(active_match.group("idx")),
                            "candidate_count": int(active_match.group("count")),
                            "is_retry": fine_retry_match is not None,
                        }
                    )

            if fine_detected_cells_match is not None:
                current_stage = "fine-scan"

            if message.startswith("run_dir=") is True:
                stage_detail["run_dir"] = message.split("=", 1)[1]

            await self._refresh_scan_side_state(side_name, recorder, bounds)
            await self._update_snapshot(
                current_side=side_name,
                current_stage=current_stage,
                state="running",
                bounds=bounds,
                stage_detail=stage_detail,
            )

        return did_receive_status

    def _build_angle_status_callback(self, side_name: str):
        async def did_receive_status(message: str) -> None:
            if self._should_preserve_cancelling_snapshot() is True or self._should_ignore_status_update() is True:
                return

            stage_detail = dict(self._snapshot.stage_detail)
            stage_detail["side"] = side_name
            stage_detail["log"] = message
            stage_detail["phase"] = "angle-calibration"

            capture_match = ANGLE_CAPTURE_RE.search(message)
            sample_match = ANGLE_SAMPLE_RE.search(message)
            scout_sample_match = ANGLE_SCOUT_SAMPLE_RE.search(message)
            alpha_match = ANGLE_RESULT_RE.search(message)
            stage_stall_capture_match = STAGE_STALL_CAPTURE_RE.search(message)
            stage_stall_probe_match = STAGE_STALL_PROBE_RE.search(message)

            if message.startswith("phase=shelf-scout") is True:
                stage_detail["subphase"] = "shelf-scout"

            if message.startswith("shelf_edges=") is True:
                stage_detail["subphase"] = "shelf-scout"

            if message.startswith("angle_capture_y_mm=") is True:
                stage_detail["capture_y_mm"] = float(message.split("=", 1)[1])

            if scout_sample_match is not None:
                laser_distance_mm: float | None = None

                if scout_sample_match.group("depth") != "none":
                    laser_distance_mm = float(scout_sample_match.group("depth"))

                stage_detail.update(
                    {
                        "subphase": "shelf-scout",
                        "laser_sample_label": scout_sample_match.group("label"),
                        "laser_sample_idx": int(scout_sample_match.group("sample_idx")),
                        "laser_sample_count": int(scout_sample_match.group("sample_count")),
                        "laser_sample_x_mm": float(scout_sample_match.group("x")),
                        "laser_sample_y_mm": float(scout_sample_match.group("y")),
                        "laser_distance_mm": laser_distance_mm,
                    }
                )

            if capture_match is not None:
                stage_detail.update(
                    {
                        "subphase": "capture",
                        "x_idx": int(capture_match.group("x_idx")),
                        "x_count": int(capture_match.group("x_count")),
                        "z_offset_mm": float(capture_match.group("z_offset")),
                        "rotation_idx": int(capture_match.group("rotation_idx")),
                        "rotation_count": int(capture_match.group("rotation_count")),
                    }
                )

            if sample_match is not None:
                stage_detail.update(
                    {
                        "subphase": "capture",
                        "sample_status": sample_match.group("status"),
                        "sample_x_mm": float(sample_match.group("x")),
                        "sample_z_mm": float(sample_match.group("z")),
                        "sample_rotation_deg": float(sample_match.group("rotation")),
                        "laser_distance_mm": (
                            None
                            if sample_match.group("distance") is None
                            else float(sample_match.group("distance"))
                        ),
                    }
                )

            if stage_stall_capture_match is not None:
                stage_detail.update(
                    {
                        "subphase": "stage-stall",
                        "rotation_idx": int(stage_stall_capture_match.group("rotation_idx")),
                        "rotation_count": int(stage_stall_capture_match.group("rotation_count")),
                        "absolute_rotation_deg": float(stage_stall_capture_match.group("absolute")),
                        "stage_stall_pass": stage_stall_capture_match.group("pass_name"),
                    }
                )

            if stage_stall_probe_match is not None:
                stage_detail.update(
                    {
                        "subphase": "stage-stall",
                        "sample_status": stage_stall_probe_match.group("status"),
                        "sample_rotation_deg": float(stage_stall_probe_match.group("rotation")),
                        "absolute_rotation_deg": float(stage_stall_probe_match.group("absolute")),
                        "stage_position_mm": float(stage_stall_probe_match.group("stage")),
                        "stage_stall_pass": stage_stall_probe_match.group("pass_name"),
                        "stage_probe_status": stage_stall_probe_match.group("probe_status"),
                        "stage_alarm_code": (
                            None
                            if stage_stall_probe_match.group("alarm_code") in (None, "none")
                            else int(stage_stall_probe_match.group("alarm_code"))
                        ),
                    }
                )


            if alpha_match is not None:
                stage_detail.update(
                    {
                        "subphase": "fit",
                        "alpha_deg": float(alpha_match.group("alpha")),
                    }
                )

            await self._update_snapshot(
                current_side=side_name,
                current_stage="angle-calibration",
                state="running",
                stage_detail=stage_detail,
            )

        return did_receive_status

    @asynccontextmanager
    async def _patch_status_callbacks(self, scan_callback=None, angle_callback=None):
        original_scan_print = laser_module.print_status
        previous_angle_printer: Any = angle_module.print_status
        pending_task: asyncio.Task[None] | None = None

        def didSchedule(callback: Any, message: str) -> None:
            nonlocal pending_task

            previous_task = pending_task

            async def didRunCallback() -> None:
                if previous_task is not None:
                    await previous_task

                await callback(message)

            pending_task = asyncio.create_task(didRunCallback())

        def did_print_scan(message: str) -> None:
            original_scan_print(message)

            if scan_callback is not None:
                didSchedule(scan_callback, message)

        def did_print_angle(message: str) -> None:
            previous_angle_printer(message)

            if angle_callback is not None:
                didSchedule(angle_callback, message)

        laser_module.print_status = did_print_scan
        previous_angle_printer = angle_module.set_status_printer(did_print_angle)

        try:
            yield
        finally:
            laser_module.print_status = original_scan_print
            angle_module.set_status_printer(previous_angle_printer)

            if pending_task is not None:
                current_task = asyncio.current_task()

                if self._should_abort_work() is True or (current_task is not None and current_task.cancelling() > 0):
                    pending_task.cancel()

                    with suppress(asyncio.CancelledError):
                        await pending_task
                else:
                    await pending_task

    async def _set_terminal_state(
        self,
        state: Literal["completed", "failed", "cancelled"],
        error: str | None,
        *,
        error_detail: LaserAutoCalibrationError | None = None,
    ) -> None:
        await self._update_snapshot(
            state=state,
            current_stage=state,
            error=error,
            error_detail=error_detail,
            stage_detail={**self._snapshot.stage_detail, "phase": state},
        )

    async def _update_snapshot(self, **updates: Any) -> None:
        snapshot = await self._sync_snapshot(**updates)
        await self._publish(snapshot)

    async def _sync_snapshot(self, **updates: Any) -> LaserAutoCalibrationSnapshot:
        async with self._lock:
            for key, value in updates.items():
                if key == "current_side" and value is not None:
                    value = StorageModuleSide(value)
                setattr(self._snapshot, key, value)

            self._snapshot.updated_at = _get_timestamp()

            shelf_edges: list[LaserAutoCalibrationShelfEdge] = []
            candidates: list[LaserAutoCalibrationCandidate] = []
            cells: list[LaserAutoCalibrationCell] = []
            angle_results: list[LaserAutoCalibrationAngleResult] = []

            for side_state in self._side_states.values():
                shelf_edges.extend(side_state.shelf_edges)
                candidates.extend(side_state.candidates)
                cells.extend(side_state.cells)

                if side_state.angle_result is not None:
                    angle_results.append(side_state.angle_result)

            self._snapshot.shelf_edges = sorted(
                shelf_edges,
                key=lambda edge: (edge.side.value, -edge.y_mm, edge.x_mm),
            )
            self._snapshot.shelf_candidates = sorted(
                candidates,
                key=lambda candidate: (candidate.side.value, candidate.shelf_idx if candidate.shelf_idx is not None else 999, candidate.y_min, candidate.x_min),
            )
            self._snapshot.detected_cells = sorted(
                cells,
                key=lambda cell: (cell.side.value, cell.shelf_idx, cell.cell_idx),
            )
            self._snapshot.angle_results = sorted(angle_results, key=lambda result: result.side.value)
            front_side_state = self._side_states.get("front_module", _SideState())
            back_side_state = self._side_states.get("back_module", _SideState())
            self._snapshot.front_scouted_shelf_count = max(
                len([edge for edge in self._snapshot.shelf_edges if edge.side == "front_module"]),
                front_side_state.live_shelf_count,
            )
            self._snapshot.back_scouted_shelf_count = max(
                len([edge for edge in self._snapshot.shelf_edges if edge.side == "back_module"]),
                back_side_state.live_shelf_count,
            )
            self._snapshot.front_candidate_count = max(
                len([candidate for candidate in self._snapshot.shelf_candidates if candidate.side == "front_module"]),
                front_side_state.live_candidate_count,
            )
            self._snapshot.back_candidate_count = max(
                len([candidate for candidate in self._snapshot.shelf_candidates if candidate.side == "back_module"]),
                back_side_state.live_candidate_count,
            )
            self._snapshot.front_detected_cell_count = max(
                len([cell for cell in self._snapshot.detected_cells if cell.side == "front_module"]),
                front_side_state.live_detected_cell_count,
            )
            self._snapshot.back_detected_cell_count = max(
                len([cell for cell in self._snapshot.detected_cells if cell.side == "back_module"]),
                back_side_state.live_detected_cell_count,
            )
            self._snapshot.front_stage_extension_mm = front_side_state.stage_extension_mm
            self._snapshot.back_stage_extension_mm = back_side_state.stage_extension_mm
            self._snapshot.scouted_shelf_count = max(
                len(self._snapshot.shelf_edges),
                self._snapshot.front_scouted_shelf_count + self._snapshot.back_scouted_shelf_count,
            )
            self._snapshot.candidate_count = max(
                len(self._snapshot.shelf_candidates),
                self._snapshot.front_candidate_count + self._snapshot.back_candidate_count,
            )
            self._snapshot.detected_cell_count = max(
                len(self._snapshot.detected_cells),
                self._snapshot.front_detected_cell_count + self._snapshot.back_detected_cell_count,
            )
            snapshot = self._snapshot.model_copy(deep=True)

        return snapshot

    async def _publish(self, snapshot: LaserAutoCalibrationSnapshot) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)

        for queue in subscribers:
            if queue.full() is True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            try:
                queue.put_nowait(snapshot.model_copy(deep=True))
            except asyncio.QueueFull:
                continue

