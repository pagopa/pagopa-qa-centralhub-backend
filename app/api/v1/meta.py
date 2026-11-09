from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/openapi")
async def get_openapi_spec(request: Request) -> dict[str, Any]:
    """The OpenAPI specification of this API, for the portal's API documentation page.

    The built-in `/api/v1/openapi.json` is not part of the specification imported into Azure
    APIM, so it is not reachable through the gateway: this regular endpoint is.
    """
    return request.app.openapi()
