from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.schemas.status_page import (
    ApplicationOut,
    CheckOut,
    CheckTargetOut,
    EaComponentOut,
    ProductOut,
    StatusOut,
    SummaryOut,
    VersionOut,
)
from app.services.status_page import VersionNotFoundError
from app.services.status_page_catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from app.services.status_page_sync import StatusPageSyncInProgressError

BASE = "/api/v1/releases"
CAT = "app.services.status_page_catalog."
USER = "qa@example.com"


def _sync_row() -> SimpleNamespace:
    return SimpleNamespace(
        status="QUEUED",
        trigger="MANUAL",
        requested_by=USER,
        queued_at=None,
        started_at=None,
        finished_at=None,
        last_success_at=None,
        applications_checked=0,
        reads_failed=0,
        errors=[],
    )


def _product() -> ProductOut:
    return ProductOut(id=uuid.uuid4(), name="P", area="CORE", hidden=False, application_count=0)


def _application() -> ApplicationOut:
    return ApplicationOut(
        id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        name="A",
        product_key="a",
        repository="repo",
        pipeline_url=None,
        runtime="AKS",
        ci="GHA",
        kind="backend",
        test_object=None,
        ea_node_id=None,
        hidden=False,
        version_source="AUTO",
        manual_versions=None,
    )


def _app_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "product_id": str(uuid.uuid4()),
        "name": "A",
        "product_key": "a",
        "repository": "repo",
        "runtime": "AKS",
        "ci": "GHA",
        "user": USER,
    }
    body.update(kw)
    return body


# --- read model ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_status_and_summary(client: AsyncClient) -> None:
    summary = SummaryOut(
        total=1, aligned=1, drift=0, unreachable=0, untested=0, last_success_at=None
    )
    with (
        patch(
            "app.services.status_page.get_status",
            new_callable=AsyncMock,
            return_value=StatusOut(sync=None, areas=[]),
        ),
        patch("app.services.status_page.get_summary", new_callable=AsyncMock, return_value=summary),
    ):
        r1 = await client.get(f"{BASE}/status")
        r2 = await client.get(f"{BASE}/summary")
    assert r1.status_code == 200
    assert r1.json() == {"sync": None, "areas": []}
    assert r2.status_code == 200
    assert r2.json()["total"] == 1


@pytest.mark.anyio
async def test_application_detail_unknown_is_404(client: AsyncClient) -> None:
    with patch(
        "app.services.status_page.get_application_detail",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.get(f"{BASE}/applications/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Applicazione non trovata"


@pytest.mark.anyio
async def test_application_detail_bad_uuid_is_422(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/applications/not-a-uuid")
    assert response.status_code == 422


@pytest.mark.anyio
async def test_version_lookup_returns_state(client: AsyncClient) -> None:
    out = VersionOut(
        product_key="fdrfase3",
        env="UAT",
        version="1.2.3",
        read_status="OK",
        error_detail=None,
        checked_at=datetime(2026, 3, 1, 10, tzinfo=UTC),
        last_ok_at=datetime(2026, 3, 1, 10, tzinfo=UTC),
        version_since=datetime(2026, 2, 1, 9, tzinfo=UTC),
    )
    with patch(
        "app.services.status_page.get_version", new_callable=AsyncMock, return_value=out
    ) as svc:
        response = await client.get(
            f"{BASE}/versions", params={"product_key": "fdrfase3", "env": "UAT"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "1.2.3"
    assert body["read_status"] == "OK"
    assert body["env"] == "UAT"
    assert svc.await_args.args[1:] == ("fdrfase3", "UAT")


@pytest.mark.anyio
async def test_version_lookup_unknown_is_404_with_reason(client: AsyncClient) -> None:
    with patch(
        "app.services.status_page.get_version",
        new_callable=AsyncMock,
        side_effect=VersionNotFoundError("Applicazione non trovata"),
    ):
        response = await client.get(
            f"{BASE}/versions", params={"product_key": "nope", "env": "DEV"}
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "Applicazione non trovata"


@pytest.mark.anyio
async def test_version_lookup_requires_valid_params(client: AsyncClient) -> None:
    assert (await client.get(f"{BASE}/versions", params={"env": "DEV"})).status_code == 422
    assert (await client.get(f"{BASE}/versions", params={"product_key": "a"})).status_code == 422
    bad_env = await client.get(f"{BASE}/versions", params={"product_key": "a", "env": "REPO"})
    assert bad_env.status_code == 422


# --- sync ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sync_status_none_and_static_route_not_shadowed(client: AsyncClient) -> None:
    with patch(
        "app.services.status_page_sync.get_sync_status", new_callable=AsyncMock, return_value=None
    ):
        response = await client.get(f"{BASE}/sync-status")
    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.anyio
async def test_sync_status_returns_row(client: AsyncClient) -> None:
    with patch(
        "app.services.status_page_sync.get_sync_status",
        new_callable=AsyncMock,
        return_value=_sync_row(),
    ):
        response = await client.get(f"{BASE}/sync-status")
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"


@pytest.mark.anyio
async def test_manual_sync_dispatches_task_and_stores_id(client: AsyncClient) -> None:
    task = SimpleNamespace(id="task-1")
    with (
        patch(
            "app.services.status_page_sync.request_manual_sync",
            new_callable=AsyncMock,
            return_value=_sync_row(),
        ) as request,
        patch("app.services.status_page_sync.set_sync_task_id", new_callable=AsyncMock) as store,
        patch(
            "app.tasks.sync_status_page.sync_status_page.apply_async", return_value=task
        ) as apply,
    ):
        response = await client.post(f"{BASE}/sync", json={"user": USER})

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert request.await_args_list[0].kwargs == {"user": USER}
    apply.assert_called_once_with(kwargs={"trigger": "MANUAL", "requested_by": USER}, retry=False)
    assert store.await_args_list[0].args[1] == "task-1"


@pytest.mark.anyio
async def test_manual_sync_in_progress_is_409(client: AsyncClient) -> None:
    with (
        patch(
            "app.services.status_page_sync.request_manual_sync",
            new_callable=AsyncMock,
            side_effect=StatusPageSyncInProgressError("busy"),
        ),
        patch("app.tasks.sync_status_page.sync_status_page.apply_async") as apply,
    ):
        response = await client.post(f"{BASE}/sync", json={"user": USER})
    assert response.status_code == 409
    assert response.json()["detail"] == "Sincronizzazione già in corso"
    apply.assert_not_called()


@pytest.mark.anyio
async def test_manual_sync_requires_user(client: AsyncClient) -> None:
    response = await client.post(f"{BASE}/sync", json={})
    assert response.status_code == 422


# --- catalog ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_and_create_product(client: AsyncClient) -> None:
    product = _product()
    with patch(CAT + "list_products", new_callable=AsyncMock, return_value=[product]):
        listed = await client.get(f"{BASE}/catalog/products")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "P"
    with patch(CAT + "create_product", new_callable=AsyncMock, return_value=product):
        created = await client.post(
            f"{BASE}/catalog/products", json={"name": "P", "area": "CORE", "user": USER}
        )
    assert created.status_code == 201


@pytest.mark.anyio
async def test_product_conflict_and_not_found(client: AsyncClient) -> None:
    pid = uuid.uuid4()
    conflict = CatalogConflictError("name", "Esiste già")
    with patch(CAT + "create_product", new_callable=AsyncMock, side_effect=conflict):
        r = await client.post(
            f"{BASE}/catalog/products", json={"name": "P", "area": "CORE", "user": USER}
        )
    assert r.status_code == 409
    assert r.json() == {"detail": "Esiste già", "field": "name"}

    with patch(
        CAT + "update_product", new_callable=AsyncMock, side_effect=CatalogNotFoundError("x")
    ):
        r = await client.patch(f"{BASE}/catalog/products/{pid}", json={"user": USER})
    assert r.status_code == 404

    with patch(CAT + "update_product", new_callable=AsyncMock, return_value=_product()):
        r = await client.patch(f"{BASE}/catalog/products/{pid}", json={"user": USER})
    assert r.status_code == 200


@pytest.mark.anyio
async def test_delete_product_with_body(client: AsyncClient) -> None:
    pid = uuid.uuid4()
    with patch(CAT + "delete_product", new_callable=AsyncMock) as delete:
        r = await client.request("DELETE", f"{BASE}/catalog/products/{pid}", json={"user": USER})
    assert r.status_code == 204
    assert delete.await_args_list[0].args[1] == pid
    assert delete.await_args_list[0].args[2].user == USER

    refused = CatalogConflictError("applications", "Il prodotto ha ancora applicazioni")
    with patch(CAT + "delete_product", new_callable=AsyncMock, side_effect=refused):
        r = await client.request("DELETE", f"{BASE}/catalog/products/{pid}", json={"user": USER})
    assert r.status_code == 409
    assert r.json()["field"] == "applications"

    with patch(
        CAT + "delete_product", new_callable=AsyncMock, side_effect=CatalogNotFoundError("x")
    ):
        r = await client.request("DELETE", f"{BASE}/catalog/products/{pid}", json={"user": USER})
    assert r.status_code == 404

    r = await client.request("DELETE", f"{BASE}/catalog/products/{pid}", json={})
    assert r.status_code == 422


@pytest.mark.anyio
async def test_applications_list_filter_create_update_delete(client: AsyncClient) -> None:
    application = _application()
    pid = uuid.uuid4()
    with patch(CAT + "list_applications", new_callable=AsyncMock, return_value=[application]) as ls:
        r = await client.get(f"{BASE}/catalog/applications", params={"product_id": str(pid)})
    assert r.status_code == 200
    assert ls.await_args_list[0].args[1] == pid

    with patch(CAT + "create_application", new_callable=AsyncMock, return_value=application):
        r = await client.post(f"{BASE}/catalog/applications", json=_app_body())
    assert r.status_code == 201

    conflict = CatalogConflictError("product_key", "Chiave già in uso")
    with patch(CAT + "create_application", new_callable=AsyncMock, side_effect=conflict):
        r = await client.post(f"{BASE}/catalog/applications", json=_app_body())
    assert r.status_code == 409
    assert r.json() == {"detail": "Chiave già in uso", "field": "product_key"}

    ea_missing = CatalogNotFoundError("Componente EA non trovato")
    with patch(CAT + "create_application", new_callable=AsyncMock, side_effect=ea_missing):
        r = await client.post(f"{BASE}/catalog/applications", json=_app_body())
    assert r.status_code == 404
    assert r.json()["detail"] == "Componente EA non trovato"

    aid = application.id
    with patch(CAT + "update_application", new_callable=AsyncMock, return_value=application):
        r = await client.patch(f"{BASE}/catalog/applications/{aid}", json={"user": USER})
    assert r.status_code == 200
    with patch(CAT + "update_application", new_callable=AsyncMock, side_effect=conflict):
        r = await client.patch(f"{BASE}/catalog/applications/{aid}", json={"user": USER})
    assert r.status_code == 409
    with patch(CAT + "update_application", new_callable=AsyncMock, side_effect=ea_missing):
        r = await client.patch(f"{BASE}/catalog/applications/{aid}", json={"user": USER})
    assert r.status_code == 404

    with patch(CAT + "delete_application", new_callable=AsyncMock):
        r = await client.request(
            "DELETE", f"{BASE}/catalog/applications/{aid}", json={"user": USER}
        )
    assert r.status_code == 204
    with patch(
        CAT + "delete_application", new_callable=AsyncMock, side_effect=CatalogNotFoundError("x")
    ):
        r = await client.request(
            "DELETE", f"{BASE}/catalog/applications/{aid}", json={"user": USER}
        )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_static_catalog_routes_not_shadowed(client: AsyncClient) -> None:
    check = CheckOut(repo=CheckTargetOut(ok=True, version="1.0.0", detail=None), envs={})
    with patch(CAT + "check_application", new_callable=AsyncMock, return_value=check):
        r = await client.post(
            f"{BASE}/catalog/applications/check",
            json={"product_key": "a", "repository": "repo"},
        )
    assert r.status_code == 200
    assert r.json()["repo"]["ok"] is True

    with patch(CAT + "list_test_objects", new_callable=AsyncMock, return_value=["obj-1"]):
        r = await client.get(f"{BASE}/catalog/test-objects")
    assert r.status_code == 200
    assert r.json() == ["obj-1"]

    with patch(
        CAT + "list_ea_components",
        new_callable=AsyncMock,
        return_value=[EaComponentOut(id="c1", name="Comp")],
    ) as ea:
        r = await client.get(f"{BASE}/catalog/ea-components", params={"q": "co"})
    assert r.status_code == 200
    assert r.json() == [{"id": "c1", "name": "Comp"}]
    assert ea.await_args_list[0].args[1] == "co"


@pytest.mark.anyio
async def test_manual_sync_enqueue_failure_is_503_and_marks_failed(client: AsyncClient) -> None:
    with (
        patch(
            "app.services.status_page_sync.request_manual_sync",
            new_callable=AsyncMock,
            return_value=_sync_row(),
        ),
        patch("app.services.status_page_sync.mark_enqueue_failed", new_callable=AsyncMock) as mark,
        patch("app.services.status_page_sync.set_sync_task_id", new_callable=AsyncMock) as store,
        patch(
            "app.tasks.sync_status_page.sync_status_page.apply_async",
            side_effect=ConnectionError("broker down"),
        ),
    ):
        response = await client.post(f"{BASE}/sync", json={"user": USER})

    assert response.status_code == 503
    assert response.json()["detail"] == "Impossibile avviare la sincronizzazione, riprova più tardi"
    assert mark.await_args_list[0].args[1] == "Impossibile accodare la sincronizzazione"
    store.assert_not_called()


@pytest.mark.anyio
async def test_application_new_fields_round_trip_and_defaults(client: AsyncClient) -> None:
    application = _application()
    application.version_source = "MANUAL"
    application.hidden = True
    application.manual_versions = {"DEV": None, "UAT": "2.0.0", "PROD": None}
    body = _app_body(
        hidden=True, version_source="MANUAL", manual_versions={"UAT": "2.0.0", "PROD": None}
    )
    with patch(
        CAT + "create_application", new_callable=AsyncMock, return_value=application
    ) as create:
        r = await client.post(f"{BASE}/catalog/applications", json=body)
    assert r.status_code == 201
    assert r.json()["version_source"] == "MANUAL"
    assert r.json()["manual_versions"] == {"DEV": None, "UAT": "2.0.0", "PROD": None}
    sent = create.await_args.args[1]
    assert sent.manual_versions.model_dump(exclude_unset=True) == {"UAT": "2.0.0", "PROD": None}

    with patch(CAT + "create_application", new_callable=AsyncMock, return_value=_application()):
        r = await client.post(f"{BASE}/catalog/applications", json=_app_body())
    assert r.json()["hidden"] is False
    assert r.json()["version_source"] == "AUTO"
    assert r.json()["manual_versions"] is None

    with patch(
        CAT + "update_application", new_callable=AsyncMock, return_value=application
    ) as update:
        r = await client.patch(
            f"{BASE}/catalog/applications/{application.id}",
            json={"user": USER, "manual_versions": {"DEV": "  1.0  "}},
        )
    assert r.status_code == 200
    patch_in = update.await_args.args[2]
    assert patch_in.manual_versions.model_dump(exclude_unset=True) == {"DEV": "1.0"}
    assert patch_in.version_source is None and patch_in.hidden is None


@pytest.mark.anyio
async def test_manual_version_too_long_is_422(client: AsyncClient) -> None:
    r = await client.patch(
        f"{BASE}/catalog/applications/{uuid.uuid4()}",
        json={"user": USER, "manual_versions": {"DEV": "x" * 101}},
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_catalog_validation_error_is_422_with_field(client: AsyncClient) -> None:
    err = CatalogValidationError("manual_versions", "Le versioni manuali richiedono")
    aid = uuid.uuid4()
    with patch(CAT + "update_application", new_callable=AsyncMock, side_effect=err):
        r = await client.patch(f"{BASE}/catalog/applications/{aid}", json={"user": USER})
    assert r.status_code == 422
    assert r.json() == {"detail": "Le versioni manuali richiedono", "field": "manual_versions"}
    with patch(CAT + "create_application", new_callable=AsyncMock, side_effect=err):
        r = await client.post(f"{BASE}/catalog/applications", json=_app_body())
    assert r.status_code == 422
    assert r.json()["field"] == "manual_versions"


@pytest.mark.anyio
async def test_product_hidden_fields(client: AsyncClient) -> None:
    product = _product()
    product.hidden = True
    with patch(CAT + "create_product", new_callable=AsyncMock, return_value=product) as create:
        r = await client.post(
            f"{BASE}/catalog/products",
            json={"name": "P", "area": "CORE", "hidden": True, "user": USER},
        )
    assert r.status_code == 201
    assert r.json()["hidden"] is True
    assert create.await_args.args[1].hidden is True
    with patch(CAT + "update_product", new_callable=AsyncMock, return_value=product) as update:
        r = await client.patch(
            f"{BASE}/catalog/products/{product.id}", json={"hidden": False, "user": USER}
        )
    assert r.status_code == 200
    assert update.await_args.args[2].hidden is False
