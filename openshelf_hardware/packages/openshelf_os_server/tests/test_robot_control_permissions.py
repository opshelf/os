"""Routes that move hardware, save calibration, or restart the service need robot:control.

The default viewer role only has storage:read, so a viewer may read state and
store or retrieve through commands, but must not move the robot directly.
"""
from typing import Any, AsyncGenerator

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import delete, select

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.database.models import AuditLog, Item, Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.main import app
from openshelf_os_server.settings import get_settings

VIEWER_EMAIL = "permissions-viewer@example.com"
CONTROLLER_EMAIL = "permissions-controller@example.com"
CONTROLLER_ROLE = "permissions-robot-control"
KNOWN_BARCODE = "permissions-known-item"

# (method, path, json body) for every gated route family.
ROBOT_CONTROL_ROUTES: list[tuple[str, str, Any]] = [
    ("POST", "/api/calibration/move", {}),
    ("POST", "/api/calibration/jog", {}),
    ("POST", "/api/calibration/save", {}),
    ("POST", "/api/calibration/abort", None),
    ("POST", "/api/calibration/laser/abort", None),
    ("GET", "/api/calibration/laser/stream", None),
    ("POST", "/api/robot/gripper?state=grip", None),
    ("POST", "/api/robot/safety/acknowledge", None),
    ("POST", "/api/robot/debug/motors/x_axis/move", {}),
    ("POST", "/api/robot/debug/motors/x_axis/home", None),
    ("POST", "/api/robot/debug/motors/x_axis/crash", None),
    ("POST", "/api/robot/debug/motors/x_axis/clear", None),
    ("POST", "/api/robot/debug/motors/probe", None),
    ("POST", "/api/robot/debug/power/motor/cycle", None),
    ("POST", "/api/robot/debug/door", {}),
    ("POST", "/api/robot/debug/scale/calibration/params", {}),
    ("POST", "/api/robot/debug/scale/calibration/zero", None),
    ("POST", "/api/robot/debug/scale/calibration/weight", {}),
    ("POST", "/api/robot/debug/scale/calibration/tare", None),
    ("POST", "/api/robot/commands", {"command": "home"}),
    ("POST", "/api/robot/commands", {"command": "move", "coordinate": {"x": 0, "y": 0, "z": 0}}),
    ("POST", "/api/demo/cycle/start", None),
    ("POST", "/api/demo/cycle/stop", None),
    ("POST", "/api/system/restart", None),
]


def _token(user: User, role: Role) -> str:
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
    return auth.create_access_token(
        user_id=user.id,
        email=user.email,
        role=role.name,
        permissions=role.permissions,
        access_level=None,
        expires_minutes=60,
    )


async def _cleanup() -> None:
    async with AsyncSessionLocal() as db:
        user_ids = (
            await db.execute(select(User.id).where(User.email.in_([VIEWER_EMAIL, CONTROLLER_EMAIL])))
        ).scalars().all()
        if user_ids:
            await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.execute(delete(Role).where(Role.name == CONTROLLER_ROLE))
        await db.execute(delete(Item).where(Item.barcode == KNOWN_BARCODE))
        await db.commit()


@pytest.fixture
async def tokens(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[dict[str, str], None]:
    await _cleanup()
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
    async with AsyncSessionLocal() as db:
        viewer_role = (await db.execute(select(Role).where(Role.name == "viewer"))).scalar_one()
        controller_role = Role(name=CONTROLLER_ROLE, permissions=["robot:control", "storage:read"])
        db.add(controller_role)
        await db.flush()
        viewer = User(
            email=VIEWER_EMAIL, first_name="View", last_name="Only",
            pin_hash=auth.hash_pin("2468"), role_id=viewer_role.id, is_active=True,
        )
        controller = User(
            email=CONTROLLER_EMAIL, first_name="Robot", last_name="Control",
            pin_hash=auth.hash_pin("1357"), role_id=controller_role.id, is_active=True,
        )
        db.add_all([viewer, controller])
        db.add(Item(barcode=KNOWN_BARCODE, name="Known", dimensions={"width": 10, "height": 10, "length": 10}))
        await db.commit()
        yield {
            "viewer": _token(viewer, viewer_role),
            "controller": _token(controller, controller_role),
        }
    await _cleanup()


async def _request(token: str, method: str, path: str, body: Any) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        if method == "GET":
            return await client.get(path)
        return await client.request(method, path, json=body)


@pytest.mark.anyio
@pytest.mark.parametrize(("method", "path", "body"), ROBOT_CONTROL_ROUTES)
async def test_viewer_cannot_drive_the_robot(
    tokens: dict[str, str], method: str, path: str, body: Any
) -> None:
    response = await _request(tokens["viewer"], method, path, body)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["message"] == "Missing required permission: robot:control."


@pytest.mark.anyio
async def test_store_of_a_new_item_needs_storage_write(tokens: dict[str, str]) -> None:
    body = {
        "command": "store_item",
        "item": {"barcode": "permissions-new-item", "dimensions": {"width": 1, "height": 1, "length": 1}},
    }

    response = await _request(tokens["controller"], "POST", "/api/robot/commands", body)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["message"] == "Missing required permission: storage:write."
