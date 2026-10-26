"""Watching a series for new volumes: POST /api/series/{name:path}/watch and
POST /api/series/watch-all, their gates, and route order (G119)."""

import json
from urllib.parse import quote

import pytest

from tests.conftest import _insert_item
from tests.test_features_gate import assert_json_403, off

HX = {"HX-Request": "true"}


def _series(db, name, n=1):
    for i in range(n):
        _insert_item(db, title=f"{name} {i + 1}", isbn=None, series_name=name)


def _meta(db, name, hc_missing, complete=None):
    db.execute(
        "INSERT INTO series_meta (name, hc_total, hc_missing, hc_checked_at, complete) "
        "VALUES (?, 3, ?, datetime('now'), ?)",
        (name, hc_missing, complete),
    )


def _watched(db):
    return sorted(r[0] for r in db.execute("SELECT series_name FROM release_watches").fetchall())


def _watch(client, name, value, **kw):
    return client.post(f"/api/series/{quote(name)}/watch", data={"watch": value}, **kw)


def _toast(resp):
    return json.loads(resp.headers["HX-Trigger"])["showToast"]


class TestWatchToggle:
    def test_watch_creates_one_row(self, editor_client, db):
        _series(db, "Dune")
        db.commit()
        resp = _watch(editor_client, "Dune", "1")
        assert resp.json() == {"ok": True, "watched": True}
        assert _watch(editor_client, "dune", "1").json()["ok"] is True  # twice, any case
        assert _watched(db) == ["Dune"]

    def test_slash_in_name_round_trips(self, editor_client, db):
        _series(db, "Foo / Bar")
        db.commit()
        assert _watch(editor_client, "Foo / Bar", "1").json()["watched"] is True
        assert _watched(db) == ["Foo / Bar"]

    def test_unwatch_removes_the_row_and_its_releases(self, editor_client, db):
        _series(db, "Dune")
        wid = db.execute("INSERT INTO release_watches (series_name) VALUES ('Dune')").lastrowid
        db.execute("INSERT INTO upcoming_releases (watch_id, hc_book_id, title) VALUES (?, 1, 'V5')", (wid,))
        db.commit()
        assert _watch(editor_client, "DUNE", "0").json() == {"ok": True, "watched": False}
        assert _watched(db) == []
        assert db.execute("SELECT COUNT(*) FROM upcoming_releases").fetchone()[0] == 0

    def test_unknown_series_is_refused(self, editor_client, db):
        resp = _watch(editor_client, "Nope", "1")
        assert resp.json() == {"ok": False, "message": "Series not found"}
        assert _watched(db) == []

    def test_bad_value_is_refused(self, editor_client, db):
        _series(db, "Dune")
        db.commit()
        assert _watch(editor_client, "Dune", "yes").json()["ok"] is False
        assert _watched(db) == []

    def test_htmx_unwatch_toasts_and_refreshes_with_an_empty_body(self, editor_client, db):
        _series(db, "Dune")
        db.execute("INSERT INTO release_watches (series_name) VALUES ('Dune')")
        db.commit()
        resp = _watch(editor_client, "Dune", "0", headers=HX)
        assert resp.status_code == 200 and resp.content == b""
        assert resp.headers["HX-Refresh"] == "true"
        assert _toast(resp) == {"message": "Stopped watching Dune", "type": "success"}

    def test_unwatch_works_after_an_item_edit_empties_the_series(self, editor_client, db):
        # Source: codex-DR-C2. The edit keeps the watch, /upcoming keeps its
        # rows, so the Stop watching button there must still succeed.
        item_id = _insert_item(db, title="Dune 1", isbn=None, series_name="Dune")
        wid = db.execute("INSERT INTO release_watches (series_name) VALUES ('Dune')").lastrowid
        db.execute("INSERT INTO upcoming_releases (watch_id, hc_book_id, title) VALUES (?, 1, 'V5')", (wid,))
        db.commit()
        editor_client.post(f"/api/items/{item_id}", data={"series_name": ""})
        assert db.execute("SELECT COUNT(*) FROM items_live WHERE series_name = 'Dune'").fetchone()[0] == 0
        assert _watched(db) == ["Dune"]
        resp = _watch(editor_client, "Dune", "0", headers=HX)
        assert _toast(resp) == {"message": "Stopped watching Dune", "type": "success"}
        assert _watched(db) == []
        assert db.execute("SELECT COUNT(*) FROM upcoming_releases").fetchone()[0] == 0

    def test_watch_guard_reads_under_write_lock(self, editor_client, db, monkeypatch):
        """Source: codex-DR-M1. G18: at the moment the live-series guard runs,
        a rival writer must already be locked out, or an item edit could
        empty the series between the guard and the insert."""
        import sqlite3
        from contextlib import contextmanager

        import app.config
        import app.routers.series as series_mod

        _series(db, "Dune")
        db.commit()
        probe_results = []
        real_get_db = series_mod.get_db

        class LockProbingConnection:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                result = self._conn.execute(sql, *args, **kwargs)
                if "FROM items_live WHERE series_name" in sql:
                    rival = sqlite3.connect(str(app.config.DATABASE_PATH), timeout=0)
                    try:
                        rival.execute("BEGIN IMMEDIATE")
                        probe_results.append("acquired")
                        rival.rollback()
                    except sqlite3.OperationalError as exc:
                        probe_results.append(f"locked: {exc}")
                    finally:
                        rival.close()
                return result

        @contextmanager
        def probing_get_db():
            with real_get_db() as conn:
                yield LockProbingConnection(conn)

        monkeypatch.setattr(series_mod, "get_db", probing_get_db)
        assert _watch(editor_client, "Dune", "1").json()["ok"] is True
        assert probe_results, "guard query never ran — the probe did not fire"
        assert probe_results[0].startswith("locked"), probe_results[0]

    def test_htmx_refusal_is_a_toast_not_a_4xx(self, editor_client, db):
        resp = _watch(editor_client, "Nope", "1", headers=HX)
        assert resp.status_code == 200 and resp.content == b""
        assert "HX-Refresh" not in resp.headers
        assert _toast(resp) == {"message": "Series not found", "type": "error"}


class TestWatchAll:
    def _post(self, client):
        return client.post("/api/series/watch-all", headers=HX)

    def test_adds_only_checked_complete_series_with_live_items(self, editor_client, db):
        for name in ("Complete", "Missing", "Unchecked", "Manual", "Gone"):
            _series(db, name)
        _meta(db, "Complete", 0)
        _meta(db, "Missing", 2)
        db.execute("INSERT INTO series_meta (name, description) VALUES ('Unchecked', 'x')")
        _meta(db, "Manual", 1, complete=1)  # manual "complete" does not count
        _meta(db, "Gone", 0)
        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE series_name = 'Gone'")
        _meta(db, "No items", 0)
        db.commit()
        resp = self._post(editor_client)
        assert resp.status_code == 200 and resp.content == b""
        assert resp.headers["HX-Refresh"] == "true"
        assert _toast(resp)["message"] == "Watching 1 more series"
        assert _watched(db) == ["Complete"]

    def test_is_idempotent_and_says_so(self, editor_client, db):
        _series(db, "Complete")
        _meta(db, "Complete", 0)
        db.commit()
        self._post(editor_client)
        resp = self._post(editor_client)
        assert resp.status_code == 200 and resp.content == b""
        assert "HX-Refresh" not in resp.headers
        assert _toast(resp)["message"] == "All 1 complete series are already watched"
        assert _watched(db) == ["Complete"]

    def test_nothing_checked_says_to_check_completeness(self, editor_client, db):
        _series(db, "Missing")
        _meta(db, "Missing", 2)
        db.commit()
        resp = self._post(editor_client)
        assert resp.status_code == 200 and "HX-Refresh" not in resp.headers
        assert _toast(resp)["message"] == (
            "No checked, complete series to add — run Check completeness first")
        assert _watched(db) == []

    def test_route_order_reaches_the_watch_all_handler(self, client):
        from starlette.routing import Match

        from app.main import app
        from app.routers.series import watch_all_complete_series

        scope = {"type": "http", "method": "POST", "path": "/api/series/watch-all",
                 "root_path": ""}
        first = next(r for r in app.router.routes
                     if hasattr(r, "matches") and r.matches(scope)[0] == Match.FULL)
        assert first.endpoint is watch_all_complete_series


class TestGates:
    ROUTES = [
        ("/api/series/Dune/watch", {"watch": "1"}),
        ("/api/series/watch-all", None),
    ]

    @pytest.mark.parametrize("path,data", ROUTES)
    def test_viewer_is_refused(self, viewer_client, db, path, data):
        _series(db, "Dune")
        db.commit()
        assert viewer_client.post(path, data=data).status_code == 403
        assert _watched(db) == []

    @pytest.mark.parametrize("path,data", ROUTES)
    def test_anonymous_is_redirected_to_login(self, client, admin_user, path, data):
        resp = client.post(path, data=data, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login"

    @pytest.mark.parametrize("path,data", ROUTES)
    @pytest.mark.parametrize("key,label", [
        ("release_radar", "Release radar"), ("hardcover", "Hardcover"), ("series", "Series"),
    ])
    def test_refused_with_a_flag_off(self, editor_client, db, path, data, key, label):
        _series(db, "Dune")
        _meta(db, "Dune", 0)
        db.commit()
        off(key)
        assert_json_403(editor_client.post(path, data=data), key, label)
        assert _watched(db) == []
