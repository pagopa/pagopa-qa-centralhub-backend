from __future__ import annotations

import base64
import io

import httpx


class ParseError(Exception):
    pass


async def parse_source(
    source_type: str,
    *,
    content: str | None,
    url: str | None,
    confluence_email: str | None = None,
    confluence_token: str | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
) -> str:
    match source_type:
        case "text":
            if not content:
                raise ParseError("content is required for text source")
            return content.strip()

        case "url":
            if not url:
                raise ParseError("url is required for url source")
            return await _fetch_url(url)

        case "confluence":
            if not url:
                raise ParseError("url is required for confluence source")
            return await _fetch_confluence(url, confluence_email, confluence_token)

        case "pdf":
            if not file_bytes:
                raise ParseError("file_bytes is required for pdf source")
            return _parse_pdf(file_bytes)

        case "docx":
            if not file_bytes:
                raise ParseError("file_bytes is required for docx source")
            return _parse_docx(file_bytes)

        case _:
            raise ParseError(f"unknown source_type: {source_type}")


async def _fetch_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "QA-Hub-BDD/1.0"})
        resp.raise_for_status()
        html = resp.text

    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=True)
        if text:
            return text.strip()
    except Exception:
        pass

    # Fallback: strip HTML tags naively
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:10000]


async def _fetch_confluence(url: str, email: str | None, token: str | None) -> str:
    if not email or not token:
        raise ParseError("Confluence credentials not configured in BDD Settings")

    # Extract page ID from URL: .../pages/12345/... or ?pageId=12345
    import re

    m = re.search(r"/pages/(\d+)", url) or re.search(r"pageId=(\d+)", url)
    if not m:
        raise ParseError(f"Cannot extract Confluence page ID from URL: {url}")
    page_id = m.group(1)

    # Determine base URL (everything up to /wiki/)
    base_match = re.match(r"(https://[^/]+)", url)
    if not base_match:
        raise ParseError("Cannot determine Confluence base URL")
    base = base_match.group(1)

    api_url = f"{base}/wiki/api/v2/pages/{page_id}?body-format=storage"
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, headers={"Authorization": f"Basic {creds}"})
        resp.raise_for_status()
        data = resp.json()

    body_html = data.get("body", {}).get("storage", {}).get("value", "")
    import re as _re

    text = _re.sub(r"<[^>]+>", " ", body_html)
    text = _re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_pdf(file_bytes: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(p.strip() for p in pages if p.strip())


def _parse_docx(file_bytes: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(file_bytes))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)
