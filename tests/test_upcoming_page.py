"""The Upcoming releases page and Dismiss: GET /upcoming and
POST /api/upcoming/{release_id}/dismiss."""

import json
import re

import pytest

from tests.conftest import _insert_item
from tests.test_features_gate import NAV, assert_disabled_page, assert_json_403, off

HX = {"HX-Request": "true"}


def _watch(db, name):
    _insert_item(db, title=f"{name} 1", isbn=None, series_name=name, owned=1)
    return db.execute("INSERT INTO release_watches (series_name) VALUES (?)", (name,)).lastrowid


def _release(db, wid, hc_id, title, release_date=None, pos=None, cover=None, dismissed=0):
    return db.execute(
        "INSERT INTO upcoming_releases (watch_id, hc_book_id, title, authors, cover_url, "
        "series_position, release_date, dismissed) VALUES (?, ?, ?, 'A. Writer', ?, ?, ?, ?)",
        (wid, hc_id, title, cover, pos, release_date, dismissed),
    ).lastrowid


def _titles(html):
    return re.findall(r'data-testid="upcoming-row-title">([^<]*)<', html)


def _tag(html, testid):
    m = re.search(rf'<[a-z]+\b[^>]*data-testid="{testid}"[^>]*>', html, re.S)
    assert m, testid
    return m.group(0)


@pytest.fixture
def seeded(db):
    wid = _watch(db, "Dune")
    other = _watch(db, "Expanse")
    ids = {
        "later": _release(db, other, 1, "Expanse 10", "2027-03-01", pos=10.0),
        "sooner": _release(db, wid, 2, "Dune 7", "2026-11-15", pos=7.0,
                            cover="https://assets.hardcover.app/d7.jpg"),
        "undated": _release(db, wid, 3, "Dune 8", None, pos=7.5),
        "dismissed": _release(db, wid, 4, "Dune Dismissed", "2026-12-01", dismissed=1),
        "owned": _release(db, wid, 5, "Dune Owned", "2026-12-02"),
        "wished": _release(db, wid, 6, "Dune Wished", "2026-12-03"),
    }
    _insert_item(db, title="dune owned", isbn=None, series_name="Dune", owned=1)
    _insert_item(db, title="Elsewhere", isbn=None, series_name="Other", owned=0,
                 hardcover_book_id=6, wishlisted=True)
    db.commit()
    return ids


class TestPage:
    def test_viewer_reads_rows_dated_first_undated_last(self, viewer_client, seeded):
        resp = viewer_client.get("/upcoming")
        assert resp.status_code == 200
        assert _titles(resp.text) == ["Dune 7", "Expanse 10", "Dune 8"]
        assert "announced — date TBA" in resp.text
        assert "2026-11-15" in resp.text
        assert "Dune #7" in resp.text and "Dune #7.5" in resp.text
        assert 'data-testid="upcoming-attribution">Powered by Hardcover' in resp.text

    def test_dismissed_and_held_rows_are_absent(self, viewer_client, seeded):
        html = viewer_client.get("/upcoming").text
        for gone in ("Dune Dismissed", "Dune Owned", "Dune Wished"):
            assert gone not in html

    def test_cover_or_placeholder_carries_its_shape(self, viewer_client, seeded):
        html = viewer_client.get("/upcoming").text
        assert re.search(r'<img src="https://assets.hardcover.app/d7.jpg"[^>]*data-cover-shape=', html)
        assert html.count("data-cover-shape=") == 3

    def test_viewer_sees_no_actions(self, viewer_client, seeded):
        html = viewer_client.get("/upcoming").text
        for testid in ("upcoming-row-wishlist", "upcoming-row-dismiss", "upcoming-row-stop"):
            assert testid not in html
        assert 'x-data="upcomingRow"' not in html

    def test_editor_sees_three_actions_per_row(self, editor_client, seeded):
        html = editor_client.get("/upcoming").text
        for testid in ("upcoming-row-wishlist", "upcoming-row-dismiss", "upcoming-row-stop"):
            assert html.count(f'data-testid="{testid}"') == 3

    def test_wishlist_row_carries_the_add_to_shelf_payload(self, editor_client, seeded):
        html = editor_client.get("/upcoming").text
        row = _tag(html, "upcoming-row")  # first row: Dune 7
        assert 'x-data="upcomingRow"' in row
        for attr in ('data-title="Dune 7"', 'data-authors="A. Writer"',
                     'data-cover-url="https://assets.hardcover.app/d7.jpg"',
                     'data-hardcover-book-id="2"', 'data-series-name="Dune"',
                     'data-series-position="7.0"'):
            assert attr in row

    def test_dismiss_swaps_its_row_and_stop_watching_swaps_nothing(self, editor_client, seeded):
        html = editor_client.get("/upcoming").text
        dismiss = _tag(html, "upcoming-row-dismiss")
        assert f'hx-post="/api/upcoming/{seeded["sooner"]}/dismiss"' in dismiss
        assert 'hx-target="closest li"' in dismiss and 'hx-swap="outerHTML"' in dismiss
        stop = _tag(html, "upcoming-row-stop")
        assert 'hx-post="/api/series/Dune/watch"' in stop
        assert 'hx-swap="none"' in stop and "hx-target" not in stop  # G54

    def test_stop_watching_url_encodes_the_series_name(self, editor_client, db):
        wid = _watch(db, "Foo & Bar")
        _release(db, wid, 1, "Foo 2", "2026-12-01")
        db.commit()
        html = editor_client.get("/upcoming").text
        stop = _tag(html, "upcoming-row-stop")
        assert 'hx-post="/api/series/Foo%20%26%20Bar/watch"' in stop
        # No position: the series name stands alone.
        assert '<div class="text-sm text-shelf-muted break-words">Foo &amp; Bar</div>' in html

    def test_empty_state(self, viewer_client, db):
        html = viewer_client.get("/upcoming").text
        assert 'data-testid="upcoming-empty"' in html
        assert "No announcements found for your watched series." in html
        assert "not the same as nothing coming" in html
        assert "Powered by Hardcover" in html

    @pytest.mark.parametrize("key,label", [("release_radar", "Release radar"),
                                           ("hardcover", "Hardcover")])
    def test_refused_with_a_flag_off(self, viewer_client, seeded, key, label):
        off(key)
        resp = viewer_client.get("/upcoming", headers=NAV)
        assert_disabled_page(resp, label, enable_form=False)

    def test_anonymous_is_redirected(self, client, admin_user):
        resp = client.get("/upcoming", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login"

    def test_route_order_reaches_the_page(self, client):
        from starlette.routing import Match

        from app.main import app
        from app.routers.upcoming import upcoming_page

        scope = {"type": "http", "method": "GET", "path": "/upcoming", "root_path": ""}
        first = next(r for r in app.router.routes
                     if hasattr(r, "matches") and r.matches(scope)[0] == Match.FULL)
        assert first.endpoint is upcoming_page


class TestDismiss:
    def test_dismiss_sets_the_flag_and_answers_empty(self, editor_client, db, seeded):
        resp = editor_client.post(f"/api/upcoming/{seeded['sooner']}/dismiss", headers=HX)
        assert resp.status_code == 200 and resp.content == b""
        row = db.execute("SELECT dismissed FROM upcoming_releases WHERE id = ?",
                         (seeded["sooner"],)).fetchone()
        assert row["dismissed"] == 1
        assert "Dune 7" not in editor_client.get("/upcoming").text

    def test_missing_id_is_a_404_with_a_toast(self, editor_client, db):
        resp = editor_client.post("/api/upcoming/9999/dismiss", headers=HX)
        assert resp.status_code == 404
        assert json.loads(resp.headers["HX-Trigger"]) == {
            "showToast": {"message": "That announcement is gone", "type": "error"}}

    def test_viewer_is_refused(self, viewer_client, db, seeded):
        assert viewer_client.post(f"/api/upcoming/{seeded['sooner']}/dismiss").status_code == 403
        assert db.execute("SELECT SUM(dismissed) FROM upcoming_releases").fetchone()[0] == 1

    @pytest.mark.parametrize("key,label", [("release_radar", "Release radar"),
                                           ("hardcover", "Hardcover")])
    def test_refused_with_a_flag_off(self, editor_client, db, seeded, key, label):
        off(key)
        assert_json_403(editor_client.post(f"/api/upcoming/{seeded['sooner']}/dismiss"), key, label)
