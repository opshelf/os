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

## Testing coverage
- Keep the smallest suite that protects critical behavior: physical safety and interlocks, security boundaries and audit integrity, persistent data and migration integrity, and recovery from failures that could leave unsafe motion or incorrect inventory.
- Before adding or retaining a test, identify the concrete consequential failure it catches and why existing coverage does not catch it. Routine changes do not require new tests; do not target coverage percentages or test counts.
- Prefer tests that exercise a real failure through the relevant behavior boundary and assert the resulting safety, access, or persisted state. Mock hardware and external services as needed, but keep the logic under test real.
- Do not add tests solely for getters/setters, framework behavior, serialization, status payloads, log wording, argument forwarding, mock bookkeeping, or routine CRUD success. An exception needs a concrete critical failure, not hypothetical regression risk.
- Keep one representative case per distinct failure mechanism. Add parameter variants only when they exercise different critical outcomes; avoid exhaustive value matrices, duplicate happy paths, and tests that mirror implementation steps.
- When changing tests, remove superseded cases and newly unused fixtures, helpers, and test-only dependencies. Run the relevant retained tests, including the shuffled backend run required above, and report failures accurately.

## API errors
- Every error body is `{"detail": {"message", "code"}}`: raise `api_error(status, code, message)` from `errors.py` so the body always carries a stable `code`, and never put exception text, enum values, ids, config keys, paths or stderr in the message — `errors.py` normalizes the body into one sentence-case operator sentence and scrubs server-side detail, and the fix is to log the exception (`logger.warning(..., exc_info=exc)`) instead of forwarding it.
- Both client surfaces show `detail.message` verbatim: the kiosk maps `code` and exact sentences in `../ui/src/lib/operator-message.ts`, and `dev_ui/` toasts it, so new copy needs a matching kiosk entry (prefer a `code`, which survives wording changes) and copy stays sentence case with a terminal period, no "please", no raw identifiers.

## Dev UI
- Report errors through the toast system (`postToast('error', …)` from `$lib/toasts`), not inline error text in pages, dialogs, or components.
- Keep a button's label unchanged while its action is busy or queued — only disable it; do not swap in "homing…"/"queued"-style titles.

## Device signing keys
- Only the unanchored `data/` gitignore rule keeps signing-key files out of git — never `git add -f` one.
- Never give a device a second key under an id another key already used, and never let a locally generated key take the provisioned id `v2`: `verify_audit_chain` selects the key by the id recorded on each row, so a reused id turns a key change into a tampering report.
