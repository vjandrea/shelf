"""Settings → Integrations → Hardcover: the Release radar block and
POST /api/settings/release-radar."""
import re

import pytest

from app.database import get_db


def _block(html):
    """The rendered release-radar block, so assertions cannot match elsewhere."""
    start = html.index('data-testid="release-radar-block"')
    end = html.index("</form>", start)
    return html[start:end]


def _value(block, testid):
    tag = re.search(r'<input[^>]*data-testid="%s"[^>]*>' % testid, block).group(0)
    return re.search(r'value="([^"]*)"', tag).group(1)


def _setting(key):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    db.commit()


class TestBlock:
    def test_default_renders_when_nothing_is_saved(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert _value(block, "release-radar-cap") == "50"
        assert 'min="1" max="200" step="1"' in block
        assert "Last run: Never" in block

    def test_honesty_line_is_visible_text(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert ("Announcements come from Hardcover. If none is found, that does not "
                "mean nothing is coming.") in block

    def test_saved_value_round_trips(self, admin_client):
        """G36: re-post what the form rendered, then change the field."""
        block = _block(admin_client.get("/settings").text)
        form = {"release_radar_cap": _value(block, "release-radar-cap")}
        resp = admin_client.post("/api/settings/release-radar", data=form, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"
        assert _setting("release_radar_cap") == "50"

        admin_client.post("/api/settings/release-radar", data={"release_radar_cap": "120"})
        block = _block(admin_client.get("/settings").text)
        assert _value(block, "release-radar-cap") == "120"

    def test_last_run_shows_the_summary_after_a_stamped_run(self, admin_client, db):
        _set(db, "release_radar_last_run", "1790000000")  # 2026-09-21 14:13:20 UTC
        _set(db, "release_radar_last_summary",
             '{"checked": 4, "not_found": 1, "failed": 1, "outcome": "rejected", '
             '"new": 0, "reminded": 0, "sent": false}')
        block = _block(admin_client.get("/settings").text)
        assert ("Last run: 2026-09-21 14:13 UTC — 4 checked, 1 not found on Hardcover, "
                "1 failed (no response from Hardcover), stopped: Hardcover refused the token") in block

    def test_notify_link_shows_without_a_notify_url(self, admin_client):
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="release-radar-notify-hint"' in block
        assert 'data-testid="release-radar-notify-link"' in block
        assert "goToField('library', 'notify-url-input')" in block

    def test_notify_link_hidden_once_a_notify_url_row_exists(self, admin_client, db, monkeypatch):
        monkeypatch.delenv("NOTIFY_URL", raising=False)
        _set(db, "notify_url", "https://ntfy.example/shelf")
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="release-radar-notify-hint"' in block
        assert 'data-testid="release-radar-notify-link"' not in block

    def test_feature_off_note_appears_with_the_flag_off(self, admin_client):
        assert 'data-feature="release_radar"' not in _block(admin_client.get("/settings").text)
        admin_client.post("/api/settings/features/release_radar", data={"enabled": "0"})
        block = _block(admin_client.get("/settings").text)
        assert 'data-testid="feature-off-note" data-feature="release_radar"' in block


class TestSave:
    @pytest.mark.parametrize("cap", ["0", "201", "abc", "²", "-5"])
    def test_rejects_out_of_range_and_writes_nothing(self, admin_client, cap):
        resp = admin_client.post(
            "/api/settings/release-radar", data={"release_radar_cap": cap}, follow_redirects=False)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "whole number" in body["message"]
        assert _setting("release_radar_cap") is None

    def test_bounds_are_accepted(self, admin_client):
        for cap in ("1", "200"):
            resp = admin_client.post(
                "/api/settings/release-radar", data={"release_radar_cap": cap}, follow_redirects=False)
            assert resp.status_code == 303
            assert _setting("release_radar_cap") == cap

    def test_editor_gets_role_refusal(self, editor_client):
        resp = editor_client.post(
            "/api/settings/release-radar", data={"release_radar_cap": "20"}, follow_redirects=False)
        assert resp.status_code == 403
        assert _setting("release_radar_cap") is None
