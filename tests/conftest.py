"""Keep pytest completely separate from an operator's attendance database."""
import os
import tempfile
from pathlib import Path


_TEST_DB = Path(tempfile.gettempdir()) / f"attendqr-pytest-{os.getpid()}.db"
for suffix in ("", "-shm", "-wal"):
    _TEST_DB.with_name(_TEST_DB.name + suffix).unlink(missing_ok=True)

# conftest is loaded before the application modules imported by the test files.
os.environ["ATTENDQR_DB_PATH"] = str(_TEST_DB)
os.environ.pop("DATABASE_URL", None)


def pytest_sessionfinish(session, exitstatus):
    """Remove the throwaway SQLite database and its WAL sidecars."""
    for suffix in ("", "-shm", "-wal"):
        _TEST_DB.with_name(_TEST_DB.name + suffix).unlink(missing_ok=True)
