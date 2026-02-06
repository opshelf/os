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

from perpetual_os1.common import CONFIG_PATH, load_config_file, resolve_grid_path
from perpetual_os1.storage import CellItem, CellStatus, Dimensions, StorageGrid, StorageModuleSide
from perpetual_os1_server.database.base import Base, engine
from perpetual_os1_server.database.models import Item, ItemLocation
from perpetual_os1_server.database.session import AsyncSessionLocal


DEFAULT_DEMO_PATH = Path("data/demo.json")
DEFAULT_STORAGE_PATH = Path("storage.json")


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


@dataclass(frozen=True)
class SeedLocation:
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int


@dataclass(frozen=True)
class SeedRecord:
	upca: str
	name: str
	weight: float
	dimensions: Dict[str, float]
	metadata: Dict[str, Any]
	location: SeedLocation


PLACEHOLDER_TEMPLATES: tuple[PlaceholderTemplate, ...] = (
	PlaceholderTemplate(
		name="Hydrochloric Acid",
		chemical_name="Hydrochloric Acid",
		cas_number="7647-01-0",
		concentration="1M",
		quantity="500mL",
		hazard_class="Corrosive",
		storage_conditions="Room Temperature",
		weight_min=560.0,
		weight_max=620.0,
		expires_in_days_min=240,
		expires_in_days_max=780,
	),
	PlaceholderTemplate(
		name="Sodium Hydroxide",
		chemical_name="Sodium Hydroxide",
		cas_number="1310-73-2",
		concentration="1M",
		quantity="1L",
		hazard_class="Corrosive",
		storage_conditions="Room Temperature",
		weight_min=1150.0,
		weight_max=1260.0,
		expires_in_days_min=300,
		expires_in_days_max=900,
	),
	PlaceholderTemplate(
		name="Sulfuric Acid",
		chemical_name="Sulfuric Acid",
		cas_number="7664-93-9",
		concentration="18M",
		quantity="250mL",
		hazard_class="Corrosive",
		storage_conditions="Cool, Dry Place",
		weight_min=420.0,
		weight_max=520.0,
		expires_in_days_min=180,
		expires_in_days_max=720,
	),
	PlaceholderTemplate(
		name="Ethanol",
		chemical_name="Ethanol (Absolute)",
		cas_number="64-17-5",
		concentration="99.5%",
		quantity="1L",
		hazard_class="Flammable",
		storage_conditions="Flammable Cabinet",
		weight_min=810.0,
		weight_max=930.0,
		expires_in_days_min=360,
		expires_in_days_max=1080,
	),
	PlaceholderTemplate(
		name="Acetone",
		chemical_name="Acetone",
		cas_number="67-64-1",
		concentration="99.9%",
		quantity="500mL",
		hazard_class="Flammable",
		storage_conditions="Flammable Cabinet",
		weight_min=390.0,
		weight_max=470.0,
		expires_in_days_min=300,
		expires_in_days_max=950,
	),
	PlaceholderTemplate(
		name="Methanol",
		chemical_name="Methanol",
		cas_number="67-56-1",
		concentration="99.8%",
		quantity="4L",
		hazard_class="Toxic, Flammable",
		storage_conditions="Flammable Cabinet",
		weight_min=3000.0,
		weight_max=3500.0,
		expires_in_days_min=180,
		expires_in_days_max=700,
	),
	PlaceholderTemplate(
		name="Hydrogen Peroxide",
		chemical_name="Hydrogen Peroxide",
		cas_number="7722-84-1",
		concentration="30%",
		quantity="500mL",
		hazard_class="Oxidizer",
		storage_conditions="Refrigerator (4C)",
		weight_min=520.0,
		weight_max=620.0,
		expires_in_days_min=120,
		expires_in_days_max=540,
	),
	PlaceholderTemplate(
		name="Ammonium Hydroxide",
		chemical_name="Ammonium Hydroxide",
		cas_number="1336-21-6",
		concentration="28-30%",
		quantity="1L",
		hazard_class="Corrosive",
		storage_conditions="Fume Hood",
		weight_min=860.0,
		weight_max=980.0,
		expires_in_days_min=220,
		expires_in_days_max=760,
	),
	PlaceholderTemplate(
		name="Nitric Acid",
		chemical_name="Nitric Acid",
		cas_number="7697-37-2",
		concentration="70%",
		quantity="250mL",
		hazard_class="Corrosive, Oxidizer",
		storage_conditions="Acid Cabinet",
		weight_min=410.0,
		weight_max=520.0,
		expires_in_days_min=120,
		expires_in_days_max=660,
	),
	PlaceholderTemplate(
		name="Formaldehyde",
		chemical_name="Formaldehyde Solution",
		cas_number="50-00-0",
		concentration="37%",
		quantity="500mL",
		hazard_class="Toxic, Carcinogen",
		storage_conditions="Fume Hood",
		weight_min=570.0,
		weight_max=700.0,
		expires_in_days_min=130,
		expires_in_days_max=640,
	),
	PlaceholderTemplate(
		name="Potassium Permanganate",
		chemical_name="Potassium Permanganate",
		cas_number="7722-64-7",
		concentration="ACS Grade",
		quantity="100g",
		hazard_class="Oxidizer",
		storage_conditions="Cool, Dry Place",
		weight_min=95.0,
		weight_max=150.0,
		expires_in_days_min=500,
		expires_in_days_max=1200,
	),
	PlaceholderTemplate(
		name="Copper(II) Sulfate",
		chemical_name="Copper(II) Sulfate Pentahydrate",
		cas_number="7758-99-8",
		concentration="ACS Grade",
		quantity="250g",
		hazard_class="Toxic",
		storage_conditions="Room Temperature",
		weight_min=220.0,
		weight_max=320.0,
		expires_in_days_min=420,
		expires_in_days_max=1080,
	),
	PlaceholderTemplate(
		name="Silver Nitrate",
		chemical_name="Silver Nitrate",
		cas_number="7761-88-8",
		concentration="ACS Grade",
		quantity="25g",
		hazard_class="Oxidizer, Toxic",
		storage_conditions="Dark Bottle, Cool Place",
		weight_min=65.0,
		weight_max=95.0,
		expires_in_days_min=180,
		expires_in_days_max=720,
	),
)


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
	if side_key == "L" or side_key == "LEFT_MODULE":
		side = StorageModuleSide.LEFT
	elif side_key == "R" or side_key == "RIGHT_MODULE":
		side = StorageModuleSide.RIGHT
	else:
		raise ValueError(f"invalid demo location side: {value!r}")

	return SeedLocation(
		side=side,
		shelf_idx=int(parts[1]),
		cell_idx=int(parts[2]),
	)


def location_key(location: SeedLocation) -> tuple[str, int, int]:
	return (location.side.value, location.shelf_idx, location.cell_idx)


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


def calculate_upca_check_digit(base_digits: str) -> str:
	if len(base_digits) != 11 or base_digits.isdigit() is False:
		raise ValueError(f"invalid base upca digits: {base_digits!r}")

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


def generate_placeholder_upca(counter: int, used: set[str]) -> str:
	index = counter
	while True:
		base_digits = f"299{index:08d}"
		upca = f"{base_digits}{calculate_upca_check_digit(base_digits)}"
		if upca not in used:
			used.add(upca)
			return upca
		index += 1


def build_placeholder_data(seed: str, template: PlaceholderTemplate) -> Dict[str, Any]:
	rng = random.Random(seed)
	weight = round(rng.uniform(template.weight_min, template.weight_max), 1)
	expires_days = rng.randint(template.expires_in_days_min, template.expires_in_days_max)
	stored_days_ago = rng.randint(1, 120)

	now = datetime.now(timezone.utc)
	expires_at = (now + timedelta(days=expires_days)).date().isoformat()
	stored_at = (now - timedelta(days=stored_days_ago)).isoformat()

	return {
		"name": template.name,
		"weight": weight,
		"chemical_name": template.chemical_name,
		"cas_number": template.cas_number,
		"concentration": template.concentration,
		"quantity": template.quantity,
		"hazard_class": template.hazard_class,
		"storage_conditions": template.storage_conditions,
		"expires_at": expires_at,
		"stored_at": stored_at,
		"initial_weight": weight,
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
		for upca, location in legacy_mapping.items():
			if isinstance(location, str) is False:
				raise ValueError(f"legacy demo entry for {upca!r} must be a location string")

			entry: Dict[str, Any] = {
				"cell": location,
				"data": {},
			}

			nudge_value = nudge_by_location.get(location)
			if nudge_value is not None:
				entry["nudge_override_mm"] = nudge_value

			converted[str(upca)] = entry

		return converted

	converted: Dict[str, Dict[str, Any]] = {}
	for upca, payload in raw.items():
		if isinstance(payload, str):
			converted[str(upca)] = {
				"cell": payload,
				"data": {},
			}
			continue

		if isinstance(payload, dict) is False:
			raise ValueError(f"demo entry for {upca!r} must be an object")

		cell = payload.get("cell")
		if isinstance(cell, str) is False:
			raise ValueError(f"demo entry for {upca!r} must include string field 'cell'")

		data = payload.get("data", {})
		if isinstance(data, dict) is False:
			raise ValueError(f"demo entry for {upca!r} field 'data' must be an object")

		entry = {
			"cell": cell,
			"data": dict(data),
		}

		nudge_override = payload.get("nudge_override_mm")
		if isinstance(nudge_override, (int, float)):
			entry["nudge_override_mm"] = float(nudge_override)

		converted[str(upca)] = entry

	return converted


def iter_grid_cells(grid: StorageGrid) -> Iterable[tuple[SeedLocation, Any]]:
	for shelf_idx, shelf in enumerate(grid.left_module):
		for cell_idx, cell in enumerate(shelf.cells):
			yield SeedLocation(StorageModuleSide.LEFT, shelf_idx, cell_idx), cell

	for shelf_idx, shelf in enumerate(grid.right_module):
		for cell_idx, cell in enumerate(shelf.cells):
			yield SeedLocation(StorageModuleSide.RIGHT, shelf_idx, cell_idx), cell


def build_seed_records(grid: StorageGrid, demo_entries: Dict[str, Dict[str, Any]]) -> tuple[list[SeedRecord], int]:
	all_cells: Dict[tuple[str, int, int], Any] = {}
	for location, cell in iter_grid_cells(grid):
		all_cells[location_key(location)] = cell

	mapped_by_location: Dict[tuple[str, int, int], tuple[str, Dict[str, Any]]] = {}
	for upca, entry in demo_entries.items():
		location = parse_demo_location(str(entry["cell"]))
		key = location_key(location)

		if key not in all_cells:
			raise ValueError(
				f"demo entry {upca!r} points to missing cell {location.side.value}:{location.shelf_idx}:{location.cell_idx}"
			)

		if key in mapped_by_location:
			existing_upca = mapped_by_location[key][0]
			raise ValueError(
				f"duplicate demo cell mapping for {location.side.value}:{location.shelf_idx}:{location.cell_idx}: "
				f"{existing_upca!r} and {upca!r}"
			)

		cell = all_cells[key]
		if cell.status in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			raise ValueError(
				f"demo entry {upca!r} targets non-placeable cell {location.side.value}:{location.shelf_idx}:{location.cell_idx} "
				f"with status {cell.status.value!r}"
			)

		mapped_by_location[key] = (upca, entry)

	used_upcas: set[str] = set(demo_entries.keys())
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
			template = PLACEHOLDER_TEMPLATES[placeholder_counter % len(PLACEHOLDER_TEMPLATES)]
			placeholder_seed = f"{location.side.value}:{location.shelf_idx}:{location.cell_idx}"
			data = build_placeholder_data(placeholder_seed, template)
			upca = generate_placeholder_upca(placeholder_counter, used_upcas)
			placeholder_counter += 1
			placeholder_count += 1
		else:
			upca = mapped_entry[0]
			data = dict(mapped_entry[1].get("data", {}))
			if upca in used_upcas:
				pass
			else:
				used_upcas.add(upca)

		cell_dimensions = {
			"width": float(cell.dimensions.width),
			"height": float(cell.dimensions.height),
			"length": float(cell.dimensions.length),
		}

		dimensions = resolve_dimensions(data, cell_dimensions)
		name = resolve_name(data, upca)
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
			metadata[meta_key] = meta_value

		metadata["name"] = name
		metadata["weight"] = weight
		metadata["stored_at"] = stored_at
		metadata["expires_at"] = expires_at
		metadata["initial_weight"] = initial_weight

		records.append(
			SeedRecord(
				upca=upca,
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
			upca=upca,
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

		item_by_upca: Dict[str, Item] = {}
		for record in records:
			item = Item(
				upca=record.upca,
				name=record.name,
				dimensions=record.dimensions,
				weight=record.weight,
				item_metadata=record.metadata,
			)
			session.add(item)
			item_by_upca[record.upca] = item

		await session.flush()

		for record in records:
			item = item_by_upca[record.upca]
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
