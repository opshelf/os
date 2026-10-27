"""Server-side port of provision/scripts/cycle_test.py.

The runner drives the device through its own public API over an in-process ASGI
transport, authenticated as the user who started the run, so every store, retrieve,
item create and delete takes the same admission, audit and inventory path the CLI
exercises over the network. Progress is kept in one snapshot that subscribers receive
in full on every change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.tool import MAX_PICK_LIFT_MM
from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.database.models import Item, ItemLocation
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_operating_mode, get_settings

logger = logging.getLogger(__name__)

ALLOWED_MODES = {OperatingMode.development, OperatingMode.mock}
BARCODE_PREFIX = "999900"
COMMAND_TIMEOUT_S = 120.0
COMMAND_POLL_S = 1.0
DB_TIMEOUT_S = 8.0
DB_POLL_S = 0.1
HARDWARE_FAULT_SETTLE_S = 5.0
LOG_LIMIT = 300
TOKEN_REFRESH_S = 600.0

CycleMode = Literal["retrieve_store", "store_retrieve", "store_only", "retrieve_only", "quick", "quick_lift"]
RunState = Literal["idle", "running", "paused", "completed", "failed", "cancelled"]
TERMINAL_STATES = {"idle", "completed", "failed", "cancelled"}
SIDES = ("F", "B")
SIDE_NAMES = {"F": "front_module", "B": "back_module"}


def _parse_target(value: str) -> dict[str, Any]:
	parts = value.split(":")
	side: Optional[str] = None
	if parts[0].upper() in SIDES:
		side = parts.pop(0).upper()
	if len(parts) > 2 or (side is None and len(parts) == 0):
		raise ValueError("expected SIDE, SHELF, SIDE:SHELF, SHELF:CELL, or SIDE:SHELF:CELL")
	try:
		numbers = [int(part) for part in parts]
	except ValueError as exc:
		raise ValueError("expected SIDE, SHELF, SIDE:SHELF, SHELF:CELL, or SIDE:SHELF:CELL") from exc
	numbers += [None] * (2 - len(numbers))
	return {"side": side, "shelf": numbers[0], "cell": numbers[1]}


def _parse_skip(value: str) -> list[dict[str, Any]]:
	patterns = []
	for raw in value.split(","):
		parts = raw.strip().split(":")
		if parts[0].upper() not in SIDES or len(parts) not in (2, 3):
			raise ValueError(f"invalid skip pattern {raw.strip()!r}: expected SIDE:SHELF or SIDE:SHELF:CELL")
		try:
			numbers = [int(part) for part in parts[1:]]
		except ValueError as exc:
			raise ValueError(f"invalid skip pattern {raw.strip()!r}: expected SIDE:SHELF or SIDE:SHELF:CELL") from exc
		patterns.append({"side": parts[0].upper(), "shelf": numbers[0], "cell": numbers[1] if len(numbers) > 1 else None})
	return patterns


def _parse_start(value: str) -> tuple[str, int, int]:
	parts = value.split(":")
	if len(parts) != 3 or parts[0].upper() not in SIDES:
		raise ValueError("expected SIDE:SHELF:CELL")
	try:
		return parts[0].upper(), int(parts[1]), int(parts[2])
	except ValueError as exc:
		raise ValueError("expected SIDE:SHELF:CELL") from exc


def _blank_to_none(value: Optional[str]) -> Optional[str]:
	if value is None or value.strip() == "":
		return None
	return value.strip()


class CycleTestConfig(BaseModel):
	mode: CycleMode = Field("retrieve_store", description="Cycle variant; mirrors the script's mode flags")
	lift_mm: Optional[float] = Field(None, gt=0, le=MAX_PICK_LIFT_MM, description="Lift height for quick_lift mode")
	target: Optional[str] = Field(None, description="SIDE, SHELF, SIDE:SHELF, SHELF:CELL, or SIDE:SHELF:CELL")
	start: Optional[str] = Field(None, description="First cell to test, SIDE:SHELF:CELL")
	skip: Optional[str] = Field(None, description="Comma-separated SIDE:SHELF or SIDE:SHELF:CELL patterns")
	shuffle: bool = False
	clear: bool = Field(False, description="Delete every item and item location before the first loop")
	loop: Optional[int] = Field(None, ge=0, description="Omit to run once, 0 to repeat until aborted, N to repeat N times")
	step: bool = Field(False, description="Pause after every cell and continue past failures")

	@field_validator("target", "start", "skip")
	@classmethod
	def _strip(cls, value: Optional[str]) -> Optional[str]:
		return _blank_to_none(value)

	@model_validator(mode="after")
	def _validate(self) -> "CycleTestConfig":
		if self.mode == "quick_lift" and self.lift_mm is None:
			raise ValueError("quick_lift mode requires lift_mm")
		if self.mode != "quick_lift":
			self.lift_mm = None
		if self.target is not None:
			_parse_target(self.target)
		if self.start is not None:
			_parse_start(self.start)
		if self.skip is not None:
			_parse_skip(self.skip)
		return self


class CellResult(BaseModel):
	cell: str
	barcode: Optional[str] = None
	steps: dict[str, bool] = Field(default_factory=dict)
	passed: bool = False
	error: Optional[str] = None
	duration_s: float = 0.0


class CycleTestSnapshot(BaseModel):
	state: RunState = "idle"
	run_id: Optional[str] = None
	config: Optional[CycleTestConfig] = None
	loop: int = 0
	loops_passed: int = 0
	total: int = 0
	current: Optional[str] = None
	results: list[CellResult] = Field(default_factory=list, description="Cell results of the current loop")
	unsized_cells: list[str] = Field(default_factory=list)
	hardware_fault: Optional[str] = None
	error: Optional[str] = None
	log: list[str] = Field(default_factory=list, description=f"Last {LOG_LIMIT} log lines")
	started_by: Optional[str] = None
	started_at: Optional[str] = None
	updated_at: Optional[str] = None


class CycleTestStartError(Exception):
	def __init__(self, code: str, message: str):
		super().__init__(message)
		self.code = code
		self.message = message


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _cell_id(location: dict[str, Any]) -> str:
	return f"{location['side']}:{location['shelf']}:{location['cell']}"


def _detail(resp: httpx.Response) -> str:
	try:
		data = resp.json()
	except ValueError:
		return resp.text.strip()
	detail = data.get("detail") if isinstance(data, dict) else None
	if isinstance(detail, dict):
		return str(detail.get("message") or detail)
	if detail is not None:
		return str(detail)
	return resp.text.strip()


def _describe_fault(status: dict[str, Any]) -> Optional[str]:
	axes = status.get("needs_home_axes")
	suffix = f"; needs home axes: {', '.join(str(a) for a in axes)}" if isinstance(axes, list) and axes else ""
	if status.get("is_emergency_stopped") is True:
		return f"emergency stop: {status.get('emergency_stop_reason') or 'unknown reason'}"
	if status.get("is_crashed") is True:
		return f"robot crashed: {status.get('crash_reason') or 'unknown reason'}{suffix}"
	if status.get("needs_home") is True:
		return f"home required: {status.get('needs_home_reason') or 'unknown reason'}{suffix}"
	return None


class _UserTokenAuth(httpx.Auth):
	"""Signs each loopback request as the starting user, re-minting before expiry on long loops."""

	def __init__(self, user: Any):
		settings = get_settings()
		self._service = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
		self._user = user
		self._token = ""
		self._minted_at = float("-inf")

	def auth_flow(self, request: httpx.Request):
		if time.monotonic() - self._minted_at > TOKEN_REFRESH_S:
			user = self._user
			self._token = self._service.create_access_token(
				user_id=user.id,
				email=user.email,
				role=user.role.name,
				permissions=user.role.permissions,
				access_level=user.access_level.name if user.access_level else None,
				expires_minutes=30,
			)
			self._minted_at = time.monotonic()
		request.headers["Authorization"] = f"Bearer {self._token}"
		yield request


class _Aborted(Exception):
	pass


class CycleTestRunner:
	def __init__(self, manager: "CycleTestManager", app: Any, user: Any, config: CycleTestConfig):
		self.manager = manager
		self.config = config
		self.snapshot = manager.snapshot
		self.client = httpx.AsyncClient(
			transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
			base_url="http://cycle-test",
			auth=_UserTokenAuth(user),
			timeout=COMMAND_TIMEOUT_S + 10,
		)
		self.barcode_counter: Optional[int] = None
		self.is_mock = get_operating_mode() == OperatingMode.mock

	def log(self, line: str) -> None:
		self.manager.log(line)

	# -- robot state -------------------------------------------------------

	async def hardware_fault(self) -> Optional[str]:
		if self.is_mock:
			return None
		try:
			worker = await get_robot_worker()
		except ValueError:
			return "robot is not initialized"
		raw = await worker.get_status_snapshot(force_refresh=True)
		if raw is None:
			return None
		status = json.loads(raw) if isinstance(raw, str) else raw
		return _describe_fault(status) if isinstance(status, dict) else None

	async def wait_for_hardware_fault(self) -> Optional[str]:
		# The worker publishes the failure before it enters the crash state.
		deadline = time.monotonic() + HARDWARE_FAULT_SETTLE_S
		while True:
			fault = await self.hardware_fault()
			if fault is not None or time.monotonic() >= deadline:
				return fault
			await asyncio.sleep(0.5)

	# -- inventory ---------------------------------------------------------

	async def item_at(self, side: str, shelf: int, cell: int) -> Optional[str]:
		async with AsyncSessionLocal() as db:
			result = await db.execute(
				select(Item.barcode)
				.join(ItemLocation, ItemLocation.item_id == Item.id)
				.where(ItemLocation.side == side, ItemLocation.shelf_idx == shelf, ItemLocation.cell_idx == cell)
			)
			return result.scalars().first()

	async def wait_for_location(self, barcode: str, side: str, shelf: int, cell: int, present: bool) -> tuple[bool, float]:
		start = time.monotonic()
		while True:
			elapsed = time.monotonic() - start
			current = await self.item_at(side, shelf, cell)
			if (current == barcode) is present:
				return True, elapsed
			if elapsed > DB_TIMEOUT_S:
				return False, elapsed
			await asyncio.sleep(DB_POLL_S)

	async def clear_database(self) -> bool:
		deleted = 0
		seen: set[str] = set()
		undeletable: dict[str, str] = {}
		while True:
			resp = await self.client.get("/api/storage/items", params={"skip": 0, "limit": 200})
			if resp.status_code != 200:
				self.log(f"  Clear failed: could not list items: {resp.status_code} {_detail(resp)}")
				return False
			candidates = [
				item["barcode"] for item in resp.json().get("items", [])
				if item.get("barcode") is not None and item["barcode"] not in seen
			]
			if not candidates:
				break
			for barcode in candidates:
				seen.add(barcode)
				del_resp = await self.client.post("/api/storage/items/delete", json={"barcode": barcode})
				if del_resp.status_code in (200, 204, 404):
					deleted += 1
				elif del_resp.status_code == 409:
					undeletable[barcode] = _detail(del_resp)
					self.log(f"  Skipping undeletable item {barcode}: {undeletable[barcode]}")
				else:
					self.log(f"  Clear failed deleting {barcode}: {del_resp.status_code} {_detail(del_resp)}")
					return False
		suffix = f"; skipped {len(undeletable)} undeletable" if undeletable else ""
		self.log(f"  Cleared {deleted} items (and their locations){suffix}")
		return True

	async def next_barcode(self) -> str:
		if self.barcode_counter is None:
			# Start past every leftover test barcode so a run never walks through old ones.
			async with AsyncSessionLocal() as db:
				existing = (await db.execute(select(Item.barcode).where(Item.barcode.startswith(BARCODE_PREFIX)))).scalars()
				suffixes = [b[len(BARCODE_PREFIX):] for b in existing]
			self.barcode_counter = 1 + max((int(x) for x in suffixes if x.isascii() and x.isdigit()), default=0)
		barcode = f"{BARCODE_PREFIX}{self.barcode_counter:06d}"
		self.barcode_counter += 1
		return barcode

	async def create_item(self, capacity: dict[str, float]) -> tuple[Optional[str], Optional[str]]:
		dimensions = {axis: int(capacity[axis] * 0.8) for axis in ("width", "height", "length")}
		for _ in range(100):
			barcode = await self.next_barcode()
			# Creating an existing barcode answers 200 and would reuse an item from an earlier
			# run, possibly still stored elsewhere, so only fresh barcodes are used.
			async with AsyncSessionLocal() as db:
				if (await db.execute(select(Item.id).where(Item.barcode == barcode))).first() is not None:
					continue
			resp = await self.client.post(
				"/api/storage/items",
				json={"barcode": barcode, "name": f"Test Item {barcode}", "dimensions": dimensions},
			)
			if resp.status_code in (200, 201):
				return barcode, None
			if resp.status_code != 409:
				return None, f"{resp.status_code} {_detail(resp)}"
		return None, "barcode collided 100 times"

	async def seed_location(self, barcode: str, side: str, shelf: int, cell: int) -> Optional[str]:
		resp = await self.client.post(
			"/api/storage/items/locations/add",
			json={"barcode": barcode, "side": side, "shelf_idx": shelf, "cell_idx": cell},
		)
		if resp.status_code in (200, 201):
			return None
		return f"{resp.status_code} {_detail(resp)}"

	# -- robot commands ----------------------------------------------------

	async def run_command(self, payload: dict[str, Any]) -> Optional[str]:
		"""Submit a command and wait for its durable result; returns an error or None."""
		resp = await self.client.post("/api/robot/commands", json=payload)
		if resp.status_code != 200:
			return f"{resp.status_code} {_detail(resp)}"
		command_id = resp.json().get("command_id")
		start = time.monotonic()
		last_fault_check = start
		while True:
			now = time.monotonic()
			if now - start > COMMAND_TIMEOUT_S:
				return "command result timeout"
			if now - last_fault_check >= 1.0:
				last_fault_check = now
				fault = await self.hardware_fault()
				if fault is not None:
					self.snapshot.hardware_fault = fault
					return fault
			row = await self.client.get(f"/api/robot/commands/{command_id}")
			if row.status_code == 200:
				data = row.json()
				if data["status"] == "succeeded":
					return None
				if data["status"] == "failed":
					result = data.get("result") or {}
					error = str(result.get("error") or result.get("message") or "command failed")
					if "not in demo mapping" in error:
						error += ". Disable demo.enabled in data/config.json and restart the API for arbitrary cycle-test barcodes."
					return error
			await asyncio.sleep(COMMAND_POLL_S)

	async def store(self, barcode: str, side: str, shelf: int, cell: int) -> Optional[str]:
		location = {"side": side, "shelf_idx": shelf, "cell_idx": cell}
		error = await self.run_command({"command": "store_item", "item": {"barcode": barcode, "location": location}})
		if error is not None:
			return error
		ok, _ = await self.wait_for_location(barcode, side, shelf, cell, True)
		return None if ok else "item was not at location in database"

	async def retrieve(self, barcode: str, side: str, shelf: int, cell: int) -> Optional[str]:
		location = {"side": side, "shelf_idx": shelf, "cell_idx": cell}
		error = await self.run_command({"command": "retrieve_item", "barcode": barcode, "location": location})
		if error is not None:
			return error
		ok, _ = await self.wait_for_location(barcode, side, shelf, cell, False)
		return None if ok else "item was not removed from location in database"

	async def quick(self, side: str, shelf: int, cell: int) -> Optional[str]:
		params = {"lift": f"{self.config.lift_mm:g}"} if self.config.lift_mm is not None else None
		resp = await self.client.post(
			"/api/calibration/test",
			params=params,
			json={"side": side, "shelf_idx": shelf, "cell_idx": cell},
		)
		return None if resp.status_code == 200 else f"{resp.status_code} {_detail(resp)}"

	# -- cells -------------------------------------------------------------

	async def locations(self) -> Optional[list[dict[str, Any]]]:
		resp = await self.client.get("/api/storage/grid")
		if resp.status_code != 200:
			self.log(f"Could not read the storage grid: {resp.status_code} {_detail(resp)}")
			return None
		grid = resp.json()
		locations: list[dict[str, Any]] = []
		unsized: list[str] = []
		skipped = {"disabled": 0, "maintenance": 0, "deleted": 0}
		for side, key in (("F", "front_module"), ("B", "back_module")):
			for shelf_position, shelf in enumerate(grid.get(key) or []):
				if not isinstance(shelf, dict):
					continue
				shelf_idx = shelf.get("shelf_idx", shelf_position)
				for cell_position, cell in enumerate(shelf.get("cells") or []):
					if not isinstance(cell, dict):
						continue
					status = str(cell.get("status") or "").lower()
					if cell.get("deleted") is True or status == "deleted":
						skipped["deleted"] += 1
						continue
					if status in ("disabled", "maintenance"):
						skipped[status] += 1
						continue
					dims = cell.get("dimensions") or {}
					capacity = {
						"width": dims.get("width", 85),
						"height": dims.get("height", 200),
						"length": dims.get("length", 85),
					}
					cell_idx = cell.get("cell_idx", cell_position)
					# A laser-recalibrated cell keeps zero dimensions until sized by hand, and the
					# robot refuses an item that does not fit, so it cannot be cycled.
					if any(capacity[axis] <= 0 for axis in capacity):
						unsized.append(f"{side}:{shelf_idx}:{cell_idx}")
						continue
					locations.append({"side": side, "shelf": shelf_idx, "cell": cell_idx, "capacity": capacity})
		for kind, count in skipped.items():
			if count > 0:
				self.log(f"  Skipped {count} {kind} cells")
		if unsized:
			self.log(f"  Skipped {len(unsized)} cells with no dimensions set: {', '.join(unsized)}")
		self.snapshot.unsized_cells = unsized
		return locations

	def filter_locations(self, locations: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
		config = self.config
		if config.target is not None:
			target = _parse_target(config.target)
			locations = [
				loc for loc in locations
				if all(target[key] is None or loc[key] == target[key] for key in ("side", "shelf", "cell"))
			]
			if not locations:
				self.log(f"No matching cells for target: {config.target}")
				return None
		if config.skip is not None:
			patterns = _parse_skip(config.skip)
			before = len(locations)
			locations = [
				loc for loc in locations
				if not any(
					loc["side"] == p["side"] and loc["shelf"] == p["shelf"] and (p["cell"] is None or loc["cell"] == p["cell"])
					for p in patterns
				)
			]
			if before != len(locations):
				self.log(f"  Skipped {before - len(locations)} cells matching skip patterns")
			if not locations:
				self.log("No cells remaining after applying skip patterns")
				return None
		if config.start is not None:
			start_key = (SIDES.index(_parse_start(config.start)[0]), *_parse_start(config.start)[1:])
			index = next(
				(i for i, loc in enumerate(locations) if (SIDES.index(loc["side"]), loc["shelf"], loc["cell"]) >= start_key),
				None,
			)
			if index is None:
				self.log(f"No enabled cells at or after: {config.start}")
				return None
			locations = locations[index:]
			self.log(f"Starting from {_cell_id(locations[0])} ({len(locations)} cells remaining)")
		if config.shuffle:
			random.shuffle(locations)
			self.log("Shuffle mode enabled - testing cells in random order")
		return locations

	async def test_cell(self, location: dict[str, Any], result: CellResult) -> None:
		side = SIDE_NAMES[location["side"]]
		shelf, cell = location["shelf"], location["cell"]
		mode = self.config.mode

		async def step(name: str, label: str, error: Optional[str]) -> bool:
			result.steps[name] = error is None
			if error is not None:
				result.error = error
				self.log(f"  - {label}: FAIL - {error}")
				return False
			self.log(f"  - {label}: OK")
			return True

		if mode in ("quick", "quick_lift"):
			label = f"Quick lift ({self.config.lift_mm:g} mm)" if mode == "quick_lift" else "Quick pick/place"
			result.passed = await step(mode, label, await self.quick(side, shelf, cell))
			return

		barcode = await self.item_at(side, shelf, cell)
		existing = barcode is not None
		if existing:
			self.log(f"  - Existing item at location: {barcode}")
		else:
			barcode, error = await self.create_item(location["capacity"])
			if not await step("create", f"Create item{f' ({barcode})' if barcode else ''}", error):
				return
		assert barcode is not None
		result.barcode = barcode
		self.manager.publish()

		if mode in ("store_retrieve", "store_only"):
			if not await step("store", "Store", await self.store(barcode, side, shelf, cell)):
				return
			if mode == "store_retrieve" and not await step("retrieve", "Retrieve", await self.retrieve(barcode, side, shelf, cell)):
				return
			result.passed = True
			return

		if not existing and not await step("seed_location", "Seed location", await self.seed_location(barcode, side, shelf, cell)):
			return
		if not await step("retrieve", "Retrieve", await self.retrieve(barcode, side, shelf, cell)):
			return
		if mode == "retrieve_only":
			result.passed = True
			return
		store_step = "re_store" if existing else "store"
		store_label = "Re-store" if existing else "Store"
		result.passed = await step(store_step, store_label, await self.store(barcode, side, shelf, cell))

	# -- run ---------------------------------------------------------------

	async def run_loop(self, is_first: bool) -> bool:
		"""Run one pass over the selected cells; returns whether every cell passed."""
		snapshot = self.snapshot
		snapshot.results = []
		snapshot.total = 0

		fault = await self.hardware_fault()
		if fault is not None:
			snapshot.hardware_fault = fault
			self.log(f"Robot has a hardware fault: {fault}")
			return False

		if is_first and self.config.clear:
			self.log("Clearing all items and item locations...")
			if not await self.clear_database():
				snapshot.error = "failed to clear the database"
				return False

		self.log("Getting storage locations...")
		locations = await self.locations()
		if locations is None:
			snapshot.error = "could not read the storage grid"
			return False
		if not locations:
			self.log("Warning: no storage cells available, skipping cell tests")
			snapshot.error = "no cells to test"
			return False
		filtered = self.filter_locations(locations)
		if filtered is None:
			snapshot.error = "no cells to test"
			return False

		snapshot.total = len(filtered)
		self.log(f"Found {len(filtered)} cells to test")
		self.manager.publish()

		for index, location in enumerate(filtered, 1):
			self.manager.check_abort()
			cell_id = _cell_id(location)
			capacity = location["capacity"]
			self.log(f"[{index}/{len(filtered)}] Cell {cell_id} ({capacity['width']}x{capacity['height']}x{capacity['length']}mm)")
			snapshot.current = cell_id
			result = CellResult(cell=cell_id)
			snapshot.results.append(result)
			self.manager.publish()

			started = time.monotonic()
			try:
				await self.test_cell(location, result)
			except httpx.HTTPError as exc:
				logger.warning("cycle test request failed", exc_info=exc)
				result.error = "request to the API failed"
				self.log(f"  - FAIL - {result.error}")
			result.duration_s = round(time.monotonic() - started, 1)
			snapshot.current = None
			self.manager.publish()

			if not result.passed and snapshot.hardware_fault is None:
				snapshot.hardware_fault = await self.wait_for_hardware_fault()
			if snapshot.hardware_fault is not None:
				self.log(f"HARDWARE FAULT at cell {cell_id}: {snapshot.hardware_fault}")
				self.log("Stopping test. Inspect the robot and home it before running again.")
				break
			if not result.passed and not self.config.step:
				self.log(f"Stopping due to failure at cell {cell_id}")
				break
			if self.config.step and index < len(filtered):
				await self.manager.pause()

		passed = sum(1 for r in snapshot.results if r.passed)
		failed = len(snapshot.results) - passed
		self.log(f"Summary: {passed}/{len(snapshot.results)} passed, {failed} failed")
		if failed:
			self.log(f"Failed cells: {', '.join(r.cell for r in snapshot.results if not r.passed)}")
		return failed == 0 and snapshot.hardware_fault is None

	async def run(self) -> None:
		snapshot = self.snapshot
		config = self.config
		name = {
			"quick": "Quick Pick/Place Test",
			"quick_lift": f"Quick Lift Test ({config.lift_mm:g} mm)" if config.lift_mm else "",
		}.get(config.mode, f"Cycle Test ({config.mode})")
		final: RunState = "completed"
		try:
			while True:
				snapshot.loop += 1
				self.log(f"{name} - loop {snapshot.loop}" if config.loop is not None else name)
				if not await self.run_loop(snapshot.loop == 1):
					final = "failed"
					if config.loop is not None:
						self.log(f"Stopping loop after failure on loop {snapshot.loop}")
					break
				snapshot.loops_passed += 1
				if config.loop is None:
					break
				if config.loop > 0 and snapshot.loop >= config.loop:
					self.log(f"Stopping after {snapshot.loop} loop(s)")
					break
				self.manager.check_abort()
		except _Aborted:
			final = "cancelled"
			self.log("Aborted by user")
		except Exception as exc:
			logger.error("cycle test run failed", exc_info=exc)
			final = "failed"
			snapshot.error = "cycle test stopped on an internal error"
			self.log(snapshot.error)
		finally:
			await self.client.aclose()
			snapshot.state = final
			snapshot.current = None
			self.manager.publish()


class CycleTestManager:
	def __init__(self) -> None:
		self.snapshot = CycleTestSnapshot()
		self._log: deque[str] = deque(maxlen=LOG_LIMIT)
		self._subscribers: set[asyncio.Queue[str]] = set()
		self._task: Optional[asyncio.Task[None]] = None
		self._continue = asyncio.Event()
		self._abort = False
		self._lock = asyncio.Lock()

	def is_active(self) -> bool:
		return self._task is not None and not self._task.done()

	def log(self, line: str) -> None:
		self._log.append(line)
		self.publish()

	def serialize(self) -> str:
		self.snapshot.log = list(self._log)
		return self.snapshot.model_dump_json()

	def publish(self) -> None:
		self.snapshot.updated_at = _now()
		payload = self.serialize()
		for queue in self._subscribers:
			if queue.full():
				queue.get_nowait()
			queue.put_nowait(payload)

	def subscribe(self) -> tuple[asyncio.Queue[str], str]:
		queue: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
		self._subscribers.add(queue)
		return queue, self.serialize()

	def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
		self._subscribers.discard(queue)

	def check_abort(self) -> None:
		if self._abort:
			raise _Aborted()

	async def pause(self) -> None:
		self.check_abort()
		self._continue.clear()
		self.snapshot.state = "paused"
		self.log("Paused; continue to test the next cell")
		await self._continue.wait()
		self.check_abort()
		self.snapshot.state = "running"
		self.publish()

	async def start(self, app: Any, user: Any, config: CycleTestConfig) -> CycleTestSnapshot:
		async with self._lock:
			if self.is_active():
				raise CycleTestStartError("cycle_test_running", "A cycle test is already running.")
			if get_operating_mode() not in ALLOWED_MODES:
				raise CycleTestStartError(
					"cycle_test_mode",
					"Cycle tests run only in development or mock mode because they create and delete items.",
				)
			self._abort = False
			self._log.clear()
			now = _now()
			self.snapshot = CycleTestSnapshot(
				state="running",
				run_id=uuid4().hex,
				config=config,
				started_by=user.email,
				started_at=now,
				updated_at=now,
			)
			runner = CycleTestRunner(self, app, user, config)
			self._task = asyncio.create_task(runner.run())
			return self.snapshot.model_copy(deep=True)

	def resume(self) -> bool:
		if self.snapshot.state != "paused":
			return False
		self.snapshot.state = "running"
		self.publish()
		self._continue.set()
		return True

	def abort(self) -> bool:
		if not self.is_active():
			return False
		self._abort = True
		self.log("Abort requested; stopping after the current cell")
		self._continue.set()
		return True

	async def wait(self) -> None:
		if self._task is not None:
			await self._task


_manager: Optional[CycleTestManager] = None


def get_cycle_test_manager() -> CycleTestManager:
	global _manager
	if _manager is None:
		_manager = CycleTestManager()
	return _manager
