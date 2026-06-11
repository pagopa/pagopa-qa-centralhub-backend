from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.role import RoleMatrixResponse, RolePermissionUpdate
from app.services import roles as roles_svc

router = APIRouter()


@router.get("", response_model=RoleMatrixResponse)
async def get_role_matrix(db: DbDep) -> RoleMatrixResponse:
    return await roles_svc.get_role_matrix(db)


@router.patch("/{role}", response_model=dict[str, bool])
async def update_role_permissions(role: str, payload: RolePermissionUpdate, db: DbDep) -> dict[str, bool]:
    try:
        return await roles_svc.update_role_permissions(db, role, payload.permissions)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
