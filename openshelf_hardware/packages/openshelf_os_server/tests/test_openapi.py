"""Contract quality and transport declarations; no robot lifespan is started."""

from unittest.mock import AsyncMock

import pytest

from fastapi.routing import APIRoute

from openshelf_os_server.core.init_progress import RobotInitTracker
from openshelf_os_server.documentation import HealthResponse, SystemInfoResponse
from openshelf_os_server.main import app
from openshelf_os_server.openapi import generate_openapi_json
from openshelf_os_server.openapi_checks import validate_document
from openshelf_os_server.routers import robot
from openshelf_os_server.routers.storage import InventoryPage


def test_generated_document_matches_live_routes():
    document = generate_openapi_json()
    assert document == app.openapi()
    registered = {
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in route.methods
    }
    published = {(path, method) for path, item in document["paths"].items() for method in item}
    assert registered == published
    validate_document(document)


def test_authentication_documents_json_login_and_query_alternative():
    document = app.openapi()
    schemes = document["components"]["securitySchemes"]
    assert schemes["UserBearer"]["type"] == "http"
    assert schemes["UserBearer"]["scheme"] == "bearer"
    assert schemes["UserQueryToken"]["in"] == "query"
    paths = document["paths"]
    assert "security" not in paths["/api/health"]["get"]
    assert "security" not in paths["/api/auth/login/json"]["post"]
    assert paths["/api/robot/events"]["get"]["security"] == [
        {"UserBearer": []},
        {"UserQueryToken": []},
    ]
    assert "/api/auth/login" not in paths


def test_bin_compartment_response_contract():
    schemas = app.openapi()["components"]["schemas"]
    bin_schema = schemas["BinResponse"]
    for field, reference in [
        ("compartment_layout", "CompartmentLayoutSettings"),
        ("compartment_dimensions", "StorageBinDimensionsSettings"),
    ]:
        assert field not in bin_schema["required"]
        assert bin_schema["properties"][field]["anyOf"] == [
            {"$ref": f"#/components/schemas/{reference}"}, {"type": "null"},
        ]
    for field in ("rows", "columns"):
        layout_field = schemas["CompartmentLayoutSettings"]["properties"][field]
        assert layout_field["type"] == "integer"
        assert layout_field["minimum"] == 1
        assert layout_field["maximum"] == 32
    for field in ("width", "height", "length"):
        dimension = schemas["StorageBinDimensionsSettings"]["properties"][field]
        assert dimension["type"] == "number"
        assert dimension["exclusiveMinimum"] == 0
    for model in ("BinItemSummary", "BinContentsResponse", "BinWorkflowResponse"):
        schema = schemas[model]
        assert "compartment" not in schema["required"]
        field = schema["properties"]["compartment"]
        assert field["anyOf"] == [
            {"type": "integer", "minimum": 1, "maximum": 1024}, {"type": "null"},
        ]
        assert "starting at 1 at the top left" in field["description"]
        assert "left-to-right then top-to-bottom at IO" in field["description"]


def test_streaming_and_development_operations_are_explicit():
    paths = app.openapi()["paths"]
    for path, media in [
        ("/api/robot/events", "text/event-stream"),
        ("/api/robot/status", "text/event-stream"),
        ("/api/calibration/laser/stream", "text/event-stream"),
        ("/api/camera/{camera_id}", "multipart/x-mixed-replace; boundary=frame"),
        ("/api/iobox/sizer/frame.jpg", "image/jpeg"),
    ]:
        assert set(paths[path]["get"]["responses"]["200"]["content"]) == {media}
    assert "subscribe_only=true" in paths["/api/calibration/laser/stream"]["get"]["description"]
    assert (
        "development mode"
        in paths["/api/robot/debug/motors/{motor_name}/home"]["post"]["description"]
    )


@pytest.mark.asyncio
async def test_representative_responses_match_documented_models(client):
    for path, model in [
        ("/api/health", HealthResponse),
        ("/api/system/info", SystemInfoResponse),
        ("/api/storage/items", InventoryPage),
    ]:
        response = await client.get(path)
        assert response.status_code in {200, 503}
        model.model_validate(response.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
async def test_motor_response_matches_tracker_snapshot(monkeypatch, populated):
    tracker = RobotInitTracker()
    if populated:
        tracker.register_components(["io", "tool"])
        tracker.report("tool", "failed", detail="Connection failed", reason="Timed out")
    monkeypatch.setattr(robot, "get_init_tracker", lambda: tracker)
    monkeypatch.setattr(robot, "get_robot_worker", AsyncMock(side_effect=ValueError("Not ready")))
    monkeypatch.setattr(robot, "get_robot_instance", lambda: None)

    response = await robot.get_motors()

    assert response["init_components"] == tracker.snapshot()["components"]
    assert robot.MotorsResponse.model_validate(response, strict=True).model_dump() == response
