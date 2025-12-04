# Perpetual OS

## Quick Start

1. Start the services (same stack as the repository root):

   ```bash
   docker compose up
   ```

2. Open your browser and navigate to:
   ```
   http://localhost:8080
   ```

## Running with Hardware Support

To run with USB device access for barcode scanners, scales, and other hardware:

```bash
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up
```

This mounts the following devices:

- `/dev/ttyUSB0`
- `/dev/ttyACM0`
- `/dev/ttyACM1`
- `/dev/bus/usb`

Without the hardware override file, the system runs in software-only mode and doesn't require physical devices.

> **Note:** This compose file extends `../docker-compose.yml` and shares the project name `opshelf`, so the containers you manage here are the same ones you would get by running `docker compose` from the repository root.

## Standalone development

If you want to work on Perpetual OS without the bundle repository:

```bash
docker compose -f docker-compose.standalone.yml up
```

Add hardware access just like the shared stack by including the override file:

```bash
docker compose -f docker-compose.standalone.yml -f docker-compose.hardware.yml up
```

> **Tip:** The standalone compose file uses the project name `perpetual_os`. Make sure the bundle stack (`opshelf`) is not running at the same time to avoid port conflicts on 8000/8080/4222.

## Running tests (workspace)

From the repository root, run all package tests with uv:

```bash
uv run --project perpetual_hardware --all-packages pytest
```

## Scripts

Convenience scripts are provided in the `scripts/` directory for easier management:

### Start Script

```bash
# Start in standalone mode (default)
scripts/start

# Start with hardware access
scripts/start -h
scripts/start --hardware
```

### Stop Script

```bash
# Stop standalone mode (default)
scripts/stop

# Stop hardware mode
scripts/stop -h
scripts/stop --hardware
```

Both scripts use the standalone Docker Compose configuration by default and include the hardware override file when the `-h` or `--hardware` flag is provided.

## Scale Calibration

The SJ-101D_X scale can be calibrated remotely via the API. This is useful when the scale reports `valid: false` status.

### Calibration Process

1. **Set calibration parameters** (full scale capacity and known weight value):

   ```bash
   curl -X POST http://<device>:8000/api/robot/debug/scale/calibration/params \
     -H "Content-Type: application/json" \
     -d '{"full_scale": 6000, "calibration_weight": 500, "precision": 1, "division": 1}'
   ```

   - `full_scale`: Maximum scale capacity in grams (e.g., 6000 for 6kg)
   - `calibration_weight`: Known reference weight in grams
   - `precision`: Decimal places (0-3, default 1)
   - `division`: Division value (default 1)

2. **Zero calibration** (ensure scale is empty):

   ```bash
   curl -X POST http://<device>:8000/api/robot/debug/scale/calibration/zero
   ```

   Wait for `{"success": true, "status": {"zero_ok": true, ...}}` response.

3. **Weight calibration** (place the known reference weight on scale):

   ```bash
   curl -X POST http://<device>:8000/api/robot/debug/scale/calibration/weight
   ```

   Wait for `{"success": true, "status": {"weight_ok": true, ...}}` response.

### Diagnostic Endpoints

- **Read current weight**: `GET /api/robot/debug/scale`
- **Check calibration status**: `GET /api/robot/debug/scale/calibration/status`
- **Read calibration parameters**: `GET /api/robot/debug/scale/calibration/params`
