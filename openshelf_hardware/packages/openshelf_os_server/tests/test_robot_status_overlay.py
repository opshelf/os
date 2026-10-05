import json

from openshelf_os_server.routers.robot import overlay_db_occupancy


def test_overlay_db_occupancy_marks_occupied_cells() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "front_module": [
                    {"cells": [{"status": "available"}, {"status": "available"}]},
                    {"cells": [{"status": "available"}]},
                ],
                "back_module": [
                    {"cells": [{"status": "available"}, {"status": "available"}]},
                ],
            }
        }
    }

    occupied = {("front_module", 0, 1), ("back_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["front_module"][0]["cells"][0]["status"] == "available"
    assert storage["front_module"][0]["cells"][1]["status"] == "occupied"
    assert storage["front_module"][1]["cells"][0]["status"] == "available"
    assert storage["back_module"][0]["cells"][0]["status"] == "occupied"
    assert storage["back_module"][0]["cells"][1]["status"] == "available"


def test_overlay_db_occupancy_clears_stale_occupied() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "front_module": [
                    {"cells": [{"status": "occupied"}, {"status": "occupied"}]},
                ],
                "back_module": [],
            }
        }
    }

    occupied = {("front_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["front_module"][0]["cells"][0]["status"] == "occupied"
    assert storage["front_module"][0]["cells"][1]["status"] == "available"


def test_overlay_db_occupancy_preserves_maintenance_status() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "front_module": [
                    {"cells": [{"status": "maintenance"}, {"status": "disabled"}]},
                ],
                "back_module": [],
            }
        }
    }

    occupied: set[tuple[str, int, int]] = set()

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["front_module"][0]["cells"][0]["status"] == "maintenance"
    assert storage["front_module"][0]["cells"][1]["status"] == "disabled"

