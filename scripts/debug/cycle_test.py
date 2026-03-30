#!/usr/bin/env python3
# /// script
# dependencies = ["requests>=2.28.0", "sseclient-py>=1.7.2"]
# ///
import argparse
import atexit
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import sseclient

BASE_URL = "http://localhost:8000"
UPCA_PREFIX = "999900"
DEFAULT_TIMEOUT = 120
REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_remote_script(explicit_path: str | None = None) -> Path:
	candidates: list[Path] = []
	if explicit_path is not None:
		candidates.append(Path(explicit_path).expanduser())

	env_path = os.environ.get("OPSHELF_REMOTE_SCRIPT")
	if env_path:
		candidates.append(Path(env_path).expanduser())

	candidates.append(REPO_ROOT.parent / "provision" / "remote")

	for candidate in candidates:
		if candidate.exists() is True:
			return candidate.resolve()

	raise FileNotFoundError(
		"Could not find the relay helper. Set --remote-script or OPSHELF_REMOTE_SCRIPT, "
		"or keep the sibling provision repo checked out."
	)


def resolve_storage_config_path(explicit_path: str | None = None) -> Path | None:
	candidates: list[Path] = []
	if explicit_path is not None:
		candidates.append(Path(explicit_path).expanduser())

	candidates.extend([
		REPO_ROOT / "perpetual_hardware" / "data" / "storage.json",
		REPO_ROOT / "perpetual_hardware" / "packages" / "perpetual_os1_server" / "data" / "storage.json",
		REPO_ROOT.parent / "provision" / "config" / "storage.json",
	])

	for candidate in candidates:
		if candidate.exists() is True:
			return candidate.resolve()

	return None


class CycleTest:
	def __init__(self, device_id: str, dry_run: bool = False, step_mode: bool = False, store_first: bool = False, shuffle: bool = False, remote_script: str | None = None, storage_config: str | None = None):
		self.device_id = device_id
		self.dry_run = dry_run
		self.step_mode = step_mode
		self.store_first = store_first
		self.shuffle = shuffle
		self.remote_script = remote_script
		self.storage_config = storage_config
		self.session = requests.Session()
		self.tunnel_proc = None
		self.upca_counter = 1
		self.results = []

	def start_tunnel(self, port: int = 8000):
		remote_script = resolve_remote_script(self.remote_script)
		remote_cwd = remote_script.parent

		if self.dry_run is True:
			print(f"[dry-run] would start tunnel: {remote_script} {self.device_id} -f {port}")
			return

		self.tunnel_proc = subprocess.Popen(
			[str(remote_script), self.device_id, "-f", str(port)],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			cwd=remote_cwd,
		)
		atexit.register(self.cleanup)

		for _ in range(10):
			time.sleep(1)
			if self.tunnel_proc.poll() is not None:
				_, stderr = self.tunnel_proc.communicate()
				print(f"Tunnel failed: {stderr.decode()}")
				return
			try:
				resp = requests.get(f"{BASE_URL}/api/health", timeout=1)
				if resp.status_code == 200:
					return
			except requests.RequestException:
				pass

		print("Warning: tunnel may not be ready")

	def cleanup(self):
		if self.tunnel_proc is not None:
			self.tunnel_proc.terminate()
			self.tunnel_proc.wait()

	def authenticate(self) -> bool:
		if self.dry_run is True:
			print("[dry-run] would authenticate")
			return True

		url = f"{BASE_URL}/api/auth/login/json"
		payload = {"email": "admin@opshelf.com", "password": "1234", "pin": "1234"}

		try:
			resp = self.session.post(url, json=payload, timeout=10)
			if resp.status_code == 200:
				data = resp.json()
				token = data.get("access_token")
				if token is not None:
					self.session.headers["Authorization"] = f"Bearer {token}"
					return True
		except requests.RequestException as e:
			print(f"Authentication failed: {e}")

		return False

	def check_robot_ready(self) -> bool:
		if self.dry_run is True:
			return True

		url = f"{BASE_URL}/api/robot/status"
		headers = dict(self.session.headers)
		headers["Accept"] = "text/event-stream"

		try:
			resp = requests.get(url, headers=headers, stream=True, timeout=(5, 30))
			if resp.status_code != 200:
				print(f"  Robot status check failed: {resp.status_code}")
				return False

			client = sseclient.SSEClient(resp)
			for event in client.events():
				if event.data is None or event.data == "":
					continue
				try:
					data = json.loads(event.data)
					is_crashed = data.get("is_crashed", False)
					error_queue = data.get("error_queue", [])

					if is_crashed is True:
						print(f"  Robot is in crash state: {data.get('crash_reason', 'unknown')}")
						return False

					if len(error_queue) > 0:
						print(f"  Robot has {len(error_queue)} pending errors (will be ignored)")

					return True
				except json.JSONDecodeError:
					continue

			return True
		except requests.RequestException as e:
			print(f"  Robot status check error: {e}")
			return False

	def get_storage_from_device(self) -> dict:
		if self.dry_run is True:
			return {}

		url = f"{BASE_URL}/api/calibration/storage"

		try:
			resp = self.session.get(url, timeout=10)
			if resp.status_code != 200:
				print(f"  Warning: could not fetch storage from device: {resp.status_code}")
				return {}

			return resp.json()
		except requests.RequestException as e:
			print(f"  Warning: could not fetch storage from device: {e}")
			return {}

	def get_storage_locations(self) -> list:
		device_storage = self.get_storage_from_device()

		if len(device_storage) > 0:
			storage = device_storage
			print("  Using storage configuration from device")
		else:
			config_path = resolve_storage_config_path(self.storage_config)

			if config_path is None:
				print("Storage config not found. Pass --storage-config or keep a sibling provision repo checked out.")
				return []

			try:
				with open(config_path) as f:
					storage = json.load(f)
			except (json.JSONDecodeError, IOError) as e:
				print(f"Failed to read storage config: {e}")
				return []

			print("  Using local storage configuration")

		locations = []
		disabled_count = 0

		for side, key in [("L", "left_module"), ("R", "right_module")]:
			module = storage.get(key, [])
			for shelf_idx, shelf in enumerate(module):
				cells = shelf.get("cells", [])
				for cell_idx, cell in enumerate(cells):
					if cell.get("status") == "disabled":
						disabled_count += 1
						continue
					dims = cell.get("dimensions", {})
					locations.append({
						"shelf": shelf_idx,
						"cell": cell_idx,
						"side": side,
						"status": cell.get("status"),
						"capacity": {
							"width": dims.get("width", 85),
							"height": dims.get("height", 200),
							"length": dims.get("length", 85),
						},
					})

		if disabled_count > 0:
			print(f"  Skipped {disabled_count} disabled cells")

		return locations

	def generate_upca(self) -> str:
		upca = f"{UPCA_PREFIX}{self.upca_counter:06d}"
		self.upca_counter += 1
		return upca

	def get_test_dimensions(self, capacity: dict) -> dict:
		return {
			"width": int(capacity.get("width", 100) * 0.8),
			"height": int(capacity.get("height", 150) * 0.8),
			"length": int(capacity.get("length", 80) * 0.8),
		}

	def create_item(self, upca: str, dimensions: dict) -> tuple[bool, str]:
		if self.dry_run is True:
			print(f"  [dry-run] would create item {upca}")
			return True, upca

		url = f"{BASE_URL}/api/storage/items"

		for _ in range(100):
			payload = {
				"upca": upca,
				"name": f"Test Item {upca}",
				"dimensions": dimensions,
			}

			try:
				resp = self.session.post(url, json=payload, timeout=10)
				if resp.status_code in (200, 201):
					return True, upca
				if resp.status_code == 409:
					upca = self.generate_upca()
					continue
				return False, upca
			except requests.RequestException:
				return False, upca

		return False, upca

	def send_store_command(self, upca: str, location: dict = None) -> dict:
		if self.dry_run is True:
			print(f"  [dry-run] would send store command {upca}")
			return {"success": True}

		url = f"{BASE_URL}/api/robot/commands"
		item_payload = {"upca": upca}

		if location is not None:
			side_raw = location.get("side", "L")
			if side_raw == "L":
				side_value = "left_module"
			elif side_raw == "R":
				side_value = "right_module"
			else:
				side_value = side_raw

			item_payload["location"] = {
				"side": side_value,
				"shelf_idx": location.get("shelf", 0),
				"cell_idx": location.get("cell", 0),
			}

		payload = {
			"command": "store_item",
			"item": item_payload,
		}

		try:
			resp = self.session.post(url, json=payload, timeout=10)
			if resp.status_code == 200:
				return {"success": True}
			return {"success": False, "status": resp.status_code, "error": resp.text}
		except requests.RequestException as e:
			return {"success": False, "error": str(e)}

	def get_item_location(self, upca: str) -> dict:
		if self.dry_run is True:
			return None

		url = f"{BASE_URL}/api/storage/items"

		try:
			resp = self.session.get(url, timeout=10)
			if resp.status_code != 200:
				return None

			data = resp.json()
			items = data.get("items", [])

			for item in items:
				if item.get("upca") == upca:
					locations = item.get("locations", [])
					if len(locations) > 0:
						return locations[0]
			return None
		except requests.RequestException:
			return None

	def get_item_at_location(self, shelf: int, cell: int, side: str) -> dict:
		if self.dry_run is True:
			return {"upca": "dry-run-item", "id": 0}

		url = f"{BASE_URL}/api/storage/items"

		try:
			resp = self.session.get(url, timeout=10)
			if resp.status_code != 200:
				return None

			data = resp.json()
			items = data.get("items", [])

			for item in items:
				locations = item.get("locations", [])
				for loc in locations:
					loc_side = loc.get("side", "")
					loc_shelf = loc.get("shelf_idx", -1)
					loc_cell = loc.get("cell_idx", -1)

					if loc_side == side and loc_shelf == shelf and loc_cell == cell:
						return {"upca": item.get("upca"), "id": item.get("id")}

			return None
		except requests.RequestException:
			return None

	def send_retrieve_command(self, upca: str, shelf: int, cell: int, side: str) -> bool:
		if self.dry_run is True:
			print(f"  [dry-run] would send retrieve command {upca}")
			return True

		url = f"{BASE_URL}/api/robot/commands"
		payload = {
			"command": "retrieve_item",
			"item": {"upca": upca},
			"location": {"shelf_idx": shelf, "cell_idx": cell, "side": side},
		}

		try:
			resp = self.session.post(url, json=payload, timeout=10)
			if resp.status_code != 200:
				print(f"    [retrieve] status={resp.status_code}, response={resp.text[:200]}")
			return resp.status_code == 200
		except requests.RequestException as e:
			print(f"    [retrieve] error: {e}")
			return False

	def send_retrieve_by_location(self, shelf: int, cell: int, side: str) -> bool:
		if self.dry_run is True:
			print(f"  [dry-run] would send retrieve command for {side}:{shelf}:{cell}")
			return True

		url = f"{BASE_URL}/api/robot/commands"
		payload = {
			"command": "retrieve_item",
			"location": {"shelf_idx": shelf, "cell_idx": cell, "side": side},
		}

		try:
			resp = self.session.post(url, json=payload, timeout=10)
			if resp.status_code != 200:
				print(f"    [retrieve] status={resp.status_code}, response={resp.text[:200]}")
			return resp.status_code == 200
		except requests.RequestException as e:
			print(f"    [retrieve] error: {e}")
			return False

	def wait_for_completion(self, command: str, upca: str, timeout: int = DEFAULT_TIMEOUT, location: dict = None) -> dict:
		if self.dry_run is True:
			print(f"  [dry-run] would wait for {command} completion")
			return {"success": True, "duration": 0}

		start_time = time.time()
		url = f"{BASE_URL}/api/robot/status"
		headers = dict(self.session.headers)
		headers["Accept"] = "text/event-stream"

		print(f"    [sse] connecting to {url}")

		try:
			resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
			print(f"    [sse] response status: {resp.status_code}")
			print(f"    [sse] content-type: {resp.headers.get('content-type', 'unknown')}")

			if resp.status_code != 200:
				return {"success": False, "error": f"status {resp.status_code}", "duration": 0}

			client = sseclient.SSEClient(resp)
			event_count = 0
			initial_error_count = None

			for event in client.events():
				event_count += 1
				elapsed = time.time() - start_time

				print(f"    [sse] event #{event_count}: type={event.event}, data={event.data[:200] if event.data else 'None'}...")

				if elapsed > timeout:
					return {"success": False, "error": "timeout", "duration": elapsed}

				if event.data is None or event.data == "":
					continue

				try:
					data = json.loads(event.data)
				except json.JSONDecodeError as e:
					print(f"    [sse] json parse error: {e}")
					continue

				print(f"    [sse] keys: {list(data.keys())}")

				running = data.get("running", False)
				current_command = data.get("current_command")
				error_queue = data.get("error_queue", [])
				is_crashed = data.get("is_crashed", False)
				print(f"    [sse] running={running}, current_command={current_command}")

				if initial_error_count is None:
					initial_error_count = len(error_queue)

				if is_crashed is True:
					crash_reason = data.get("crash_reason", "unknown")
					return {"success": False, "error": f"Robot crashed: {crash_reason}", "duration": elapsed}

				if len(error_queue) > initial_error_count:
					new_error = error_queue[-1]
					print(f"    [sse] new error: {json.dumps(new_error, indent=2, default=str)}")
					error = new_error.get("message", "unknown error")
					return {"success": False, "error": error, "duration": elapsed}

				if running is True and current_command is None:
					return {"success": True, "duration": elapsed}

		except requests.RequestException as e:
			elapsed = time.time() - start_time
			print(f"    [sse] request error: {e}")
			return {"success": False, "error": str(e), "duration": elapsed}

		elapsed = time.time() - start_time
		print(f"    [sse] stream ended after {event_count} events")
		return {"success": False, "error": "stream ended", "duration": elapsed}

	def format_cell_id(self, location: dict) -> str:
		side = location.get("side", "?")
		shelf = location.get("shelf", "?")
		cell = location.get("cell", "?")
		return f"{side}:{shelf}:{cell}"

	def format_dimensions(self, capacity: dict) -> str:
		w = capacity.get("width", "?")
		h = capacity.get("height", "?")
		l = capacity.get("length", "?")
		return f"{w}x{h}x{l}mm"

	def test_cell(self, location: dict, index: int, total: int) -> dict:
		cell_id = self.format_cell_id(location)
		capacity = location.get("capacity", {})
		dims_str = self.format_dimensions(capacity)

		print(f"\n[{index}/{total}] Cell {cell_id} ({dims_str})")

		shelf = location.get("shelf", 0)
		cell = location.get("cell", 0)
		side_raw = location.get("side", "L")

		if side_raw == "L":
			side = "left_module"
		elif side_raw == "R":
			side = "right_module"
		else:
			side = side_raw

		result = {
			"cell": cell_id,
			"upca": None,
			"steps": {},
			"passed": False,
		}

		existing_item = self.get_item_at_location(shelf, cell, side)

		if existing_item is not None:
			upca = existing_item.get("upca")
			result["upca"] = upca
			print(f"  - Existing item at location: {upca}")
		else:
			upca = self.generate_upca()
			dimensions = self.get_test_dimensions(capacity)

			success, upca = self.create_item(upca, dimensions)
			if success is False:
				print("  - Create item: FAIL")
				result["steps"]["create"] = False
				return result
			print(f"  - Create item: OK ({upca})")
			result["steps"]["create"] = True
			result["upca"] = upca

		if self.store_first is True:
			cycle_result = self.run_store_retrieve_cycle(upca, location, shelf, cell, side, result)
		else:
			cycle_result = self.run_retrieve_store_cycle(upca, location, shelf, cell, side, result, existing_item)

		return cycle_result

	def run_store_retrieve_cycle(self, upca: str, location: dict, shelf: int, cell: int, side: str, result: dict) -> dict:
		store_cmd = self.send_store_command(upca, location)
		if store_cmd.get("success") is False:
			error = store_cmd.get("error", "unknown")
			print(f"  - Store: FAIL - {error}")
			result["steps"]["store"] = False
			return result

		store_result = self.wait_for_completion("store_item", upca)
		if store_result["success"] is False:
			error = store_result.get("error", "unknown")
			print(f"  - Store: FAIL - {error}")
			result["steps"]["store"] = False
			return result
		print(f"  - Store: OK ({store_result['duration']:.1f}s)")
		result["steps"]["store"] = True

		if self.send_retrieve_by_location(shelf, cell, side) is False:
			print("  - Retrieve: FAIL - command rejected")
			result["steps"]["retrieve"] = False
			return result

		retrieve_result = self.wait_for_completion("retrieve_item", None)
		if retrieve_result["success"] is False:
			error = retrieve_result.get("error", "unknown")
			print(f"  - Retrieve: FAIL - {error}")
			result["steps"]["retrieve"] = False
			return result
		print(f"  - Retrieve: OK ({retrieve_result['duration']:.1f}s)")
		result["steps"]["retrieve"] = True

		result["passed"] = True
		return result

	def run_retrieve_store_cycle(self, upca: str, location: dict, shelf: int, cell: int, side: str, result: dict, existing_item: dict) -> dict:
		if existing_item is None:
			store_cmd = self.send_store_command(upca, location)
			if store_cmd.get("success") is False:
				error = store_cmd.get("error", "unknown")
				print(f"  - Store: FAIL - {error}")
				result["steps"]["store"] = False
				return result

			store_result = self.wait_for_completion("store_item", upca)
			if store_result["success"] is False:
				error = store_result.get("error", "unknown")
				print(f"  - Store: FAIL - {error}")
				result["steps"]["store"] = False
				return result
			print(f"  - Store: OK ({store_result['duration']:.1f}s)")
			result["steps"]["store"] = True

		if self.send_retrieve_by_location(shelf, cell, side) is False:
			print("  - Retrieve: FAIL - command rejected")
			result["steps"]["retrieve"] = False
			return result

		retrieve_result = self.wait_for_completion("retrieve_item", None)
		if retrieve_result["success"] is False:
			error = retrieve_result.get("error", "unknown")
			print(f"  - Retrieve: FAIL - {error}")
			result["steps"]["retrieve"] = False
			return result
		print(f"  - Retrieve: OK ({retrieve_result['duration']:.1f}s)")
		result["steps"]["retrieve"] = True

		restore_cmd = self.send_store_command(upca, location)
		if restore_cmd.get("success") is False:
			error = restore_cmd.get("error", "unknown")
			print(f"  - Re-store: FAIL - {error}")
			result["steps"]["re_store"] = False
			return result

		restore_result = self.wait_for_completion("store_item", upca)
		if restore_result["success"] is False:
			error = restore_result.get("error", "unknown")
			print(f"  - Re-store: FAIL - {error}")
			result["steps"]["re_store"] = False
			return result
		print(f"  - Re-store: OK ({restore_result['duration']:.1f}s)")
		result["steps"]["re_store"] = True

		result["passed"] = True
		return result

	def parse_skip_patterns(self, skip_patterns: str) -> list:
		if skip_patterns is None:
			return []

		patterns = []

		for pattern in skip_patterns.split(","):
			pattern = pattern.strip()
			parts = pattern.split(":")

			if len(parts) == 2:
				patterns.append({
					"side": parts[0].upper(),
					"shelf": int(parts[1]),
					"cell": None,
				})
			elif len(parts) == 3:
				patterns.append({
					"side": parts[0].upper(),
					"shelf": int(parts[1]),
					"cell": int(parts[2]),
				})

		return patterns

	def should_skip_location(self, location: dict, skip_patterns: list) -> bool:
		for pattern in skip_patterns:
			if location.get("side") != pattern["side"]:
				continue

			if location.get("shelf") != pattern["shelf"]:
				continue

			if pattern["cell"] is None:
				return True

			if location.get("cell") == pattern["cell"]:
				return True

		return False

	def run(self, cell_filter: str = None, start_from: str = None, skip_patterns: str = None):
		print(f"Cycle Test - Device {self.device_id}")
		print("=" * 40)

		if self.dry_run is True:
			print("[DRY RUN MODE]")

		try:
			print("Starting SSH tunnel...")
			self.start_tunnel()

			print("Authenticating...")
			if self.authenticate() is False:
				print("Authentication failed")
				return 1

			if self.check_robot_ready() is False:
				print("Robot not ready - may be crashed or have pending errors")
				return 1

			print("Getting storage locations...")
			locations = self.get_storage_locations()

			if len(locations) == 0:
				print("No storage locations found")
				return 1

			if cell_filter is not None:
				parts = cell_filter.split(":")
				if len(parts) == 3:
					filter_side = parts[0].upper()
					filter_shelf = int(parts[1])
					filter_cell = int(parts[2])
					locations = [
						loc for loc in locations
						if loc.get("side") == filter_side and loc.get("shelf") == filter_shelf and loc.get("cell") == filter_cell
					]
				elif len(parts) == 2:
					filter_shelf = int(parts[0])
					filter_cell = int(parts[1])
					locations = [
						loc for loc in locations
						if loc.get("shelf") == filter_shelf and loc.get("cell") == filter_cell
					]

				if len(locations) == 0:
					print(f"No matching cells for filter: {cell_filter}")
					return 1

			if skip_patterns is not None:
				parsed_patterns = self.parse_skip_patterns(skip_patterns)
				before_count = len(locations)
				locations = [loc for loc in locations if self.should_skip_location(loc, parsed_patterns) is False]
				skipped_count = before_count - len(locations)

				if skipped_count > 0:
					print(f"  Skipped {skipped_count} cells matching skip patterns")

				if len(locations) == 0:
					print("No cells remaining after applying skip patterns")
					return 1

			if start_from is not None:
				parts = start_from.split(":")
				if len(parts) != 3:
					print(f"Invalid start format: {start_from} (expected SIDE:SHELF:CELL)")
					return 1

				start_side = parts[0].upper()
				start_shelf = int(parts[1])
				start_cell = int(parts[2])

				def is_at_or_after(loc: dict) -> bool:
					loc_side = loc.get("side", "")
					loc_shelf = loc.get("shelf", 0)
					loc_cell = loc.get("cell", 0)

					side_order = {"L": 0, "R": 1}
					start_order = side_order.get(start_side, 0)
					loc_order = side_order.get(loc_side, 0)

					if loc_order > start_order:
						return True
					if loc_order < start_order:
						return False
					if loc_shelf > start_shelf:
						return True
					if loc_shelf < start_shelf:
						return False
					return loc_cell >= start_cell

				start_idx = None
				for i, loc in enumerate(locations):
					if is_at_or_after(loc) is True:
						start_idx = i
						break

				if start_idx is None:
					print(f"No enabled cells at or after: {start_from}")
					return 1

				locations = locations[start_idx:]
				actual_start = locations[0]
				actual_start_str = f"{actual_start['side']}:{actual_start['shelf']}:{actual_start['cell']}"

				if actual_start_str != start_from:
					print(f"Starting from {actual_start_str} ({start_from} is disabled, {len(locations)} cells remaining)")
				else:
					print(f"Starting from {start_from} ({len(locations)} cells remaining)")

			if self.shuffle is True:
				random.shuffle(locations)
				print("Shuffle mode enabled - testing cells in random order")

			total = len(locations)
			print(f"Found {total} cells to test")

			for i, location in enumerate(locations, 1):
				result = self.test_cell(location, i, total)
				self.results.append(result)

				if result["passed"] is False and self.step_mode is False:
					print(f"\nStopping due to failure at cell {result['cell']}")
					break

				if self.step_mode is True and i < total:
					try:
						input("\nPress Enter to continue to next cell (Ctrl+C to abort)...")
					except KeyboardInterrupt:
						print("\nAborted by user")
						break

			self.print_summary()
			return 0 if all(r["passed"] for r in self.results) else 1
		finally:
			self.cleanup()

	def print_summary(self):
		print("\n" + "=" * 40)
		print("Summary")
		print("=" * 40)

		passed = sum(1 for r in self.results if r["passed"] is True)
		failed = sum(1 for r in self.results if r["passed"] is False)
		total = len(self.results)

		print(f"{passed}/{total} passed, {failed} failed")

		if failed > 0:
			failed_cells = [r["cell"] for r in self.results if r["passed"] is False]
			print(f"Failed cells: {', '.join(failed_cells)}")


def signal_handler(sig, frame):
	print("\nInterrupted, cleaning up...")
	sys.exit(1)


def main():
	parser = argparse.ArgumentParser(description="Store/retrieve cycle test for opshelf devices")
	parser.add_argument("device_id", help="Device ID (8 character hex string)")
	parser.add_argument("--cell", help="Test specific cell (format: SIDE:SHELF:CELL or SHELF:CELL)")
	parser.add_argument("--start", help="Start from cell (format: SIDE:SHELF:CELL), tests all cells from this point")
	parser.add_argument("--dry-run", action="store_true", help="Run without executing commands")
	parser.add_argument("--step", action="store_true", help="Pause after each cell test")
	parser.add_argument("--store-first", action="store_true", help="Run store->retrieve cycle instead of retrieve->store")
	parser.add_argument("--shuffle", action="store_true", help="Test cells in random order")
	parser.add_argument("--skip", help="Skip cells or shelves (format: SIDE:SHELF or SIDE:SHELF:CELL, comma-separated)")
	parser.add_argument("--remote-script", help="Path to the provision/remote helper used for SSH port forwarding")
	parser.add_argument("--storage-config", help="Optional local storage.json fallback path when the device does not provide calibration storage")

	args = parser.parse_args()

	signal.signal(signal.SIGINT, signal_handler)

	test = CycleTest(
		args.device_id,
		dry_run=args.dry_run,
		step_mode=args.step,
		store_first=args.store_first,
		shuffle=args.shuffle,
		remote_script=args.remote_script,
		storage_config=args.storage_config,
	)
	sys.exit(test.run(cell_filter=args.cell, start_from=args.start, skip_patterns=args.skip))


if __name__ == "__main__":
	main()
