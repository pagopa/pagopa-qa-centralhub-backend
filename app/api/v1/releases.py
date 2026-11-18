from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from app.deps import DbDep
from app.schemas.status_page import (
    ApplicationDetailOut,
    ApplicationIn,
    ApplicationOut,
    ApplicationPatch,
    CheckIn,
    CheckOut,
    DeleteIn,
    EaComponentOut,
    EnvTarget,
    ManualSyncIn,
    ProductIn,
    ProductOut,
    ProductPatch,
    StatusOut,
    SummaryOut,
    SyncStatusOut,
    VersionOut,
)
from app.services import status_page as status_page_svc
from app.services import status_page_catalog as catalog_svc
from app.services import status_page_sync as sync_svc
from app.services.status_page_catalog import CatalogConflictError, CatalogNotFoundError
from app.services.status_page_sync import StatusPageSyncInProgressError

logger = structlog.get_logger(__name__)

router = APIRouter()

_DELETE_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "Not found"},
    409: {"description": "Conflict"},
}


def _not_found(exc: CatalogNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _conflict(exc: CatalogConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": exc.message, "field": exc.field},
    )


# --- read model ---------------------------------------------------------------------------------


@router.get("/status", response_model=StatusOut)
async def get_status(db: DbDep) -> StatusOut:
    return await status_page_svc.get_status(db)


@router.get("/summary", response_model=SummaryOut)
async def get_summary(db: DbDep) -> SummaryOut:
    return await status_page_svc.get_summary(db)


@router.get("/applications/{application_id}", response_model=ApplicationDetailOut)
async def get_application(application_id: uuid.UUID, db: DbDep) -> ApplicationDetailOut:
    detail = await status_page_svc.get_application_detail(db, application_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Applicazione non trovata"
        )
    return detail


@router.get(
    "/versions",
    response_model=VersionOut,
    responses={404: {"description": "Application or version not found"}},
)
async def get_version(
    db: DbDep,
    product_key: Annotated[str, Query(min_length=1)],
    env: Annotated[EnvTarget, Query()],
) -> VersionOut:
    """Last known version of an application (by ``product_key``) in an environment."""
    try:
        return await status_page_svc.get_version(db, product_key, env)
    except status_page_svc.VersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --- sync ---------------------------------------------------------------------------------------


@router.get("/sync-status", response_model=SyncStatusOut | None)
async def get_sync_status(db: DbDep) -> SyncStatusOut | None:
    row = await sync_svc.get_sync_status(db)
    return None if row is None else SyncStatusOut.model_validate(row)


@router.post("/sync", response_model=SyncStatusOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(body: ManualSyncIn, db: DbDep) -> SyncStatusOut:
    try:
        row = await sync_svc.request_manual_sync(db, user=body.user)
    except StatusPageSyncInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Sincronizzazione già in corso"
        ) from exc
    result = SyncStatusOut.model_validate(row)

    from app.tasks.sync_status_page import sync_status_page

    try:
        task = sync_status_page.apply_async(
            kwargs={"trigger": "MANUAL", "requested_by": body.user}, retry=False
        )
    except Exception as exc:
        logger.exception("status_page_sync_enqueue_failed", requested_by=body.user)
        await sync_svc.mark_enqueue_failed(db, "Impossibile accodare la sincronizzazione")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Impossibile avviare la sincronizzazione, riprova più tardi",
        ) from exc
    logger.info("status_page_sync_dispatched", task_id=task.id, requested_by=body.user)
    await sync_svc.set_sync_task_id(db, task.id)
    return result


# --- catalog ------------------------------------------------------------------------------------


@router.get("/catalog/products", response_model=list[ProductOut])
async def list_products(db: DbDep) -> list[ProductOut]:
    return await catalog_svc.list_products(db)


@router.post(
    "/catalog/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Conflict"}},
)
async def create_product(body: ProductIn, db: DbDep) -> ProductOut | JSONResponse:
    try:
        return await catalog_svc.create_product(db, body)
    except CatalogConflictError as exc:
        return _conflict(exc)


@router.patch(
    "/catalog/products/{product_id}",
    response_model=ProductOut,
    responses=_DELETE_RESPONSES,
)
async def update_product(
    product_id: uuid.UUID, body: ProductPatch, db: DbDep
) -> ProductOut | JSONResponse:
    try:
        return await catalog_svc.update_product(db, product_id, body)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
    except CatalogConflictError as exc:
        return _conflict(exc)


@router.delete(
    "/catalog/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_DELETE_RESPONSES,
)
async def delete_product(
    product_id: uuid.UUID, db: DbDep, body: Annotated[DeleteIn, Body(...)]
) -> Response:
    try:
        await catalog_svc.delete_product(db, product_id, body)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
    except CatalogConflictError as exc:
        return _conflict(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/catalog/applications", response_model=list[ApplicationOut])
async def list_applications(
    db: DbDep, product_id: Annotated[uuid.UUID | None, Query()] = None
) -> list[ApplicationOut]:
    return await catalog_svc.list_applications(db, product_id)


@router.post(
    "/catalog/applications",
    response_model=ApplicationOut,
    status_code=status.HTTP_201_CREATED,
    responses=_DELETE_RESPONSES,
)
async def create_application(body: ApplicationIn, db: DbDep) -> ApplicationOut | JSONResponse:
    try:
        return await catalog_svc.create_application(db, body)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
    except CatalogConflictError as exc:
        return _conflict(exc)


# Declared before the "/{application_id}" routes so the static segment is never shadowed.
@router.post("/catalog/applications/check", response_model=CheckOut)
async def check_application(body: CheckIn) -> CheckOut:
    return await catalog_svc.check_application(body)


@router.patch(
    "/catalog/applications/{application_id}",
    response_model=ApplicationOut,
    responses=_DELETE_RESPONSES,
)
async def update_application(
    application_id: uuid.UUID, body: ApplicationPatch, db: DbDep
) -> ApplicationOut | JSONResponse:
    try:
        return await catalog_svc.update_application(db, application_id, body)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
    except CatalogConflictError as exc:
        return _conflict(exc)


@router.delete(
    "/catalog/applications/{application_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_DELETE_RESPONSES,
)
async def delete_application(
    application_id: uuid.UUID, db: DbDep, body: Annotated[DeleteIn, Body(...)]
) -> Response:
    try:
        await catalog_svc.delete_application(db, application_id, body)
    except CatalogNotFoundError as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/catalog/test-objects", response_model=list[str])
async def list_test_objects(db: DbDep) -> list[str]:
    return await catalog_svc.list_test_objects(db)


@router.get("/catalog/ea-components", response_model=list[EaComponentOut])
async def list_ea_components(
    db: DbDep, q: Annotated[str | None, Query()] = None
) -> list[EaComponentOut]:
    return await catalog_svc.list_ea_components(db, q)
