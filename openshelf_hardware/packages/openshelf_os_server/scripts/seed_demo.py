#!/usr/bin/env python3
import argparse
import asyncio
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

sys.path.append(str(Path(__file__).parent.parent / "src"))

from sqlalchemy import delete

from openshelf_os.common import CONFIG_PATH, load_config_file, resolve_grid_path
from openshelf_os.runtime_state import RuntimeStatePaths, ensure_mode_workspace
from openshelf_os.storage import CellItem, CellStatus, Dimensions, StorageGrid, StorageModuleSide
from openshelf_os_server.database.base import Base, engine
from openshelf_os_server.database.models import Item, ItemLocation
from openshelf_os_server.database.session import AsyncSessionLocal


RUNTIME_PATHS = RuntimeStatePaths.current()
DEFAULT_DEMO_PATH = RUNTIME_PATHS.data_dir / "demo.json"
DEFAULT_STORAGE_PATH = RUNTIME_PATHS.resolve_mode_owned_path("storage.json")


@dataclass(frozen=True)
class PlaceholderTemplate:
	name: str
	chemical_name: str
	cas_number: str
	concentration: str
	quantity: str
	hazard_class: str
	storage_conditions: str
	weight_min: float
	weight_max: float
	expires_in_days_min: int
	expires_in_days_max: int
	fill_ratio_min: float
	fill_ratio_max: float


@dataclass(frozen=True)
class PlaceholderProfile:
	name: str
	cas_number: str
	hazard_class: str
	storage_conditions: str
	weight_min: float
	weight_max: float
	expires_in_days_min: int
	expires_in_days_max: int
	concentration_options: tuple[str, ...]
	quantity_options: tuple[str, ...]


@dataclass(frozen=True)
class SeedLocation:
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int


@dataclass(frozen=True)
class SeedRecord:
	barcode: str
	name: str
	weight: float
	dimensions: Dict[str, float]
	metadata: Dict[str, Any]
	location: SeedLocation


PLACEHOLDER_TEMPLATES: tuple[PlaceholderTemplate, ...] = tuple()


MAX_PLACEHOLDER_NAME_DUPLICATES = 5


PLACEHOLDER_PROFILES: tuple[PlaceholderProfile, ...] = (
	PlaceholderProfile("Hydrochloric Acid", "7647-01-0", "Corrosive", "Acid Cabinet", 540.0, 640.0, 240, 780, ("0.5M", "1M"), ("500mL", "1L")),
	PlaceholderProfile("Sodium Hydroxide", "1310-73-2", "Corrosive", "Base Cabinet", 1080.0, 1300.0, 300, 900, ("0.5M", "1M"), ("500mL", "1L")),
	PlaceholderProfile("Sulfuric Acid", "7664-93-9", "Corrosive", "Acid Cabinet", 420.0, 520.0, 180, 720, ("9M", "18M"), ("250mL", "500mL")),
	PlaceholderProfile("Ethanol", "64-17-5", "Flammable", "Flammable Cabinet", 780.0, 940.0, 360, 1080, ("70%", "99.5%"), ("500mL", "1L")),
	PlaceholderProfile("Acetone", "67-64-1", "Flammable", "Flammable Cabinet", 380.0, 470.0, 300, 950, ("95%", "99.9%"), ("500mL", "1L")),
	PlaceholderProfile("Methanol", "67-56-1", "Toxic, Flammable", "Flammable Cabinet", 900.0, 3500.0, 180, 700, ("95%", "99.8%"), ("1L", "4L")),
	PlaceholderProfile("Hydrogen Peroxide", "7722-84-1", "Oxidizer", "Refrigerator", 520.0, 640.0, 120, 540, ("3%", "30%"), ("500mL", "1L")),
	PlaceholderProfile("Ammonium Hydroxide", "1336-21-6", "Corrosive", "Fume Hood", 850.0, 980.0, 220, 760, ("10%", "28%"), ("500mL", "1L")),
	PlaceholderProfile("Nitric Acid", "7697-37-2", "Corrosive, Oxidizer", "Acid Cabinet", 410.0, 520.0, 120, 660, ("35%", "70%"), ("250mL", "500mL")),
	PlaceholderProfile("Formaldehyde", "50-00-0", "Toxic", "Fume Hood", 540.0, 700.0, 130, 640, ("10%", "37%"), ("250mL", "500mL")),
	PlaceholderProfile("Isopropanol", "67-63-0", "Flammable", "Flammable Cabinet", 760.0, 980.0, 320, 980, ("70%", "99%"), ("500mL", "1L")),
	PlaceholderProfile("Acetic Acid", "64-19-7", "Corrosive, Flammable", "Acid Cabinet", 520.0, 620.0, 260, 900, ("10%", "99.7%"), ("500mL", "1L")),
	PlaceholderProfile("Sodium Chloride", "7647-14-5", "Non-hazardous", "Room Temperature", 450.0, 650.0, 500, 1400, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Potassium Permanganate", "7722-64-7", "Oxidizer", "Cool, Dry Place", 90.0, 160.0, 500, 1200, ("ACS", "Lab Grade"), ("100g", "250g")),
	PlaceholderProfile("Copper(II) Sulfate", "7758-99-8", "Toxic", "Room Temperature", 220.0, 320.0, 420, 1080, ("ACS", "Lab Grade"), ("250g", "500g")),
	PlaceholderProfile("Silver Nitrate", "7761-88-8", "Oxidizer, Toxic", "Dark Bottle", 60.0, 100.0, 180, 720, ("ACS", "99%"), ("25g", "100g")),
	PlaceholderProfile("Calcium Chloride", "10043-52-4", "Irritant", "Dry Place", 500.0, 650.0, 500, 1400, ("Anhydrous", "Dihydrate"), ("500g", "1kg")),
	PlaceholderProfile("Magnesium Sulfate", "7487-88-9", "Irritant", "Dry Place", 350.0, 520.0, 480, 1320, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Potassium Chloride", "7447-40-7", "Non-hazardous", "Room Temperature", 480.0, 700.0, 520, 1380, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Sodium Phosphate", "7558-79-4", "Irritant", "Room Temperature", 420.0, 650.0, 520, 1360, ("Monobasic", "Dibasic"), ("500g", "1kg")),
	PlaceholderProfile("Tris Buffer", "77-86-1", "Irritant", "Room Temperature", 220.0, 420.0, 480, 1240, ("0.1M", "1M"), ("500g", "1kg")),
	PlaceholderProfile("EDTA Disodium Salt", "6381-92-6", "Irritant", "Room Temperature", 320.0, 500.0, 500, 1320, ("ACS", "Molecular Biology"), ("500g", "1kg")),
	PlaceholderProfile("Citric Acid", "77-92-9", "Irritant", "Dry Place", 400.0, 620.0, 520, 1400, ("Anhydrous", "Monohydrate"), ("500g", "1kg")),
	PlaceholderProfile("Sodium Bicarbonate", "144-55-8", "Non-hazardous", "Dry Place", 420.0, 650.0, 520, 1400, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Ammonium Chloride", "12125-02-9", "Irritant", "Room Temperature", 420.0, 640.0, 500, 1320, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Potassium Iodide", "7681-11-0", "Irritant", "Dark Bottle", 220.0, 420.0, 420, 1200, ("ACS", "99%"), ("250g", "500g")),
	PlaceholderProfile("Lithium Chloride", "7447-41-8", "Irritant", "Dry Place", 180.0, 320.0, 420, 1200, ("ACS", "99%"), ("100g", "250g")),
	PlaceholderProfile("Urea", "57-13-6", "Low Hazard", "Room Temperature", 420.0, 700.0, 520, 1400, ("ACS", "Molecular Biology"), ("500g", "1kg")),
	PlaceholderProfile("Glycine", "56-40-6", "Low Hazard", "Room Temperature", 220.0, 480.0, 520, 1400, ("ACS", "Cell Culture"), ("500g", "1kg")),
	PlaceholderProfile("Trisodium Citrate", "6132-04-3", "Low Hazard", "Room Temperature", 320.0, 580.0, 500, 1320, ("Dihydrate", "Tribasic"), ("500g", "1kg")),
	PlaceholderProfile("Sodium Acetate", "127-09-3", "Low Hazard", "Room Temperature", 320.0, 580.0, 500, 1320, ("Anhydrous", "Trihydrate"), ("500g", "1kg")),
	PlaceholderProfile("Chloroform", "67-66-3", "Toxic", "Flammable Cabinet", 1350.0, 1650.0, 200, 640, ("Stabilized", "HPLC"), ("500mL", "1L")),
	PlaceholderProfile("Hexane", "110-54-3", "Flammable", "Flammable Cabinet", 560.0, 760.0, 260, 900, ("95%", "99%"), ("500mL", "1L")),
	PlaceholderProfile("Toluene", "108-88-3", "Flammable", "Flammable Cabinet", 700.0, 960.0, 260, 900, ("Reagent", "HPLC"), ("500mL", "1L")),
	PlaceholderProfile("Dimethyl Sulfoxide", "67-68-5", "Irritant", "Room Temperature", 520.0, 720.0, 320, 980, ("99%", "Molecular Biology"), ("500mL", "1L")),
	PlaceholderProfile("Acetonitrile", "75-05-8", "Flammable", "Flammable Cabinet", 620.0, 820.0, 260, 900, ("HPLC", "LC-MS"), ("500mL", "1L")),
	PlaceholderProfile("Sodium Hypochlorite", "7681-52-9", "Corrosive, Oxidizer", "Cool, Dark Place", 920.0, 1280.0, 60, 240, ("5%", "12%"), ("1L", "4L")),
	PlaceholderProfile("Boric Acid", "10043-35-3", "Irritant", "Room Temperature", 420.0, 700.0, 500, 1380, ("ACS", "USP"), ("500g", "1kg")),
	PlaceholderProfile("Phosphoric Acid", "7664-38-2", "Corrosive", "Acid Cabinet", 700.0, 1040.0, 240, 860, ("10%", "85%"), ("500mL", "1L")),
	PlaceholderProfile("Sodium Carbonate", "497-19-8", "Irritant", "Dry Place", 420.0, 700.0, 520, 1400, ("Anhydrous", "Monohydrate"), ("500g", "1kg")),
)


def build_placeholder_templates() -> tuple[PlaceholderTemplate, ...]:
	templates: list[PlaceholderTemplate] = []
	for profile in PLACEHOLDER_PROFILES:
		option_count = max(len(profile.concentration_options), len(profile.quantity_options))
		for option_index in range(option_count):
			concentration = profile.concentration_options[option_index % len(profile.concentration_options)]
			quantity = profile.quantity_options[option_index % len(profile.quantity_options)]
			templates.append(
				PlaceholderTemplate(
					name=f"{profile.name} {concentration}",
					chemical_name=profile.name,
					cas_number=profile.cas_number,
					concentration=concentration,
					quantity=quantity,
					hazard_class=profile.hazard_class,
					storage_conditions=profile.storage_conditions,
					weight_min=profile.weight_min,
					weight_max=profile.weight_max,
					expires_in_days_min=profile.expires_in_days_min,
					expires_in_days_max=profile.expires_in_days_max,
					fill_ratio_min=0.32,
					fill_ratio_max=0.98,
				)
			)
	return tuple(templates)


PLACEHOLDER_TEMPLATES = build_placeholder_templates()


def resolve_default_paths() -> tuple[Path, Path]:
	if CONFIG_PATH.exists() is False:
		return DEFAULT_DEMO_PATH, DEFAULT_STORAGE_PATH

	try:
		config = load_config_file(CONFIG_PATH)
	except Exception:
		return DEFAULT_DEMO_PATH, DEFAULT_STORAGE_PATH

	demo_path = Path(config.demo.cell_map_path)
	storage_path = Path(resolve_grid_path(config))
	return demo_path, storage_path


def parse_demo_location(value: str) -> SeedLocation:
	parts = value.split(":")
	if len(parts) != 3:
		raise ValueError(f"invalid demo location format: {value!r}")

	side_key = parts[0].strip().upper()
	if side_key == "F" or side_key == "FRONT_MODULE":
		side = StorageModuleSide.FRONT
	elif side_key == "B" or side_key == "BACK_MODULE":
		side = StorageModuleSide.BACK
	else:
		raise ValueError(f"invalid demo location side: {value!r}")

	return SeedLocation(
		side=side,
		shelf_idx=int(parts[1]),
		cell_idx=int(parts[2]),
	)


def location_key(location: SeedLocation) -> tuple[str, int, int]:
	return (location.side.value, location.shelf_idx, location.cell_idx)


def format_cell_coordinate(location: SeedLocation) -> str:
	side = "F"
	if location.side == StorageModuleSide.BACK:
		side = "B"

	return f"{side}:{location.shelf_idx}:{location.cell_idx}"


def coerce_positive_float(value: Any) -> Optional[float]:
	if isinstance(value, (int, float)) is False:
		return None

	converted = float(value)
	if converted <= 0:
		return None
	return converted


def resolve_dimensions(data: Dict[str, Any], fallback: Dict[str, float]) -> Dict[str, float]:
	candidate = data.get("dimensions")
	if isinstance(candidate, dict) is False:
		return fallback

	width = coerce_positive_float(candidate.get("width"))
	height = coerce_positive_float(candidate.get("height"))
	length = coerce_positive_float(candidate.get("length"))
	if width is None or height is None or length is None:
		return fallback

	return {
		"width": width,
		"height": height,
		"length": length,
	}


def resolve_name(data: Dict[str, Any], fallback: str) -> str:
	name = data.get("name")
	if isinstance(name, str) is False:
		return fallback
	stripped = name.strip()
	if len(stripped) == 0:
		return fallback
	return stripped


def resolve_expiration(data: Dict[str, Any], fallback: str) -> str:
	for key in ("expires_at", "expiresAt", "expiryDate"):
		value = data.get(key)
		if isinstance(value, str) and len(value.strip()) > 0:
			return value.strip()

	return fallback


def calculate_check_digit(base_digits: str) -> str:
	if len(base_digits) != 11 or base_digits.isdigit() is False:
		raise ValueError(f"invalid base barcode digits: {base_digits!r}")

	odd_sum = 0
	even_sum = 0
	for index, char in enumerate(base_digits):
		digit = int(char)
		if index % 2 == 0:
			odd_sum += digit
		else:
			even_sum += digit

	total = odd_sum * 3 + even_sum
	check_digit = (10 - (total % 10)) % 10
	return str(check_digit)


def generate_placeholder_barcode(counter: int, used: set[str]) -> str:
	index = counter
	while True:
		base_digits = f"299{index:08d}"
		barcode = f"{base_digits}{calculate_check_digit(base_digits)}"
		if barcode not in used:
			used.add(barcode)
			return barcode
		index += 1


def make_name_key(value: str) -> str:
	return value.strip().casefold()


def choose_placeholder_template(
	seed: str,
	name_counts: Dict[str, int],
	reserved_name_keys: set[str],
) -> PlaceholderTemplate:
	candidate_indices = list(range(len(PLACEHOLDER_TEMPLATES)))
	random.Random(f"{seed}:template").shuffle(candidate_indices)

	for template_index in candidate_indices:
		template = PLACEHOLDER_TEMPLATES[template_index]
		name_key = make_name_key(template.name)
		if name_key in reserved_name_keys:
			continue

		count = name_counts.get(template.name, 0)
		if count < MAX_PLACEHOLDER_NAME_DUPLICATES:
			return template

	for template_index in candidate_indices:
		template = PLACEHOLDER_TEMPLATES[template_index]
		count = name_counts.get(template.name, 0)
		if count < MAX_PLACEHOLDER_NAME_DUPLICATES:
			return template

	non_reserved_templates = [
		template for template in PLACEHOLDER_TEMPLATES if make_name_key(template.name) not in reserved_name_keys
	]
	if len(non_reserved_templates) > 0:
		return min(
			non_reserved_templates,
			key=lambda template: (name_counts.get(template.name, 0), template.name),
		)

	return min(
		PLACEHOLDER_TEMPLATES,
		key=lambda template: (name_counts.get(template.name, 0), template.name),
	)


def build_placeholder_data(seed: str, template: PlaceholderTemplate) -> Dict[str, Any]:
	rng = random.Random(seed)
	initial_weight = round(rng.uniform(template.weight_min, template.weight_max), 1)
	fill_ratio = rng.uniform(template.fill_ratio_min, template.fill_ratio_max)
	weight = round(initial_weight * fill_ratio, 1)
	if weight <= 0:
		weight = round(initial_weight * template.fill_ratio_min, 1)
	if weight > initial_weight:
		weight = initial_weight

	expires_days = rng.randint(template.expires_in_days_min, template.expires_in_days_max)
	stored_days_ago = rng.randint(1, 120)

	now = datetime.now(timezone.utc)
	expires_at = (now + timedelta(days=expires_days)).date().isoformat()
	stored_at = (now - timedelta(days=stored_days_ago)).isoformat()

	return {
		"name": template.name,
		"weight": weight,
		"chemical_name": template.chemical_name,
		"chemicalName": template.chemical_name,
		"cas_number": template.cas_number,
		"casNumber": template.cas_number,
		"concentration": template.concentration,
		"quantity": template.quantity,
		"hazard_class": template.hazard_class,
		"hazardClass": template.hazard_class,
		"storage_conditions": template.storage_conditions,
		"storageConditions": template.storage_conditions,
		"expires_at": expires_at,
		"stored_at": stored_at,
		"initial_weight": initial_weight,
		"fill_ratio": round(weight / initial_weight, 4),
	}


def normalize_demo_payload(demo_path: Path) -> Dict[str, Dict[str, Any]]:
	raw = json.loads(demo_path.read_text(encoding="utf-8"))
	if isinstance(raw, dict) is False:
		raise ValueError("demo.json must contain a top-level object")

	legacy_mapping = raw.get("code_cell_mapping")
	if isinstance(legacy_mapping, dict):
		nudge_by_location: Dict[str, float] = {}
		nudge_raw = raw.get("nudge_override_mm", {})
		if isinstance(nudge_raw, dict):
			for location, nudge in nudge_raw.items():
				if isinstance(location, str) is False:
					continue
				if isinstance(nudge, (int, float)) is False:
					continue
				nudge_by_location[location] = float(nudge)

		converted: Dict[str, Dict[str, Any]] = {}
		for barcode, location in legacy_mapping.items():
			if isinstance(location, str) is False:
				raise ValueError(f"legacy demo entry for {barcode!r} must be a location string")

			entry: Dict[str, Any] = {
				"cell": location,
				"data": {},
			}

			nudge_value = nudge_by_location.get(location)
			if nudge_value is not None:
				entry["nudge_override_mm"] = nudge_value

			converted[str(barcode)] = entry

		return converted

	converted: Dict[str, Dict[str, Any]] = {}
	for barcode, payload in raw.items():
		if isinstance(payload, str):
			converted[str(barcode)] = {
				"cell": payload,
				"data": {},
			}
			continue

		if isinstance(payload, dict) is False:
			raise ValueError(f"demo entry for {barcode!r} must be an object")

		cell = payload.get("cell")
		if isinstance(cell, str) is False:
			raise ValueError(f"demo entry for {barcode!r} must include string field 'cell'")

		data = payload.get("data", {})
		if isinstance(data, dict) is False:
			raise ValueError(f"demo entry for {barcode!r} field 'data' must be an object")

		entry = {
			"cell": cell,
			"data": dict(data),
		}

		nudge_override = payload.get("nudge_override_mm")
		if isinstance(nudge_override, (int, float)):
			entry["nudge_override_mm"] = float(nudge_override)

		converted[str(barcode)] = entry

	return converted


def iter_grid_cells(grid: StorageGrid) -> Iterable[tuple[SeedLocation, Any]]:
	for shelf_idx, shelf in enumerate(grid.front_module):
		for cell_idx, cell in enumerate(shelf.cells):
			yield SeedLocation(StorageModuleSide.FRONT, shelf_idx, cell_idx), cell

	for shelf_idx, shelf in enumerate(grid.back_module):
		for cell_idx, cell in enumerate(shelf.cells):
			yield SeedLocation(StorageModuleSide.BACK, shelf_idx, cell_idx), cell


def build_seed_records(grid: StorageGrid, demo_entries: Dict[str, Dict[str, Any]]) -> tuple[list[SeedRecord], int]:
	all_cells: Dict[tuple[str, int, int], Any] = {}
	for location, cell in iter_grid_cells(grid):
		all_cells[location_key(location)] = cell

	demo_rank_by_barcode: Dict[str, int] = {}
	for demo_rank, demo_barcode in enumerate(demo_entries.keys()):
		demo_rank_by_barcode[demo_barcode] = demo_rank

	mapped_by_location: Dict[tuple[str, int, int], tuple[str, Dict[str, Any]]] = {}
	for barcode, entry in demo_entries.items():
		location = parse_demo_location(str(entry["cell"]))
		key = location_key(location)

		if key not in all_cells:
			raise ValueError(
				f"demo entry {barcode!r} points to missing cell {location.side.value}:{location.shelf_idx}:{location.cell_idx}"
			)

		if key in mapped_by_location:
			existing_barcode = mapped_by_location[key][0]
			raise ValueError(
				f"duplicate demo cell mapping for {location.side.value}:{location.shelf_idx}:{location.cell_idx}: "
				f"{existing_barcode!r} and {barcode!r}"
			)

		cell = all_cells[key]
		if cell.status in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			raise ValueError(
				f"demo entry {barcode!r} targets non-placeable cell {location.side.value}:{location.shelf_idx}:{location.cell_idx} "
				f"with status {cell.status.value!r}"
			)

		mapped_by_location[key] = (barcode, entry)

	demo_name_to_barcode: Dict[str, str] = {}
	for mapped_barcode, mapped_entry in mapped_by_location.values():
		mapped_data = mapped_entry.get("data", {})
		if isinstance(mapped_data, dict) is False:
			mapped_data = {}

		mapped_name = resolve_name(mapped_data, mapped_barcode)
		name_key = make_name_key(mapped_name)
		existing_barcode = demo_name_to_barcode.get(name_key)
		if existing_barcode is not None and existing_barcode != mapped_barcode:
			raise ValueError(
				f"demo items must have unique names: {mapped_barcode!r} and {existing_barcode!r} both use {mapped_name!r}"
			)

		demo_name_to_barcode[name_key] = mapped_barcode

	reserved_demo_name_keys = set(demo_name_to_barcode.keys())
	name_counts: Dict[str, int] = {}

	used_barcodes: set[str] = set(demo_entries.keys())
	placeholder_counter = 0
	placeholder_count = 0

	records: list[SeedRecord] = []
	ordered_cells = sorted(all_cells.items(), key=lambda item: (item[0][0], item[0][1], item[0][2]))
	for key, cell in ordered_cells:
		location = SeedLocation(StorageModuleSide(key[0]), key[1], key[2])

		if cell.status in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			cell.item = None
			continue

		mapped_entry = mapped_by_location.get(key)
		if mapped_entry is None:
			placeholder_seed = f"{location.side.value}:{location.shelf_idx}:{location.cell_idx}"
			template = choose_placeholder_template(placeholder_seed, name_counts, reserved_demo_name_keys)
			data = build_placeholder_data(placeholder_seed, template)
			data["is_demo_physical"] = False
			if "demo_rank" in data:
				del data["demo_rank"]
			barcode = generate_placeholder_barcode(placeholder_counter, used_barcodes)
			placeholder_counter += 1
			placeholder_count += 1
		else:
			barcode = mapped_entry[0]
			data = dict(mapped_entry[1].get("data", {}))
			data["is_demo_physical"] = True
			if isinstance(data.get("demo_rank"), int) is False:
				data["demo_rank"] = demo_rank_by_barcode.get(barcode)
			if barcode in used_barcodes:
				pass
			else:
				used_barcodes.add(barcode)

		cell_dimensions = {
			"width": float(cell.dimensions.width),
			"height": float(cell.dimensions.height),
			"length": float(cell.dimensions.length),
		}

		if location.shelf_idx == 12:
			data["name"] = f"Well plate {format_cell_coordinate(location)}"

		dimensions = resolve_dimensions(data, cell_dimensions)
		name = resolve_name(data, barcode)
		name_counts[name] = name_counts.get(name, 0) + 1
		weight = coerce_positive_float(data.get("weight"))
		if weight is None:
			weight = coerce_positive_float(data.get("initial_weight"))
		if weight is None:
			weight = coerce_positive_float(data.get("initialWeight"))
		if weight is None:
			weight = 100.0

		stored_at = data.get("stored_at")
		if isinstance(stored_at, str) is False or len(stored_at.strip()) == 0:
			stored_at = data.get("storedAt")
		if isinstance(stored_at, str) is False or len(stored_at.strip()) == 0:
			stored_at = datetime.now(timezone.utc).isoformat()

		expires_at = resolve_expiration(data, (datetime.now(timezone.utc) + timedelta(days=365)).date().isoformat())

		initial_weight = coerce_positive_float(data.get("initial_weight"))
		if initial_weight is None:
			initial_weight = coerce_positive_float(data.get("initialWeight"))
		if initial_weight is None:
			initial_weight = weight

		metadata: Dict[str, Any] = {}
		for meta_key, meta_value in data.items():
			if meta_key in {"weight", "dimensions", "storedAt", "expiresAt", "initialWeight"}:
				continue
			if meta_value is None:
				continue
			metadata[meta_key] = meta_value

		metadata["name"] = name
		metadata["weight"] = weight
		metadata["stored_at"] = stored_at
		metadata["expires_at"] = expires_at
		metadata["initial_weight"] = initial_weight

		records.append(
			SeedRecord(
				barcode=barcode,
				name=name,
				weight=weight,
				dimensions=dimensions,
				metadata=metadata,
				location=location,
			)
		)

		if cell.status not in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			cell.status = CellStatus.OCCUPIED

		cell.item = CellItem(
			barcode=barcode,
			weight=weight,
			dimensions=Dimensions(
				width=dimensions["width"],
				height=dimensions["height"],
				length=dimensions["length"],
			),
			data={
				"name": name,
				"storedAt": stored_at,
				"expiresAt": expires_at,
				"initialWeight": initial_weight,
				**metadata,
			},
		)

	return records, placeholder_count


async def seed_database(records: list[SeedRecord]) -> None:
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)

	async with AsyncSessionLocal() as session:
		await session.execute(delete(ItemLocation))
		await session.execute(delete(Item))

		item_by_barcode: Dict[str, Item] = {}
		for record in records:
			item = Item(
				barcode=record.barcode,
				name=record.name,
				dimensions=record.dimensions,
				weight=record.weight,
				item_metadata=record.metadata,
			)
			session.add(item)
			item_by_barcode[record.barcode] = item

		await session.flush()

		for record in records:
			item = item_by_barcode[record.barcode]
			location = ItemLocation(
				item_id=item.id,
				side=record.location.side.value,
				shelf_idx=record.location.shelf_idx,
				cell_idx=record.location.cell_idx,
			)
			session.add(location)

		await session.commit()


def parse_args() -> argparse.Namespace:
	default_demo_path, default_storage_path = resolve_default_paths()

	parser = argparse.ArgumentParser(description="Reset and seed demo inventory data")
	parser.add_argument(
		"--demo-json",
		type=Path,
		default=default_demo_path,
		help=f"Path to demo mapping file (default: {default_demo_path})",
	)
	parser.add_argument(
		"--storage-json",
		type=Path,
		default=default_storage_path,
		help=f"Path to storage grid file (default: {default_storage_path})",
	)
	parser.add_argument(
		"--skip-storage-json",
		action="store_true",
		help="Do not write occupied placeholder items into storage.json",
	)
	return parser.parse_args()


async def main() -> None:
	ensure_mode_workspace()
	args = parse_args()
	demo_path = args.demo_json
	storage_path = args.storage_json

	if demo_path.exists() is False:
		raise FileNotFoundError(f"demo file not found: {demo_path}")
	if storage_path.exists() is False:
		raise FileNotFoundError(f"storage grid file not found: {storage_path}")

	grid = StorageGrid.load_from_file(str(storage_path))
	demo_entries = normalize_demo_payload(demo_path)
	records, placeholder_count = build_seed_records(grid, demo_entries)

	await seed_database(records)

	if args.skip_storage_json is False:
		grid.save_to_file(str(storage_path))

	print(
		"demo seed complete: "
		f"items={len(records)} "
		f"mapped={len(demo_entries)} "
		f"placeholders={placeholder_count} "
		f"storage_json_updated={args.skip_storage_json is False}"
	)


if __name__ == "__main__":
	asyncio.run(main())
