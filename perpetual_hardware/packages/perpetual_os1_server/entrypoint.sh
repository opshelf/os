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
	for config_path in "/app/perpetual_hardware/data/config.json" "/app/perpetual_hardware/data/settings.json"; do
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

build_supplementary_groups() {
	group_ids=""

	for group_id in "${OPSHELF_DIALOUT_GID:-}" "${OPSHELF_VIDEO_GID:-}" "${OPSHELF_INPUT_GID:-}" "${OPSHELF_RENDER_GID:-}"; do
		if [ -z "$group_id" ]; then
			continue
		fi

		if [ -z "$group_ids" ]; then
			group_ids="$group_id"
		else
			group_ids="$group_ids,$group_id"
		fi
	done

	printf '%s\n' "$group_ids"
}

start_api() {
	if [ "$(id -u)" != "0" ] || [ -z "${OPSHELF_UID:-}" ] || [ -z "${OPSHELF_GID:-}" ]; then
		cd /app/perpetual_hardware && exec uv run start
	fi

	SUPPLEMENTARY_GROUPS="$(build_supplementary_groups)"
	export HOME=/tmp
	export USER=opshelf
	export LOGNAME=opshelf

	cd /app/perpetual_hardware

	exec python3 - "$OPSHELF_UID" "$OPSHELF_GID" "$SUPPLEMENTARY_GROUPS" <<'PY'
import os
import sys

uid = int(sys.argv[1])
gid = int(sys.argv[2])
groups = [int(value) for value in sys.argv[3].split(",") if value != ""]

os.setgroups(groups)
os.setgid(gid)
os.setuid(uid)
os.execvp("uv", ["uv", "run", "start"])
PY
}

start_api
