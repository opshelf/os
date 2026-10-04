from __future__ import annotations

import asyncio
import multiprocessing
import queue
import struct
import time
from unittest.mock import MagicMock

import pytest

from openshelf_os_server.core.camera import (
	ToolCameraStreamManager,
	_decode_nalu_frames,
	_encode_nalu_frames,
	_nalu_queue_put,
	_parse_avcc_meta,
	_split_annexb_nalus,
	_annexb_to_avcc_au,
	_au_contains_idr,
)

FAKE_AVC_C = (
	b"\x01\x42\xe0\x1e\xe0"  # version, baseline, compatibility, level, 1 SPS
	b"\x00\x04"
	b"\x67\x42\x00\x1e"  # SPS
	b"\x00\x02"
	b"\x68\x1e"  # PPS
)
FAKE_AU = b"\x00\x00\x00\x01\x65" + b"access-unit"
FAKE_DELTA_AU = b"\x00\x00\x00\x01\x41" + b"delta-slice"
FAKE_IDR_AU = b"\x00\x00\x00\x01\x65" + b"idr-slice"
# Annex-B SPS/PPS as PyAV/ffmpeg surface h264 extradata over RTSP
# (baseline profile, level 12), instead of an avcC record.
FAKE_ANNEX_B_SPS = (
	b"\x67"
	b"\x42\xc0\x0c"
	b"\xda\x05\x07\xec"
	b"\x04\x40\x00\x00\x00\x03"
	b"\x00\x04\x00\x00\x00\x00\x00"
	b"\x07\x83\xc5\x0a\xa8"
	b"\x00\x00"
)
FAKE_ANNEX_B_PPS = b"\x68\xce\x0f\xc8"
FAKE_ANNEX_B_EXTRADATA = (
	b"\x00\x00\x00\x01" + FAKE_ANNEX_B_SPS + b"\x00\x00\x00\x01" + FAKE_ANNEX_B_PPS
)


def test_nalu_frame_roundtrip_preserves_units() -> None:
	units = [b"one", FAKE_AU, b"\x00\x00\x00\x01"]

	decoded = _decode_nalu_frames(_encode_nalu_frames(list(units)))

	assert decoded == units


def test_nalu_frame_decode_rejects_malformed_messages() -> None:
	with pytest.raises(ValueError):
		_decode_nalu_frames(b"\x00\x00\x00\x05xy")

	with pytest.raises(ValueError):
		_decode_nalu_frames(b"\x00\x00\x00\x02ab\x00")


def test_avcc_meta_builds_webcodecs_codec_string() -> None:
	meta = _parse_avcc_meta(FAKE_AVC_C, width=704, height=576, fps=25.0)

	assert meta is not None
	assert meta.codec == "avc1.42E01E"
	assert meta.description == FAKE_AVC_C
	assert meta.width == 704
	assert meta.height == 576
	# AVCC AUs need no conversion at the WebCodecs fan-out.
	assert meta.au_is_avcc is True


def test_annexb_extradata_builds_webcodecs_meta() -> None:
	meta = _parse_avcc_meta(FAKE_ANNEX_B_EXTRADATA, width=704, height=576, fps=25.0)

	assert meta is not None
	assert meta.codec == "avc1.42C00C"
	assert meta.au_is_avcc is False
	assert meta.width == 704
	assert meta.height == 576
	assert meta.fps == 25.0

	avc_c = meta.description
	assert avc_c[:4] == b"\x01\x42\xc0\x0c"
	# 0b11111100|lengthSizeMinusOne and 0bE0|(numSPS-1) for one SPS.
	assert avc_c[4:6] == b"\xff\xe1"
	sps_len = struct.unpack_from(">H", avc_c, 6)[0]
	sps = avc_c[8:8 + sps_len]
	# The fake SPS ends in two zero bytes; they merge with the next
	# start code into a 5-zero run, which the splitter consumes whole.
	assert sps == FAKE_ANNEX_B_SPS[:-2]
	assert avc_c[8 + sps_len] == 1
	pps_len = struct.unpack_from(">H", avc_c, 9 + sps_len)[0]
	assert avc_c[11 + sps_len:11 + sps_len + pps_len] == FAKE_ANNEX_B_PPS
	assert len(avc_c) == 11 + sps_len + pps_len


def test_annexb_extradata_rejects_missing_parameter_sets() -> None:
	assert _parse_avcc_meta(b"\x00\x00\x00\x01" + FAKE_ANNEX_B_SPS, 10, 10, 0.0) is None
	assert _parse_avcc_meta(b"\x00\x42\xc0\x0c" + FAKE_ANNEX_B_SPS, 10, 10, 0.0) is None


def test_annexb_to_avcc_au_roundtrips_access_unit() -> None:
	converted = _annexb_to_avcc_au(FAKE_AU)

	expected_nalu = b"\x65" + b"access-unit"
	assert _decode_nalu_frames(converted) == [expected_nalu]
	assert converted == struct.pack(">I", len(expected_nalu)) + expected_nalu


def test_split_annexb_nalus_accepts_long_zero_run_start_codes() -> None:
	# A start code written with four zeros (00 0 0 0 1) must split
	# exactly like the standard spellings, with no spurious 1-byte NAL
	# and no zero glued onto the previous NAL.
	au = (
		b"\x00" * 4 + b"\x01" + b"\x67sps"
		+ b"\x00" * 4 + b"\x01" + b"\x65slice"
	)
	canonical = (
		b"\x00\x00\x00\x01" + b"\x67sps"
		+ b"\x00\x00\x00\x01" + b"\x65slice"
	)

	assert _split_annexb_nalus(au) == _split_annexb_nalus(canonical) == [
		b"\x67sps",
		b"\x65slice",
	]
	assert _decode_nalu_frames(_annexb_to_avcc_au(au)) == [
		b"\x67sps",
		b"\x65slice",
	]
	assert _split_annexb_nalus(b"\x00" * 4 + b"\x01" + b"\x65slice") == [b"\x65slice"]


def _nalu_manager() -> ToolCameraStreamManager:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_process.exitcode = None
	return manager


def test_drain_nalu_queue_freshness_tracks_view_aus_only() -> None:
	manager = _nalu_manager()
	manager.nalu_queue = multiprocessing.Queue(maxsize=16)
	now = time.monotonic()
	manager.nalu_queue.put(("view", "au", 0.0, now, FAKE_AU))

	deadline = time.monotonic() + 5.0
	while time.monotonic() < deadline and len(manager.nalu_view_ring) < 1:
		manager._drain_nalu_queue()

	assert manager.nalu_view_ring and manager.nalu_last_frame_at == now

	manager.nalu_queue.put(("record", "au", 1.0, now + 1.0, FAKE_AU))

	deadline = time.monotonic() + 5.0
	while time.monotonic() < deadline and len(manager.nalu_record_ring) < 1:
		manager._drain_nalu_queue()

	# The record AU fills its ring and byte counter but must not advance the
	# view freshness signal, even though its arrival is newer.
	assert manager.nalu_record_ring and manager.nalu_last_frame_at == now
	assert manager.nalu_ring_bytes["record"] == len(FAKE_AU)
	assert len(manager.nalu_view_ring) == 1

	manager.nalu_queue.put(("view", "au", 2.0, now + 2.0, FAKE_AU))

	deadline = time.monotonic() + 5.0
	while time.monotonic() < deadline and len(manager.nalu_view_ring) < 2:
		manager._drain_nalu_queue()

	assert len(manager.nalu_view_ring) == 2
	assert manager.nalu_last_frame_at == now + 2.0


def test_add_nalu_subscriber_anchors_catchup_at_idr() -> None:
	manager = _nalu_manager()
	manager.nalu_extradata = FAKE_ANNEX_B_EXTRADATA
	manager.nalu_meta = (704, 576, 25.0, "h264")
	now = time.monotonic()
	# An out-of-window IDR must not anchor the replay either.
	manager.nalu_view_ring.append((0.0, b"\x00\x00\x00\x01\x65" + b"stale-idr", now - 10.0))
	manager.nalu_view_ring.append((1.0, FAKE_DELTA_AU, now - 1.8))
	manager.nalu_view_ring.append((2.0, FAKE_DELTA_AU, now - 1.6))
	manager.nalu_view_ring.append((3.0, FAKE_IDR_AU, now - 1.4))
	manager.nalu_view_ring.append((4.0, FAKE_DELTA_AU, now - 1.2))

	subscriber = manager.add_nalu_subscriber()

	# The pre-IDR deltas are dropped; replay starts at the IDR.
	assert _decode_nalu_frames(subscriber.get_nowait()) == [_annexb_to_avcc_au(FAKE_IDR_AU)]
	assert _decode_nalu_frames(subscriber.get_nowait()) == [_annexb_to_avcc_au(FAKE_DELTA_AU)]
	with pytest.raises(asyncio.QueueEmpty):
		subscriber.get_nowait()


def test_au_contains_idr_sees_both_wire_forms() -> None:
	# Annex-B spelling.
	assert _au_contains_idr(FAKE_IDR_AU, False) is True
	assert _au_contains_idr(FAKE_DELTA_AU, False) is False
	assert _au_contains_idr(b"", False) is False

	# AVCC spelling, including a unit led by a header NAL and a
	# unit whose first length field is zero.
	idr_avcc = struct.pack(">I", 5) + b"\x65" + b"idr-slice"
	delta_avcc = struct.pack(">I", 2) + b"\x41ab"
	sps_then_idr_avcc = struct.pack(">I", 1) + b"\x07" + struct.pack(">I", 4) + b"\x65" + b"idr"

	assert _au_contains_idr(idr_avcc, True) is True
	assert _au_contains_idr(delta_avcc, True) is False
	assert _au_contains_idr(sps_then_idr_avcc, True) is True
	assert _au_contains_idr(b"\x00\x00\x00\x00\x07sps", True) is False


@pytest.mark.anyio
async def test_capture_loop_survives_nalu_spawn_failure() -> None:
	manager = _nalu_manager()
	manager.nalu_process = None
	manager.is_running = True

	attempts: list[float] = []

	def flaky_start() -> None:
		attempts.append(time.monotonic())
		raise RuntimeError("spawn exploded")

	manager._start_nalu_worker = flaky_start
	manager.capture_task = asyncio.create_task(manager._capture_loop())

	try:
		# The first retry must wait out the 1.0s restart backoff (not the
		# old 0.25s), and a raised spawn must not end the capture loop.
		await asyncio.sleep(1.6)
		assert len(attempts) == 1
		assert manager.capture_task.done() is False
	finally:
		manager.is_running = False
		manager.capture_task.cancel()
		try:
			await manager.capture_task
		except asyncio.CancelledError:
			pass


@pytest.mark.anyio
async def test_start_rolls_back_is_running_when_nalu_spawn_raises() -> None:
	manager = _nalu_manager()

	def flaky_start() -> None:
		raise OSError("spawn exploded")

	manager._start_nalu_worker = flaky_start

	with pytest.raises(OSError, match="spawn exploded"):
		await manager.start()

	# A failed spawn must not leave the manager "running": the MJPEG
	# fallback only (re)starts the capture loop when is_running is False.
	assert manager.is_running is False
	assert manager.capture_task is None


def test_nalu_queue_put_drops_oldest_without_duplicating() -> None:
	# Stdlib queue.Queue (no mp feeder-thread buffer/pipe split) makes
	# drop-oldest deterministic: each drop is visible to the next put.
	nalu_queue = queue.Queue(maxsize=2)

	for i in range(5):
		_nalu_queue_put(nalu_queue, ("view", "au", 0.0, 0.0, b"aux-%d" % i))

	drained: list[tuple] = []
	while True:
		try:
			drained.append(nalu_queue.get_nowait())
		except queue.Empty:
			break

	payloads = [payload for _source, _kind, _pts, _arrival, payload in drained]

	assert len(payloads) == len(set(payloads))
	assert b"aux-4" in payloads


