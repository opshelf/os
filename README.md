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

production keeps the legacy `base.db`, `config.json`, and `storage.json` filenames. development and demo use persistent mode-specific variants such as `base.development.db`, `config.development.json`, and `storage.development.json`. the first start in either mode copies the existing production state without modifying it, so returning to production restores the original workspace. modes are selected only through `scripts/start`; the api exposes the current mode read-only.

mock may copy the production configuration to `config.mock.json`, but it creates a fresh `base.mock.db` and never copies `base.db`, `storage.json`, items, or item locations. its first workspace therefore has empty inventory while retaining the seeded users, roles, and three access levels. later starts reuse `base.mock.db`, so item, command, and audit state survives container restarts without affecting production.

demo additionally seeds only its demo database/storage workspace and restricts robot storage to the barcode-to-cell mappings in `openshelf_hardware/data/demo.json`.

useful variants:

```bash
./scripts/start --mode development --detached
./scripts/start --mode development --hardware
./scripts/start --mode demo
./scripts/start --mode mock
./scripts/start --mode production
./scripts/start --build
```

use `--hardware` on machines with connected robot, camera, scanner, or printer devices.

### mock mode

mock mode runs the real authenticated fastapi service and sqlite persistence without initializing robot, camera, scanner, sizer, printer, cups, or sentry integrations. it does not automatically add the hardware compose overlay, even when `config.mock.json` was copied from a hardware-enabled production config.

mock is production-like for authentication and audit integrity. on provisioned devices `../provision` delivers `JWT_SECRET`, `AUDIT_HMAC_KEY`, and `AUDIT_HMAC_KEY_ID` in the checkout `.env`, and where provisioning has not reached, the api generates and keeps its own audit key per mode in the data directory. to pin your own secrets in a workspace that is not disposable, set them in that `.env` (or export them) and give the audit key an id of its own: every audit hmac key needs one, an id must never be reused with a different key, `v2` is what provisioning delivers and `v2-<mode>` is what the api generates for itself, so a locally pinned key must use some other id. the file is shared with other Compose values, so append rather than overwrite it; a repeated run leaves duplicate assignments where the last one wins, which discards the key that signed earlier rows, so those rows will report that no key is available for their id:

```bash
umask 077
printf 'JWT_SECRET=%s\nAUDIT_HMAC_KEY=%s\nAUDIT_HMAC_KEY_ID=%s\n' \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  "local-$(date -u +%Y%m%d%H%M%S)" >> .env
chmod 600 .env
./scripts/start --mode mock --detached
```

the normal developer account (`developer@opshelf.com`, pin `1111`) is seeded, but authentication remains required. existing item CRUD, users, roles, access levels, permissions, audit logging, correlation-id propagation, and response schemas continue to use sqlite.

the virtual cabinet has 80 deterministic cells: left module then right module, four shelves per side, and ten cells per shelf. `items` and `item_locations` in `base.mock.db` are the occupancy source of truth. the runtime exposes:

- `GET /api/health`: reports `mode=mock`, a ready mock robot, and no hardware dependency
- `POST /api/robot/commands`: queues `store_item` or `retrieve_item` and returns a command uuid
- `GET /api/robot/commands/{command_id}`: returns the persisted lifecycle and result
- `GET /api/robot/status` and `GET /api/robot/events`: stream status snapshots and results over sse
- `POST /api/print`: prints the supplied barcode (in the request body) as a Code 128 label and returns `printed=true` without producing a label in mock mode

commands run through one deterministic fifo worker with a 30-second simulated execution time and a limit of 32 queued/running commands. an optional top-level `request_id` makes a request idempotent for seven days: an exact replay returns the original command, while reuse with a different payload returns `409`. a full queue returns `429` with `Retry-After: 1`. commands interrupted by a service restart are persisted as failed rather than replayed.

store allocation selects the first fitting free cell in stable layout order and persists a deterministic weight when none is supplied. retrieve validates the requested location and the caller's access before queueing, then removes the item location on completion. invalid input and permissions retain normal `4xx` responses; deterministic execution-time conflicts become failed commands and error events.

send `X-Correlation-ID` to associate API activity with audit records. audit entries can be filtered by exact `correlation_id` through `GET /api/audit/logs`; historical entries created before correlation IDs remain verifiable.

hardware-only routes outside this integration surface may still report disabled or unavailable in mock mode. command polling exposes persisted results in both mock and production; available measurements depend on the command producer.

see the [Local API reference](https://api.opshelf.com/docs/local/) storage group for container mass, cycle measurements, and production limits.

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

### relay service authentication

The integration relay authenticates to a device with a unique opaque bearer token instead of a
user email and PIN. Give the raw token only to the relay and start the OS container with its
lowercase SHA-256 digest in `OPSHELF_RELAY_SERVICE_TOKEN_SHA256`.

The credential is accepted only by the relay-facing inventory item endpoints, command
submission and polling, robot status/events streams, and printing. It is not valid for users,
roles, access levels, audit, calibration, system configuration, or other authenticated APIs.

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

on deployed devices the stack runs prebuilt images pulled from the team registry (`registry.opshelf.com/opshelf/*`) and never builds on device. production runs slim multi-stage images without dev tooling; development runs the dev variants (`openshelf_os_api_dev`, `openshelf_os_dev_ui_dev`) which carry debugpy, pytest/mypy/ruff, and node_modules for the bind-mounted sources. plain starts never build implicitly (`--no-build` is always passed); `./scripts/start --pull` pulls prebuilt images instead of building on a boundary-crossing mode switch, and `../provision/remote <device> -c sync:dev` pulls the images matching the checkout sha before starting. the `--build` flag remains the escape hatch for on-device source builds when dependencies change before a release exists.

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

### Chemical name lookup

The optional CAS Common Chemistry integration uses `CAS_COMMON_CHEMISTRY_API_KEY`
in the **API server environment only**. Configure it in the ignored repository
`.env` file for Compose, or inject it into the server process environment when
running directly. Never put it in frontend configuration. Restart the API after
changing the key. Without a key, inventory suggestions still work (plus dictionary fixtures in mocked mode), and
the chemical endpoints return HTTP 503.

Authenticated users with `storage:read` may call
`GET /api/chemicals/search?q=ethanol&offset=0` (at most three results) and
`GET /api/chemicals/64-17-5` (name, synonyms, CAS number, source URL).
Search accepts complete CAS numbers with or without hyphens, checks their checksum,
and queries them exactly. Incomplete CAS numbers (such as `112-`), invalid
complete numeric identifiers, and explicit numeric wildcard queries return no
results without contacting CAS. Prefix matching remains available in the UI's
local inventory (and dictionary fixtures in mocked mode). Live checks confirmed that `112-12-9` resolves,
while `112-*` and `64-17-*` return no matches from Common Chemistry.
Text searches try an exact query, then a trailing wildcard if there are no exact
results. Name wildcards are supported by the upstream service.
Upstream requests time out after eight seconds per request, share concurrent identical
lookups, and cache up to 256 responses for one hour per server process. Rate
limits temporarily disable remote lookup; custom item entry remains available.
The upstream contract is documented at
<https://commonchemistry.cas.org/swagger/commonchemistry-swagger.json>.


## API documentation

[Local API reference](https://api.opshelf.com/docs/local/) covers every application
endpoint, including engineering and development operations. The running device
also serves `/docs`, `/redoc`, and `/openapi.json`.

From `openshelf_hardware/`:

```sh
uv sync --all-packages
uv run openapi
uv run openapi --check
uv run pytest packages/openshelf_os_server/tests/test_openapi.py
```

OpenAPI is generated from the application without running its lifespan. Update
route summaries/docstrings and Pydantic field descriptions/examples alongside
behavior changes. Put shared concepts in `documentation.py`; keep endpoint prose
with the handler. Document effects, prerequisites, completion, and recovery when
they are not apparent from the schema. Use `response_docs` for documentation-only
response models so adding documentation does not change runtime serialization.
`uv run openapi` writes the JSON on demand; it is a generated artifact and is not
committed. `uv run openapi --check` validates the generated contract without
writing. The contract tests check coverage, examples, references, and media types.

Publishing both references is owned by the sibling relay's `scripts/deploy-docs.py`,
which regenerates each document at build time.

<!-- release workflow smoke test 1/2 -->
