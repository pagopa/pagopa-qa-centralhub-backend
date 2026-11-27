# ruff: noqa: F811, S101
"""Git sync import keeps every tag and takes the test type from the file (design 2026-11-26)."""
from __future__ import annotations

from sqlalchemy import select

from app.models.test_lifecycle import TlScenarioContent
from tests._db import TestSession
from tests.test_tl_features import (  # noqa: F401  (fixture riusate)
    _use_test_db,
    actor,
    scope,
)
from tests.test_tl_git_import import (  # noqa: F401
    _detail,
    _events,
    _feature,
    _import,
    _main_text,
    _merged,
    _tags,
    fake,
)
from tests.test_tl_rebase import _reopened  # noqa: F401
from tests.test_tl_workflow import reviewer  # noqa: F401


def _edited(fake, done, scope, *, test_type="", scenario_extra=""):
    feature_tag, _ = _tags(scope)
    text = _main_text(fake, done)
    if test_type:
        assert f"{feature_tag}\n@integration\n" in text  # the hub file already carries the type
        text = text.replace(f"{feature_tag}\n@integration", f"{feature_tag}\n{test_type}", 1)
    if scenario_extra:
        text = text.replace(
            "@runnable\n  @positive", f"@runnable {scenario_extra} @positive", 1
        )
    return text


async def _extra_tags(revision_id):
    async with TestSession() as s:
        rows = (
            await s.execute(
                select(TlScenarioContent.extra_tags).where(
                    TlScenarioContent.revision_id == revision_id
                )
            )
        ).scalars()
        return list(rows)


async def test_sync_keeps_custom_tags_and_takes_the_test_type(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    assert (await _feature(done["id"])).test_type == "integration"
    sha = fake.seed_file(
        path, _edited(fake, done, scope, test_type="@api", scenario_extra="@checkout @flaky")
    )
    await _import(fake, path, sha)

    assert (await _feature(done["id"])).test_type == "api"
    detail = await _detail(client, done["id"])
    assert detail["open_issues"] == 0
    tags = (await _extra_tags(detail["published_revision"]["id"]))[0]
    assert tags == ["runnable", "checkout", "flaky"]


async def test_sync_records_a_system_event_only_when_the_test_type_changes(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    sha = fake.seed_file(path, _edited(fake, done, scope, test_type="@api"))
    await _import(fake, path, sha)
    await _import(fake, path, sha)  # same content again: nothing new
    events = await _events(done["id"], "tl.feature.updated")
    changes = [e for e in events if e.get("test_type") == "api"]
    assert len(changes) == 1


async def test_sync_without_test_type_tag_does_not_clear_it(client, scope, actor, reviewer, fake):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    path = done["git_path"]
    text = _main_text(fake, done).replace(" @integration", "", 1)
    sha = fake.seed_file(path, text.replace("Quando paga\n", "Quando paga in contanti\n"))
    await _import(fake, path, sha)
    assert (await _feature(done["id"])).test_type == "integration"


async def test_sync_does_not_overwrite_the_test_type_of_a_reopened_draft(
    client, scope, actor, reviewer, fake
):
    done, _ = await _reopened(client, scope, actor, reviewer, fake)
    resp = await client.patch(
        f"/api/v1/test-lifecycle/features/{done['id']}",
        json={"actor_email": actor["email"], "test_type": "e2e"},
    )
    assert resp.status_code == 200, resp.text
    path = done["git_path"]
    # an unrelated push to the same file that still declares the old type
    sha = fake.seed_file(
        path, _main_text(fake, done).replace("Quando paga\n", "Quando paga in contanti\n")
    )
    await _import(fake, path, sha)
    assert (await _feature(done["id"])).test_type == "e2e"
