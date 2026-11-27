# ruff: noqa: F811, S101
"""Working-branch sync: accept the repository version (design 2026-11-26, git sync)."""
from __future__ import annotations

import pytest
from sqlalchemy import update

from app.models.test_lifecycle import TlFeature, TlFeatureRevision, TlRevisionStatus
from app.services.test_lifecycle import branch_sync
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.git_import_file import ImportFileNotFound, ImportParseError
from tests._db import TestSession
from tests.test_tl_branch_sync import (  # noqa: F401  (helpers e fixture riusate)
    BASE,
    BRANCH,
    STEPS_A,
    _edit,
    _path,
    _push,
    _rev,
    _setup,
    _status,
    _use_test_db,
    actor,
    fake,
    scope,
)
from tests.test_tl_git_import import _events


async def _pull(fake, fid, rid, actor):
    async with TestSession() as s:
        user = await resolve_actor(s, actor["email"])
        return await branch_sync.pull(s, fid, rid, user, fake)


async def _detail(client, fid):
    return (await client.get(f"{BASE}/features/{fid}")).json()


async def _pushed(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(fake, fid, rid, actor)
    path = await _path(fid, rid)
    return fid, rid, path, fake.files(BRANCH)[path][0]


async def test_pull_brings_the_repository_version_into_the_draft(client, scope, actor, fake):
    fid, rid, path, text = await _pushed(client, scope, actor, fake)
    before = await _detail(client, fid)
    first = before["working_revision"]["scenarios"][0]
    new_tag = first["tag"][:-2] + "02"
    edited = text.replace("Quando paga", "Quando paga con carta")
    edited = edited.replace(f"{first['tag'][:-3]}\n", f"{first['tag'][:-3]} @api\n", 1)
    edited = edited.replace(
        "@runnable\n  @positive", "@runnable @checkout @positive", 1
    )
    edited += f"\n  @runnable @positive\n  {new_tag}\n  Scenario: Nuovo\n    Dato x\n    Allora y\n"
    fake.seed_file(path, edited, branch=BRANCH)

    detail = await _pull(fake, fid, rid, actor)
    rev = detail.working_revision
    assert [s.tag for s in rev.scenarios] == [first["tag"], new_tag]
    assert "con carta" in rev.scenarios[0].steps
    assert "checkout" in rev.scenarios[0].extra_tags
    assert str(rev.scenarios[0].id) == first["id"]  # the identity survives
    assert detail.test_type == "api"
    assert rev.lock_version > before["working_revision"]["lock_version"]
    assert (await _rev(rid)).work_file_sha == fake.files(BRANCH)[path][1]
    assert (await _status(fake, fid, rid)).state == "in_sync"
    assert len(await _events(fid, "tl.git.pulled")) == 1


async def test_pull_takes_the_title_of_the_file(client, scope, actor, fake):
    fid, rid, path, text = await _pushed(client, scope, actor, fake)
    title = (await _detail(client, fid))["working_revision"]["title"]
    renamed = text.replace(f"Funzionalità: {title}", "Funzionalità: Altro titolo")
    fake.seed_file(path, renamed, BRANCH)
    detail = await _pull(fake, fid, rid, actor)
    assert detail.working_revision.title == "Altro titolo"


async def _unchanged(client, fid, rid, before):
    after = await _detail(client, fid)
    assert after["working_revision"]["scenarios"] == before["working_revision"]["scenarios"]
    assert after["working_revision"]["lock_version"] == before["working_revision"]["lock_version"]
    assert (await _rev(rid)).work_file_sha == before["sha"]


async def test_a_file_that_does_not_parse_changes_nothing(client, scope, actor, fake):
    fid, rid, path, text = await _pushed(client, scope, actor, fake)
    before = await _detail(client, fid)
    before["sha"] = (await _rev(rid)).work_file_sha
    fake.seed_file(path, text.replace("Quando paga", "Pippo paga"), branch=BRANCH)
    with pytest.raises(ImportParseError):
        await _pull(fake, fid, rid, actor)
    await _unchanged(client, fid, rid, before)


async def test_a_scenario_without_a_valid_identity_tag_is_refused(client, scope, actor, fake):
    fid, rid, path, text = await _pushed(client, scope, actor, fake)
    before = await _detail(client, fid)
    before["sha"] = (await _rev(rid)).work_file_sha
    edited = text + "\n  @runnable @positive\n  Scenario: Senza tag\n    Dato x\n    Allora y\n"
    fake.seed_file(path, edited, branch=BRANCH)
    with pytest.raises(ImportParseError) as exc:
        await _pull(fake, fid, rid, actor)
    assert any("Senza tag" in r for r in exc.value.reasons)
    await _unchanged(client, fid, rid, before)


async def test_pull_needs_a_draft_with_a_working_branch_and_a_file(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.branches[BRANCH] = fake.branches["main"]
    with pytest.raises(ImportFileNotFound):
        await _pull(fake, fid, rid, actor)
    async with TestSession() as s:
        await s.execute(
            update(TlFeatureRevision)
            .where(TlFeatureRevision.id == rid)
            .values(status=TlRevisionStatus.APPROVED)
        )
        await s.commit()
    with pytest.raises(branch_sync.SyncNotAllowedError):
        await _pull(fake, fid, rid, actor)
    fid2, rid2, _ = await _setup(client, scope, actor, branch=None)
    with pytest.raises(branch_sync.SyncNotAllowedError):
        await _pull(fake, fid2, rid2, actor)
    async with TestSession() as s:
        assert (await s.get(TlFeature, fid2)).git_path is None
