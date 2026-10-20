from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import fcntl
import os
import socket
import struct
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.errors import api_error
from openshelf_os_server.routers.auth_deps import get_current_user, require_permissions

logger = get_logger(__name__)

router = APIRouter(tags=["wifi"])

WIFI_INTERFACE = "wlan0"
CONNECTION_NAME = "opshelf-wifi"
PENDING_NAME = "opshelf-wifi-pending"
CONFIG_FILE = Path("/boot/firmware/wifi.cfg")
DBUS_ADDRESS = "unix:path=/run/dbus/system_bus_socket"
NMCLI_WAIT_SECONDS = "30"
SUBPROCESS_TIMEOUT_SECONDS = 45.0


class WifiStatus(BaseModel):
	connected: bool
	ssid: str | None = None
	signal: int | None = None
	ip: str | None = None


class WifiNetwork(BaseModel):
	ssid: str
	signal: int
	security: str
	in_use: bool


class WifiNetworkList(BaseModel):
	networks: list[WifiNetwork]


class WifiConnectRequest(BaseModel):
	ssid: str
	password: str


class WifiConnectResponse(BaseModel):
	success: bool
	ssid: str
	message: str


class _NmcliResult:
	def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
		self.returncode = returncode
		self.stdout = stdout
		self.stderr = stderr

	@property
	def ok(self) -> bool:
		return self.returncode == 0


async def _run_nmcli(*args: str) -> _NmcliResult:
	env = {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": DBUS_ADDRESS}
	try:
		process = await asyncio.create_subprocess_exec(
			"nmcli",
			*args,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			env=env,
		)
	except FileNotFoundError as exc:
		logger.error("wifi_nmcli_missing", error=str(exc))
		raise api_error(
			status.HTTP_500_INTERNAL_SERVER_ERROR,
			"wifi_unavailable",
			"Network settings are not available on this cabinet.",
		) from exc

	try:
		stdout_bytes, stderr_bytes = await asyncio.wait_for(
			process.communicate(), timeout=SUBPROCESS_TIMEOUT_SECONDS
		)
	except asyncio.TimeoutError:
		process.kill()
		await process.wait()
		return _NmcliResult(124, "", "nmcli timed out")

	return _NmcliResult(
		process.returncode if process.returncode is not None else 1,
		stdout_bytes.decode(errors="replace"),
		stderr_bytes.decode(errors="replace"),
	)


def _split_terse_line(line: str) -> list[str]:
	fields: list[str] = []
	current: list[str] = []
	index = 0
	while index < len(line):
		char = line[index]
		if char == "\\" and index + 1 < len(line):
			current.append(line[index + 1])
			index += 2
			continue
		if char == ":":
			fields.append("".join(current))
			current = []
			index += 1
			continue
		current.append(char)
		index += 1
	fields.append("".join(current))
	return fields


def _wlan_ip() -> str | None:
	try:
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		ip = socket.inet_ntoa(
			fcntl.ioctl(
				sock.fileno(),
				0x8915,
				struct.pack("256s", WIFI_INTERFACE.encode()),
			)[20:24]
		)
		sock.close()
		return ip
	except OSError:
		return None


async def active_status() -> WifiStatus:
	result = await _run_nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi")
	if result.ok:
		for line in result.stdout.splitlines():
			fields = _split_terse_line(line)
			if len(fields) >= 2 and fields[0] == "yes":
				signal = int(fields[2]) if len(fields) >= 3 and fields[2].isdigit() else None
				return WifiStatus(
					connected=True,
					ssid=fields[1] or None,
					signal=signal,
					ip=_wlan_ip(),
				)
	return WifiStatus(connected=False, ssid=None, signal=None, ip=_wlan_ip())


async def _active_connection_name() -> str | None:
	result = await _run_nmcli("-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
	if not result.ok:
		return None
	for line in result.stdout.splitlines():
		fields = _split_terse_line(line)
		if len(fields) >= 2 and fields[1] == WIFI_INTERFACE:
			return fields[0]
	return None


def _write_config(ssid: str, password: str) -> None:
	try:
		CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
		CONFIG_FILE.write_text(f"ssid={ssid}\npassword={password}\n", encoding="utf-8")
		os.chmod(CONFIG_FILE, 0o600)
	except OSError as exc:
		logger.warning("wifi_config_write_failed", error=str(exc))


async def _bring_up_pending(ssid: str, password: str) -> _NmcliResult:
	await _run_nmcli("connection", "delete", PENDING_NAME)
	await _run_nmcli(
		"connection",
		"add",
		"type",
		"wifi",
		"ifname",
		WIFI_INTERFACE,
		"con-name",
		PENDING_NAME,
		"ssid",
		ssid,
	)
	if password == "":
		return await _run_nmcli("-w", NMCLI_WAIT_SECONDS, "connection", "up", PENDING_NAME)

	await _run_nmcli("connection", "modify", PENDING_NAME, "wifi-sec.key-mgmt", "wpa-psk")
	await _run_nmcli("connection", "modify", PENDING_NAME, "wifi-sec.psk", password)

	result = await _run_nmcli("-w", NMCLI_WAIT_SECONDS, "connection", "up", PENDING_NAME)
	if result.ok:
		return result

	logger.info("wifi_wpa_psk_failed_trying_sae", ssid=ssid, error=result.stderr.strip())
	await _run_nmcli("connection", "modify", PENDING_NAME, "wifi-sec.key-mgmt", "sae")
	return await _run_nmcli("-w", NMCLI_WAIT_SECONDS, "connection", "up", PENDING_NAME)


async def _rollback(previous_connection: str | None) -> None:
	await _run_nmcli("connection", "delete", PENDING_NAME)
	if previous_connection is not None and previous_connection != PENDING_NAME:
		restore = await _run_nmcli("-w", NMCLI_WAIT_SECONDS, "connection", "up", previous_connection)
		logger.info(
			"wifi_rollback",
			connection=previous_connection,
			restored=restore.ok,
			error=restore.stderr.strip(),
		)


@router.get(
    "/status",
    response_model=WifiStatus,
    dependencies=[Depends(get_current_user)],
    summary="Read Wi-Fi connection state",
    responses=response_docs(errors=(401, 500)),
)
async def get_wifi_status() -> WifiStatus:
	"""Read Wi-Fi connection state.

	Returns the active wireless SSID, signal, and IP when available. Requires
	authentication. A disconnected state is represented in the body.
	"""
	return await active_status()


async def scan_networks() -> WifiNetworkList:
	await _run_nmcli("dev", "wifi", "rescan")
	result = await _run_nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list")
	if not result.ok:
		logger.warning("wifi_list_failed", error=result.stderr.strip())
		raise api_error(
			status.HTTP_502_BAD_GATEWAY,
			"wifi_list_failed",
			"The network list could not be read. Try again.",
		)

	strongest: dict[str, WifiNetwork] = {}
	for line in result.stdout.splitlines():
		fields = _split_terse_line(line)
		if len(fields) < 4:
			continue
		ssid = fields[1]
		if ssid == "":
			continue
		signal = int(fields[2]) if fields[2].isdigit() else 0
		network = WifiNetwork(
			ssid=ssid,
			signal=signal,
			security=fields[3] or "open",
			in_use=fields[0] == "*",
		)
		existing = strongest.get(ssid)
		if existing is None or network.signal > existing.signal or network.in_use:
			strongest[ssid] = network

	networks = sorted(strongest.values(), key=lambda n: n.signal, reverse=True)
	return WifiNetworkList(networks=networks)


async def perform_connect(requested_ssid: str, password: str) -> WifiConnectResponse:
	ssid = requested_ssid.strip()

	if ssid == "":
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a network to connect to.")
	previous_connection = await _active_connection_name()
	logger.info("wifi_connect_requested", ssid=ssid, previous=previous_connection)

	result = await _bring_up_pending(ssid, password)
	current = await active_status()

	if result.ok and current.connected and current.ssid == ssid:
		await _run_nmcli("connection", "delete", CONNECTION_NAME)
		await _run_nmcli("connection", "modify", PENDING_NAME, "connection.id", CONNECTION_NAME)
		_write_config(ssid, password)
		logger.info("wifi_connect_succeeded", ssid=ssid)
		return WifiConnectResponse(success=True, ssid=ssid, message=f"connected to {ssid}")

	await _rollback(previous_connection)
	detail = result.stderr.strip() or "connection did not activate"
	logger.warning(
		"wifi_connect_failed",
		ssid=ssid,
		error=detail,
		rolled_back_to=previous_connection,
	)
	raise api_error(
		status.HTTP_502_BAD_GATEWAY,
		"wifi_connect_failed",
		"The cabinet could not connect to that network. Check the password and try again.",
	)


@router.get(
    "/networks",
    response_model=WifiNetworkList,
    dependencies=[Depends(get_current_user)],
    summary="Scan available Wi-Fi networks",
    responses=response_docs(errors=(401, 500, 502)),
)
async def list_wifi_networks() -> WifiNetworkList:
	"""Scan available Wi-Fi networks.

	Scans using NetworkManager and returns visible SSIDs, signal, security, and
	active-network markers. Requires authentication; this can take longer than a cached
	status read.
	"""
	return await scan_networks()


@router.post(
    "/connect",
    response_model=WifiConnectResponse,
    dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
    summary="Connect and persist Wi-Fi",
    responses=response_docs(errors=(400, 401, 403, 500, 502)),
)
async def connect_wifi(request: WifiConnectRequest) -> WifiConnectResponse:
	"""Connect and persist Wi-Fi.

	Requires `robot:control`. Creates a pending NetworkManager connection, activates it, and
	writes boot Wi-Fi configuration on success. Failure rolls back the pending connection.
	Changing networks may disconnect the requesting client.
	"""
	return await perform_connect(request.ssid, request.password)
