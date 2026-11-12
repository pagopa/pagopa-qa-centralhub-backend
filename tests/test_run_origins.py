from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.services import quality_gate as svc
from app.services.github import GitHubClient

HEX = "a" * 32
RUN_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "tok")


@pytest.mark.parametrize(
    ("title", "origin"),
    [
        (f"tas-qg-{HEX}", "QG"),
        (f"tas-{HEX}", "TAS"),
        (f"nrt-{HEX}", "NRT"),
        ("tas-checkout-123", None),
        ("tas-12345", None),
        ("NRT - Main Dispatch", None),
    ],
)
def test_title_regexes_keep_origins_apart(title: str, origin: str | None) -> None:
    matching = [name for name, cfg in svc.ORIGINS.items() if cfg.title_regex.match(title)]
    assert matching == ([origin] if origin else [])


def _gh_run(name: str, created: datetime, status_: str = "in_progress") -> dict:
    return {
        "display_title": name,
        "status": status_,
        "conclusion": None,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/x/runs/{name}",
    }


async def test_list_runs_per_origin(configured: None) -> None:
    now = datetime.now(UTC)
    runs = [
        _gh_run(f"tas-qg-{HEX}", now),
        _gh_run(f"tas-{'b' * 32}", now - timedelta(minutes=1)),
        _gh_run(f"nrt-{'c' * 32}", now - timedelta(minutes=2)),
        _gh_run("tas-12345", now - timedelta(minutes=3)),
    ]
    with patch.object(
        GitHubClient, "list_dispatched_runs", new_callable=AsyncMock, return_value=runs
    ) as listed:
        tas = await svc.list_quality_gate_runs("TAS")
        nrt = await svc.list_quality_gate_runs("NRT")
        qg = await svc.list_quality_gate_runs("QG")

    assert [r.correlation_id for r in tas] == ["b" * 32]
    assert [r.run_name for r in tas] == [f"tas-{'b' * 32}"]
    assert [r.correlation_id for r in nrt] == ["c" * 32]
    assert [r.correlation_id for r in qg] == [f"qg-{HEX}"]
    # NRT runs are looked up in the NRT workflow, the others in the TAS one.
    assert [c.args[0] for c in listed.await_args_list] == [
        settings.tas_workflow_file,
        settings.nrt_workflow_file,
        settings.tas_workflow_file,
    ]


@pytest.mark.parametrize(
    ("origin", "correlation_id", "run_name", "workflow_attr"),
    [
        ("QG", f"qg-{HEX}", f"tas-qg-{HEX}", "tas_workflow_file"),
        ("TAS", HEX, f"tas-{HEX}", "tas_workflow_file"),
        ("NRT", HEX, f"nrt-{HEX}", "nrt_workflow_file"),
    ],
)
async def test_status_run_name_per_origin(
    configured: None, origin: str, correlation_id: str, run_name: str, workflow_attr: str
) -> None:
    with patch.object(
        GitHubClient, "find_workflow_run_by_name", new_callable=AsyncMock, return_value=None
    ) as find:
        out = await svc.get_quality_gate_run_status(correlation_id, origin)
    find.assert_awaited_once_with(getattr(settings, workflow_attr), run_name)
    assert out.run_name == run_name
    assert out.state == "pending"


def _run(origin: str | None) -> MagicMock:
    run = MagicMock()
    run.test_origin = origin
    run.suite_id = "s"
    run.timestamp_start = "2026-01-01"
    run.allure_url = "u"
    run.revision_result = None
    return run


def _db_returning(run: MagicMock | None) -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.parametrize(("origin", "run_origin"), [("TAS", "TAS"), ("NRT", "NRT"), ("NRT", None)])
async def test_delete_accepts_runs_of_the_requested_origin(
    origin: str, run_origin: str | None
) -> None:
    db = _db_returning(_run(run_origin))
    await svc.delete_quality_gate_run(db, uuid.UUID(RUN_ID), origin)
    assert db.execute.await_count == 2  # lookup + delete
    audit = db.add.call_args.args[0]
    assert audit.action == svc.DELETE_AUDIT_ACTION
    assert f'"origin": "{origin}"' in audit.detail


@pytest.mark.parametrize(
    ("origin", "run_origin"),
    [("TAS", "QG"), ("TAS", "NRT"), ("TAS", None), ("NRT", "TAS"), ("NRT", "QG"), ("QG", "TAS")],
)
async def test_delete_refuses_runs_of_another_origin(origin: str, run_origin: str | None) -> None:
    db = _db_returning(_run(run_origin))
    with pytest.raises(svc.NotAQualityGateRunError):
        await svc.delete_quality_gate_run(db, uuid.UUID(RUN_ID), origin)
    assert db.execute.await_count == 1  # lookup only, nothing deleted
    db.add.assert_not_called()


# --- launch ---------------------------------------------------------------------------------


def _fake_db() -> MagicMock:
    return _db_returning(None)


@pytest.fixture
def no_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "qg_launch_suites", "")
    monkeypatch.setattr(svc, "_suite_catalog_cache", None)
    monkeypatch.setattr(svc, "_nrt_registry_cache", None)
    monkeypatch.setattr(svc, "get_suite_catalog", AsyncMock(return_value=None))


async def test_launch_tas_uses_bare_hex_and_origin_flag(configured: None, no_catalog: None) -> None:
    db = _fake_db()
    body = svc.QualityGateRunCreate(test_suite="checkout", test_type="e2e", environment="uat")
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        out = await svc.launch_quality_gate_run(db, body, "TAS")

    assert re.fullmatch(r"[0-9a-f]{32}", out.correlation_id)
    assert out.run_name == f"tas-{out.correlation_id}"
    assert dispatch.await_args.args[0] == settings.tas_workflow_file
    inputs = dispatch.await_args.kwargs["inputs"]
    assert inputs["isQualityGateRun"] == "false"
    assert inputs["correlation_id"] == out.correlation_id
    assert svc.ORIGINS["TAS"].title_regex.match(out.run_name)
    assert db.add.call_args.args[0].action == "tas.run.dispatch"


async def test_launch_qg_keeps_its_id_format(configured: None, no_catalog: None) -> None:
    body = svc.QualityGateRunCreate(test_suite="checkout")
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        out = await svc.launch_quality_gate_run(_fake_db(), body)
    assert out.run_name == f"tas-{out.correlation_id}"
    assert out.correlation_id.startswith("qg-")
    assert dispatch.await_args.kwargs["inputs"]["isQualityGateRun"] == "true"


REGISTRY = {
    "suites": [
        {"id": "wisp", "label": "WISP", "envs": ["dev", "uat"]},
        {"id": "checkout", "label": "Checkout E2E", "envs": ["uat"]},
    ]
}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, no_catalog: None) -> AsyncMock:
    reader = AsyncMock(return_value=json.dumps(REGISTRY).encode())
    monkeypatch.setattr(GitHubClient, "get_file_content", reader)
    return reader


async def test_nrt_launch_options_are_parsed_and_cached(
    configured: None, registry: AsyncMock
) -> None:
    first = await svc.get_nrt_launch_options()
    second = await svc.get_nrt_launch_options()
    assert first.available is True
    assert [(s.id, s.label, s.envs) for s in first.suites] == [
        ("wisp", "WISP", ["dev", "uat"]),
        ("checkout", "Checkout E2E", ["uat"]),
    ]
    assert second == first
    registry.assert_awaited_once()
    assert registry.await_args.args[0] == ".github/nrt-suites.json"


async def test_nrt_launch_options_unavailable_when_the_repository_cannot_be_read(
    configured: None, no_catalog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        GitHubClient, "get_file_content", AsyncMock(side_effect=httpx.ConnectError("boom"))
    )
    out = await svc.get_nrt_launch_options()
    assert out.available is False
    assert out.suites == []


async def test_launch_nrt_dispatches_the_nrt_workflow(
    configured: None, registry: AsyncMock
) -> None:
    db = _fake_db()
    body = svc.NrtRunCreate(suites=["wisp", "checkout"], target_env="uat")
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        out = await svc.launch_nrt_run(db, body)

    assert dispatch.await_args.args[0] == settings.nrt_workflow_file
    inputs = dispatch.await_args.kwargs["inputs"]
    assert inputs == {
        "suites": "wisp,checkout",
        "target_env": "uat",
        "correlation_id": out.correlation_id,
    }
    assert out.run_name == f"nrt-{out.correlation_id}"
    assert svc.ORIGINS["NRT"].title_regex.match(out.run_name)
    assert db.add.call_args.args[0].action == "nrt.run.dispatch"


async def test_launch_nrt_all_sends_all(configured: None, registry: AsyncMock) -> None:
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        await svc.launch_nrt_run(_fake_db(), svc.NrtRunCreate(suites="all", target_env="dev"))
    assert dispatch.await_args.kwargs["inputs"]["suites"] == "all"


async def test_launch_nrt_rejects_unknown_suites_before_dispatching(
    configured: None, registry: AsyncMock
) -> None:
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        with pytest.raises(svc.NrtSuiteSelectionError) as excinfo:
            await svc.launch_nrt_run(
                _fake_db(), svc.NrtRunCreate(suites=["wisp", "nope"], target_env="uat")
            )
    assert excinfo.value.unknown == ["nope"]
    dispatch.assert_not_awaited()


async def test_launch_nrt_rejects_a_selection_without_suites_for_the_environment(
    configured: None, registry: AsyncMock
) -> None:
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        with pytest.raises(svc.NrtSuiteSelectionError) as excinfo:
            await svc.launch_nrt_run(
                _fake_db(), svc.NrtRunCreate(suites=["checkout"], target_env="dev")
            )
    assert excinfo.value.unknown == []
    assert excinfo.value.target_env == "dev"
    dispatch.assert_not_awaited()


async def test_launch_nrt_skips_validation_when_the_registry_is_unavailable(
    configured: None, no_catalog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        GitHubClient, "get_file_content", AsyncMock(side_effect=httpx.ConnectError("boom"))
    )
    with patch.object(GitHubClient, "dispatch_workflow", new_callable=AsyncMock) as dispatch:
        body = svc.NrtRunCreate(suites=["whatever"], target_env="dev")
        await svc.launch_nrt_run(_fake_db(), body)
    dispatch.assert_awaited_once()


async def test_launch_nrt_audits_a_failed_dispatch(configured: None, registry: AsyncMock) -> None:
    db = _fake_db()
    failing = AsyncMock(side_effect=httpx.ConnectError("x"))
    with patch.object(GitHubClient, "dispatch_workflow", failing):
        with pytest.raises(httpx.ConnectError):
            await svc.launch_nrt_run(db, svc.NrtRunCreate(suites="all", target_env="uat"))
    assert '"outcome": "failed"' in db.add.call_args.args[0].detail


async def test_launch_nrt_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tas_github_token", "")
    with pytest.raises(svc.QualityGateConfigError):
        await svc.launch_nrt_run(_fake_db(), svc.NrtRunCreate())
