# AGENTS.md

## Setup
- Run frontend commands from `dev_ui/` and backend commands from `openshelf_hardware/`. Full stack: `./scripts/start` / `./scripts/stop` from the repo root — never restart repo-managed containers with raw `docker compose up/down/restart` or `docker restart` (the API container can come back without the hardware compose overlay and lose USB/device passthrough).
- On hardware devices, restart with `./scripts/stop` then `./scripts/start -d -h` (with the appropriate `--mode`/`--build` flags); on deployed devices prefer `--pull` (prebuilt images from the team registry).
- Always sync the backend with `uv sync --all-packages` — bare `uv sync` only syncs the workspace root project and prunes the dependencies the members need, leaving the venv unable to import `sqlalchemy`/`fastapi` until re-synced.
- For on-device inspection, use the team's device access helper instead of guessing SSH details. Do not make persistent source edits on a device — change, check, commit, push, then pull on the device and restart through the repo-managed scripts.

## Runtime
- Keep released config migration steps immutable, register new steps in `runner.MIGRATIONS`, and bump `config.schema.CURRENT_SCHEMA_VERSION`; never stamp the new schema version onto unmigrated data, and never restore an old snapshot without migrating it first.
- Do not reintroduce digit-only or fixed-length barcode validation, and do not remove the `upca` → `barcode` read-time shim in `CellItem`.
- Server tests run every async test and fixture under pytest-asyncio auto mode; `pytest.ini` disables AnyIO's pytest plugin (`-p no:anyio`). Do not re-enable it: with both plugins loaded, a plain async fixture of an anyio-marked test runs on whichever plugin registered last (site-packages order), so a fixture-started worker such as the mock robot can land on a different event loop than the test.
- The backend suite is order-dependent only because collection order is stable — before landing test changes, run it shuffled at least once: `uv run pytest --random-order` (`--random-order-bucket=global` to interleave modules, `--random-order-seed=<seed>` to replay a failing order).

## Device signing keys
- Only the unanchored `data/` gitignore rule keeps signing-key files out of git — never `git add -f` one.
- Never give a device a second key under an id another key already used, and never let a locally generated key take the provisioned id `v2`: `verify_audit_chain` selects the key by the id recorded on each row, so a reused id turns a key change into a tampering report.
