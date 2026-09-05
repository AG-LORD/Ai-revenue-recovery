"""Pytest-wide isolation for the production SQLite database."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATABASE_PATH = PROJECT_ROOT / "revenue_recovery.db"
_TEST_DATABASE_DIRECTORY: Path | None = None
_PRODUCTION_DATABASE_FINGERPRINT: tuple[int, str] | None = None
_ORIGINAL_DATABASE_PATH: str | None = None


def _configure_test_database() -> None:
    global _TEST_DATABASE_DIRECTORY, _PRODUCTION_DATABASE_FINGERPRINT, _ORIGINAL_DATABASE_PATH
    _TEST_DATABASE_DIRECTORY = Path(tempfile.mkdtemp(prefix="ai-revenue-recovery-pytest-"))
    _PRODUCTION_DATABASE_FINGERPRINT = _fingerprint_if_present(PRODUCTION_DATABASE_PATH)
    _ORIGINAL_DATABASE_PATH = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(_TEST_DATABASE_DIRECTORY / "collection.db")


def _fingerprint(path: Path) -> tuple[int, str]:
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint_if_present(path: Path) -> tuple[int, str] | None:
    """Return a fingerprint only for an existing production database."""
    if not path.exists():
        return None
    return _fingerprint(path)


_configure_test_database()
from app.repositories import database as _database

_database.DATABASE_PATH = str(_TEST_DATABASE_DIRECTORY / "collection.db")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give every test an initialized merchant-aware SQLite database."""
    from app.repositories import database
    from app.repositories.merchant_migration import init_merchant_data

    database_path = tmp_path / "revenue_recovery_test.db"
    assert database_path.resolve() != PRODUCTION_DATABASE_PATH.resolve()
    monkeypatch.setattr(database, "DATABASE_PATH", str(database_path))
    database.init_db()
    init_merchant_data()
    yield database_path


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if a test modified the user's production database."""
    try:
        if _PRODUCTION_DATABASE_FINGERPRINT is not None:
            assert _PRODUCTION_DATABASE_FINGERPRINT == _fingerprint_if_present(PRODUCTION_DATABASE_PATH), (
                "Tests modified the production revenue_recovery.db"
            )
    finally:
        if _ORIGINAL_DATABASE_PATH is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = _ORIGINAL_DATABASE_PATH
        if _TEST_DATABASE_DIRECTORY is not None:
            shutil.rmtree(_TEST_DATABASE_DIRECTORY, ignore_errors=True)
