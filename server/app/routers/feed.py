"""Router for the short-form video feed."""

from fastapi import APIRouter, HTTPException, Query

from app.services.feed_service import FeedError, get_shorts_feed

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("/shorts", summary="Get South Asian wedding short-form video feed")
def shorts_feed(
    page_token: str = Query(default="", description="Pagination token from a previous response"),
    max_results: int = Query(default=10, ge=1, le=25, description="Number of videos to return"),
):
    """Returns a paginated feed of short-form South Asian wedding performance videos
    sourced from YouTube. Pass `next_page_token` from the previous response as
    `page_token` to fetch the next page. No auth required."""
    try:
        return get_shorts_feed(page_token=page_token, max_results=max_results)
    except FeedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
