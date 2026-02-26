# perpetual os

## overview

this repo contains the local runtime for perpetual devices.
the development stack is container-first and runs three services:

- `dev_ui`: sveltekit ui on `http://localhost:8080`
- `api`: fastapi backend on `http://localhost:8000`
- `nats`: message bus used by backend and hardware flows

the backend persists runtime data in `perpetual_hardware/data` (including local sqlite state).

## architecture

- `dev_ui/`: sveltekit frontend used for local and device ui workflows
- `perpetual_hardware/packages/hardware/`: low-level hardware integrations
- `perpetual_hardware/packages/perpetual_os1/`: domain and orchestration logic
- `perpetual_hardware/packages/perpetual_os1_server/`: fastapi app, routers, auth, storage, and database migrations
- `docker-compose.yml`: base stack (`nats`, `api`, `dev_ui`)
- `docker-compose.dev.yml`: development overrides (hot reload for api/ui)
- `docker-compose.hardware.yml`: usb/device passthrough for hardware-connected machines
- `scripts/start`, `scripts/stop`: preferred lifecycle commands for local development

## development setup

### prerequisites

- docker desktop (with compose v2)

### start

default start (development mode with reload enabled):

```bash
./scripts/start
```

useful variants:

```bash
./scripts/start --build
./scripts/start --hardware
./scripts/start --mode demo
./scripts/start --mode production
./scripts/start --detached
```

### stop

```bash
./scripts/stop
```

## quick checks

- ui: `http://localhost:8080`
- api health: `http://localhost:8000/api/health`

## local quality checks

backend:

```bash
cd perpetual_hardware
uv run pytest
```

frontend:

```bash
cd dev_ui
npm run check
```
