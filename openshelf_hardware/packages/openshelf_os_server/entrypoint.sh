#!/bin/sh
set -eu

PYTHON_BIN=""
ENV_PATH="/app/openshelf_hardware/.venv"
RUNTIME_MODE="$(cat /app/openshelf_hardware/data/.mode 2>/dev/null || printf '%s' "${OPERATING_MODE:-development}")"

if [ -x "${ENV_PATH}/bin/python" ]; then
	PYTHON_BIN="${ENV_PATH}/bin/python"
fi

# Start cupsd if available
if [ "$RUNTIME_MODE" != "mock" ] && command -v cupsd >/dev/null 2>&1; then
	cupsd || true
fi

# Ensure serial_asyncio is available (needed by robot worker). Install only when the
# image environment is missing it: an unconditional unpinned install replaces the
# version the image was built and locked with. Checked on disk rather than by import,
# since every interpreter launch here delays the API start.
if [ -n "$PYTHON_BIN" ]; then
	if ! ls -d "$ENV_PATH"/lib/python3*/site-packages/serial_asyncio >/dev/null 2>&1; then
		echo "serial_asyncio missing from the image environment, installing it"
		uv pip install --python "$PYTHON_BIN" pyserial-asyncio >/dev/null 2>&1 || true
	fi
elif command -v uv >/dev/null 2>&1; then
	uv pip install pyserial-asyncio >/dev/null 2>&1 || true
fi

# Resolve printer queue/URI from the configured runtime config.
QUEUE_FROM_CONFIG=""
if [ "$RUNTIME_MODE" != "mock" ] && [ -z "${PRINTER_QUEUE:-}" ] && [ -z "${PRINTER:-}" ] && [ -n "$PYTHON_BIN" ]; then
	case "$RUNTIME_MODE" in
		development|demo|mock) ACTIVE_CONFIG="/app/openshelf_hardware/data/config.$RUNTIME_MODE.json" ;;
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

if [ "$RUNTIME_MODE" = "mock" ]; then
	echo "Mock mode; skipping CUPS setup"
elif [ -z "$QUEUE" ]; then
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
	cd /app/openshelf_hardware
	# Run the environment the image was built with. `uv run` re-syncs on every start,
	# which installs dev tooling into the runtime environment and can replace installed
	# versions with a resolution the image was not built or tested against.
	if [ -x "${ENV_PATH}/bin/start" ]; then
		exec "${ENV_PATH}/bin/start"
	fi
	exec uv run start
}

if [ "$#" -gt 0 ]; then
	cd /app/openshelf_hardware
	exec "$@"
fi

start_api
