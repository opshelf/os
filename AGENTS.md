# AGENTS.md

This file is for coding agents working in `perpetual_os`.

## Scope
- Prefer repository-specific commands and conventions over generic defaults.
- Keep edits narrow and local to the package you are touching.
- Do not reformat unrelated files or do broad cleanup unless asked.

## Repo Map
- `dev_ui/`: SvelteKit dev UI for diagnostics and local workflows.
- `perpetual_hardware/`: uv workspace for Python packages, tests, and tooling.
- `perpetual_hardware/packages/hardware/`: low-level hardware drivers.
- `perpetual_hardware/packages/perpetual_os1/`: robot logic, storage logic, shared config models.
- `perpetual_hardware/packages/perpetual_os1_server/`: FastAPI app, routers, auth, DB, middleware.
- `scripts/start` and `scripts/stop`: preferred local stack lifecycle commands.

## Rule Files
- `.claude/CLAUDE.md` exists and its useful repo guidance is folded into this file.
- No `.cursor/rules/` files were found.
- No `.cursorrules` file was found.
- No `.github/copilot-instructions.md` file was found.

## Working Directories
- Repo root: Docker, compose, and top-level scripts.
- `perpetual_hardware/`: `uv`, `pytest`, `ruff`, `mypy`.
- `dev_ui/`: `npm` commands.

## Stack Lifecycle
- Start dev stack: `./scripts/start`
- Start with rebuild: `./scripts/start --build`
- Start with hardware passthrough: `./scripts/start --hardware`
- Start in demo mode: `./scripts/start --mode demo`
- Start detached: `./scripts/start --detached`
- Stop stack: `./scripts/stop`

## Laser Script
- Script path: `scripts/laser.py`
- Run it from `perpetual_hardware/`, not from repo root, so `data/config.json` resolves correctly.
- Host-side runs on devices use the host venv and usually require the API container to be stopped first because it holds the motor USB bus.
- Current scan setup in `laser.py` is fixed to stage `13mm` and z `Z_EXTENSION_PLACE + 10mm`.
- Host-side scan runs home the tool during `robot.configure()` before the scan starts. Do not assume the current position is safe or reusable after a failed run.
- `--module`, `--x`, `--y`, and `--step` are required unless `--continue` is used.
- `--y-offset` is explicit and defaults to `0.0`. Do not assume any hidden Y adjustment.
- `--continue` resumes the latest incomplete staged scan from `--scans-dir`.
- Scan artifacts are written to `/tmp` by default and include `run_parameters.json`, `progress.json`, `scan.log`, `coarse_depth.npy`, and any `fine_XX.npy` partials.
- Long device scans should be launched with `nohup`, not in a foreground SSH session.
- On `lb2-3`, do not use the physical X extremes for repeated scans. The measured hard-stop envelope is around `0..950`, but practical scan-safe bounds are narrower.
- On `lb2-3`, prefer `x=50..940` for left-module shelf scans unless a narrower range is known.
- Typical host-side pattern on a device:
- `docker stop perpetual_os-api-1`
- `source ~/.venvs/perpetual_os_min/bin/activate`
- `cd /opt/opshelf/repos/perpetual_os/perpetual_hardware`
- `nohup python ../scripts/laser.py --module left --x 50 940 --y 1203.5 1243.5 --step 1 > /tmp/laser_scan.out 2>&1 < /dev/null &`

## Frontend Commands
- Install deps: `npm install`
- Dev server: `npm run develop`
- Type/Svelte checks: `npm run check`
- Production build: `npm run build`
- Preview build: `npm run preview`

## Python Workspace Commands
- Sync workspace deps: `uv sync`
- Install editable packages: `uv pip install -e packages/hardware -e packages/perpetual_os1 -e packages/perpetual_os1_server`
- Run API entry point: `uv run start`
- Run all backend tests: `uv run pytest`
- Collect tests: `uv run pytest --collect-only -q`
- Lint Python packages: `uv run ruff check packages`
- Type-check packages: `uv run mypy packages/hardware/src packages/perpetual_os1/src packages/perpetual_os1_server/src`

## Single-Test Patterns
- Single file: `uv run pytest packages/perpetual_os1_server/tests/test_storage_endpoints.py`
- Single function: `uv run pytest packages/perpetual_os1_server/tests/test_storage_endpoints.py::test_create_storage_item`
- Single class: `uv run pytest packages/perpetual_os1/tests/test_storage_grid_io.py::TestLoadFromFile`
- Single class method: `uv run pytest packages/perpetual_os1/tests/test_storage_grid_io.py::TestLoadFromFile::test_invalid_json`
- Pattern inside a file: `uv run pytest packages/hardware/tests/hardware/test_controller.py -k connect`
- Stop on first failure: `uv run pytest -x`

## Migrations
- Migration working dir: `perpetual_hardware/packages/perpetual_os1_server/`
- Upgrade DB: `uv run alembic upgrade head`
- Create migration: `uv run alembic revision --autogenerate -m "description"`

## Verified Status
- `cd dev_ui && npm run check` succeeds.
- `cd dev_ui && npm run build` succeeds.
- `cd perpetual_hardware && uv run pytest --collect-only -q` succeeds and collects 265 tests.
- `cd perpetual_hardware && uv run ruff check packages` currently passes.
- `cd perpetual_hardware && uv run mypy ...` currently fails on pre-existing issues; do not assume a green mypy baseline.

## Test Layout
- Hardware tests: `perpetual_hardware/packages/hardware/tests/`
- Domain tests: `perpetual_hardware/packages/perpetual_os1/tests/`
- API tests: `perpetual_hardware/packages/perpetual_os1_server/tests/`
- Async tests use `pytest-asyncio` and/or `anyio` markers.
- Server tests rely on `perpetual_os1_server/tests/conftest.py` to set env and DB state before imports.

## Architecture Notes
- FastAPI exposes device, calibration, storage, auth, and audit endpoints.
- SQLAlchemy async models live in `perpetual_os1_server/database/`.
- Robot orchestration and config models live in `perpetual_os1/`.
- Hardware packages wrap Modbus devices, cameras, printers, and sensors.
- The UI is a thin operator/dev console over backend endpoints.

## General Editing
- Follow the style already used in the file you edit; the repo is not perfectly uniform.
- Avoid large refactors unless the task requires them.
- Keep comments minimal.
- Prefer small helpers and early returns over extra nesting.
- Preserve public API shapes unless the task calls for a contract change.

## Python Style
- Use 4-space indentation in Python.
- Keep lines within Ruff's configured `line-length = 100`.
- Many modern modules start with `from __future__ import annotations`; keep using it in new typed modules.
- Add type hints for new functions and methods.
- Prefer `X | None` in newer files when the file already uses modern union syntax.
- Older files also use `Optional[...]`; match the style already present in that file.
- Use `snake_case` for functions, variables, and helpers.
- Use `PascalCase` for classes, Pydantic models, ORM models, and exceptions.
- Use `UPPER_CASE` for constants like `CONFIG_PATH`.
- Match surrounding whitespace and docstring style instead of normalizing older modules.

## Python Imports And Typing
- Group imports as stdlib, third-party, then local.
- Keep a blank line between import groups.
- Within `perpetual_os1_server`, both absolute and relative local imports exist; match the dominant style of the file.
- Use package-root imports across package boundaries, for example `from perpetual_os1.common import ...`.
- Prefer Pydantic models for structured config and request/response payloads.
- Use SQLAlchemy `Mapped[...]` and `mapped_column(...)` for ORM fields.

## Backend Errors And Logging
- Raise domain-specific exceptions or `ValueError` in lower-level packages for invalid state.
- In FastAPI routers, convert expected domain/validation failures into `HTTPException` with useful status codes.
- Keep broad `except Exception` blocks only when logging, cleanup, or framework boundaries justify them.
- When wrapping and re-raising, use `raise ... from exc`.
- Preserve structured logging patterns like `logger.info("event_name", key=value)` where used.
- Never log secrets, auth headers, cookies, or credentials.

## Frontend Style
- TypeScript is strict; keep new code type-safe.
- Prettier is configured for tabs, single quotes, `printWidth: 100`, and `trailingComma: none`.
- Use explicit exported types/interfaces for API payloads and significant state.
- Use `PascalCase` for Svelte components.
- Use `camelCase` for TS variables and helpers.
- UI handlers commonly use Apple-style `did...` names such as `didClickMove` and `didChangeShelf`.
- Boolean state commonly uses `is...`, `needs...`, or `has...` names.
- Keep raw fetch logic centralized in `$lib/api.ts` or nearby shared helpers.

## Svelte Patterns
- This UI uses Svelte 5 runes such as `$state`, `$derived`, and `$props`.
- Prefer named functions for event logic.
- Bind named handlers in markup rather than inline anonymous lambdas.
- Use `onMount` for browser-only startup work.
- Guard browser-only code with explicit `browser === true` / `browser === false` checks when needed.
- Surface user-facing errors through shared state like `error`, `message`, and `warning` when the page already follows that pattern.

## Test Expectations
- Run the smallest relevant test first, then widen scope if needed.
- For Python changes, prefer the narrowest `uv run pytest <file>::<test>` command that covers the edit.
- For frontend changes, run `npm run check`; run `npm run build` for route or bundle-affecting work.
- For cross-stack changes, run both frontend checks and the relevant backend tests.

## Avoid
- Do not move files between packages unless the task requires it.
- Do not replace typed models with raw dicts without a strong reason.
- Do not bypass auth, audit, or safety-related logic for convenience.
- Do not assume mypy failures are your regression without checking whether they are pre-existing.
- Do not weaken validation or error reporting just to make tests pass faster.
