from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Annotated

import httpx
import psutil
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse

from app.deps import DbDep
from app.schemas.bdd import (
    GenerateRequest,
    OllamaStatusOut,
    ParseRequest,
    ParseResponse,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ScenarioCreate,
    ScenarioOut,
    ScenarioUpdate,
    SettingsOut,
    SettingsUpdate,
)
from app.schemas.common import PaginatedResponse
from app.services import bdd as bdd_svc
from app.services.bdd_ai import build_prompt, get_ai_provider, get_system_prompt
from app.services.bdd_parsers import ParseError, parse_source

router = APIRouter()


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsOut)
async def get_settings(db: DbDep) -> SettingsOut:
    s = await bdd_svc.get_settings(db)
    return SettingsOut(**bdd_svc.settings_to_out(s))


@router.put("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate, db: DbDep) -> SettingsOut:
    s = await bdd_svc.update_settings(db, body.model_dump(exclude_unset=True))
    return SettingsOut(**bdd_svc.settings_to_out(s))


@router.post("/settings/test")
async def test_connection(db: DbDep) -> dict:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)
    try:
        provider = get_ai_provider(
            dec["ai_provider"], dec["claude_api_key"], dec["claude_model"],
            dec["ollama_base_url"], dec["ollama_model"],
        )
        chunks = []
        async for chunk in provider.generate_stream("You are helpful.", "Say 'ok' in one word."):
            chunks.append(chunk)
            if len(chunks) > 20:
                break
        return {"status": "ok", "provider": dec["ai_provider"]}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(db: DbDep) -> list[ProjectOut]:
    pairs = await bdd_svc.list_projects(db)
    return [ProjectOut.model_validate(p).model_copy(update={"scenario_count": cnt}) for p, cnt in pairs]


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, db: DbDep) -> ProjectOut:
    p = await bdd_svc.create_project(db, name=body.name, description=body.description)
    return ProjectOut.model_validate(p).model_copy(update={"scenario_count": 0})


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, db: DbDep) -> ProjectOut:
    p = await bdd_svc.get_project(db, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    _, total = await bdd_svc.list_scenarios(db, project_id=project_id, page_size=1)
    return ProjectOut.model_validate(p).model_copy(update={"scenario_count": total})


@router.put("/projects/{project_id}", response_model=ProjectOut)
async def update_project(project_id: uuid.UUID, body: ProjectUpdate, db: DbDep) -> ProjectOut:
    p = await bdd_svc.get_project(db, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    p = await bdd_svc.update_project(db, p, body.model_dump(exclude_unset=True))
    _, total = await bdd_svc.list_scenarios(db, project_id=project_id, page_size=1)
    return ProjectOut.model_validate(p).model_copy(update={"scenario_count": total})


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, db: DbDep) -> None:
    p = await bdd_svc.get_project(db, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    await bdd_svc.delete_project(db, p)


# ── Scenarios ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/scenarios", response_model=list[ScenarioOut])
async def list_project_scenarios(
    project_id: uuid.UUID,
    db: DbDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[ScenarioOut]:
    scenarios, _ = await bdd_svc.list_scenarios(db, project_id=project_id, status=status_filter, page_size=500)
    return [_scenario_out(s) for s in scenarios]


@router.get("/scenarios", response_model=PaginatedResponse[ScenarioOut])
async def list_all_scenarios(
    db: DbDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[ScenarioOut]:
    scenarios, total = await bdd_svc.list_scenarios(
        db, project_id=project_id, status=status_filter, page=page, page_size=page_size
    )
    return PaginatedResponse(items=[_scenario_out(s) for s in scenarios], total=total, page=page, page_size=page_size)


@router.post("/scenarios", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def create_scenario(body: ScenarioCreate, db: DbDep) -> ScenarioOut:
    s = await bdd_svc.create_scenario(db, **body.model_dump())
    return _scenario_out(s)


@router.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
async def get_scenario(scenario_id: uuid.UUID, db: DbDep) -> ScenarioOut:
    s = await bdd_svc.get_scenario(db, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _scenario_out(s)


@router.put("/scenarios/{scenario_id}", response_model=ScenarioOut)
async def update_scenario(scenario_id: uuid.UUID, body: ScenarioUpdate, db: DbDep) -> ScenarioOut:
    s = await bdd_svc.get_scenario(db, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    s = await bdd_svc.update_scenario(db, s, body.model_dump(exclude_unset=True))
    return _scenario_out(s)


@router.delete("/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scenario(scenario_id: uuid.UUID, db: DbDep) -> None:
    s = await bdd_svc.get_scenario(db, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await bdd_svc.delete_scenario(db, s)


@router.get("/scenarios/{scenario_id}/export")
async def export_scenario(scenario_id: uuid.UUID, db: DbDep) -> Response:
    s = await bdd_svc.get_scenario(db, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    filename = re.sub(r'[^\w\-]', '_', s.title[:50].lower()) + ".feature"
    return Response(
        content=s.gherkin,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Parse ─────────────────────────────────────────────────────────────────────

@router.post("/parse", response_model=ParseResponse)
async def parse_requirement(body: ParseRequest, db: DbDep) -> ParseResponse:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)
    try:
        text = await parse_source(
            body.source_type,
            content=body.content,
            url=body.url,
            confluence_email=dec["confluence_email"],
            confluence_token=dec["confluence_api_token"],
        )
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}") from exc
    return ParseResponse(text=text)


@router.post("/parse/file", response_model=ParseResponse)
async def parse_file(
    source_type: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    db: DbDep,
) -> ParseResponse:
    if source_type not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="source_type must be pdf or docx")
    file_bytes = await file.read()
    try:
        text = await parse_source(source_type, content=None, url=None, file_bytes=file_bytes, filename=file.filename)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}") from exc
    return ParseResponse(text=text)


# ── Generate (SSE) ────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_scenarios(body: GenerateRequest, db: DbDep) -> StreamingResponse:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)

    language = body.language or dec["gherkin_language"]
    max_scenarios = body.max_scenarios or dec["max_scenarios"]

    try:
        provider = get_ai_provider(
            dec["ai_provider"], dec["claude_api_key"], dec["claude_model"],
            dec["ollama_base_url"], dec["ollama_model"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    system_prompt = get_system_prompt(language)
    user_prompt = build_prompt(body.requirement, body.title, language, max_scenarios)

    async def event_stream():
        start = time.monotonic()
        full_text: list[str] = []
        try:
            async for chunk in provider.generate_stream(system_prompt, user_prompt):
                full_text.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            elapsed_ms = int((time.monotonic() - start) * 1000)
            raw = "".join(full_text)

            m = re.search(r"\{.*\}", raw, re.DOTALL)
            scenarios: list = []
            if m:
                try:
                    parsed = json.loads(m.group())
                    scenarios = parsed.get("scenarios", [])
                except json.JSONDecodeError:
                    pass

            ai_model = dec["claude_model"] if dec["ai_provider"] == "claude" else dec["ollama_model"]
            yield f"data: {json.dumps({'done': True, 'scenarios': scenarios, 'generation_time_ms': elapsed_ms, 'ai_provider': dec['ai_provider'], 'ai_model': ai_model})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scenario_out(s) -> ScenarioOut:
    data = ScenarioOut.model_validate(s)
    if data.tags is None:
        data.tags = []
    return data


# ── Ollama process controls ────────────────────────────────────────────────────

async def _probe_ollama(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get(f"{url}/api/tags")
        return True
    except Exception:
        return False


@router.get("/ollama/status", response_model=OllamaStatusOut)
async def ollama_status(db: DbDep) -> OllamaStatusOut:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)
    url = dec["ollama_base_url"]
    return OllamaStatusOut(running=await _probe_ollama(url), url=url)


@router.post("/ollama/start", response_model=OllamaStatusOut)
async def ollama_start(db: DbDep) -> OllamaStatusOut:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)
    url = dec["ollama_base_url"]

    if await _probe_ollama(url):
        return OllamaStatusOut(running=True, url=url)

    try:
        await asyncio.create_subprocess_exec(
            "ollama", "serve",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=502, detail="ollama binary not found") from exc

    for _ in range(10):
        await asyncio.sleep(0.5)
        if await _probe_ollama(url):
            return OllamaStatusOut(running=True, url=url)

    return OllamaStatusOut(running=False, url=url)


@router.post("/ollama/stop", response_model=OllamaStatusOut)
async def ollama_stop(db: DbDep) -> OllamaStatusOut:
    s = await bdd_svc.get_settings(db)
    dec = bdd_svc.get_decrypted_settings(s)
    url = dec["ollama_base_url"]

    procs = [p for p in psutil.process_iter(["name", "pid"]) if p.info["name"] == "ollama"]
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(procs, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass

    return OllamaStatusOut(running=await _probe_ollama(url), url=url)
