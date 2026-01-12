# perpetual os

## up and running

1. start the stack:

   ```bash
   docker compose up
   ```

   for hardware support (usb devices like scanners/scales):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.hardware.yml up
   ```

2. open:

   ```
   http://localhost:8080
   ```
