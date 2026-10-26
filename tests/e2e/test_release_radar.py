"""E2E: the release radar — the Series card's watch button, Watch all, and the
/upcoming page (wishlist, dismiss, stop watching).

No Hardcover request is made anywhere: the radar loop is disabled in the E2E
env block (`SHELF_DISABLE_RELEASE_RADAR`), the announced rows are seeded by
direct SQLite with `cover_url` NULL (so + Wishlist's add-to-shelf makes no
outbound cover download), and no control here calls Hardcover.
"""
import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, insert_item

pytestmark = pytest.mark.e2e

SIZES = [(1280, 800), (390, 844)]
IDS = ["desktop", "phone"]


def _csrf_headers(pg):
    return {"X-CSRF-Token": pg.evaluate("() => window.csrfToken()")}


def _set_token(pg, base_url, token):
    resp = pg.request.post(
        f"{base_url}/api/settings", form={"hardcover_token": token}, headers=_csrf_headers(pg))
    assert resp.status in (200, 303), resp.status


def _clear_token(pg, base_url):
    resp = pg.request.post(
        f"{base_url}/api/settings",
        form={"hardcover_token": "", "clear_hardcover_token": "on"},
        headers=_csrf_headers(pg),
    )
    assert resp.status in (200, 303), resp.status


def _db(data_dir):
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _query(data_dir, sql, params=()):
    conn = _db(data_dir)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write(data_dir, sql, params=()):
    conn = _db(data_dir)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _seed_series(data_dir, series, isbns):
    for n, isbn in enumerate(isbns, start=1):
        insert_item(data_dir, title=f"{series} Vol {n}", isbn=isbn,
                    series_name=series, series_position=n)
    _write(data_dir, "INSERT INTO series_meta (name, hc_missing) VALUES (?, 0)", (series,))


def _seed_releases(data_dir, series, base_id):
    """Three announcements under the series' watch: dated, undated, and one
    dated earlier that the test sends to the wishlist. cover_url stays NULL."""
    watch_id = _query(data_dir, "SELECT id FROM release_watches WHERE series_name = ?",
                      (series,))[0]["id"]
    rows = [
        (base_id + 1, f"{series} Dated", "2099-01-01", 3),
        (base_id + 2, f"{series} Undated", None, 4),
        (base_id + 3, f"{series} Wish", "2098-03-01", 5),
    ]
    for hc_id, title, date, pos in rows:
        _write(
            data_dir,
            "INSERT INTO upcoming_releases (watch_id, hc_book_id, title, authors, "
            "cover_url, series_position, release_date) VALUES (?, ?, ?, 'E2E Author', NULL, ?, ?)",
            (watch_id, hc_id, title, pos, date))
    return {"dated": f"{series} Dated", "undated": f"{series} Undated",
            "wish": f"{series} Wish", "wish_hc_id": base_id + 3}


def _cleanup(data_dir, series, base_id, preexisting_watch_ids):
    """Delete everything this test seeded, plus watches Watch all added for
    other files' complete series."""
    conn = _db(data_dir)
    try:
        conn.execute("DELETE FROM upcoming_releases WHERE hc_book_id BETWEEN ? AND ?",
                     (base_id + 1, base_id + 3))
        conn.execute("DELETE FROM upcoming_releases WHERE watch_id IN "
                     "(SELECT id FROM release_watches WHERE series_name = ?)", (series,))
        conn.execute("DELETE FROM release_watches WHERE series_name = ?", (series,))
        if preexisting_watch_ids is not None:
            marks = ",".join("?" for _ in preexisting_watch_ids)
            conn.execute(
                "DELETE FROM upcoming_releases WHERE watch_id NOT IN (%s)" % (marks or "NULL"),
                list(preexisting_watch_ids))
            conn.execute(
                "DELETE FROM release_watches WHERE id NOT IN (%s)" % (marks or "NULL"),
                list(preexisting_watch_ids))
        conn.execute("DELETE FROM list_items WHERE item_id IN "
                     "(SELECT id FROM items WHERE hardcover_book_id BETWEEN ? AND ? "
                     "OR series_name = ?)", (base_id + 1, base_id + 3, series))
        conn.execute("DELETE FROM items WHERE hardcover_book_id BETWEEN ? AND ? "
                     "OR series_name = ?", (base_id + 1, base_id + 3, series))
        conn.execute("DELETE FROM series_meta WHERE name = ?", (series,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("size", SIZES, ids=IDS)
def test_release_radar_series_card_and_upcoming_page(live_server, authed_page, size):
    pg = authed_page
    base = live_server["url"]
    data_dir = live_server["data_dir"]
    width = size[0]
    series = f"RR Saga {width}"
    base_id = 9_100_000 + width * 10
    isbns = {1280: ["9789030001015", "9789030001022"],
             390: ["9789030002012", "9789030002029"]}[width]
    preexisting = [r["id"] for r in _query(data_dir, "SELECT id FROM release_watches")]
    pg.set_viewport_size({"width": size[0], "height": size[1]})

    try:
        _cleanup(data_dir, series, base_id, None)
        _set_token(pg, base, "e2e-fake-hardcover-token")
        # Seeded BEFORE the first page load of /series.
        _seed_series(data_dir, series, isbns)

        card_sel = f'[data-testid="series-card"][data-series-name="{series}"]'
        pg.goto(f"{base}/series")
        card = pg.locator(card_sel)
        button = card.get_by_test_id("watch-series")
        expect(button).to_have_text("Watch for new volumes", timeout=10_000)

        # Watch: the button flips, then the page reloads and still says Watching.
        with pg.expect_navigation(timeout=15_000):
            button.click()
        button = pg.locator(card_sel).get_by_test_id("watch-series")
        expect(button).to_have_text("Watching", timeout=10_000)
        expect(button).to_have_attribute("aria-pressed", "true")
        assert len(_query(data_dir, "SELECT 1 FROM release_watches WHERE series_name = ?",
                          (series,))) == 1

        # Toggle off, and survive that reload too.
        with pg.expect_navigation(timeout=15_000):
            button.click()
        button = pg.locator(card_sel).get_by_test_id("watch-series")
        expect(button).to_have_text("Watch for new volumes", timeout=10_000)
        assert _query(data_dir, "SELECT 1 FROM release_watches WHERE series_name = ?",
                      (series,)) == []

        # Watch all adds the seeded complete series (hc_missing = 0).
        with pg.expect_navigation(timeout=15_000):
            pg.get_by_test_id("watch-all").click()
        expect(pg.locator(card_sel).get_by_test_id("watch-series")).to_have_text(
            "Watching", timeout=10_000)
        assert len(_query(data_dir, "SELECT 1 FROM release_watches WHERE series_name = ?",
                          (series,))) == 1

        rel = _seed_releases(data_dir, series, base_id)

        # The upcoming link on the Series page reaches the page.
        with pg.expect_navigation(timeout=15_000):
            pg.get_by_test_id("upcoming-link").click()
        rows = pg.get_by_test_id("upcoming-row")
        expect(rows.filter(has_text=rel["dated"])).to_have_count(1, timeout=10_000)

        titles = [t for t in pg.get_by_test_id("upcoming-row-title").all_inner_texts()
                  if t.startswith(series)]
        assert titles == [rel["wish"], rel["dated"], rel["undated"]], titles
        expect(rows.filter(has_text=rel["undated"]).get_by_test_id("upcoming-row-date")
               ).to_have_text("announced — date TBA")
        expect(rows.filter(has_text=rel["dated"]).get_by_test_id("upcoming-row-date")
               ).to_have_text("2099-01-01")
        expect(pg.get_by_test_id("upcoming-attribution")).to_have_text("Powered by Hardcover")

        # + Wishlist: add-to-shelf, reload, the row is gone and the item is on the wishlist.
        # cover_url is NULL, so the route makes no outbound download.
        with pg.expect_navigation(timeout=15_000):
            rows.filter(has_text=rel["wish"]).get_by_test_id("upcoming-row-wishlist").click()
        expect(pg.get_by_test_id("upcoming-row").filter(has_text=rel["wish"])).to_have_count(
            0, timeout=10_000)
        on_list = _query(
            data_dir,
            "SELECT i.title FROM items i JOIN list_items li ON li.item_id = i.id "
            "JOIN lists l ON l.id = li.list_id "
            "WHERE l.slug = 'wishlist' AND i.hardcover_book_id = ?", (rel["wish_hc_id"],))
        assert [r["title"] for r in on_list] == [rel["wish"]]

        # Dismiss: the row leaves without a reload.
        pg.evaluate("() => { window.__rr_marker = 1; }")
        rows = pg.get_by_test_id("upcoming-row")
        with pg.expect_response(lambda r: "/dismiss" in r.url and r.request.method == "POST"):
            rows.filter(has_text=rel["dated"]).get_by_test_id("upcoming-row-dismiss").click()
        expect(rows.filter(has_text=rel["dated"])).to_have_count(0, timeout=10_000)
        assert pg.evaluate("() => window.__rr_marker") == 1
        assert _query(data_dir,
                      "SELECT dismissed FROM upcoming_releases WHERE title = ?",
                      (rel["dated"],))[0]["dismissed"] == 1

        # Stop watching: the watch and its rows go; the page falls to the empty state.
        with pg.expect_navigation(timeout=15_000):
            rows.filter(has_text=rel["undated"]).get_by_test_id("upcoming-row-stop").click()
        expect(pg.get_by_test_id("upcoming-empty")).to_be_visible(timeout=10_000)
        expect(pg.get_by_test_id("upcoming-attribution")).to_have_text("Powered by Hardcover")
        assert _query(data_dir, "SELECT 1 FROM release_watches WHERE series_name = ?",
                      (series,)) == []
    finally:
        try:
            _clear_token(pg, base)
        finally:
            _cleanup(data_dir, series, base_id, preexisting)
    # At the end of the body, not in the finally (G44) — authed_page's fixture
    # also asserts on teardown.
    assert_page_clean(pg)
