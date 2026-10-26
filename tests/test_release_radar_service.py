"""The release-radar service: watch selection, holding, the snapshot, the digest, the pass."""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.services import provider_result, release_radar
from tests.conftest import _insert_item

TODAY = date(2026, 10, 22)


def _watch(db, name, checked_at=None, state=None):
    cur = db.execute(
        "INSERT INTO release_watches (series_name, checked_at, check_state) VALUES (?, ?, ?)",
        (name, checked_at, state),
    )
    return cur.lastrowid


def _series_item(db, series, n, **kw):
    return _insert_item(db, title=kw.pop("title", f"{series} {n}"),
                        isbn=kw.pop("isbn", None), series_name=series, **kw)


def _hc(book_id, title, release_date=None, release_year=None, pos=None, cover=None):
    return {"hardcover_book_id": book_id, "title": title, "authors": "A. Writer",
            "cover_url": cover, "series_position": pos,
            "release_date": release_date, "release_year": release_year}


def _found(rows):
    return provider_result.found("hardcover", rows)


def _rows(db):
    return [dict(r) for r in db.execute(
        "SELECT * FROM upcoming_releases ORDER BY id").fetchall()]


def _seed_watched(db, series="Dune"):
    _series_item(db, series, 1)
    wid = _watch(db, series)
    db.commit()
    return wid


class TestCap:
    def test_default_when_missing(self, db):
        assert release_radar.get_cap(db) == 50

    @pytest.mark.parametrize("raw,expected", [
        ("1", 1), ("200", 200), ("75", 75), ("0", 50), ("201", 50), ("-1", 50), ("abc", 50),
    ])
    def test_bounds(self, db, raw, expected):
        db.execute("INSERT INTO settings (key, value) VALUES ('release_radar_cap', ?)", (raw,))
        assert release_radar.get_cap(db) == expected


class TestSelectWatches:
    def _names(self, rows):
        return [r["series_name"] for r in rows]

    def test_never_checked_first_then_oldest_then_id(self, db):
        for s in ("A", "B", "C", "D"):
            _series_item(db, s, 1)
        _watch(db, "A", "2026-10-02 00:00:00")
        _watch(db, "B")
        _watch(db, "C", "2026-10-01 00:00:00")
        _watch(db, "D")
        assert self._names(release_radar.select_watches(db, 10)) == ["B", "D", "C", "A"]

    def test_ties_break_by_id(self, db):
        for s in ("A", "B"):
            _series_item(db, s, 1)
        _watch(db, "B", "2026-10-01 00:00:00")
        _watch(db, "A", "2026-10-01 00:00:00")
        assert self._names(release_radar.select_watches(db, 10)) == ["B", "A"]

    def test_cap(self, db):
        for s in ("A", "B", "C"):
            _series_item(db, s, 1)
            _watch(db, s)
        assert len(release_radar.select_watches(db, 2)) == 2

    def test_watch_with_no_live_item_is_skipped(self, db):
        gone = _series_item(db, "Gone", 1)
        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (gone,))
        _watch(db, "Gone")
        _watch(db, "Nothing at all")
        _series_item(db, "dune", 1)
        _watch(db, "Dune")  # case-insensitive match to the item's series
        assert self._names(release_radar.select_watches(db, 10)) == ["Dune"]


class TestQualifies:
    @pytest.mark.parametrize("row,expected", [
        (_hc(1, "x", release_date=(TODAY - timedelta(days=30)).isoformat()), True),
        (_hc(1, "x", release_date=(TODAY - timedelta(days=31)).isoformat()), False),
        (_hc(1, "x", release_date="2027-03-01"), True),
        (_hc(1, "x", release_year=TODAY.year), True),
        (_hc(1, "x", release_year=TODAY.year + 1), True),
        (_hc(1, "x", release_year=TODAY.year - 1), False),
        (_hc(1, "x"), False),
    ])
    def test_edges(self, row, expected):
        assert release_radar.qualifies(row, TODAY) is expected


class TestHeld:
    def test_owned_or_wishlisted_by_id_anywhere_or_title_in_series(self, db):
        _series_item(db, "Dune", 1, title="Dune Messiah", owned=1)
        _series_item(db, "Other", 1, hardcover_book_id=77, owned=1)
        _series_item(db, "Dune", 2, title="Children of Dune", owned=0, wishlisted=True)
        _series_item(db, "Dune", 3, title="God Emperor", owned=0)  # neither: does not hold
        _series_item(db, "Elsewhere", 1, title="Heretics", owned=1)  # other series: title does not hold
        ids, titles = release_radar.held_keys(db, "dune")
        assert 77 in ids
        assert {"dune messiah", "children of dune"} <= titles
        assert "god emperor" not in titles and "heretics" not in titles

    def test_trashed_item_does_not_hold(self, db):
        gone = _series_item(db, "Dune", 1, title="Heretics", owned=1, hardcover_book_id=5)
        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (gone,))
        ids, titles = release_radar.held_keys(db, "Dune")
        assert 5 not in ids and "heretics" not in titles


class TestUpsert:
    def test_insert_then_refresh_keeps_flags(self, db):
        wid = _watch(db, "Dune")
        release_radar.upsert_rows(db, wid, [_hc(1, "Vol 5", "2026-12-06", pos=5.0)])
        db.execute("UPDATE upcoming_releases SET notified_at = 'x', reminded_at = 'y', dismissed = 1")
        release_radar.upsert_rows(db, wid, [_hc(1, "Vol 5 (retitled)", "2027-01-10", pos=5.0)])
        [r] = _rows(db)
        assert (r["title"], r["release_date"]) == ("Vol 5 (retitled)", "2027-01-10")
        assert (r["notified_at"], r["reminded_at"], r["dismissed"]) == ("x", "y", 1)

    @pytest.mark.parametrize("url,stored", [
        ("https://assets.hardcover.app/x.jpg", "https://assets.hardcover.app/x.jpg"),
        ("http://assets.hardcover.app/x.jpg", None),
        ("//assets.hardcover.app/x.jpg", None),
        (None, None),
    ])
    def test_only_https_covers_are_stored(self, db, url, stored):
        wid = _watch(db, "Dune")
        release_radar.upsert_rows(db, wid, [_hc(1, "Vol 5", "2026-12-06", cover=url)])
        assert _rows(db)[0]["cover_url"] == stored


class TestDigest:
    def test_lines_and_title(self):
        new = [{"title": "Vol 5", "series_name": "Dune", "series_position": 5.0,
                "release_date": "2026-12-06"},
               {"title": "Vol 5.5", "series_name": "Dune", "series_position": 5.5,
                "release_date": None},
               {"title": "Side", "series_name": "Dune", "series_position": None,
                "release_date": None}]
        week = [{"title": "Vol 4", "series_name": "Dune", "series_position": 4,
                 "release_date": "2026-10-25"}]
        title, body = release_radar.build_digest(new, week)
        assert title == "Shelf: 4 release updates"
        assert body == (
            "New:\nVol 5 — Dune #5 — 2026-12-06\nVol 5.5 — Dune #5.5 — date TBA\n"
            "Side — Dune — date TBA\n\nOut this week:\nVol 4 — Dune #4 — 2026-10-25"
        )

    def test_singular_title_and_omitted_section(self):
        title, body = release_radar.build_digest(
            [], [{"title": "Vol 4", "series_name": "Dune", "series_position": None,
                  "release_date": "2026-10-25"}])
        assert title == "Shelf: 1 release update"
        assert body == "Out this week:\nVol 4 — Dune — 2026-10-25"


class TestPrune:
    def test_old_undismissed_go_dismissed_and_recent_stay(self, db):
        wid = _watch(db, "Dune")
        old = (TODAY - timedelta(days=61)).isoformat()
        recent = (TODAY - timedelta(days=59)).isoformat()
        release_radar.upsert_rows(db, wid, [_hc(1, "Old", old), _hc(2, "Old dismissed", old),
                                            _hc(3, "Recent", recent), _hc(4, "Undated")])
        db.execute("UPDATE upcoming_releases SET dismissed = 1 WHERE hc_book_id = 2")
        assert release_radar.prune(db, TODAY) == 1
        assert sorted(r["title"] for r in _rows(db)) == ["Old dismissed", "Recent", "Undated"]


class TestDescribeLastRun:
    def test_never(self):
        assert release_radar.describe_last_run(None, None) == "Never"
        assert release_radar.describe_last_run("garbage", None) == "Never"

    def test_summary(self):
        stamp = "1792627200"  # 2026-10-22 00:00 UTC
        summary = json.dumps({"checked": 4, "not_found": 1, "failed": 2, "outcome": "rejected",
                              "new": 0, "reminded": 0, "sent": False})
        assert release_radar.describe_last_run(stamp, summary) == (
            "2026-10-22 00:00 UTC — 4 checked, 1 not found on Hardcover, "
            "2 failed (no response from Hardcover), stopped: Hardcover refused the token"
        )

    def test_unreadable_summary_shows_time_alone(self):
        assert release_radar.describe_last_run("1792627200", "{") == "2026-10-22 00:00 UTC"


class TestRunPass:
    async def _run(self, upcoming, send=None, **kw):
        args = {"cap": 50, "notify_url": "http://n.test/t", "notify_format": "ntfy",
                "today": TODAY}
        args.update(kw)
        send = send or AsyncMock(return_value=True)
        with patch("app.services.hardcover.get_series_upcoming", upcoming), \
             patch("app.services.notify.send_notification", send):
            result = await release_radar.run_pass("tok", **args)
        return result, send

    async def test_new_volume_one_row_one_line_then_quiet(self, db):
        wid = _seed_watched(db)
        upcoming = AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06", pos=5)]))
        result, send = await self._run(upcoming)
        assert result == {"checked": 1, "not_found": 0, "failed": 0, "outcome": "complete",
                          "new": 1, "reminded": 0, "sent": True}
        url, title, body, fmt = send.await_args.args
        assert (url, fmt, title) == ("http://n.test/t", "ntfy", "Shelf: 1 release update")
        assert body == "New:\nVol 5 — Dune #5 — 2026-12-06"
        [row] = _rows(db)
        assert row["watch_id"] == wid and row["notified_at"] is not None
        assert row["reminded_at"] is None
        w = db.execute("SELECT check_state, checked_at FROM release_watches").fetchone()
        assert w["check_state"] == "ok" and w["checked_at"] is not None

        result, send = await self._run(upcoming)
        assert result["new"] == 0 and result["sent"] is False
        assert send.await_count == 0
        assert len(_rows(db)) == 1

    async def test_date_slip_updates_without_a_line(self, db):
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")])))
        result, send = await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2027-02-01")])))
        assert send.await_count == 0 and result["new"] == 0
        assert _rows(db)[0]["release_date"] == "2027-02-01"

    async def test_slip_past_the_window_still_refreshes_a_stored_row(self, db):
        # Source: codex-DR-C1. 2026-01-01 is far older than the 30-day admit
        # window; a stored row must take the date anyway, then prune.
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")])))
        result, send = await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-01-01")])))
        assert send.await_count == 0 and result["new"] == 0
        assert _rows(db) == []  # refreshed to 2026-01-01, then pruned (> 60 days old)

    async def test_slip_inside_the_prune_window_keeps_the_refreshed_date(self, db):
        # Source: codex-DR-C1. 31 days back: outside admit, inside prune.
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")])))
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-09-21")])))
        assert _rows(db)[0]["release_date"] == "2026-09-21"

    async def test_an_old_book_hardcover_returns_is_still_not_admitted(self, db):
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-09-21")])))
        assert _rows(db) == []

    async def test_release_week_reminder_fires_once_for_an_announced_row(self, db):
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-11-20")])))
        # The date moves into this week: one reminder, then nothing.
        soon = (TODAY + timedelta(days=3)).isoformat()
        upcoming = AsyncMock(return_value=_found([_hc(10, "Vol 5", soon)]))
        result, send = await self._run(upcoming)
        assert result["reminded"] == 1 and result["new"] == 0
        assert send.await_args.args[2] == f"Out this week:\nVol 5 — Dune — {soon}"
        result, send = await self._run(upcoming)
        assert send.await_count == 0 and result["reminded"] == 0

    async def test_new_row_dated_this_week_is_new_only_and_stamped_both(self, db):
        _seed_watched(db)
        soon = (TODAY + timedelta(days=2)).isoformat()
        upcoming = AsyncMock(return_value=_found([_hc(10, "Vol 5", soon)]))
        result, send = await self._run(upcoming)
        assert (result["new"], result["reminded"]) == (1, 0)
        assert "Out this week" not in send.await_args.args[2]
        row = _rows(db)[0]
        assert row["notified_at"] is not None and row["reminded_at"] is not None
        result, send = await self._run(upcoming)
        assert send.await_count == 0

    async def test_held_rows_are_never_inserted_or_digested(self, db):
        _series_item(db, "Dune", 1, title="Dune Messiah", owned=1)
        _series_item(db, "Dune", 2, title="Children of Dune", owned=0, wishlisted=True)
        _series_item(db, "Other", 1, title="Unrelated", hardcover_book_id=77, owned=1)
        _series_item(db, "Dune", 3, title="God Emperor", owned=0)  # neither
        _watch(db, "Dune")
        db.commit()
        upcoming = AsyncMock(return_value=_found([
            _hc(1, "DUNE MESSIAH ", "2026-12-01"),
            _hc(2, "Children of Dune", "2026-12-02"),
            _hc(77, "Some edition", "2026-12-03"),
            _hc(4, "God Emperor", "2026-12-04"),
        ]))
        result, send = await self._run(upcoming)
        assert [r["title"] for r in _rows(db)] == ["God Emperor"]
        assert result["new"] == 1 and "God Emperor" in send.await_args.args[2]
        assert [r["title"] for r in release_radar.visible_rows(db)] == ["God Emperor"]

    async def test_row_that_becomes_held_disappears_from_digest_and_page(self, db):
        _seed_watched(db)
        await self._run(AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-11-20")])), notify_url="")
        assert len(release_radar.visible_rows(db)) == 1
        _series_item(db, "Dune", 5, title="Vol 5", owned=0, wishlisted=True)
        db.commit()
        assert release_radar.visible_rows(db) == []
        new, week = release_radar.digest_rows(db, TODAY)
        assert new == [] and week == []

    async def test_dismissed_row_survives_upsert_and_stays_out(self, db):
        _seed_watched(db)
        upcoming = AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")]))
        await self._run(upcoming, notify_url="")
        db.execute("UPDATE upcoming_releases SET dismissed = 1, notified_at = NULL")
        db.commit()
        result, send = await self._run(upcoming)
        assert send.await_count == 0 and result["new"] == 0
        assert _rows(db)[0]["dismissed"] == 1
        assert release_radar.visible_rows(db) == []

    async def test_unqualified_rows_are_not_recorded(self, db):
        _seed_watched(db)
        upcoming = AsyncMock(return_value=_found([
            _hc(1, "Long out", "2020-01-01"), _hc(2, "Undated old", release_year=2019),
            _hc(3, "No date at all"), _hc(4, "Undated this year", release_year=TODAY.year),
        ]))
        await self._run(upcoming, notify_url="")
        assert [r["title"] for r in _rows(db)] == ["Undated this year"]

    async def test_no_match_marks_not_found(self, db):
        _seed_watched(db)
        result, _ = await self._run(AsyncMock(return_value=provider_result.no_match("hardcover")))
        assert (result["checked"], result["not_found"]) == (1, 1)
        w = db.execute("SELECT check_state, checked_at FROM release_watches").fetchone()
        assert w["check_state"] == "not_found" and w["checked_at"] is not None

    async def test_transport_failure_records_nothing_and_the_next_watch_runs(self, db):
        _series_item(db, "A", 1)
        _series_item(db, "B", 1)
        _watch(db, "A")
        _watch(db, "B")
        db.commit()
        upcoming = AsyncMock(side_effect=[
            provider_result.ProviderResult("transport_failed", "hardcover"),
            _found([_hc(1, "B vol 2", "2026-12-01")]),
        ])
        result, _ = await self._run(upcoming, notify_url="")
        assert upcoming.await_count == 2
        assert (result["failed"], result["checked"], result["outcome"]) == (1, 1, "complete")
        a = db.execute("SELECT checked_at, check_state FROM release_watches WHERE series_name = 'A'").fetchone()
        assert a["checked_at"] is None and a["check_state"] is None
        assert [r["title"] for r in _rows(db)] == ["B vol 2"]

    @pytest.mark.parametrize("ending", [
        provider_result.rejected("hardcover", status=401),
        provider_result.ProviderResult("rate_limited", "hardcover", status=429),
    ])
    async def test_refused_or_rate_limited_ends_the_pass(self, db, ending):
        for s in ("A", "B"):
            _series_item(db, s, 1)
            _watch(db, s)
        db.commit()
        upcoming = AsyncMock(side_effect=[ending, _found([])])
        result, _ = await self._run(upcoming, notify_url="")
        assert upcoming.await_count == 1
        assert result["outcome"] == ending.outcome and result["checked"] == 0
        assert db.execute("SELECT COUNT(*) FROM release_watches WHERE checked_at IS NOT NULL").fetchone()[0] == 0

    async def test_no_url_stamps_without_sending(self, db):
        _seed_watched(db)
        send = AsyncMock(return_value=True)
        result, send = await self._run(
            AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")])), send=send, notify_url="")
        assert send.await_count == 0 and result["sent"] is False
        assert _rows(db)[0]["notified_at"] is not None

    async def test_failed_send_leaves_stamps_for_the_next_pass(self, db):
        _seed_watched(db)
        upcoming = AsyncMock(return_value=_found([_hc(10, "Vol 5", "2026-12-06")]))
        result, _ = await self._run(upcoming, send=AsyncMock(return_value=False))
        assert result["sent"] is False
        assert _rows(db)[0]["notified_at"] is None
        result, send = await self._run(upcoming)
        assert send.await_count == 1 and result["sent"] is True
        assert _rows(db)[0]["notified_at"] is not None

    async def test_pass_prunes(self, db):
        wid = _seed_watched(db)
        release_radar.upsert_rows(db, wid, [_hc(9, "Ancient", (TODAY - timedelta(days=90)).isoformat())])
        db.execute("UPDATE upcoming_releases SET notified_at = 'x'")
        db.commit()
        await self._run(AsyncMock(return_value=_found([])), notify_url="")
        assert _rows(db) == []


class TestVisibleRows:
    def test_dated_first_by_date_then_undated(self, db):
        a = _watch(db, "Alpha")
        b = _watch(db, "Beta")
        release_radar.upsert_rows(db, b, [_hc(1, "B later", "2027-01-01"), _hc(2, "B undated")])
        release_radar.upsert_rows(db, a, [_hc(3, "A sooner", "2026-11-01"), _hc(4, "A undated", pos=2),
                                          _hc(5, "A undated first", pos=1)])
        assert [r["title"] for r in release_radar.visible_rows(db)] == [
            "A sooner", "B later", "A undated first", "A undated", "B undated"]

    def test_limit(self, db):
        a = _watch(db, "Alpha")
        release_radar.upsert_rows(db, a, [_hc(i, f"T{i}", f"2026-11-{i:02d}") for i in range(1, 6)])
        assert len(release_radar.visible_rows(db, limit=3)) == 3
