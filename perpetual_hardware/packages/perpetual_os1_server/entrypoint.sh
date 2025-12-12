#!/bin/sh
set -eu

# Start cupsd if available
if command -v cupsd >/dev/null 2>&1; then
	cupsd || true
fi

# Ensure serial_asyncio is available (needed by robot worker)
if command -v uv >/dev/null 2>&1; then
	ENV_PATH="/app/perpetual_hardware/.venv"
	if [ -x "${ENV_PATH}/bin/python" ]; then
		uv pip install --python "${ENV_PATH}/bin/python" pyserial-asyncio >/dev/null 2>&1 || true
	else
		uv pip install pyserial-asyncio >/dev/null 2>&1 || true
	fi
fi

# Resolve printer queue/URI from config.json
QUEUE_FROM_CONFIG=""
if [ -z "${PRINTER_QUEUE:-}" ] && [ -z "${PRINTER:-}" ] && command -v python3 >/dev/null 2>&1; then
	for config_path in "/app/perpetual_hardware/packages/perpetual_os1_server/data/config.json" "/app/perpetual_hardware/packages/perpetual_os1_server/data/settings.json"; do
		if [ -f "$config_path" ]; then
			QUEUE_FROM_CONFIG=$(python3 - "$config_path" <<'PY'
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

exec uv run --no-dev start
