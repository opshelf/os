#!/bin/sh
set -eu

PYTHON_BIN=""
ENV_PATH="/app/openshelf_hardware/.venv"

if [ -x "${ENV_PATH}/bin/python" ]; then
	PYTHON_BIN="${ENV_PATH}/bin/python"
fi

# Start cupsd if available
if command -v cupsd >/dev/null 2>&1; then
	cupsd || true
fi

# Ensure serial_asyncio is available (needed by robot worker)
if command -v uv >/dev/null 2>&1; then
	if [ -n "$PYTHON_BIN" ]; then
		uv pip install --python "$PYTHON_BIN" pyserial-asyncio >/dev/null 2>&1 || true
	else
		uv pip install pyserial-asyncio >/dev/null 2>&1 || true
	fi
fi

# Resolve printer queue/URI from the configured runtime config.
QUEUE_FROM_CONFIG=""
if [ -z "${PRINTER_QUEUE:-}" ] && [ -z "${PRINTER:-}" ] && [ -n "$PYTHON_BIN" ]; then
	MODE="$(cat /app/openshelf_hardware/data/.mode 2>/dev/null || printf '%s' "${OPERATING_MODE:-development}")"
	case "$MODE" in
		development|demo) ACTIVE_CONFIG="/app/openshelf_hardware/data/config.$MODE.json" ;;
		*) ACTIVE_CONFIG="/app/openshelf_hardware/data/config.json" ;;
	esac
	for config_path in "${OPENSHELF_CONFIG_PATH:-}" "$ACTIVE_CONFIG" "/app/openshelf_hardware/data/config.json" "/app/openshelf_hardware/data/settings.json"; do
		if [ -f "$config_path" ]; then
			if QUEUE_FROM_CONFIG=$("$PYTHON_BIN" - "$config_path" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    printer = data.get("printer")
    queue = printer.get("queue") if isinstance(printer, dict) else None
    if isinstance(queue, str) and queue:
        print(queue)
except Exception:
    sys.exit(1)
PY
	); then
				break
			fi
		fi
	done
fi

QUEUE="${PRINTER_QUEUE:-${PRINTER:-${QUEUE_FROM_CONFIG}}}"
URI="${PRINTER_URI:-usb://Caysn/T9E-US?serial=000015753145}"

if [ -z "$QUEUE" ]; then
	echo "Printer queue not configured; skipping CUPS setup"
elif command -v lpadmin >/dev/null 2>&1; then
	# Create queue if missing
	if ! lpstat -p "$QUEUE" >/dev/null 2>&1; then
		lpadmin -p "$QUEUE" -E -v "$URI" -m raw || true
	fi

	# Enable and set default
	cupsenable "$QUEUE" || true
	cupsaccept "$QUEUE" || true
	lpoptions -d "$QUEUE" || true
else
	echo "lpadmin missing; printing may fail"
fi

start_api() {
	cd /app/openshelf_hardware && exec uv run start
}

start_api
