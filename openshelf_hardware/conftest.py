import os
import tempfile
from pathlib import Path

# Set the test environment before any test module (from any package) imports the app.
# Test modules import at collection time, which can precede a package's own
# conftest; the app reads these at import time (mode file, config path, DB URL).
if os.environ.get("OPENSHELF_TEST_ENV_INITIALIZED") != "1":
	_tmpdir = Path(tempfile.mkdtemp(prefix="perp_os1_testdb_"))
	_db_path = _tmpdir / "test.db"
	os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
	os.environ["JWT_SECRET"] = "test-jwt-secret"
	os.environ["AUDIT_HMAC_KEY"] = "test-hmac-key"
	os.environ["ENV"] = "test"
	os.environ["OPENSHELF_ONBOARDING_STATE_PATH"] = str(_tmpdir / "onboarding_state")
	# Isolate the suite from the repo's data/ runtime state (mode file, config.json).
	os.environ["OPENSHELF_DATA_DIR"] = str(_tmpdir)
	os.environ["OPERATING_MODE"] = "development"
	os.environ["OPENSHELF_TEST_ENV_INITIALIZED"] = "1"
