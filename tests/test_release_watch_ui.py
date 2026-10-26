"""The Series page's release-radar controls: the card's watch button or pill,
the not-found line, and the header's Upcoming link and Watch all button."""

import re

import pytest

from tests.conftest import _insert_item
from tests.test_features_gate import off


def _token(db):
    db.execute("INSERT INTO settings (key, value) VALUES ('hardcover_token', 'tok')")


def _seed(db, *, token=True):
    _insert_item(db, title="Dune 1", isbn=None, series_name="Dune")
    _insert_item(db, title="Expanse 1", isbn=None, series_name="Expanse")
    _insert_item(db, title="Lost 1", isbn=None, series_name="Lost")
    db.execute("INSERT INTO release_watches (series_name, check_state) VALUES ('dune', 'ok')")
    db.execute("INSERT INTO release_watches (series_name, check_state) VALUES ('Lost', 'not_found')")
    if token:
        _token(db)
    db.commit()


def _card(html, name):
    m = re.search(rf'<div class="bg-shelf-card[^>]*data-series-name="{re.escape(name)}".*?'
                  r'(?=<div class="bg-shelf-card[^>]*x-data="seriesCard"|\Z)', html, re.S)
    assert m, name
    return m.group(0)


def _tag(html, testid):
    m = re.search(rf'<[a-z]+\b[^>]*data-testid="{testid}"[^>]*>', html, re.S)
    assert m, testid
    return m.group(0)


class TestEditor:
    def test_button_label_follows_watch_state(self, editor_client, db):
        _seed(db)
        html = editor_client.get("/series").text
        dune, expanse = _card(html, "Dune"), _card(html, "Expanse")
        assert 'data-watched="1"' in dune and 'data-watch-state="ok"' in dune
        assert 'data-testid="watch-series"' in dune and ">Watching</button>" in dune
        assert 'data-watched=""' in expanse
        assert ">Watch for new volumes</button>" in expanse
        assert 'data-testid="watching-pill"' not in html

    def test_not_found_watch_says_so(self, editor_client, db):
        _seed(db)
        html = editor_client.get("/series").text
        assert "Not found on Hardcover" in _card(html, "Lost")
        assert "Not found on Hardcover" not in _card(html, "Dune")

    def test_header_has_the_upcoming_link_and_watch_all(self, editor_client, db):
        _seed(db)
        html = editor_client.get("/series").text
        assert 'href="/upcoming"' in _tag(html, "upcoming-link")
        watch_all = _tag(html, "watch-all")
        assert 'hx-post="/api/series/watch-all"' in watch_all
        assert 'hx-swap="none"' in watch_all and "hx-target" not in watch_all  # G54
        assert "Watch all complete series" in html


class TestViewer:
    def test_sees_only_the_pill_when_watched(self, viewer_client, db):
        _seed(db)
        html = viewer_client.get("/series").text
        assert 'data-testid="watch-series"' not in html
        assert 'data-testid="watching-pill"' in _card(html, "Dune")
        assert 'data-testid="watching-pill"' not in _card(html, "Expanse")
        assert 'data-testid="watch-all"' not in html
        assert 'data-testid="upcoming-link"' in html


class TestHidden:
    @pytest.mark.parametrize("key", ["release_radar", "hardcover"])
    def test_flag_off_hides_every_control(self, editor_client, db, key):
        _seed(db)
        off(key)
        html = editor_client.get("/series").text
        for testid in ("watch-series", "watching-pill", "upcoming-link", "watch-all",
                       "watch-not-found"):
            assert f'data-testid="{testid}"' not in html

    def test_no_token_hides_every_control(self, editor_client, db, monkeypatch):
        monkeypatch.delenv("HARDCOVER_TOKEN", raising=False)
        _seed(db, token=False)
        html = editor_client.get("/series").text
        for testid in ("watch-series", "watching-pill", "upcoming-link", "watch-all",
                       "watch-not-found"):
            assert f'data-testid="{testid}"' not in html
