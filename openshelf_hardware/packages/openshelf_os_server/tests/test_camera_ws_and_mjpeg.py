from __future__ import annotations

import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os_server.core.camera import (
	ToolCameraStreamManager,
	camera_registry,
	_decode_nalu_frames,
)

FAKE_AVC_C = (
	b"\x01\x42\xe0\x1e\xe0"
	b"\x00\x04"
	b"\x67\x42\x00\x1e"
	b"\x00\x02"
	b"\x68\x1e"
)
FAKE_AU = b"\x00\x00\x00\x01\x65" + b"access-unit"


def _make_ws_token() -> str:
	from openshelf_os_server.settings import get_settings
	from openshelf_os_server.core.auth import AuthService

	settings = get_settings()
	auth = AuthService(
		settings.auth.jwt_secret, settings.auth.jwt_algorithm
	)
	return auth.create_access_token(
		user_id=1,
		email="admin@opshelf.com",
		role="admin",
		permissions=["users:read", "users:write", "robot:control"],
		access_level=None,
		expires_minutes=60,
	)


def _ws_manager() -> ToolCameraStreamManager:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.is_running = True
	manager.consumer_count = 0
	manager.nalu_consumer_count = 0
	manager.background_consumer_count = 0
	manager.increment_nalu_consumers = MagicMock(return_value=1)
	manager.decrement_nalu_consumers = MagicMock(return_value=0)
	manager.stale_frame_timeout = 10.0
	manager.first_frame_timeout = 5.0
	manager.nalu_last_frame_at = time.monotonic()
	manager.nalu_extradata = FAKE_AVC_C
	manager.nalu_meta = (704, 576, 25.0)

	meta = manager.nalu_codec_meta()
	assert meta is not None
	manager.nalu_codec_meta = MagicMock(return_value=meta)
	return manager


def _encode_nalu_header_and_frames(units: list[bytes]) -> bytes:
	from openshelf_os_server.core.camera import _encode_nalu_frames

	return _encode_nalu_frames(units)


def test_camera_ws_streams_header_then_binary_access_units() -> None:
	from starlette.testclient import TestClient

	from openshelf_os_server.main import app

	manager = _ws_manager()
	subscriber: asyncio.Queue = asyncio.Queue(maxsize=16)
	subscriber.put_nowait(_encode_nalu_header_and_frames([FAKE_AU]))
	subscriber.put_nowait(None)
	manager.add_nalu_subscriber = MagicMock(return_value=subscriber)

	client = TestClient(app, raise_server_exceptions=False)
	camera_registry["tool"] = manager

	try:
		token = _make_ws_token()

		with client.websocket_connect(
			f"/api/camera/ws/tool?token={token}"
		) as websocket:
			header = websocket.receive_json(mode="text")
			assert header["type"] == "header"
			assert header["codec"] == "avc1.42E01E"
			assert header["width"] == 704
			assert header["description"] == base64.b64encode(FAKE_AVC_C).decode()

			payload = websocket.receive_bytes()
			assert _decode_nalu_frames(payload) == [FAKE_AU]
	finally:
		camera_registry.pop("tool", None)


def test_camera_ws_rejects_unknown_camera() -> None:
	from starlette.testclient import TestClient

	from openshelf_os_server.main import app

	client = TestClient(app, raise_server_exceptions=False)
	with pytest.raises(Exception):
		with client.websocket_connect(
			"/api/camera/ws/missing?token=unauthenticated"
		) as websocket:
			websocket.receive_text()


def test_camera_ws_closes_stale_camera_with_4503() -> None:
	from starlette.testclient import TestClient
	from starlette.websockets import WebSocketDisconnect

	from openshelf_os_server.main import app

	manager = _ws_manager()
	# nalu_last_frame_at is frozen at its (fresh) header time and no AUs
	# ever arrive, so the view goes stale while the subscriber waits. The
	# stream must end on its own instead of parking a live NALU consumer.
	manager.stale_frame_timeout = 1.0
	subscriber: asyncio.Queue = asyncio.Queue(maxsize=16)
	manager.add_nalu_subscriber = MagicMock(return_value=subscriber)

	client = TestClient(app, raise_server_exceptions=False)
	camera_registry["tool"] = manager

	try:
		token = _make_ws_token()

		with pytest.raises(WebSocketDisconnect) as excinfo:
			with client.websocket_connect(
				f"/api/camera/ws/tool?token={token}"
			) as websocket:
				header = websocket.receive_json(mode="text")
				assert header["type"] == "header"
				websocket.receive_bytes()
	finally:
		camera_registry.pop("tool", None)

	assert excinfo.value.code == 4503


def test_camera_ws_closes_4503_when_start_raises() -> None:
	# start_nalu_stream_by_id only maps "not found" to ValueError; any other
	# start failure (e.g. an OSError from spawning the NALU worker) must
	# still close with 4503, not escape as an unhandled ASGI exception.
	from starlette.testclient import TestClient
	from starlette.websockets import WebSocketDisconnect

	from openshelf_os_server.main import app
	from openshelf_os_server.routers import camera as camera_router

	manager = _ws_manager()

	client = TestClient(app, raise_server_exceptions=False)
	camera_registry["tool"] = manager

	try:
		token = _make_ws_token()

		with (
			patch.object(
				camera_router,
				"start_nalu_stream_by_id",
				new_callable=AsyncMock,
				side_effect=OSError("spawn failed"),
			),
			pytest.raises(WebSocketDisconnect) as excinfo,
		):
			with client.websocket_connect(
				f"/api/camera/ws/tool?token={token}"
			) as websocket:
				websocket.receive_json(mode="text")
	finally:
		camera_registry.pop("tool", None)

	assert excinfo.value.code == 4503


def test_camera_ws_unmounts_on_client_close_while_delivering() -> None:
	# The client goes away while the camera is still delivering, and the
	# stale gate stays dormant (fresh nalu_last_frame_at, 10 s timeout,
	# so a 1 s tick can only end the stream via the disconnect). The
	# handler must end on the disconnect and release the consumer lease,
	# not park on the dead client. close() inside the body delivers the
	# disconnect without the __exit__ scope cancel, so a parked handler
	# keeps the lease held until teardown.
	manager = _ws_manager()
	subscriber: asyncio.Queue = asyncio.Queue(maxsize=16)
	subscriber.put_nowait(_encode_nalu_header_and_frames([FAKE_AU]))
	subscriber.put_nowait(_encode_nalu_header_and_frames([FAKE_AU]))
	manager.add_nalu_subscriber = MagicMock(return_value=subscriber)
	manager.stop = AsyncMock()

	from starlette.testclient import TestClient

	from openshelf_os_server.main import app

	client = TestClient(app, raise_server_exceptions=False)
	camera_registry["tool"] = manager

	try:
		token = _make_ws_token()

		with client.websocket_connect(
			f"/api/camera/ws/tool?token={token}"
		) as websocket:
			header = websocket.receive_json(mode="text")
			assert header["type"] == "header"
			# First AU: the stream is delivering before the client closes.
			websocket.receive_bytes()
			websocket.close()

			# The app is only cancelled in __exit__, so the unmount must
			# come from observing the close: the 1 s tick, plus margin.
			deadline = time.monotonic() + 2.0
			while manager.stop.await_count == 0 and time.monotonic() < deadline:
				time.sleep(0.02)

			manager.stop.assert_awaited_once()
	finally:
		camera_registry.pop("tool", None)


def _make_mjpeg_manager() -> SimpleNamespace:
	state = SimpleNamespace(latest_frame=None)
	manager = SimpleNamespace()
	manager.unregistered = asyncio.Event()
	manager.is_running = True
	manager.stale_frame_timeout = 5.0
	manager.target_fps = 20
	manager.is_frame_stale = lambda: False
	manager.mjpeg_path_stale = lambda: False
	manager._state = state

	async def get_latest_frame() -> bytes | None:
		frame = state.latest_frame
		# Fresh object per call: the real capture loop replaces latest_frame
		# with a new object for every frame, and the generator's identity
		# check treats that as "a new frame arrived" (bytes(bytes) is
		# identity-preserving, so force a copy through bytearray).
		return bytes(bytearray(frame)) if frame is not None else None

	manager.get_latest_frame = get_latest_frame
	return manager


def _solid_jpeg(color: int) -> bytes:
	import cv2
	import numpy as np

	image = np.full((48, 64, 3), color, dtype=np.uint8)
	_, encoded = cv2.imencode(".jpg", image)
	return encoded.tobytes()


def _make_stall_gate_manager(
	*,
	nalu_last_frame_at: float | None,
	last_frame_at: float | None,
) -> SimpleNamespace:
	state = SimpleNamespace(latest_frame=_solid_jpeg(100))
	manager = SimpleNamespace()
	manager.unregistered = asyncio.Event()
	manager.is_running = True
	manager.stale_frame_timeout = 10.0
	manager.target_fps = 20
	manager.nalu_last_frame_at = nalu_last_frame_at
	manager.last_frame_at = last_frame_at
	manager._state = state

	def is_frame_stale() -> bool:
		# ToolCameraStreamManager semantics (NALU-preferring): kept on the
		# fake so the test fails again if the generator stalls on this gate.
		if manager.nalu_last_frame_at is None:
			if manager.last_frame_at is None:
				return True

			return (time.monotonic() - manager.last_frame_at) > manager.stale_frame_timeout

		return (time.monotonic() - manager.nalu_last_frame_at) > manager.stale_frame_timeout

	def mjpeg_path_stale() -> bool:
		if manager.is_running is False or manager.last_frame_at is None:
			return True

		return (time.monotonic() - manager.last_frame_at) > manager.stale_frame_timeout

	manager.is_frame_stale = is_frame_stale
	manager.mjpeg_path_stale = mjpeg_path_stale

	async def get_latest_frame() -> bytes | None:
		frame = state.latest_frame
		# Fresh object per call, mirroring _make_mjpeg_manager's identity
		# check.
		return bytes(bytearray(frame)) if frame is not None else None

	manager.get_latest_frame = get_latest_frame
	return manager


@pytest.mark.anyio
async def test_mjpeg_generator_suppresses_unchanged_frames() -> None:
	from openshelf_os_server.routers.camera import (
		mjpeg_stream_generator_for_camera,
		_mjpeg_frame_thumbnail,
	)

	manager = _make_mjpeg_manager()
	manager._state.latest_frame = _solid_jpeg(100)
	request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
	generator = mjpeg_stream_generator_for_camera(manager, request)

	frames: list[bytes] = []

	async def drive() -> None:
		try:
			while len(frames) < 2:
				frames.append(await generator.__anext__())
		except StopAsyncIteration:
			pass
		manager.unregistered.set()

	driver = asyncio.create_task(drive())

	async def wait_until(predicate, timeout: float) -> bool:
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if predicate():
				return True
			await asyncio.sleep(0.01)
		return predicate()

	first_arrived = await wait_until(lambda: len(frames) >= 1, timeout=2.0)
	assert first_arrived, "expected the first frame"

	first = frames[0]

	# Identical frames must stay suppressed inside the heartbeat window.
	await asyncio.sleep(0.5)
	assert len(frames) == 1, f"unchanged frame was re-sent: {frames}"

	# A visibly different frame is delivered.
	manager._state.latest_frame = _solid_jpeg(220)
	delivered = await wait_until(lambda: len(frames) >= 2, timeout=2.0)
	assert delivered, "expected the changed frame"
	second = frames[1]
	assert second != first

	def _jpeg_only(part: bytes) -> bytes:
		return part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")

	assert _mjpeg_frame_thumbnail(_jpeg_only(first)) is not None
	assert _mjpeg_frame_thumbnail(_jpeg_only(second)) is not None

	await asyncio.wait_for(driver, timeout=5.0)


@pytest.mark.anyio
async def test_mjpeg_generator_reanchors_reference_after_scene_change() -> None:
	from openshelf_os_server.routers.camera import (
		mjpeg_stream_generator_for_camera,
	)

	manager = _make_mjpeg_manager()
	manager._state.latest_frame = _solid_jpeg(100)
	request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
	generator = mjpeg_stream_generator_for_camera(manager, request)

	frames: list[bytes] = []

	async def drive() -> None:
		try:
			while len(frames) < 3:
				frames.append(await generator.__anext__())
		except StopAsyncIteration:
			pass
		manager.unregistered.set()

	driver = asyncio.create_task(drive())

	async def wait_until(predicate, timeout: float) -> bool:
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if predicate():
				return True
			await asyncio.sleep(0.01)
		return predicate()

	# Scene A is established with the first send.
	assert await wait_until(lambda: len(frames) >= 1, timeout=2.0), "expected the first frame"

	# One-time scene change: the visibly different frame is delivered.
	manager._state.latest_frame = _solid_jpeg(220)
	assert await wait_until(lambda: len(frames) >= 2, timeout=2.0), "expected the changed frame"

	# The new static scene must be suppressed again inside the heartbeat
	# window. With the reference stuck on the first frame, every fresh copy
	# of the changed scene was re-sent at frame rate.
	await asyncio.sleep(0.5)
	assert len(frames) == 2, "new static scene was not suppressed"

	# The heartbeat still re-sends the unchanged scene after the window.
	assert await wait_until(lambda: len(frames) >= 3, timeout=3.0), "expected the heartbeat frame"
	assert frames[2] == frames[1]

	await asyncio.wait_for(driver, timeout=5.0)


def test_mjpeg_thumbnail_rejects_garbage() -> None:
	from openshelf_os_server.routers.camera import (
		_mjpeg_frame_thumbnail,
	)

	assert _mjpeg_frame_thumbnail(b"not a jpeg") is None


@pytest.mark.anyio
async def test_mjpeg_generator_stall_gate_tracks_mjpeg_path() -> None:
	from openshelf_os_server.routers.camera import (
		mjpeg_stream_generator_for_camera,
	)

	now = time.monotonic()
	stale = now - 60.0
	request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

	# Dead NALU pipe (delivered, then stalled), MJPEG path still fresh:
	# the MJPEG stream must keep delivering. The NALU-preferring gate
	# (is_frame_stale) reports True here and cut the stream with zero
	# frames, hiding the working fallback after the 4503 WS close.
	manager = _make_stall_gate_manager(
		nalu_last_frame_at=stale,
		last_frame_at=now,
	)
	generator = mjpeg_stream_generator_for_camera(manager, request)

	frames: list[bytes] = []

	async def drive() -> None:
		try:
			while len(frames) < 1:
				frames.append(await generator.__anext__())
		except StopAsyncIteration:
			pass
		manager.unregistered.set()

	driver = asyncio.create_task(drive())

	deadline = time.monotonic() + 2.0
	while len(frames) < 1 and time.monotonic() < deadline:
		await asyncio.sleep(0.01)
	assert len(frames) >= 1, "fresh MJPEG path was cut by a stale NALU pipe"
	await asyncio.wait_for(driver, timeout=5.0)

	# Fresh NALU pipe, MJPEG path stalled: the stream must break with
	# zero frames, not keep shipping frozen frames under a healthy NALU
	# timestamp.
	manager = _make_stall_gate_manager(
		nalu_last_frame_at=time.monotonic(),
		last_frame_at=stale,
	)
	generator = mjpeg_stream_generator_for_camera(manager, request)

	with pytest.raises(StopAsyncIteration):
		await asyncio.wait_for(generator.__anext__(), timeout=1.0)
