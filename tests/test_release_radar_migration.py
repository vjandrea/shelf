"""Migrations 49 and 50: `release_watches` and `upcoming_releases`.

The legacy fixture is built *without* the current `MIGRATION_TABLES` (G98):
`bootstrap_sql_before(48)` with both new CREATEs stripped. Running the current
bootstrap first would pre-create both tables and turn the numbered entries into
`IF NOT EXISTS` no-ops.

Neither table has seed rows, so deleting a numbered entry alone cannot red the
upgrade test here: `executescript(MIGRATION_TABLES)` creates the table from its
copy afterwards. The pins are the absent-before assertion, the
`schema_version` rows (a deleted entry loses its row), and
`tests/test_schema_parity.py`, which checks each numbered body against its
`MIGRATION_TABLES` copy.
"""

import re
import sqlite3

import pytest

from app.database import MIGRATIONS, SCHEMA, _run_migrations
from tests.conftest import bootstrap_sql_before

_NEW_TABLES = ("release_watches", "upcoming_releases")


def _bootstrap_sql_before_49():
    sql = bootstrap_sql_before(48)
    for table in _NEW_TABLES:
        sql = re.sub(
            rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n?\);\n",
            "",
            sql,
            flags=re.S,
        )
    return sql


def _legacy_db(tmp_path, up_to=48):
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for version, description, sql in MIGRATIONS:
        if version > up_to:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (version, description),
        )
    conn.executescript(_bootstrap_sql_before_49())
    conn.commit()
    return conn


def _tables(conn):
    return {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_the_fixture_predates_the_new_tables(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        assert not _tables(conn) & set(_NEW_TABLES)
    finally:
        conn.close()


def test_migrations_create_both_tables_on_a_legacy_database(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        assert not _tables(conn) & set(_NEW_TABLES)
        _run_migrations(conn)
        assert set(_NEW_TABLES) <= _tables(conn)
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_version")
        }
        assert {49, 50} <= versions
    finally:
        conn.close()


def test_a_second_run_changes_nothing(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        _run_migrations(conn)
        before = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        _run_migrations(conn)
        after = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
        assert set(_NEW_TABLES) <= _tables(conn)
    finally:
        conn.close()


def test_a_fresh_database_has_the_designed_columns(db):
    assert _columns(db, "release_watches") == {
        "id", "series_name", "checked_at", "check_state", "created_at",
    }
    assert _columns(db, "upcoming_releases") == {
        "id", "watch_id", "hc_book_id", "title", "authors", "cover_url",
        "series_position", "release_date", "notified_at", "reminded_at",
        "dismissed", "created_at",
    }


def test_upcoming_releases_cascades_from_its_watch(db):
    fks = db.execute("PRAGMA foreign_key_list(upcoming_releases)").fetchall()
    assert [(r["table"], r["from"], r["on_delete"]) for r in fks] == [
        ("release_watches", "watch_id", "CASCADE"),
    ]


def _watch(db, name="Dune"):
    return db.execute(
        "INSERT INTO release_watches (series_name) VALUES (?)", (name,)
    ).lastrowid


def _release(db, watch_id, hc_book_id=1, title="Book"):
    db.execute(
        "INSERT INTO upcoming_releases (watch_id, hc_book_id, title)"
        " VALUES (?, ?, ?)",
        (watch_id, hc_book_id, title),
    )


def test_deleting_a_watch_removes_its_releases(db):
    keep, drop = _watch(db, "Keep"), _watch(db, "Drop")
    _release(db, keep)
    _release(db, drop, 1)
    _release(db, drop, 2)
    db.execute("DELETE FROM release_watches WHERE id = ?", (drop,))
    rows = db.execute("SELECT watch_id FROM upcoming_releases").fetchall()
    assert [r["watch_id"] for r in rows] == [keep]


def test_series_name_is_unique_ignoring_case(db):
    _watch(db, "Dune")
    with pytest.raises(sqlite3.IntegrityError):
        _watch(db, "dune")


def test_a_book_is_unique_within_a_watch(db):
    a, b = _watch(db, "A"), _watch(db, "B")
    _release(db, a, 7)
    _release(db, b, 7)  # same book under another watch is fine
    with pytest.raises(sqlite3.IntegrityError):
        _release(db, a, 7)
