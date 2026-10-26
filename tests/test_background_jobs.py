"""T8: each of the five background jobs skips its pass while its feature is
off — no service call, no last-sync/last-sent stamp written — and resumes on
the very next pass once the feature is turned back on (no restart needed).

See tests/test_loan_reminders.py for the pre-existing digest-task tests,
which this file leaves unchanged; it only adds feature-gate coverage on top.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.features import set_feature_enabled
from tests.conftest import _insert_borrower, _insert_item


def _set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )


def _stamp(db, key):
    return db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()


def _toggle(key, enabled):
    """Flip a feature flag on its own committed connection.

    `set_feature_enabled` drops the flag/nav caches itself, but only once its
    write actually commits — a fresh `get_db()` connection (rather than the
    test's own `db` fixture connection, which stays open/uncommitted until
    teardown) is what makes the flip visible to the pass under test.
    """
    from app.database import get_db
    with get_db() as conn:
        set_feature_enabled(conn, key, enabled)


class TestAbsSyncPassFeatureGate:
    def _seed(self, db):
        _set_setting(db, "abs_sync_interval", "daily")
        _set_setting(db, "abs_url", "https://abs.example")
        _set_setting(db, "abs_token", "tok")
        db.execute("COMMIT")

    @pytest.mark.asyncio
    async def test_off_skips_sync_and_writes_no_stamp(self, db):
        from app.main import _abs_sync_pass
        self._seed(db)
        _toggle("abs_sync", False)

        with patch("app.services.audiobookshelf.sync", new=AsyncMock()) as sync:
            assert await _abs_sync_pass() is False
        sync.assert_not_awaited()
        assert _stamp(db, "abs_last_sync") is None

    @pytest.mark.asyncio
    async def test_on_after_off_syncs_and_writes_stamp(self, db):
        from app.main import _abs_sync_pass
        self._seed(db)
        _toggle("abs_sync", False)
        with patch("app.services.audiobookshelf.sync", new=AsyncMock()) as sync:
            assert await _abs_sync_pass() is False
        sync.assert_not_awaited()
        assert _stamp(db, "abs_last_sync") is None

        _toggle("abs_sync", True)
        with patch("app.services.audiobookshelf.sync", new=AsyncMock()) as sync:
            assert await _abs_sync_pass() is True
        sync.assert_awaited_once()
        assert _stamp(db, "abs_last_sync") is not None


class TestHardcoverSyncPassFeatureGate:
    def _seed(self, db):
        _set_setting(db, "hc_sync_interval", "daily")
        _set_setting(db, "hardcover_token", "tok")
        db.execute("COMMIT")

    @pytest.mark.asyncio
    async def test_off_skips_sync_and_writes_no_stamp(self, db):
        from app.main import _hardcover_sync_pass
        self._seed(db)
        _toggle("hardcover", False)

        with patch("app.services.hardcover.sync_reading_statuses", new=AsyncMock()) as sync:
            assert await _hardcover_sync_pass() is False
        sync.assert_not_awaited()
        assert _stamp(db, "hc_last_sync") is None

    @pytest.mark.asyncio
    async def test_on_after_off_syncs_and_writes_stamp(self, db):
        from app.main import _hardcover_sync_pass
        self._seed(db)
        _toggle("hardcover", False)
        with patch("app.services.hardcover.sync_reading_statuses", new=AsyncMock()) as sync:
            assert await _hardcover_sync_pass() is False
        sync.assert_not_awaited()
        assert _stamp(db, "hc_last_sync") is None

        _toggle("hardcover", True)
        with patch("app.services.hardcover.sync_reading_statuses", new=AsyncMock()) as sync:
            assert await _hardcover_sync_pass() is True
        sync.assert_awaited_once()
        assert _stamp(db, "hc_last_sync") is not None


class TestLoanRemindersFeatureGate:
    def _seed_overdue(self, db):
        item_id = _insert_item(db, title="Very Late Book", isbn="9789000002019")
        borrower_id = _insert_borrower(db, name="Slow Reader")
        db.execute(
            "INSERT INTO checkouts (item_id, borrower_id, checked_out, due_date, checked_in) "
            "VALUES (?, ?, datetime('now', '-60 days'), NULL, NULL)",
            (item_id, borrower_id),
        )
        _set_setting(db, "notify_url", "https://ntfy.example/shelf")
        db.execute("COMMIT")

    @pytest.mark.asyncio
    async def test_off_skips_send_and_writes_no_stamp(self, db):
        from app.main import check_loan_reminders
        self._seed_overdue(db)
        _toggle("lending", False)

        with patch("app.services.notify.send_notification", new=AsyncMock()) as send:
            assert await check_loan_reminders() is False
        send.assert_not_awaited()
        assert _stamp(db, "loan_reminder_last_sent") is None

    @pytest.mark.asyncio
    async def test_on_after_off_sends_and_writes_stamp(self, db):
        from app.main import check_loan_reminders
        self._seed_overdue(db)
        _toggle("lending", False)
        with patch("app.services.notify.send_notification", new=AsyncMock()) as send:
            assert await check_loan_reminders() is False
        send.assert_not_awaited()
        assert _stamp(db, "loan_reminder_last_sent") is None

        _toggle("lending", True)
        with patch("app.services.notify.send_notification", new=AsyncMock(return_value=True)) as send:
            assert await check_loan_reminders() is True
        send.assert_awaited_once()
        assert _stamp(db, "loan_reminder_last_sent") is not None


class TestPriceAlertsFeatureGate:
    def _seed(self, db):
        _insert_item(db, title="Wanted Book", isbn="9780306406157", wishlisted=True)
        _set_setting(db, "isbndb_api_key", "test-key")
        db.execute("COMMIT")

    @pytest.fixture(autouse=True)
    def _no_cache_file(self):
        # isbndb.CACHE_FILE is frozen at import and not redirected by conftest.
        with patch("app.services.isbndb._load_cache", return_value={}), \
             patch("app.services.isbndb._save_cache"):
            yield

    @pytest.mark.asyncio
    async def test_off_skips_lookup_and_writes_no_stamp(self, db):
        from app.main import check_price_alerts
        self._seed(db)
        _toggle("price_alerts", False)

        with patch("app.services.isbndb.lookup_price", new=AsyncMock()) as lookup:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()
        assert _stamp(db, "price_alert_last_run") is None

    @pytest.mark.asyncio
    async def test_on_after_off_runs_and_writes_stamp(self, db):
        from app.main import check_price_alerts
        self._seed(db)
        _toggle("price_alerts", False)
        with patch("app.services.isbndb.lookup_price", new=AsyncMock()) as lookup:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()

        _toggle("price_alerts", True)
        with patch("app.services.isbndb.lookup_price",
                   new=AsyncMock(return_value={"msrp": "12.99"})) as lookup:
            assert await check_price_alerts() is True
        lookup.assert_awaited_once()
        assert _stamp(db, "price_alert_last_run") is not None


class TestReleaseRadarFeatureGate:
    def _seed(self, db):
        _insert_item(db, title="Dune", isbn=None, series_name="Dune")
        db.execute("INSERT INTO release_watches (series_name) VALUES ('Dune')")
        _set_setting(db, "hardcover_token", "test-token")
        db.execute("COMMIT")

    def _found(self):
        from app.services import provider_result
        return AsyncMock(return_value=provider_result.found("hardcover", []))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["release_radar", "hardcover"])
    async def test_off_skips_lookup_and_writes_no_stamp(self, db, key):
        from app.main import check_release_radar
        self._seed(db)
        _toggle(key, False)

        with patch("app.services.hardcover.get_series_upcoming", new=self._found()) as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()
        assert _stamp(db, "release_radar_last_run") is None

    @pytest.mark.asyncio
    async def test_on_after_off_runs_and_writes_stamp(self, db):
        from app.main import check_release_radar
        self._seed(db)
        _toggle("release_radar", False)
        with patch("app.services.hardcover.get_series_upcoming", new=self._found()) as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()

        _toggle("release_radar", True)
        with patch("app.services.hardcover.get_series_upcoming", new=self._found()) as lookup:
            assert await check_release_radar() is True
        lookup.assert_awaited_once()
        assert _stamp(db, "release_radar_last_run") is not None
