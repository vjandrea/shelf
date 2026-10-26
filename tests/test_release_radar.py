"""The daily release-radar task: `check_release_radar()` gates and stamps, and
`_periodic_release_radar()` stays offline under its test knob.

The pass itself (selection, holding, snapshot, digest) is covered in
tests/test_release_radar_service.py; this file pins the gate in `app/main.py`.
"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.services import provider_result
from tests.conftest import _insert_item


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )


def _get(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _seed(db, *, token=True):
    _insert_item(db, title="Dune", isbn=None, series_name="Dune")
    db.execute("INSERT INTO release_watches (series_name) VALUES ('Dune')")
    if token:
        _set(db, "hardcover_token", "test-token")
    db.execute("COMMIT")


def _upcoming(result=None):
    if result is None:
        result = provider_result.found("hardcover", [])
    return patch("app.services.hardcover.get_series_upcoming", new=AsyncMock(return_value=result))


def _toggle(key, enabled):
    from app.database import get_db
    from app.features import set_feature_enabled
    with get_db() as conn:  # its own committed connection, so the flip is visible
        set_feature_enabled(conn, key, enabled)


class TestGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["release_radar", "hardcover"])
    async def test_flag_off_is_a_no_op(self, db, key):
        from app.main import check_release_radar
        _seed(db)
        _toggle(key, False)
        with _upcoming() as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()
        assert _get(db, "release_radar_last_run") is None

    @pytest.mark.asyncio
    async def test_no_token_is_a_no_op(self, db, monkeypatch):
        from app.main import check_release_radar
        monkeypatch.delenv("HARDCOVER_TOKEN", raising=False)
        _seed(db, token=False)
        with _upcoming() as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()
        assert _get(db, "release_radar_last_run") is None

    @pytest.mark.asyncio
    async def test_recent_stamp_is_a_no_op(self, db):
        from app.main import check_release_radar
        _seed(db)
        _set(db, "release_radar_last_run", str(time.time() - 3600))
        db.execute("COMMIT")
        with _upcoming() as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_day_old_stamp_runs(self, db):
        from app.main import check_release_radar
        _seed(db)
        _set(db, "release_radar_last_run", str(time.time() - 86401))
        db.execute("COMMIT")
        with _upcoming() as lookup:
            assert await check_release_radar() is True
        lookup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_stamp_runs(self, db):
        from app.main import check_release_radar
        _seed(db)
        _set(db, "release_radar_last_run", "not-a-number")
        db.execute("COMMIT")
        with _upcoming() as lookup:
            assert await check_release_radar() is True
        lookup.assert_awaited_once()
        assert float(_get(db, "release_radar_last_run")) > 0

    @pytest.mark.asyncio
    async def test_env_only_token_enables_the_pass(self, db, monkeypatch):
        from app.main import check_release_radar
        _seed(db, token=False)
        monkeypatch.setenv("HARDCOVER_TOKEN", "env-token")
        with _upcoming() as lookup:
            assert await check_release_radar() is True
        assert lookup.await_args.args[:2] == ("Dune", "env-token")


class TestStamp:
    @pytest.mark.asyncio
    async def test_run_writes_stamp_and_summary(self, db):
        from app.main import check_release_radar
        _seed(db)
        before = time.time()
        with _upcoming():
            assert await check_release_radar() is True
        assert float(_get(db, "release_radar_last_run")) >= before - 1
        summary = json.loads(_get(db, "release_radar_last_summary"))
        assert summary == {"checked": 1, "not_found": 0, "failed": 0, "outcome": "complete",
                           "new": 0, "reminded": 0, "sent": False}

    @pytest.mark.asyncio
    async def test_a_refused_token_still_stamps_and_names_why(self, db):
        from app.main import check_release_radar
        _seed(db)
        with _upcoming(provider_result.rejected("hardcover", status=401)):
            assert await check_release_radar() is True
        assert _get(db, "release_radar_last_run") is not None
        assert json.loads(_get(db, "release_radar_last_summary"))["outcome"] == "rejected"
        # And the next wake inside the day does not spend another call.
        with _upcoming() as lookup:
            assert await check_release_radar() is False
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cap_and_notify_settings_reach_the_pass(self, db):
        from app.main import check_release_radar
        _seed(db)
        _set(db, "release_radar_cap", "7")
        _set(db, "notify_format", "webhook")
        db.execute("COMMIT")
        run = AsyncMock(return_value={"checked": 0, "not_found": 0, "failed": 0,
                                      "outcome": "complete", "new": 0, "reminded": 0,
                                      "sent": False})
        with patch("app.services.release_radar.run_pass", new=run):
            assert await check_release_radar() is True
        assert run.await_args.args == ("test-token",)
        assert run.await_args.kwargs["cap"] == 7
        assert run.await_args.kwargs["notify_format"] == "webhook"


class TestPeriodicLoop:
    @pytest.mark.asyncio
    async def test_returns_before_sleeping_under_the_test_knob(self, monkeypatch):
        from app.main import _periodic_release_radar
        monkeypatch.setenv("SHELF_DISABLE_RELEASE_RADAR", "1")
        with patch("app.main.asyncio.sleep", new=AsyncMock()) as sleep, \
             patch("app.main.check_release_radar", new=AsyncMock()) as check:
            await _periodic_release_radar()
        sleep.assert_not_awaited()
        check.assert_not_awaited()
