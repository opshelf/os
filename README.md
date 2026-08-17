# openshelf os

## overview

this repo contains the local runtime for openshelf devices.
the stack is container-first and runs two services:

- `dev_ui`: sveltekit ui on `http://localhost:8080`
- `api`: fastapi backend on `http://localhost:8000`

the backend uses in-process queues for robot commands and live event/status fanout.

the backend persists runtime data in `openshelf_hardware/data` (including local sqlite state, settings, and mode tracking).

## architecture

- `dev_ui/`: sveltekit frontend used for local and device ui workflows
- `openshelf_hardware/packages/hardware/`: low-level hardware integrations
- `openshelf_hardware/packages/openshelf_os/`: domain and orchestration logic
- `openshelf_hardware/packages/openshelf_os_server/`: fastapi app, routers, auth, storage, and database migrations
- `docker-compose.yml`: base stack (`api`, `dev_ui`)
- `docker-compose.dev.yml`: development overrides (hot reload for api/ui source)
- `docker-compose.hardware.yml`: usb/device passthrough for hardware-connected machines
- `scripts/start`, `scripts/stop`: preferred lifecycle commands
- `scripts/laser.py`, `scripts/motor.py`: hardware debugging helpers

## development setup

### prerequisites

- docker desktop or docker engine with compose v2
- `uv` for backend checks and local scripts
- node/npm for frontend checks outside docker

### start locally

first-time local development start:

```bash
./scripts/start --mode development --build
```

normal local development start:

```bash
./scripts/start --mode development
```

plain `./scripts/start` reuses `openshelf_hardware/data/.mode` when present. if no mode file exists, it starts in `production` mode.

production keeps the legacy `base.db`, `config.json`, and `storage.json` filenames. development and demo use persistent mode-specific variants such as `base.development.db`, `config.development.json`, and `storage.development.json`. the first start in a non-production mode copies the existing production state without modifying it, so returning to production restores the original workspace. modes are selected only through `scripts/start`; the api exposes the current mode read-only.

demo additionally seeds only its demo database/storage workspace and restricts robot storage to the barcode-to-cell mappings in `openshelf_hardware/data/demo.json`.

useful variants:

```bash
./scripts/start --mode development --detached
./scripts/start --mode development --hardware
./scripts/start --mode demo
./scripts/start --mode production
./scripts/start --build
```

use `--hardware` on machines with connected robot, camera, scanner, or printer devices.

### waveshare usb to 4ch rs485 (b)

the default modbus bus config prefers stable symlinks for the waveshare ch344l hub:

```text
/dev/openshelf-rs485-ch1  motor bus, 115200 baud
/dev/openshelf-rs485-ch2  scale bus, 9600 baud
/dev/openshelf-rs485-ch3  laser bus, 9600 baud
/dev/openshelf-rs485-ch4  spare rs485 channel
```

the required device naming rule is installed by the provisioning toolkit from `../provision/init/assets/etc/udev/rules.d/99-openshelf-waveshare-rs485.rules`.

without the rule, waveshare devices are not selected by generic `/dev/ttyACM*` names because those can belong to scanners or other cdc-acm devices; the app only falls back to the legacy usb adapter paths.

### barcode scanner port

`OPENSHELF_BARCODE_PORT` is passed through to the api container when set in the shell or deployment environment. set it explicitly for scanners that do not appear at the built-in scanner by-id path, for example:

```bash
OPENSHELF_BARCODE_PORT=/dev/serial/by-id/<scanner-device-id> ./scripts/start --hardware
```

prefer a scanner by-id path or a scanner-specific udev symlink. do not use `/dev/ttyACM0` as a default on waveshare systems because it can be rs485 channel 1 instead of the scanner.

### stop

```bash
./scripts/stop
```

## quick checks

- ui: `http://localhost:8080`
- api health: `http://localhost:8000/api/health`
- developer login: `developer@opshelf.com` with pin `1111`

## debugging

show service logs:

```bash
docker compose -f docker-compose.yml logs -f api
docker compose -f docker-compose.yml logs -f dev_ui
```

in development mode, api source under `openshelf_hardware/packages/*/src` and ui source under `dev_ui/src` are bind-mounted into the containers.

dependency changes, dockerfile changes, or mode switches across the development/non-development boundary need a rebuild on laptops:

```bash
./scripts/start --mode development --build
```

on deployed devices the stack runs prebuilt images pulled from the team registry (`fleet.opshelf.com/opshelf/*`) and never builds on device. `./scripts/start --pull` pulls prebuilt images instead of building on a boundary-crossing mode switch, and `../provision/remote <device> -c sync:dev` pulls the images matching the checkout sha before starting. the `--build` flag remains the escape hatch for on-device source builds when dependencies change before a release exists.

runtime state lives in `openshelf_hardware/data`. delete or edit this directory carefully because it contains the local sqlite database and mode files.

## local quality checks

backend:

```bash
cd openshelf_hardware
uv sync
uv run pytest
uv run ruff check packages
uv run mypy packages/openshelf_os_server/src
```

frontend:

```bash
cd dev_ui
npm install
npm run check
npm run build
```

## development pi workflow

list known devices from the sibling provision repo:

```bash
../provision/remote list
```

open a shell on a device:

```bash
../provision/remote <device-id-or-alias>
```

forward local ports for ui/api access:

```bash
../provision/remote <device-id-or-alias> -f 8000 -f 8080
```

on current lab devices, the checkout is usually:

```bash
/opt/opshelf/repos/openshelf_os
```

for feature work on a development pi, inspect the branch and worktree before pulling:

```bash
cd /opt/opshelf/repos/openshelf_os
git status --short --branch
git pull
./scripts/start --mode development --hardware --pull
```

source-only changes are picked up by the bind mounts immediately; `--pull` fetches the prebuilt images matching the checkout (see `../provision/remote <device> -c sync:dev` for the one-command variant). when the change touches dependencies, publish a release from the provision repo (`../provision/release`) and sync the device, or rebuild on the device with `./scripts/start --mode development --hardware --build`.

if the worktree is dirty, stop and reconcile it before pulling.

## hardware scripts

run hardware scripts from a hardware-enabled development environment. for device-side laser work, stop the long-running api first so serial ports are not locked, then restart it afterward.

example one-off laser command on a development pi:

```bash
cd /opt/opshelf/repos/openshelf_os
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.hardware.yml run --rm --entrypoint sh -v /opt/opshelf/repos/openshelf_os/scripts:/app/scripts:ro api -lc 'cd /app/openshelf_hardware && uv run python /app/scripts/laser.py --config /app/openshelf_hardware/data/config.development.json'
```
