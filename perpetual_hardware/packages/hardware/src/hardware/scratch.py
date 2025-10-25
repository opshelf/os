
# s1 = [(910.44, 21.048), (745.44, 21.048), (580.44, 21.048), (415.44000000000005, 21.048), (250.44000000000005, 21.048), (85.44000000000005, 21.048)]
# s2 = [(912.344, 372.04), (807.344, 372.04), (702.344, 372.04), (597.344, 372.04), (492.34400000000005, 372.04), (387.34400000000000005, 372.04), (282.34400000000005, 372.04)]
# s3 = [(913.008, 575.44), (808.008, 575.44), (703.008, 575.44), (598.008, 575.44), (493.00800000000004, 575.44), (388.00800000000004, 575.44), (283.00800000000004, 575.44)]
# s4 = [(921.272, 776.84), (831.272, 776.84), (741.272, 776.84), (651.272, 776.84), (561.272, 776.84), (471.27200000000005, 776.84), (381.27200000000005, 776.84), (291.27200000000005, 776.84)]
# s5 = [(921.68, 927.24), (831.68, 927.24), (741.68, 927.24), (651.68, 927.24), (561.68, 927.24), (471.67999999999995, 927.24), (381.67999999999995, 927.24), (291.67999999999995, 927.24)]

# # Simple linspace implementation (inclusive)
# def linspace(start, stop, num):
# 	if num == 1:
# 		return [start]
# 	step = (stop - start) / (num - 1)
# 	return [start + i * step for i in range(num)]

# sl1 = linspace(17, 21.5, 6)
# sl2 = linspace(10, 12.75, 7)
# sl3 = linspace(11.5, 14.25, 7)
# sl4 = linspace(10, 12.25, 8)
# sl5 = linspace(10, 13.5, 8)

# p1 = [(x + 4, y + dy) for (x, y), dy in zip(s1, sl1)]
# p2 = [(x + 4, y + dy) for (x, y), dy in zip(s2, sl2)]
# p3 = [(x + 4, y + dy) for (x, y), dy in zip(s3, sl3)]
# p4 = [(x + 4, y + dy) for (x, y), dy in zip(s4, sl4)]
# p5 = [(x + 4, y + dy) for (x, y), dy in zip(s5, sl5)]

# import json
# from dataclasses import asdict, dataclass


# @dataclass
# class CellDef:
# 	x: float
# 	y: float
# 	width: float
# 	height: float
# 	depth: float


# def generate_storage_schema(box_width: float = 50.0, box_height: float = 100.0, box_length: float = 400.0):
# 	"""Generate a storage grid schema from p1..p5.

# 	Strategy:
# 	- Treat each pN list as a shelf row, left-to-right order as given.
# 	- Divide into left/right modules by median x across all points.
# 	- For each shelf build a list of cells with identical box dimensions.
# 	"""
# 	rows = [p1, p2, p3, p4, p5]
# 	all_points = [pt for row in rows for pt in row]
# 	xs = sorted([p[0] for p in all_points])
# 	if not xs:
# 		raise ValueError("No points to build schema from")
# 	median_x = xs[len(xs) // 2]

# 	left_module = []
# 	right_module = []

# 	for row in rows:
# 		shelf_cells_left = []
# 		shelf_cells_right = []
# 		for (x, y) in row:
# 			cell = CellDef(x=x, y=y, width=box_width, height=box_height, depth=box_length)
# 			if x <= median_x:
# 				shelf_cells_left.append(cell)
# 			else:
# 				shelf_cells_right.append(cell)

# 		# Keep order from left->right by x
# 		shelf_cells_left.sort(key=lambda c: c.x)
# 		shelf_cells_right.sort(key=lambda c: c.x)

# 		# Convert to simple dicts
# 		left_module.append([asdict(c) for c in shelf_cells_left])
# 		right_module.append([asdict(c) for c in shelf_cells_right])

# 	schema = {
# 		"left_module": left_module,
# 		"right_module": right_module,
# 		"box_dimensions": {"width": box_width, "height": box_height, "length": box_length},
# 		"generated_from": "scratch.py"
# 	}

# 	out_path = "storage_schema.json"
# 	with open(out_path, "w+") as f:
# 		json.dump(schema, f, indent=2)

# 	# Print summary
# 	total_left = sum(len(s) for s in left_module)
# 	total_right = sum(len(s) for s in right_module)
# 	print(f"Wrote {out_path}: left_cells={total_left}, right_cells={total_right}")


# if __name__ == "__main__":
# 	generate_storage_schema()
# 	# After schema generation, also convert to StorageGrid-style JSON
# 	def convert_schema_to_storage(schema_path: str = "storage_schema.json", out_path: str = "storage.json"):
# 		"""Convert the simple schema to a StorageGrid-like storage.json and place all cells on the left side."""
# 		with open(schema_path, "r") as f:
# 			sch = json.load(f)

# 		box_dims = sch.get("box_dimensions", {"width": 50.0, "height": 100.0, "length": 400.0})

# 		left_module_shelves = []

# 		# Gather all shelves from both modules and place them into left_module
# 		all_shelves = []
# 		for shelf in sch.get("left_module", []):
# 			all_shelves.append(shelf)
# 		for shelf in sch.get("right_module", []):
# 			all_shelves.append(shelf)

# 		for shelf in all_shelves:
# 			# shelf is a list of cell dicts
# 			cells = []
# 			xs = [c["x"] for c in shelf] if shelf else []
# 			# approximate shelf_width from span or number of cells
# 			if xs:
# 				shelf_width = max(xs) - min(xs) + box_dims.get("width", 50.0)
# 			else:
# 				shelf_width = len(shelf) * box_dims.get("width", 50.0)

# 			# set shelf_height equal to box height plus a small margin
# 			shelf_height = box_dims.get("height", 100.0) + 10.0

# 			for c in shelf:
# 				cell = {
# 					"status": "EMPTY",
# 					"item": None,
# 					"calibration_point": {"x": float(c.get("x", 0.0)), "y": float(c.get("y", 0.0)), "z": 0.0},
# 					"dimensions": {"length": float(box_dims.get("length", 400.0)), "width": float(box_dims.get("width", 50.0)), "height": float(box_dims.get("height", 100.0))},
# 					"empty": True
# 				}
# 				cells.append(cell)

# 			left_module_shelves.append({
# 				"shelf_height": float(shelf_height),
# 				"shelf_width": float(shelf_width),
# 				"cells": cells,
# 				"divider_width": 8.0
# 			})

# 		storage = {
# 			"left_module": left_module_shelves,
# 			"right_module": [],
# 			"filename": "storage.json"
# 		}

# 		# ensure target directory exists
# 		out_file = "./storage.json"
# 		with open(out_file, "w+") as f:
# 			json.dump(storage, f, indent=2)

# 		total_cells = sum(len(s["cells"]) for s in storage["left_module"])
# 		print(f"Wrote storage.json with {len(storage['left_module'])} shelves and {total_cells} total cells to {out_file}")

# 	convert_schema_to_storage()

#!/usr/bin/env python3
import cv2
import sys

DEVICE_INDEX = 0          # /dev/video0 → index 0; change if needed
OUTPUT_PATH = "snapshot.jpg"

cam = cv2.VideoCapture(DEVICE_INDEX, cv2.CAP_V4L2)  # CAP_V4L2 helps on Linux
if not cam.isOpened():
    sys.exit(f"Unable to open camera {DEVICE_INDEX}")

# Optional: configure resolution/exposure/etc.
# cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
# cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

ret, frame = cam.read()
cam.release()

if not ret or frame is None:
    sys.exit("Failed to capture frame")

cv2.imwrite(OUTPUT_PATH, frame)
print(f"Saved frame to {OUTPUT_PATH}")