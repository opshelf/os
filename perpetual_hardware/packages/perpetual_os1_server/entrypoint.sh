#!/bin/sh
set -euo pipefail

# Start cupsd if available
if command -v cupsd >/dev/null 2>&1; then
	cupsd || true
fi

QUEUE="${PRINTER_QUEUE:-${PRINTER:-}}"
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
fi

exec uv run --no-dev start
