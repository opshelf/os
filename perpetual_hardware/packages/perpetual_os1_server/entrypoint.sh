#!/bin/sh
set -eu

PYTHON_BIN=""
ENV_PATH="/app/perpetual_hardware/.venv"

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

# Resolve printer queue/URI from config.json
QUEUE_FROM_CONFIG=""
if [ -z "${PRINTER_QUEUE:-}" ] && [ -z "${PRINTER:-}" ] && [ -n "$PYTHON_BIN" ]; then
	for config_path in "/app/perpetual_hardware/data/config.json" "/app/perpetual_hardware/data/settings.json"; do
		if [ -f "$config_path" ]; then
			QUEUE_FROM_CONFIG=$("$PYTHON_BIN" - "$config_path" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
    queue = data.get("printer", {}).get("queue")
    if queue:
        print(queue)
except Exception:
    pass
PY
)
			break
		fi
	done
fi

QUEUE="${PRINTER_QUEUE:-${PRINTER:-${QUEUE_FROM_CONFIG}}}"
if [ -z "$QUEUE" ]; then
	QUEUE="Caysn_T9E-US"
fi
URI="${PRINTER_URI:-usb://Caysn/T9E-US?serial=000015753145}"

if [ -n "$QUEUE" ] && command -v lpadmin >/dev/null 2>&1; then
	# Create queue if missing
	if ! lpstat -p "$QUEUE" >/dev/null 2>&1; then
		lpadmin -p "$QUEUE" -E -v "$URI" -m raw || true
	fi

	# Enable and set default
	cupsenable "$QUEUE" || true
	cupsaccept "$QUEUE" || true
	lpoptions -d "$QUEUE" || true
else
	echo "Printer queue not configured or lpadmin missing; printing may fail"
fi

start_api() {
	cd /app/perpetual_hardware && exec uv run start
}

start_api
