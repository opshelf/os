# AGENTS.md

## Repo shape
- This is not a root workspace. The two real work areas are `dev_ui/` (SvelteKit/Vite UI) and `perpetual_hardware/` (Python `uv` workspace with `hardware`, `perpetual_os1`, and `perpetual_os1_server`). Run frontend commands from `dev_ui/` and backend commands from `perpetual_hardware/`.
- The backend entrypoint is `perpetual_hardware/packages/perpetual_os1_server/src/perpetual_os1_server/main.py`. `uv run start` serves FastAPI on port `8000`, auto-runs Alembic migrations on startup, and reads runtime state from `perpetual_hardware/data/`.
- The frontend default API base is `http://<current-hostname>:8000/api` (`dev_ui/src/lib/api.ts`), so UI work usually assumes the API is running on the same host.

## Preferred commands
- Full stack: `./scripts/start` and `./scripts/stop` from repo root.
- `./scripts/start` defaults to `production` mode unless `perpetual_hardware/data/.mode` already exists. Use `./scripts/start --mode development` for hot reload, `./scripts/start --hardware` for USB/device passthrough, and `./scripts/start --build` when switching between development and non-development stacks.
- Do not restart repo-managed containers with raw `docker compose up/down/restart` when mode matters. Use `./scripts/start` and `./scripts/stop` so `OPERATING_MODE`, build-boundary handling, and `data/.mode` stay in sync.
- Frontend checks: `cd dev_ui && npm run check` and `cd dev_ui && npm run build`. There is no repo-local frontend lint or test script.
- Backend setup/checks: `cd perpetual_hardware && uv sync`, `cd perpetual_hardware && uv run pytest`, `cd perpetual_hardware && uv run ruff check packages`, `cd perpetual_hardware && uv run mypy packages/perpetual_os1_server/src`.
- Focused backend tests work directly with package paths, for example `cd perpetual_hardware && uv run pytest packages/perpetual_os1_server/tests/test_storage_endpoints.py`.

## Runtime gotchas
- `perpetual_hardware/data/` is the live source of truth for local state: SQLite lives at `data/base.db`, config is read from `data/config.json` / `data/settings.json`, `scripts/start` tracks the requested mode in `data/.mode` and `data/.last_start_mode`, and the API records runtime mode transitions in `data/.last_mode`.
- In development mode, `docker-compose.dev.yml` only bind-mounts Python source directories plus `dev_ui/src`. Dependency changes still need a rebuild.
- The app seeds a developer login on startup: `developer@opshelf.com` with PIN `1111`. The UI auto-attempts this login in `dev_ui/src/routes/+layout.svelte`.
- `README.md` still references `scripts/debug/*`, but that directory is not in this repo anymore.

## Remote device inspection
- If a user asks to inspect or debug something "on device", use the sibling `../provision/remote` helper instead of guessing SSH details or inspecting only local files.
- `../provision/remote <device-id|alias>` resolves known aliases, derives the relay SSH port from the device id, and uses `../provision/keys/team_access` automatically.
- Use it in three modes: plain shell access (`../provision/remote <id-or-alias>`), one-off remote commands (`../provision/remote <id-or-alias> -t "<command>"`), and local port forwards (`../provision/remote <id-or-alias> -f 8000 -f 8080`). Start with `../provision/remote list` if the device id or alias is unknown.
- The helper also supports predefined maintenance commands with `-c`, including `sync`, `api:restart`, `api:backup`, `api:restore`, `api:enable <component>`, and `api:disable <component>`.
- For on-device `scripts/laser.py` work, treat `development` mode as required and verify `perpetual_hardware/data/.mode` first. On current lab devices the checkout lives at `/opt/opshelf/repos/perpetual_os` and the API container is `perpetual_os-api-1`.
- Do not use `../provision/remote <device> -c sync` for laser/dev work. That helper syncs `main`, which is wrong if the device should track a feature or development branch.
- For laser/dev work, sync the device checkout with git on the branch already checked out there: inspect with `git status --short --branch`, confirm the branch is the intended one, then run `git pull` in `/opt/opshelf/repos/perpetual_os`. If the worktree is dirty, stop and reconcile that state before pulling instead of copying files into the container.
- Keep remote device checkouts clean. Do not make persistent source edits directly on a device; make changes locally, run checks, commit, push, then pull on the device and restart with repo-managed scripts. If the device is not in development mode, include `./scripts/start --build` when the change affects dependencies, generated assets, or other build-boundary files.
- `scripts/laser.py` is intended to run while the long-running `api` container is stopped, otherwise the robot serial ports can stay locked by the app server.
- The reliable device-side runtime is a one-off hardware-enabled compose container, not `docker exec` into the running API. Use `docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.hardware.yml run --rm --entrypoint sh -v /opt/opshelf/repos/perpetual_os/scripts:/app/scripts:ro api -lc 'cd /app/perpetual_hardware && uv run python /app/scripts/laser.py ... --config /app/perpetual_hardware/data/config.json'`.
- Stop `api` before starting that one-off run, and always bring `api` back up afterward. Prefer wrapping the remote command with a shell `trap` so `docker compose ... up -d api` runs even if the scan fails.
- The `scripts/` directory is not bind-mounted by `docker-compose.dev.yml`, so fresh one-off containers will not see `/app/scripts/laser.py` unless you add the explicit `-v /opt/opshelf/repos/perpetual_os/scripts:/app/scripts:ro` bind mount.
- After pulling laser-related changes on device, restart the development stack from the device repo only if the running containers need to pick up updated mounted code or rebuilt dependencies. Use the repo-managed startup flow such as `./scripts/start --mode development` and add `--build` when dependencies or build-boundary changes require it.
