"""Persistence helpers for publication-level periodicals and concrete issues.

A publication is the continuing title (for example *Private Eye*). A Shelf
``items`` row is one concrete issue. Keeping those identities separate lets
Shelf browse a publication as a run without pretending the whole run is one
catalogue item.

This validation slice owns its schema locally so no upstream migration number
is consumed before the maintainer has agreed the final shape.
"""

from __future__ import annotations

import re

from app import i18n


def normalise_issn(value: str | None) -> str | None:
    raw = re.sub(r"[^0-9X]", "", (value or "").upper())
    if len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:]}"


def find_publication(db, *, title: str, issn: str | None = None) -> int | None:
    """Return the publication an upsert with these values would update, if any.

    ISSN is the publication's identity. A title match is used only when the
    incoming ISSN is blank, or to let an existing ISSN-less row adopt one; a
    stored ISSN is never replaced by a different one.
    """
    title = (title or "").strip()
    issn = normalise_issn(issn)
    row = None
    if issn:
        row = db.execute(
            "SELECT id FROM periodical_publications WHERE issn = ? COLLATE NOCASE",
            (issn,),
        ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT id FROM periodical_publications WHERE title = ? COLLATE NOCASE "
            "AND (? IS NULL OR issn IS NULL) ORDER BY id LIMIT 1",
            (title, issn),
        ).fetchone()
    return row["id"] if row else None


def upsert_publication(
    db,
    *,
    title: str,
    issn: str | None = None,
    publisher: str | None = None,
    language: str | None = None,
) -> int:
    """Create or update one continuing publication identity (see `find_publication`)."""
    title = (title or "").strip()
    if not title:
        raise ValueError(i18n.gettext("Periodical title is required"))
    issn = normalise_issn(issn)

    publication_id = find_publication(db, title=title, issn=issn)
    if publication_id is not None:
        db.execute(
            "UPDATE periodical_publications SET "
            "title = ?, issn = COALESCE(?, issn), "
            "publisher = COALESCE(?, publisher), language = COALESCE(?, language), "
            "updated_at = datetime('now') WHERE id = ?",
            (title, issn, publisher or None, language or None, publication_id),
        )
        return publication_id

    return db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher, language) "
        "VALUES (?, ?, ?, ?)",
        (title, issn, publisher or None, language or None),
    ).lastrowid


def link_issue(
    db,
    *,
    item_id: int,
    publication_id: int,
    volume: str | None = None,
    issue_number: str | None = None,
    issue_date: str | None = None,
    barcode_ean: str | None = None,
    barcode_supplement: str | None = None,
    cover_date_label: str | None = None,
) -> None:
    """Attach concrete issue identity to an existing Shelf item."""
    if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
        raise ValueError(i18n.gettext("Item not found"))
    if not db.execute(
        "SELECT 1 FROM periodical_publications WHERE id = ?", (publication_id,)
    ).fetchone():
        raise ValueError(i18n.gettext("Publication not found"))

    db.execute(
        "INSERT INTO periodical_issues "
        "(item_id, publication_id, volume, issue_number, issue_date, barcode_ean, "
        "barcode_supplement, cover_date_label, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "publication_id = excluded.publication_id, volume = excluded.volume, "
        "issue_number = excluded.issue_number, issue_date = excluded.issue_date, "
        "barcode_ean = excluded.barcode_ean, "
        "barcode_supplement = excluded.barcode_supplement, "
        "cover_date_label = excluded.cover_date_label, updated_at = datetime('now')",
        (
            item_id,
            publication_id,
            (volume or "").strip() or None,
            (issue_number or "").strip() or None,
            (issue_date or "").strip() or None,
            (barcode_ean or "").strip() or None,
            (barcode_supplement or "").strip() or None,
            (cover_date_label or "").strip() or None,
        ),
    )


def find_duplicate_issue(
    db,
    publication_id: int | None,
    *,
    volume: str | None = None,
    issue_number: str | None = None,
    issue_date: str | None = None,
    barcode_ean: str | None = None,
    barcode_supplement: str | None = None,
) -> int | None:
    """Return an existing item id for a confidently matching concrete issue.

    Prefer a full carrier+supplement barcode when available. Otherwise use
    publication + volume + issue number, then publication + issue date. The
    977 serial variant is intentionally not treated as an issue number.

    **Live wins.** `periodical_issues` has no unique index, and all three
    reads are bare `LIMIT 1` with no `ORDER BY`, so a trashed issue can
    shadow a live twin that matches equally well. Every read therefore runs
    **joined to `items_live` first**, across all three strategies, and only
    when all three miss does the unjoined pass run to find a trashed
    candidate the caller may restore. Without the ordering, a user who
    deletes one of two equally-matching issues and re-scans gets the deleted
    one back while the live one is left alone.

    No physical `items` read is involved — the fallback pass simply does not
    join the items relation — so this needs no lint exemption.
    """
    ean = (barcode_ean or "").strip() or None
    supplement = (barcode_supplement or "").strip() or None
    issue_number = (issue_number or "").strip() or None
    volume = (volume or "").strip() or None
    issue_date = (issue_date or "").strip() or None

    for live_only in (True, False):
        join = " JOIN items_live i ON i.id = pi.item_id" if live_only else ""
        if ean and supplement:
            row = db.execute(
                f"SELECT pi.item_id FROM periodical_issues pi{join} "
                "WHERE pi.barcode_ean = ? AND pi.barcode_supplement = ? LIMIT 1",
                (ean, supplement),
            ).fetchone()
            if row:
                return row["item_id"]

        if issue_number:
            row = db.execute(
                f"SELECT pi.item_id FROM periodical_issues pi{join} "
                "WHERE pi.publication_id = ? "
                "AND COALESCE(pi.volume, '') = COALESCE(?, '') "
                "AND pi.issue_number = ? COLLATE NOCASE LIMIT 1",
                (publication_id, volume, issue_number),
            ).fetchone()
            if row:
                return row["item_id"]

        if issue_date:
            row = db.execute(
                f"SELECT pi.item_id FROM periodical_issues pi{join} "
                "WHERE pi.publication_id = ? AND pi.issue_date = ? LIMIT 1",
                (publication_id, issue_date),
            ).fetchone()
            if row:
                return row["item_id"]
    return None


def issues_for_publication(db, publication_id: int) -> list[dict]:
    """Return concrete issues newest first, with catalogue item metadata."""
    rows = db.execute(
        "SELECT pi.*, i.title, i.cover_path, i.owned, i.publish_year "
        "FROM periodical_issues pi JOIN items_live i ON i.id = pi.item_id "
        "WHERE pi.publication_id = ? "
        "ORDER BY CASE WHEN pi.issue_date IS NULL OR TRIM(pi.issue_date) = '' "
        "THEN 1 ELSE 0 END, pi.issue_date DESC, "
        "CASE WHEN pi.issue_number GLOB '[0-9]*' "
        "AND pi.issue_number NOT GLOB '*[^0-9]*' "
        "THEN CAST(pi.issue_number AS INTEGER) END DESC, "
        "pi.issue_number COLLATE NOCASE DESC, i.id DESC",
        (publication_id,),
    ).fetchall()
    return [dict(row) for row in rows]
