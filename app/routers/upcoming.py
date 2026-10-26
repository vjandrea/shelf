"""The Upcoming releases page: announced volumes in watched series.

- `GET /upcoming` — every undismissed announcement the release radar has
  recorded, minus any the library now owns or wishlists. Viewer-visible;
  reached from the Series page header, not the nav.
- `POST /api/upcoming/{release_id}/dismiss` — hide one announcement. HTMX
  swaps the row out with the empty 200 body; a missing id answers 404 with
  an `HX-Trigger` toast, since htmx never swaps a 4xx body (G145).

Both need `release_radar` and `hardcover` on: the rows come from Hardcover,
and the page's + Wishlist button calls a Hardcover-gated route. Role first,
then features — the order the registry lint checks.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.routers import items_common
from app.services import release_radar

page_router = APIRouter()
router = APIRouter(prefix="/api/upcoming")


@page_router.get("/upcoming")
async def upcoming_page(
    request: Request,
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("release_radar")),
    ___=Depends(require_feature("hardcover")),
):
    with get_db() as db:
        rows = release_radar.visible_rows(db)
    return request.app.state.templates.TemplateResponse(
        request, "upcoming.html", {"releases": rows},
    )


@router.post("/{release_id}/dismiss")
async def dismiss_release(
    release_id: int,
    _=Depends(require_role("editor")),
    __=Depends(require_feature("release_radar")),
    ___=Depends(require_feature("hardcover")),
):
    with get_db() as db:
        cur = db.execute("UPDATE upcoming_releases SET dismissed = 1 WHERE id = ?", (release_id,))
        found = cur.rowcount == 1
    if not found:
        return Response(status_code=404, headers={
            "HX-Trigger": items_common._toast_header("That announcement is gone", "error")})
    return Response(status_code=200)
