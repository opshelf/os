# openshelf os

## overview

this repo contains the local runtime for openshelf devices.
the stack is container-first and runs two services:

- `dev_ui`: sveltekit ui on `http://localhost:8080`
- `api`: fastapi backend on `http://localhost:8000`

the backend uses in-process queues for robot commands and live event/status fanout.

the backend persists runtime data in `perpetual_hardware/data` (including local sqlite state, settings, and mode tracking).

## architecture

- `dev_ui/`: sveltekit frontend used for local and device ui workflows
- `perpetual_hardware/packages/hardware/`: low-level hardware integrations
- `perpetual_hardware/packages/openshelf_os/`: domain and orchestration logic
- `perpetual_hardware/packages/openshelf_os_server/`: fastapi app, routers, auth, storage, and database migrations
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

plain `./scripts/start` reuses `perpetual_hardware/data/.mode` when present. if no mode file exists, it starts in `production` mode.

useful variants:

```bash
./scripts/start --mode development --detached
./scripts/start --mode development --hardware
./scripts/start --mode demo
./scripts/start --mode production
./scripts/start --build
```

use `--hardware` on machines with connected robot, camera, scanner, or printer devices.

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

in development mode, api source under `perpetual_hardware/packages/*/src` and ui source under `dev_ui/src` are bind-mounted into the containers.

dependency changes, dockerfile changes, or mode switches across the development/non-development boundary need a rebuild:

```bash
./scripts/start --mode development --build
```

runtime state lives in `perpetual_hardware/data`. delete or edit this directory carefully because it contains the local sqlite database and mode files.

## local quality checks

backend:

```bash
cd perpetual_hardware
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
./scripts/start --mode development --hardware
```

if the worktree is dirty, stop and reconcile it before pulling.

## hardware scripts

run hardware scripts from a hardware-enabled development environment. for device-side laser work, stop the long-running api first so serial ports are not locked, then restart it afterward.

example one-off laser command on a development pi:

```bash
cd /opt/opshelf/repos/openshelf_os
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.hardware.yml run --rm --entrypoint sh -v /opt/opshelf/repos/openshelf_os/scripts:/app/scripts:ro api -lc 'cd /app/perpetual_hardware && uv run python /app/scripts/laser.py --config /app/perpetual_hardware/data/config.json'
```
