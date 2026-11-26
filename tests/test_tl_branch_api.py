# ruff: noqa: F811, S101
"""Working-branch sync over HTTP (design 2026-11-26, git sync)."""
from __future__ import annotations

from tests.test_tl_branch_sync import (  # noqa: F401  (helpers e fixture riusate)
    BASE,
    BRANCH,
    STEPS_A,
    STEPS_B,
    _edit,
    _path,
    _setup,
    _use_test_db,
    actor,
    fake,
    scope,
)


def _url(fid, rid, suffix=""):
    return f"{BASE}/features/{fid}/revisions/{rid}/git-sync{suffix}"


async def _push(client, fid, rid, actor, **extra):
    return await client.post(
        _url(fid, rid, "/push"), json={"actor_email": actor["email"], **extra}
    )


async def test_get_reports_the_state(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    resp = await client.get(_url(fid, rid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "branch_missing" and resp.json()["branch"] == BRANCH
    assert (await _push(client, fid, rid, actor)).status_code == 200
    body = (await client.get(_url(fid, rid))).json()
    assert body["state"] == "in_sync" and body["hub_unsynced"] is False
    assert body["compare_url"] and BRANCH in body["compare_url"]


async def test_get_without_a_branch_and_with_unknown_ids(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor, branch=None)
    body = (await client.get(_url(fid, rid))).json()
    assert body["state"] == "no_branch" and body["branch"] is None
    unknown = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(_url(unknown, rid))).status_code == 404
    assert (await client.get(_url(fid, unknown))).status_code == 404


async def test_get_never_fails_when_git_does(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.fail_once("branch_exists", 502, "bad gateway")
    resp = await client.get(_url(fid, rid))
    assert resp.status_code == 200 and resp.json()["state"] == "unreachable"


async def test_get_without_a_token_is_not_configured(client, scope, actor):
    fid, rid, _ = await _setup(client, scope, actor)
    resp = await client.get(_url(fid, rid))
    assert resp.status_code == 200 and resp.json()["state"] == "not_configured"


async def test_the_revision_exposes_its_working_branch(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    detail = (await client.get(f"{BASE}/features/{fid}")).json()
    assert detail["working_revision"]["work_branch"] == BRANCH
    fid2, _, _ = await _setup(client, scope, actor, branch=None)
    other = (await client.get(f"{BASE}/features/{fid2}")).json()
    assert other["working_revision"]["work_branch"] is None


async def test_push_and_the_repository_changed_error(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    assert (await _push(client, fid, rid, actor)).json()["state"] == "in_sync"
    path = await _path(fid, rid)
    fake.seed_file(path, fake.files(BRANCH)[path][0] + "# dev\n", branch=BRANCH)
    await _edit(client, fid, actor, STEPS_B)
    resp = await _push(client, fid, rid, actor)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "repo_changed"
    resp = await _push(client, fid, rid, actor, overwrite=True)
    assert resp.status_code == 200 and resp.json()["state"] == "in_sync"


async def test_push_not_allowed_without_a_branch(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor, branch=None)
    resp = await _push(client, fid, rid, actor)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "sync_not_allowed"


async def test_push_unknown_actor_and_no_token(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    resp = await client.post(
        _url(fid, rid, "/push"), json={"actor_email": "nobody@example.test"}
    )
    assert resp.status_code == 403


async def test_push_without_a_token_is_503(client, scope, actor):
    fid, rid, _ = await _setup(client, scope, actor)
    resp = await _push(client, fid, rid, actor)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "git_not_configured"


async def test_pull_over_http(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(client, fid, rid, actor)
    path = await _path(fid, rid)
    fake.seed_file(
        path, fake.files(BRANCH)[path][0].replace("Quando paga", "Quando paga con carta"), BRANCH
    )
    resp = await client.post(
        _url(fid, rid, "/pull"), json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 200, resp.text
    assert "con carta" in resp.json()["working_revision"]["scenarios"][0]["steps"]


async def test_pull_of_a_file_that_does_not_parse_is_422(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    await _edit(client, fid, actor, STEPS_A)
    await _push(client, fid, rid, actor)
    path = await _path(fid, rid)
    fake.seed_file(path, fake.files(BRANCH)[path][0].replace("Quando paga", "Pippo paga"), BRANCH)
    resp = await client.post(
        _url(fid, rid, "/pull"), json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "import_parse_error" and detail["reasons"]


async def test_pull_of_a_missing_file_is_404(client, scope, actor, fake):
    fid, rid, _ = await _setup(client, scope, actor)
    fake.branches[BRANCH] = fake.branches["main"]
    resp = await client.post(
        _url(fid, rid, "/pull"), json={"actor_email": actor["email"]}
    )
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "import_file_not_found"
