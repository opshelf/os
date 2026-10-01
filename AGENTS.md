# AGENTS.md

## Repo shape
- Run frontend commands from `dev_ui/` and backend commands from `openshelf_hardware/`.

## Preferred commands
- Full stack: `./scripts/start` and `./scripts/stop` from repo root.
- `./scripts/start` defaults to `production` mode unless `openshelf_hardware/data/.mode` already exists. Use `./scripts/start --mode development` for hot reload, `./scripts/start --mode mock` for the hardware-free API/runtime, `./scripts/start --hardware` for USB/device passthrough, and `./scripts/start --build` when switching between development and non-development stacks on a laptop. On deployed devices prefer `--pull`, which fetches prebuilt images from the team registry instead of building on the pi.
- Do not restart repo-managed containers with raw `docker compose up/down/restart` when mode matters. Use `./scripts/start` and `./scripts/stop` so `OPERATING_MODE`, build-boundary handling, and `data/.mode` stay in sync.
- On hardware devices, do not use bare `./scripts/start` for restarts. Use `./scripts/start --hardware` with the appropriate `--mode`/`--build` flags, otherwise the API container can restart without the hardware compose overlay and lose USB/device passthrough.
- For hardware-device restarts, stop first and start detached with hardware passthrough from the device checkout: `./scripts/stop` then `./scripts/start -d -h`. Do not use `docker restart` for the API container; it can come back without the hardware compose overlay.
- Frontend checks: `cd dev_ui && npm run check` and `cd dev_ui && npm run build`. There is no repo-local frontend lint or test script.
- Backend setup/checks: `cd openshelf_hardware && uv sync --all-packages`, `cd openshelf_hardware && uv run pytest`, `cd openshelf_hardware && uv run ruff check packages`, `cd openshelf_hardware && uv run mypy packages/openshelf_os_server/src`.
- Always sync the backend with `uv sync --all-packages`. Bare `uv sync` only syncs the workspace root project and prunes every dependency the members need, which leaves the venv unable to import `sqlalchemy`, `fastapi`, and the rest until you re-sync with `--all-packages`.
- Focused backend tests work directly with package paths, for example `cd openshelf_hardware && uv run pytest packages/openshelf_os_server/tests/test_storage_endpoints.py`.
- The backend suite is order-dependent by default only because collection order is stable. Before landing test changes, run it shuffled at least once: `cd openshelf_hardware && uv run pytest --random-order`. Use `--random-order-bucket=global` to interleave modules and `--random-order-seed=<seed>` to replay a failing order.

## Runtime gotchas
- Never stamp the new config schema version onto unmigrated data. Restored old snapshots are auto-migrated by the server on the next startup, but the auto-migration refuses to run when a migration backup already exists in the data directory (e.g. an old snapshot restored over a previously migrated config) — reconcile the backup before retrying.
- Keep released config migration steps immutable, register new steps in `runner.MIGRATIONS`, and bump `config.schema.CURRENT_SCHEMA_VERSION`.
- Do not reintroduce digit-only or fixed-length barcode validation, keep grouped display formatting (`formattedNumberString` in `../ui`) display-only, and do not remove the `upca` → `barcode` read-time shim in `CellItem`.
- Set `limit_switches_enabled` to `false` on the motors that have no switch, and keep the config decision in `openshelf_os` construction (`tool.py` / `iobox.py`) rather than gating inside `hardware.motor.Motor`, which also receives limit inputs from diagnostic scripts that want the read.

## Mock mode
- Do not add hardware fallbacks to mock mode; an explicit `--hardware` is unnecessary there.
- Keep `JWT_SECRET` and `AUDIT_HMAC_KEY` stable and non-default when running mock outside disposable local tests.
- Preserve the mock cabinet's stable ordering when changing allocation behavior or tests.
- Keep item CRUD, access checks, audit records, correlation IDs, and response schemas active in mock mode. Hardware-oriented routes that are not part of the integration contract may remain disabled or unavailable.
- Run the focused mock checks with `cd openshelf_hardware && uv run pytest packages/openshelf_os_server/tests/test_mock_robot.py packages/openshelf_os_server/tests/test_printer_endpoint.py`. Also run the full shuffled backend suite after runtime or fixture changes.
- Tests marked with `pytest.mark.anyio` must use AnyIO-owned async fixtures (`@pytest.fixture`) for background tasks; mixing them with `@pytest_asyncio.fixture` can attach the worker to a different event loop in the full suite.

## Remote device inspection
- If a user asks to inspect or debug something "on device", use the sibling `../provision/remote` helper instead of guessing SSH details or inspecting only local files.
- Use it in three modes: plain shell access (`../provision/remote <id-or-alias>`), one-off remote commands (`../provision/remote <id-or-alias> -t "<command>"`), and local port forwards (`../provision/remote <id-or-alias> -f 8000 -f 8080`). Start with `../provision/remote list` if the device id or alias is unknown.
- There is no `api:enable`/`api:disable`; toggle a component in the active mode's config file and restart through the repo-managed scripts.
- For on-device `scripts/laser.py` work, treat `development` mode as required and verify `openshelf_hardware/data/.mode` first.
- Do not use `../provision/remote <device> -c sync` for laser/dev work. That helper syncs `main`, which is wrong if the device should track a feature or development branch.
- For laser/dev work, sync the device checkout with git on the branch already checked out there: inspect with `git status --short --branch`, confirm the branch is the intended one, then run `git pull` in `/opt/opshelf/repos/openshelf_os`. If the worktree is dirty, stop and reconcile that state before pulling instead of copying files into the container.
- Keep remote device checkouts clean. Do not make persistent source edits directly on a device; make changes locally, run checks, commit, push, then pull on the device and restart with repo-managed scripts. On a development device, pull the matching prebuilt images with `../provision/remote <device> -c sync:dev`; use `./scripts/start --build` only when the change affects dependencies and no release has been published yet.
- The reliable device-side runtime for `scripts/laser.py` is a one-off hardware-enabled compose container, not `docker exec` into the running API.
- Stop `api` before starting that one-off run, and always bring `api` back up afterward. Prefer wrapping the remote command with a shell `trap` so `docker compose ... up -d api` runs even if the scan fails.
- After pulling laser-related changes on device, restart the development stack from the device repo only if the running containers need to pick up updated mounted code or rebuilt dependencies. Use the repo-managed startup flow such as `./scripts/start --mode development --pull`, and add `--build` only when dependencies or build-boundary changes require an on-device rebuild.

## Migrating existing devices onto per-device signing keys
- Only the unanchored `data/` gitignore rule keeps the signing-key files out of git, so never `git add -f` one.
- Never give a device a second key under an id that another key already used, and never let a locally generated key take the provisioned id `v2`: `verify_audit_chain` selects the key by the id recorded on each row, so a reused id turns a key change into a tampering report.
