from tests.conftest import _fingerprint_if_present


def test_missing_production_database_does_not_require_a_fingerprint(tmp_path) -> None:
    assert _fingerprint_if_present(tmp_path / "revenue_recovery.db") is None
