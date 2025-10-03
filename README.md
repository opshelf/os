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
