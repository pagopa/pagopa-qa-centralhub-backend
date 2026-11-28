# ruff: noqa: F811, S101
"""Scenario numbers: a draft-only number is reused, a published one never is."""

from __future__ import annotations

from tests.test_tl_branch_workflow import _reopen
from tests.test_tl_features import BASE, _use_test_db, actor, scope  # noqa: F401
from tests.test_tl_git_import import _merged, fake  # noqa: F401
from tests.test_tl_workflow import _get, reviewer  # noqa: F401


async def test_a_published_scenario_number_is_never_reused(
    client, scope, actor, reviewer, fake
):
    done, _ = await _merged(client, scope, actor, reviewer, fake)
    resp, published = await _reopen(client, done, actor)
    assert resp.status_code == 200, resp.text
    draft = (await _get(client, done["id"], actor["email"]))["working_revision"]
    taken = {s["number"] for s in draft["scenarios"]}
    assert taken  # the published revision had scenarios

    # remove one of them from the new draft: its identity still lives in the published revision
    first = draft["scenarios"][0]
    url = f"{BASE}/features/{done['id']}/revisions/{draft['id']}/scenarios"
    gone = await client.delete(
        f"{url}/{first['id']}", params={"actor_email": actor["email"]}
    )
    assert gone.status_code == 200
    added = await client.post(url, json={"actor_email": actor["email"], "title": "Nuovo"})
    assert added.status_code == 201, added.text
    assert added.json()["number"] not in taken
    assert added.json()["number"] > max(taken)
