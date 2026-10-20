"""GET /taxonomy — the service taxonomy every picker reads (docs/DECISIONS.md
#35). Public reference data, like /vendors/categories, which keeps serving
today's category/subcategory list for older clients until they move over."""
from fastapi import APIRouter, Request, Response

from app import taxonomy

router = APIRouter(tags=["taxonomy"])


@router.get("/taxonomy", summary="Groups, services and attributes for every picker")
def taxonomy_route(request: Request, response: Response):
    """The whole taxonomy, in browse order. `version` changes whenever any
    of it does; send it back as If-None-Match to get a 304 instead."""
    view = taxonomy.view()
    etag = f'"{view["version"]}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return view
