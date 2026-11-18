from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from app.models.test_metrics import TestSuite
from app.schemas.test_metrics import TestSuiteOut
from app.services.test_metrics import get_latest_test_suite_version
from tests._db import TestSession

URL = "/api/v1/test-metrics/test-suites/latest/"
SVC = "app.services.test_metrics.get_latest_test_suite_version"


def _suite_out(version: str = "1.0.0") -> TestSuiteOut:
    return TestSuiteOut(
        id=uuid.uuid4(), test_object="x", test_type="integration", test_object_version=version
    )


@pytest.mark.anyio
async def test_latest_without_filters_keeps_previous_behaviour(client: AsyncClient) -> None:
    with patch(SVC, new_callable=AsyncMock, return_value=_suite_out()) as svc:
        response = await client.get(URL, params={"test_object": "x"})
    assert response.status_code == 200
    assert svc.await_args.args[1:] == ("x", None, None)


@pytest.mark.anyio
async def test_latest_forwards_optional_version_and_type(client: AsyncClient) -> None:
    with patch(SVC, new_callable=AsyncMock, return_value=_suite_out("0.9.0")) as svc:
        response = await client.get(
            URL,
            params={"test_object": "x", "test_object_version": "0.9.0", "test_type": "e2e"},
        )
    assert response.status_code == 200
    assert svc.await_args.args[1:] == ("x", "0.9.0", "e2e")


@pytest.mark.anyio
async def test_latest_not_found_is_404(client: AsyncClient) -> None:
    with patch(SVC, new_callable=AsyncMock, return_value=None):
        response = await client.get(URL, params={"test_object": "x", "test_object_version": "9"})
    assert response.status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("param", ["test_object_version", "test_type"])
async def test_latest_rejects_empty_optional_filters(client: AsyncClient, param: str) -> None:
    response = await client.get(URL, params={"test_object": "x", param: ""})
    assert response.status_code == 400


# ---------------------------------------------------------------- service (real DB)


@pytest_asyncio.fixture
async def suites() -> AsyncIterator[str]:
    test_object = f"zz-{uuid.uuid4().hex[:8]}"
    async with TestSession() as db:
        for version, test_type in (
            ("0.9.0", "integration"),
            ("0.10.0", "integration"),
            ("0.10.0", "e2e"),
        ):
            db.add(
                TestSuite(test_object=test_object, test_type=test_type, test_object_version=version)
            )
        await db.commit()
    yield test_object
    async with TestSession() as db:
        await db.execute(delete(TestSuite).where(TestSuite.test_object == test_object))
        await db.commit()


async def test_service_exact_version_is_not_confused_by_string_ordering(suites: str) -> None:
    async with TestSession() as db:
        found = await get_latest_test_suite_version(db, suites, "0.10.0", "integration")
        missing = await get_latest_test_suite_version(db, suites, "0.11.0", "integration")
    assert found is not None
    assert (found.test_object_version, found.test_type) == ("0.10.0", "integration")
    assert missing is None


async def test_service_type_filter_selects_the_matching_suite(suites: str) -> None:
    async with TestSession() as db:
        found = await get_latest_test_suite_version(db, suites, "0.10.0", "e2e")
    assert found is not None
    assert found.test_type == "e2e"


async def test_service_without_filters_returns_the_previous_latest(suites: str) -> None:
    async with TestSession() as db:
        found = await get_latest_test_suite_version(db, suites)
    assert found is not None
    assert found.test_object_version == "0.9.0"  # legacy string ordering, unchanged
