from __future__ import annotations

import re
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from app.deps import DbDep
from app.schemas.docs import DocItemCreate, DocItemOut, DocItemUpdate
from app.services import docs as docs_svc

router = APIRouter()


@router.get("", response_model=list[DocItemOut])
async def list_items(db: DbDep) -> list[DocItemOut]:
    return await docs_svc.list_items(db)


@router.post("", response_model=DocItemOut, status_code=status.HTTP_201_CREATED)
async def create_item(body: DocItemCreate, db: DbDep) -> DocItemOut:
    item = await docs_svc.create_item(db, body.model_dump())
    return DocItemOut.model_validate(item)


@router.put("/{item_id}", response_model=DocItemOut)
async def update_item(item_id: uuid.UUID, body: DocItemUpdate, db: DbDep) -> DocItemOut:
    item = await docs_svc.get_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item = await docs_svc.update_item(db, item, body.model_dump(exclude_unset=True))
    return DocItemOut.model_validate(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: uuid.UUID, db: DbDep) -> None:
    item = await docs_svc.get_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    await docs_svc.delete_item(db, item)


# ── HTML proxy ────────────────────────────────────────────────────────────────
# Fetches an external HTML page server-side and re-serves it without
# X-Frame-Options, with <base href> injected so relative assets resolve.

_RAW_GITHUB = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)",
    re.IGNORECASE,
)


def _to_raw_github(url: str) -> str:
    """Convert a github.com blob URL to raw.githubusercontent.com."""
    m = _RAW_GITHUB.match(url)
    if m:
        owner, repo, path = m.group(1), m.group(2), m.group(3)
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{path}"
    return url


def _inject_base(html: str, base_url: str) -> str:
    """Inject <base href> so relative assets resolve against the source URL."""
    base_tag = f'<base href="{base_url}">'
    if re.search(r"<base\b", html, re.IGNORECASE):
        return html
    return re.sub(r"(<head[^>]*>)", rf"\1{base_tag}", html, count=1, flags=re.IGNORECASE)


@router.get("/proxy", response_class=HTMLResponse)
async def proxy_html(url: Annotated[str, Query()]) -> HTMLResponse:
    fetch_url = _to_raw_github(url)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                fetch_url,
                headers={"User-Agent": "QAHub-Proxy/1.0"},
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and not fetch_url.endswith(".html"):
        raise HTTPException(status_code=400, detail="URL does not point to an HTML page")

    html = _inject_base(resp.text, fetch_url)
    return HTMLResponse(content=html)
