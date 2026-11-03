# AGENTS.md

## Setup
- Run frontend commands from `dev_ui/` and backend commands from `openshelf_hardware/`. Full stack: `./scripts/start` / `./scripts/stop` from the repo root — never restart repo-managed containers with raw `docker compose up/down/restart` or `docker restart` (the API container can come back without the hardware compose overlay and lose USB/device passthrough).
- On hardware devices, restart with `./scripts/stop` then `./scripts/start -d -h` (with the appropriate `--mode`/`--build` flags); on deployed devices prefer `--pull` (prebuilt images from the team registry).
- Always sync the backend with `uv sync --all-packages` — bare `uv sync` only syncs the workspace root project and prunes the dependencies the members need, leaving the venv unable to import `sqlalchemy`/`fastapi` until re-synced.
- Datasheets, manuals, and hardware notes live in Google Drive and Confluence. When Google Drive or Confluence (Atlassian) MCP tools are available, search them before guessing hardware specs or part behavior; they are local per-machine setup, not configured in this repo.
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
- Raise route errors `from exc`: the HTTP handler logs the chained exception with its traceback under the request's correlation id, and every error body carries `correlation_id`. The exception (chain, motor `fault_context`, validation field paths) goes only to that log line, never into any client body, for any user.
- Failures recorded outside an HTTP body (worker failure events, command results, bin workflow errors, laser calibration state) follow the same split: `error` is an operator sentence, `fault_code` names the cause from `openshelf_os.faults`, and the exception goes to a module-logger line with `exc_info` at the failure site, never into the payload. Use `errors.failure_detail(exc, fallback_code)` for the sentence; never store `str(exc)`. Keep the event `code` unchanged: it drives crash recovery (`CRASH_FAILURE_CODES`). Needs-home and crash reasons stay raw on purpose: recovery parses motor ids out of them.
- For a catch-all around hardware, raise `hardware_api_error(exc, status, code, message)` so a drive alarm, stall, timeout, bus disconnect, config error or rotation refusal gets its own code instead of the route's "Try again" fallback; add new codes to `classify_hardware_error`.
- Every `HTTPException` needs a stable code (`test_every_http_error_carries_a_code`): one code per meaning, reused across routes (`motor_not_configured`, `robot_not_initialized`), with its sentence in `CODE_MESSAGES`. Clients key on the code; the sentence may be edited.
- `test_router_messages_never_carry_raw_exception_text` fails when a router puts a caught exception into a message; an exception whose text is hand-written operator copy (e.g. `BinWorkflowConflict`) belongs in its allowlist, anything else gets a code sentence.
- `CODE_MESSAGES` (with `openshelf_os.faults.FAULT_MESSAGES`) is the one source of operator copy per code: the kiosk loads it from `GET /api/system/error-messages` at startup and prefers it over its built-in table, which only covers older devices and kiosk-raised codes; `dev_ui/` toasts `detail.message`. Change a code's wording here, not in `../ui`. Copy stays sentence case with a terminal period, no "please", no raw identifiers. End with "Try again" only when a retry alone can succeed (a transient read, a busy service); after a failed hardware action, name what to check first.

## Logging
- Log through the module logger (`logger = logging.getLogger(__name__)`, or `get_logger` where the module uses structlog), never the root `logging.` functions: the logger name is how a correlation id leads to the right module.
- A warning or error about a caught exception carries `exc_info=exc`, so the line a client's `ref` points at has the traceback. Leave it off only for a warning repeated on every tick of a polling loop.

## Dev UI
- Report errors through the toast system (`postToast('error', …)` from `$lib/toasts`), not inline error text in pages, dialogs, or components.
- A toast is the sentence plus one clue line (request, code, phase, `ref` correlation id); never render exception chains or `fault_context` in the UI. The detail lives in the server log under that ref.
- Keep a button's label unchanged while its action is busy or queued — only disable it; do not swap in "homing…"/"queued"-style titles.

## Device signing keys
- Only the unanchored `data/` gitignore rule keeps signing-key files out of git — never `git add -f` one.
- Never give a device a second key under an id another key already used, and never let a locally generated key take the provisioned id `v2`: `verify_audit_chain` selects the key by the id recorded on each row, so a reused id turns a key change into a tampering report.
