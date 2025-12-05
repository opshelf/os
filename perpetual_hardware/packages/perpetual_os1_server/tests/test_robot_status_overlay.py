import json

from perpetual_os1_server.routers.robot import overlay_db_occupancy


def test_overlay_db_occupancy_marks_occupied_cells() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "left_module": [
                    {"cells": [{"status": "available"}, {"status": "available"}]},
                    {"cells": [{"status": "available"}]},
                ],
                "right_module": [
                    {"cells": [{"status": "available"}, {"status": "available"}]},
                ],
            }
        }
    }

    occupied = {("left_module", 0, 1), ("right_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["left_module"][0]["cells"][0]["status"] == "available"
    assert storage["left_module"][0]["cells"][1]["status"] == "occupied"
    assert storage["left_module"][1]["cells"][0]["status"] == "available"
    assert storage["right_module"][0]["cells"][0]["status"] == "occupied"
    assert storage["right_module"][0]["cells"][1]["status"] == "available"


def test_overlay_db_occupancy_clears_stale_occupied() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "left_module": [
                    {"cells": [{"status": "occupied"}, {"status": "occupied"}]},
                ],
                "right_module": [],
            }
        }
    }

    occupied = {("left_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["left_module"][0]["cells"][0]["status"] == "occupied"
    assert storage["left_module"][0]["cells"][1]["status"] == "available"


def test_overlay_db_occupancy_preserves_maintenance_status() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "left_module": [
                    {"cells": [{"status": "maintenance"}, {"status": "disabled"}]},
                ],
                "right_module": [],
            }
        }
    }

    occupied: set[tuple[str, int, int]] = set()

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    storage = result["robot_status"]["storage"]
    assert storage["left_module"][0]["cells"][0]["status"] == "maintenance"
    assert storage["left_module"][0]["cells"][1]["status"] == "disabled"


def test_overlay_db_occupancy_empty_storage() -> None:
    payload = {
        "robot_status": {
            "storage": {
                "left_module": [],
                "right_module": [],
            }
        }
    }

    occupied = {("left_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    assert result["robot_status"]["storage"]["left_module"] == []
    assert result["robot_status"]["storage"]["right_module"] == []


def test_overlay_db_occupancy_missing_storage() -> None:
    payload = {"robot_status": {}}

    occupied = {("left_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    assert result["robot_status"] == {}


def test_overlay_db_occupancy_preserves_other_fields() -> None:
    payload = {
        "robot_status": {
            "gantry": {"x": 100, "y": 200},
            "storage": {
                "left_module": [{"cells": [{"status": "available", "extra": "data"}]}],
                "right_module": [],
            },
        },
        "current_command": None,
        "queue_length": 0,
    }

    occupied = {("left_module", 0, 0)}

    result = json.loads(overlay_db_occupancy(json.dumps(payload), occupied))

    assert result["robot_status"]["gantry"] == {"x": 100, "y": 200}
    assert result["current_command"] is None
    assert result["queue_length"] == 0
    assert result["robot_status"]["storage"]["left_module"][0]["cells"][0]["extra"] == "data"
    assert result["robot_status"]["storage"]["left_module"][0]["cells"][0]["status"] == "occupied"
