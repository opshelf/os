# Perpetual OS

## Quick Start

1. Start the services:
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
docker compose --profile hardware up
```

This mounts the following devices:
- `/dev/ttyUSB0`
- `/dev/ttyACM0`
- `/dev/ttyACM1`
- `/dev/bus/usb`

Without the `hardware` profile, the system runs in software-only mode and doesn't require physical devices.