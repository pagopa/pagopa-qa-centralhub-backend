import base64
import json

import httpx
import pytest

from app.config import settings
from app.services.test_lifecycle import git_client
from app.services.test_lifecycle.git_client import (
    TlGitClient,
    TlGitError,
    TlGitNotConfigured,
    get_git_client,
)


def make_client(handler) -> TlGitClient:
    return TlGitClient("tok", "org/repo", transport=httpx.MockTransport(handler))


def b64(text: bytes) -> str:
    return base64.b64encode(text).decode()


async def test_head_sha_and_branch_exists():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        return httpx.Response(404, json={"message": "Not Found"})

    c = make_client(handler)
    assert await c.head_sha("main") == "abc"
    assert await c.branch_exists("main") is True
    assert await c.branch_exists("nope") is False


async def test_put_file_sends_base64_and_sha():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        seen["ver"] = req.headers["x-github-api-version"]
        return httpx.Response(200, json={"commit": {"sha": "c1"}})

    c = make_client(handler)
    sha = await c.put_file("a/b.feature", "ciao è", "br", "msg", blob_sha="blob1")
    assert sha == "c1"
    assert seen["method"] == "PUT"
    assert seen["path"] == "/repos/org/repo/contents/a/b.feature"
    assert seen["body"]["content"] == b64("ciao è".encode())
    assert seen["body"]["sha"] == "blob1"
    assert seen["body"]["branch"] == "br"
    assert seen["body"]["message"] == "msg"
    assert seen["auth"] == "Bearer tok"
    assert seen["ver"] == "2022-11-28"


async def test_put_file_without_sha_omits_it():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"commit": {"sha": "c2"}})

    await make_client(handler).put_file("p", "t", "br", "m")
    assert "sha" not in seen["body"]


async def test_create_branch_already_exists_is_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Reference already exists"})

    await make_client(handler).create_branch("br", "sha1")


async def test_create_branch_posts_ref():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={})

    await make_client(handler).create_branch("br", "sha1")
    assert seen["body"] == {"ref": "refs/heads/br", "sha": "sha1"}


async def test_create_branch_other_422_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Object does not exist"})

    with pytest.raises(TlGitError):
        await make_client(handler).create_branch("br", "bad")


async def test_get_file_404_returns_none():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert await make_client(handler).get_file("x", "main") is None


async def test_get_file_returns_text_and_sha():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["ref"] == "main"
        return httpx.Response(
            200, json={"content": b64(b"hello"), "sha": "bs", "encoding": "base64"}
        )

    f = await make_client(handler).get_file("x.feature", "main")
    assert (f.path, f.text, f.blob_sha) == ("x.feature", "hello", "bs")


async def test_get_file_strips_bom():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": b64(b"\xef\xbb\xbf# qa-hub: X"), "sha": "s"})

    f = await make_client(handler).get_file("x", "main")
    assert f.text == "# qa-hub: X"


async def test_find_open_pull():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["head"] == "org:br"
        assert req.url.params["state"] == "open"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "html_url": "u",
                    "state": "open",
                    "merged": False,
                    "merge_commit_sha": None,
                    "head": {"ref": "br"},
                }
            ],
        )

    p = await make_client(handler).find_open_pull("br")
    assert p.number == 7 and p.state == "open" and p.head_branch == "br"


async def test_find_open_pull_none():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    assert await make_client(handler).find_open_pull("br") is None


async def test_create_pull():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "number": 3,
                "html_url": "http://pr/3",
                "state": "open",
                "merged": False,
                "merge_commit_sha": None,
                "head": {"ref": "br"},
            },
        )

    p = await make_client(handler).create_pull("br", "main", "T", "B")
    assert seen["body"] == {"head": "br", "base": "main", "title": "T", "body": "B"}
    assert p.number == 3 and p.url == "http://pr/3"


async def test_get_pull_maps_merged_state():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 1,
                "html_url": "u",
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "mc",
                "head": {"ref": "br"},
            },
        )

    p = await make_client(handler).get_pull(1)
    assert p.state == "merged" and p.merge_commit_sha == "mc"


async def test_get_pull_maps_closed_unmerged():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 1,
                "html_url": "u",
                "state": "closed",
                "merged": False,
                "merge_commit_sha": None,
                "head": {"ref": "br"},
            },
        )

    assert (await make_client(handler).get_pull(1)).state == "closed"


async def test_compare_maps_renamed_previous_path():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/org/repo/compare/a...b"
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "filename": "n.feature",
                        "status": "renamed",
                        "previous_filename": "o.feature",
                    },
                    {"filename": "m.feature", "status": "modified"},
                    {"filename": "d.feature", "status": "removed"},
                    {"filename": "a.feature", "status": "added"},
                    {"filename": "c.feature", "status": "changed"},
                ]
            },
        )

    files = await make_client(handler).compare("a", "b")
    assert [(f.path, f.status, f.previous_path) for f in files] == [
        ("n.feature", "renamed", "o.feature"),
        ("m.feature", "modified", None),
        ("d.feature", "removed", None),
        ("a.feature", "added", None),
        ("c.feature", "modified", None),
    ]


async def test_list_tree_returns_blobs_only():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["recursive"] == "1"
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "a", "type": "tree"},
                    {"path": "a/b.feature", "type": "blob"},
                ]
            },
        )

    assert await make_client(handler).list_tree("main") == ["a/b.feature"]


async def test_error_carries_github_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by PAT"})

    with pytest.raises(TlGitError) as ei:
        await make_client(handler).head_sha("main")
    assert ei.value.status == 403
    assert ei.value.message == "Resource not accessible by PAT"
    assert "Resource not accessible" in str(ei.value)


async def test_error_with_non_json_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(TlGitError) as ei:
        await make_client(handler).head_sha("main")
    assert ei.value.status == 502 and ei.value.message


def test_get_git_client_without_token_raises(monkeypatch):
    monkeypatch.setattr(settings, "tl_github_token", "")
    monkeypatch.setattr(settings, "tas_github_token", "")
    with pytest.raises(TlGitNotConfigured):
        get_git_client()


def test_get_git_client_falls_back_to_tas_token(monkeypatch):
    monkeypatch.setattr(settings, "tl_github_token", "")
    monkeypatch.setattr(settings, "tas_github_token", "tas-tok")
    monkeypatch.setattr(settings, "tl_repo", "o/r")
    c = get_git_client()
    assert c.token == "tas-tok" and c.repo == "o/r"  # noqa: S105


def test_get_git_client_prefers_tl_token(monkeypatch):
    monkeypatch.setattr(settings, "tl_github_token", "tl-tok")
    monkeypatch.setattr(settings, "tas_github_token", "tas-tok")
    assert get_git_client().token == "tl-tok"  # noqa: S105


def test_settings_defaults():
    from app.config import Settings

    s = Settings()
    assert s.tl_base_branch == "main"
    assert s.tl_sync_interval_s == 300.0
    assert s.tl_repo == "pagopa/pagopa-platform-integration-test"
    assert hasattr(s, "tl_hub_base_url")
    assert git_client.TlGitClient
