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

mock is production-like for authentication and audit integrity. on provisioned devices the provisioning tooling delivers `JWT_SECRET`, `AUDIT_HMAC_KEY`, and `AUDIT_HMAC_KEY_ID` in the checkout `.env`, and where provisioning has not reached, the api generates and keeps its own audit key per mode in the data directory. to pin your own secrets in a workspace that is not disposable, set them in that `.env` (or export them) and give the audit key an id of its own: every audit hmac key needs one, an id must never be reused with a different key, `v2` is what provisioning delivers and `v2-<mode>` is what the api generates for itself, so a locally pinned key must use some other id. the file is shared with other Compose values, so append rather than overwrite it; a repeated run leaves duplicate assignments where the last one wins, which discards the key that signed earlier rows, so those rows will report that no key is available for their id:

```bash
umask 077
printf 'JWT_SECRET=%s\nAUDIT_HMAC_KEY=%s\nAUDIT_HMAC_KEY_ID=%s\n' \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  "local-$(date -u +%Y%m%d%H%M%S)" >> .env
chmod 600 .env
./scripts/start --mode mock --detached
```

the developer account (`developer@opshelf.com`, pin `1111` unless `DEVELOPER_PIN` is set; see [developer account](#developer-account)) is seeded, but authentication remains required. existing item CRUD, users, roles, access levels, permissions, audit logging, correlation-id propagation, and response schemas continue to use sqlite.

the virtual cabinet has 80 deterministic cells: front module then back module, four shelves per side, and ten cells per shelf. `items` and `item_locations` in `base.mock.db` are the occupancy source of truth. the runtime exposes:

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
/dev/openshelf-rs485-ch3  laser bus, 115200 baud
/dev/openshelf-rs485-ch4  spare rs485 channel
```

the symlinks come from a udev rule, for example `/etc/udev/rules.d/99-openshelf-waveshare-rs485.rules`:

```text
SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="1a86", ENV{ID_MODEL_ID}=="55d5", ENV{ID_USB_INTERFACE_NUM}=="00", SYMLINK+="openshelf-rs485-ch1", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="1a86", ENV{ID_MODEL_ID}=="55d5", ENV{ID_USB_INTERFACE_NUM}=="02", SYMLINK+="openshelf-rs485-ch2", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="1a86", ENV{ID_MODEL_ID}=="55d5", ENV{ID_USB_INTERFACE_NUM}=="04", SYMLINK+="openshelf-rs485-ch3", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="1a86", ENV{ID_MODEL_ID}=="55d5", ENV{ID_USB_INTERFACE_NUM}=="06", SYMLINK+="openshelf-rs485-ch4", ENV{ID_MM_DEVICE_IGNORE}="1"
```

reload with `sudo udevadm control --reload-rules && sudo udevadm trigger`.

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
- developer login: `developer@opshelf.com` with pin `1111` in development and mock mode

### roles

routes that move hardware, start or abort calibration, save calibration, run the demo cycle, or restart the api need `robot:control` (the default `admin` and `operator` roles; `viewer` has only `storage:read`). reading state needs only a login. `store_item` and `retrieve_item` commands stay open to every signed-in user, with retrieve still checking the item's access level, but a store that creates a new item needs `storage:write`. the relay keeps its scoped access.

### developer account

every database gets the system user `developer@opshelf.com` (admin role, hidden from user lists). its pin follows the mode on every start:

- development and mock: `1111`
- production and demo: the value of `DEVELOPER_PIN` in the api environment (the checkout `.env`, next to `JWT_SECRET`); without one the user is locked with a random pin, and a device still carrying `1111` from an older release is moved off it at startup
- `DEVELOPER_PIN` overrides the pin in every mode

the dev ui never asks for the pin: behind the relay it signs in with the pin from the `opshelf_developer_pin` cookie the relay sets, and on a development or mock device reached directly it needs no sign-in.

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

in development mode, api source under `openshelf_hardware/packages/*/src` and ui source under `dev_ui/src` are bind-mounted into the containers. the overlay does not mount `scripts/` or the test directories, so a one-off container that needs a script needs its own `-v` for the scripts (see the `migrate_config.py` docker example above).

dependency changes, dockerfile changes, or mode switches across the development/non-development boundary need a rebuild on laptops:

```bash
./scripts/start --mode development --build
```

on deployed devices the stack runs prebuilt images pulled from the team registry (`registry.opshelf.com/opshelf/*`) and never builds on device. production runs slim multi-stage images without dev tooling; development runs the dev variants (`openshelf_os_api_dev`, `openshelf_os_dev_ui_dev`) which carry pytest/mypy/ruff, and node_modules for the bind-mounted sources. plain starts never build implicitly (`--no-build` is always passed); `./scripts/start --pull` pulls prebuilt images instead of building on a boundary-crossing mode switch. the `--build` flag remains the escape hatch for on-device source builds when dependencies change before a release exists.

runtime state lives in `openshelf_hardware/data`. delete or edit this directory carefully because it contains the local sqlite database and mode files.

every checkout of this repository shares the compose project name `openshelf_os`, so starting a second checkout replaces the containers of the first. set `OPSHELF_COMPOSE_PROJECT` per worktree when two stacks must run side by side. `scripts/start` also adds `docker-compose.hardware.yml` by itself when the selected mode's config enables `robot.enabled` or `robot_app.enabled` (never in mock mode), and accepts `--force-recreate` next to `-d`, `-h`, `-b`, and `-p`.

the api accepts cross-origin requests from any origin because the ui is served from its own port, and it never advertises credentialed access, because authentication travels in an `Authorization` header or a `token` query parameter rather than a cookie. set `OPENSHELF_CORS_ORIGINS` to a comma-separated list of exact origins to narrow the policy to those origins.

## local quality checks

backend:

```bash
cd openshelf_hardware
uv sync --all-packages   # bare `uv sync` prunes the dependencies workspace members need
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

## config schema migrations

`schema_version` is a top-level positive integer in the config files, independent of the application's `version` string. a missing `schema_version` means **1** (the pre-versioning provisioning schema); the current schema is **7** (`config.schema.CURRENT_SCHEMA_VERSION`). the server auto-migrates the active config file on startup (with an exclusive `.schema-v<N>.bak` rollback backup) and refuses a newer schema before parsing nested config models; device scripts and other offline entry points require the manual command instead. do not fix a startup error by changing only `schema_version`: the old field names would then lose their meaning.

### applying a migration

the server auto-migrates the active config file on startup, so a device booting the new image needs no manual step; each other mode's config file is migrated the first time that mode starts. to pre-migrate every mode's config file (or to work offline), use the new release's migration runner and dependencies. from the checkout:

```bash
cd openshelf_hardware
uv sync --all-packages
uv run --no-sync python ../scripts/migrate_config.py data
```

the directory form selects only `config.json` and `config.*.json` immediately in that directory (production, development, demo, mock); it never recurses into config history or touches storage grids, databases, or scan recordings. individual files and multiple directories can also be supplied explicitly.

when the preview is correct, stop the application and write:

```bash
./scripts/stop
cd openshelf_hardware
uv run --no-sync python ../scripts/migrate_config.py data --write
```

on a containerized device, obtain the matching released api image and checkout, stop the stack, then run the new image without starting its server or dependencies:

```bash
docker compose -f docker-compose.yml run --rm --no-deps --pull never --entrypoint sh \
  -v "$PWD/scripts:/app/scripts:ro" api -lc \
  'cd /app/openshelf_hardware && uv run --no-sync python /app/scripts/migrate_config.py data'
```

repeat with `--write` added after `data` once the preview is correct. hardware passthrough is unnecessary for this offline operation; restart through the repo-managed flow, preserving the selected mode and hardware overlay (e.g. `./scripts/start -d -h --mode development`).

every selected file is migrated in memory and validated against the current config model before anything is written; unknown fields and numeric calibration values are retained, and the output lists changed key names, not config values. each changed file gets an exclusive byte-for-byte `<filename>.schema-v<N>.bak` backup (the number is its source schema) and is replaced atomically, preserving owner and permissions. replacement is atomic per file, not across the directory: if a batch is interrupted, inspect the reported files and backups before retrying. for rollback, restore the affected config files from their backups together with the prior firmware; do not lower only the version number. backups contain credentials — keep them private.

at release time, migrate any config template you ship to devices with the same runner and coordinate its schema with the firmware schema; do not ship a new template to firmware predating the release.

### adding a migration

add `v007_<description>.py` under `openshelf_hardware/packages/openshelf_os/src/openshelf_os/config_migrations/`. its `migrate(config)` function accepts the raw previous-schema dictionary and returns the transformed dictionary plus a list of changed field paths. register it under the destination version in `runner.MIGRATIONS` and increment `config.schema.CURRENT_SCHEMA_VERSION`. the runner advances the version only after each step succeeds and validates the complete result before persistence.

never modify a released migration; append a new step. test value preservation, conflicting keys, optional fields, the whole migration chain, and startup rejection. update generated config defaults, fixtures, release notes, and the provisioning template for each new schema; do not include secrets in migration diagnostics.

## development pi workflow

open a shell on the device over ssh, forwarding the ui/api ports when you want them locally:

```bash
ssh -L 8000:localhost:8000 -L 8080:localhost:8080 <user>@<device-host>
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

source-only changes are picked up by the bind mounts immediately; `--pull` fetches the prebuilt images matching the checkout. when the change touches dependencies, publish a release and pull it on the device, or rebuild on the device with `./scripts/start --mode development --hardware --build`.

if the worktree is dirty, stop and reconcile it before pulling.

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
```

OpenAPI is generated from the application without running its lifespan. Update
route summaries/docstrings and Pydantic field descriptions/examples alongside
behavior changes. Put shared concepts in `documentation.py`; keep endpoint prose
with the handler. Document effects, prerequisites, completion, and recovery when
they are not apparent from the schema. Use `response_docs` for documentation-only
response models so adding documentation does not change runtime serialization.
`uv run openapi` writes the JSON on demand; it is a generated artifact and is not
committed. `uv run openapi --check` validates the generated contract without
writing.

