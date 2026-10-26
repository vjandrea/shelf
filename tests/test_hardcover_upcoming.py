"""Release-radar Hardcover query: the filter and the failure contract."""

import json
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.services import hardcover as hc
from app.services import provider_result as pr

TODAY = date(2026, 10, 22)


def _book(bid, title, *, pos, users=0, rd=None, ry=None, **extra):
    book = {
        "id": bid, "title": title, "release_date": rd, "release_year": ry,
        "cached_image": {"url": f"https://assets.hardcover.app/{bid}.jpg"},
        "canonical_id": None, "compilation": False, "users_count": users,
        "contributions": [{"author": {"name": "A. Writer"}}],
    }
    book.update(extra)
    return {"position": pos, "book": book}


FIXTURE = [
    _book(1, "Book One", pos=1, users=10000, rd="2015-01-01", ry=2015),
    _book(2, "Past Obscure", pos=2, users=5, rd="2016-01-01", ry=2016),
    _book(3, "Future Zero", pos=3, users=0, rd="2027-03-01", ry=2027),
    _book(4, "Undated", pos=4, users=0),
]


def _by_id(rows):
    return {r["hardcover_book_id"]: r for r in rows}


def test_future_dated_zero_readers_survives_beside_big_volume():
    rows = _by_id(hc._parse_series_upcoming(FIXTURE, TODAY))
    assert 3 in rows and rows[3]["release_date"] == "2027-03-01"
    assert 1 in rows


def test_past_dated_same_count_is_floored():
    assert 2 not in _by_id(hc._parse_series_upcoming(FIXTURE, TODAY))


def test_undated_survives():
    rows = _by_id(hc._parse_series_upcoming(FIXTURE, TODAY))
    assert 4 in rows and rows[4]["release_date"] is None


def test_canonical_and_compilation_dropped():
    entries = FIXTURE + [
        _book(5, "Translation", pos=5, canonical_id=1),
        _book(6, "Box Set", pos=6, compilation=True),
    ]
    rows = _by_id(hc._parse_series_upcoming(entries, TODAY))
    assert 5 not in rows and 6 not in rows


def test_equal_positions_collapse_to_most_shelved():
    entries = [
        _book(10, "Minor", pos=7, users=3, rd="2027-01-01"),
        _book(11, "Major", pos=7, users=900, rd="2027-01-01"),
    ]
    rows = hc._parse_series_upcoming(entries, TODAY)
    assert [r["hardcover_book_id"] for r in rows] == [11]


def test_malformed_release_date_is_none():
    entries = [_book(20, "Bad", pos=1, rd="soon-ish"), _book(21, "Short", pos=2, rd="")]
    rows = _by_id(hc._parse_series_upcoming(entries, TODAY))
    assert rows[20]["release_date"] is None and rows[21]["release_date"] is None


def test_timestamp_date_is_cut_to_iso_day():
    rows = hc._parse_series_upcoming([_book(30, "Ts", pos=1, rd="2027-05-04T00:00:00")], TODAY)
    assert rows[0]["release_date"] == "2027-05-04"


def test_output_shape():
    row = hc._parse_series_upcoming([_book(40, "Shape", pos=1, rd="2027-01-02", ry=2027)], TODAY)[0]
    assert set(row) == {
        "hardcover_book_id", "title", "authors", "cover_url",
        "series_position", "release_date", "release_year",
    }


def _data(entries):
    return {"book_series": entries}


@pytest.mark.asyncio
async def test_shape_one_found_returns_parsed_one_call():
    m = AsyncMock(side_effect=[pr.found("hardcover", _data(FIXTURE))])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.found and m.await_count == 1
    assert {r["hardcover_book_id"] for r in res.payload} == {1, 3, 4}


@pytest.mark.asyncio
async def test_shape_one_no_match_falls_through_to_shape_two():
    m = AsyncMock(side_effect=[
        pr.no_match("hardcover"),
        pr.found("hardcover", {"series": [{"book_series": FIXTURE}]}),
    ])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.found and m.await_count == 2 and len(res.payload) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    pr.rejected("hardcover", status=401),
    pr.rate_limited("hardcover", status=429),
    pr.transport_failed("hardcover"),
])
async def test_non_no_match_failure_returns_as_is_with_one_call(bad):
    m = AsyncMock(side_effect=[bad])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.outcome == bad.outcome and m.await_count == 1


@pytest.mark.asyncio
async def test_failure_on_shape_two_is_returned_as_is():
    m = AsyncMock(side_effect=[pr.no_match("hardcover"), pr.transport_failed("hardcover")])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.outcome == "transport_failed" and m.await_count == 2


@pytest.mark.asyncio
async def test_both_no_match_is_no_match():
    m = AsyncMock(side_effect=[pr.no_match("hardcover"), pr.no_match("hardcover")])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.outcome == "no_match"


@pytest.mark.asyncio
async def test_series_without_row_in_shape_two_is_no_match():
    m = AsyncMock(side_effect=[pr.no_match("hardcover"), pr.found("hardcover", {"series": []})])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.outcome == "no_match"


@pytest.mark.asyncio
async def test_found_but_empty_series_is_found_empty_list():
    m = AsyncMock(side_effect=[
        pr.found("hardcover", _data([])),
        pr.found("hardcover", {"series": [{"book_series": []}]}),
    ])
    with patch.object(hc, "_graphql_outcome", m):
        res = await hc.get_series_upcoming("S", "tok", today=TODAY)
    assert res.found and res.payload == []


@pytest.mark.asyncio
async def test_no_token_makes_no_call():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(hc.API_URL).mock(return_value=httpx.Response(200, json={}))
        res = await hc.get_series_upcoming("S", "", today=TODAY)
    assert res.outcome == "no_credential" and not route.called


# --- completeness unchanged (G31: mutate the floor once, see this fail) ----

_COMPLETENESS_FIXTURE = [
    _book(1, "Book One", pos=1, users=10000, rd="2015-01-01", ry=2015),
    _book(2, "Past Obscure", pos=2, users=5, rd="2016-01-01", ry=2016),
    _book(3, "Future Zero", pos=3, users=0, rd="2027-03-01", ry=2027),
    _book(4, "Undated", pos=4, users=0),
    _book(5, "Healthy", pos=5, users=500, rd="2020-01-01", ry=2020),
]


@pytest.mark.asyncio
async def test_get_series_books_completeness_is_unchanged():
    with patch.object(hc, "_graphql", new=AsyncMock(return_value=_data(_COMPLETENESS_FIXTURE))):
        books = await hc.get_series_books("S", "tok")
    # The 1% floor still applies to every row, announced or not: the
    # zero-reader future volume must NOT appear as a missing book here.
    assert [b["hardcover_book_id"] for b in books] == [1, 5]


@pytest.mark.asyncio
async def test_query_reaches_api_url_through_outbound_acquire(monkeypatch):
    acquired = []

    async def fake_acquire(host):
        acquired.append(host)

    monkeypatch.setattr(hc.outbound, "acquire", fake_acquire)
    with respx.mock as router:
        route = router.post(hc.API_URL).mock(
            return_value=httpx.Response(200, json={"data": _data(FIXTURE)})
        )
        async with httpx.AsyncClient() as client:
            res = await hc.get_series_upcoming("S", "tok", client, today=TODAY)
    assert acquired == ["api.hardcover.app"]
    assert route.call_count == 1 and res.found
    body = json.loads(route.calls[0].request.content)
    assert "release_date" in body["query"] and body["variables"] == {"name": "S"}
