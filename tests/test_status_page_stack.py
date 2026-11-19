from __future__ import annotations

# ruff: noqa: S101
import base64
import json

import httpx
import pytest

from app.services.status_page_github import EnrichGitHub, GitHubBudgetExhaustedError
from app.services.status_page_stack import (
    detect_stack,
    go_from_mod,
    java_from_gradle,
    java_from_pom,
    node_from_nvmrc,
    node_from_package,
    python_from_pyproject,
    python_from_version_file,
    runtime_from_dockerfile,
    rust_from_cargo,
    rust_from_toolchain,
)

_D = "sha256:ea58bccaed00c346eea5ed0ad221d8763feb34c8cce620d86cd4370d7df35125"

WISP = f"""
FROM maven:3.9.6-amazoncorretto-17-al2023@{_D} as buildtime
RUN mvn package
FROM amazoncorretto:17.0.10-alpine3.19@{_D} as builder
RUN true
# runtime
FROM ghcr.io/pagopa/docker-base-springboot-openjdk17:v2.2.7@{_D}
COPY --from=builder /app /app
"""

AFM = """
FROM maven:3.9.11-eclipse-temurin-21 AS buildtime
FROM eclipse-temurin:21-jre AS builder
FROM --platform=linux/amd64 eclipse-temurin:21-jre
"""

PY = f"""
FROM python:3.12-slim@{_D} AS builder
FROM python:3.12-slim@{_D} AS runtime
"""


def test_dockerfile_springboot_openjdk_image() -> None:
    assert runtime_from_dockerfile(WISP) == ("JAVA", "17")


def test_dockerfile_last_stage_wins() -> None:
    assert runtime_from_dockerfile(AFM) == ("JAVA", "21")


def test_dockerfile_python_runtime() -> None:
    assert runtime_from_dockerfile(PY) == ("PYTHON", "3.12")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FROM nginx:1.27", None),
        ("FROM scratch", None),
        ("ARG B=x\nFROM ${B}", None),
        ("FROM node:20-alpine", ("NODE", "20")),
        ("FROM node:20.11.1-alpine AS RUN", ("NODE", "20")),
        ("FROM rust:1.75-slim", ("RUST", "1.75")),
        ("FROM golang:1.22-alpine", ("GO", "1.22")),
        ("FROM openjdk:17-jdk-slim", ("JAVA", "17")),
        ("FROM eclipse-temurin:jre", ("JAVA", None)),
        ("FROM python:3", ("PYTHON", None)),
        ("FROM python:latest", ("PYTHON", None)),
        ("FROM python", ("PYTHON", None)),
        ("", None),
    ],
)
def test_dockerfile_variants(text: str, expected: tuple[str, str | None] | None) -> None:
    assert runtime_from_dockerfile(text) == expected


def test_dockerfile_line_continuation() -> None:
    assert runtime_from_dockerfile("FROM \\\n  python:3.11-slim") == ("PYTHON", "3.11")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<java.version>17</java.version>", "17"),
        ("<java.version>1.8</java.version>", "8"),
        ("<maven.compiler.source>11</maven.compiler.source>", "11"),
        (
            "<maven.compiler.release>21</maven.compiler.release>"
            "<maven.compiler.source>11</maven.compiler.source>",
            "21",
        ),
        ("<java.version>${x}</java.version>", None),
        ("<project/>", None),
    ],
)
def test_java_from_pom(text: str, expected: str | None) -> None:
    assert java_from_pom(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("languageVersion = JavaLanguageVersion.of(21)", "21"),
        ("sourceCompatibility = '17'", "17"),
        ("sourceCompatibility = JavaVersion.VERSION_11", "11"),
        ("sourceCompatibility = JavaVersion.VERSION_1_8", "8"),
        ("plugins {}", None),
    ],
)
def test_java_from_gradle(text: str, expected: str | None) -> None:
    assert java_from_gradle(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('requires-python = ">=3.12"', "3.12"),
        ('requires-python = "~=3.11"', "3.11"),
        ('requires-python = "^3.10"', "3.10"),
        ('requires-python = "==3.12.*"', "3.12"),
        ('requires-python = ">=3.9,<3.13"', None),
        ('requires-python = ">=3"', None),
        ("[project]", None),
    ],
)
def test_python_from_pyproject(text: str, expected: str | None) -> None:
    assert python_from_pyproject(text) == expected


def test_python_version_file() -> None:
    assert python_from_version_file("3.12.1\n") == "3.12"
    assert python_from_version_file("# c\n3.11\n") == "3.11"
    assert python_from_version_file("pypy3.10") is None


@pytest.mark.parametrize(
    ("engines", "expected"),
    [
        (">=20", "20"),
        ("20.x", "20"),
        ("^20", "20"),
        (">=18 <21", None),
        (">=18 || >=20", None),
    ],
)
def test_node_from_package(engines: str, expected: str | None) -> None:
    assert node_from_package(json.dumps({"engines": {"node": engines}})) == expected


def test_node_from_package_missing_or_invalid() -> None:
    assert node_from_package(json.dumps({"name": "x"})) is None
    assert node_from_package("not json") is None


def test_node_from_nvmrc() -> None:
    assert node_from_nvmrc("v20.11.0\n") == "20"
    assert node_from_nvmrc("lts/*") is None


def test_rust_parsers() -> None:
    assert rust_from_cargo('[package]\nrust-version = "1.75"') == "1.75"
    assert rust_from_cargo("[package]") is None
    assert rust_from_toolchain('[toolchain]\nchannel = "1.78.0"') == "1.78"
    assert rust_from_toolchain('[toolchain]\nchannel = "stable"') is None
    assert rust_from_toolchain("1.70.0\n") == "1.70"


def test_go_from_mod() -> None:
    assert go_from_mod("module x\n\ngo 1.22.3\ntoolchain go1.22.5\n") == "1.22"
    assert go_from_mod("module x") is None


# --- detect_stack ---------------------------------------------------------------------------


def _repo(files: dict[str, str], tags: tuple[str, ...] = ("v1.0.0",)) -> httpx.MockTransport:
    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.startswith("/repos/pagopa/repo/git/ref/tags/"):
            ok = path.rsplit("/", 1)[-1] in tags
            return httpx.Response(200 if ok else 404, json={})
        assert r.url.params["ref"] == "v1.0.0"
        if path == "/repos/pagopa/repo/contents":
            return httpx.Response(200, json=[{"name": n, "type": "file"} for n in files])
        name = path.removeprefix("/repos/pagopa/repo/contents/")
        if name in files:
            content = base64.b64encode(files[name].encode()).decode()
            return httpx.Response(200, json={"encoding": "base64", "content": content})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _detect(files: dict[str, str], version: str = "1.0.0"):  # noqa: ANN202
    async with httpx.AsyncClient(transport=_repo(files)) as c:
        return await detect_stack(EnrichGitHub(c, 0), "repo", version)


async def test_java_with_dockerfile() -> None:
    r = await _detect({"pom.xml": "<java.version>11</java.version>", "Dockerfile": WISP})
    assert (r.status, r.language, r.version, r.source, r.ref) == (
        "OK",
        "JAVA",
        "17",
        "dockerfile",
        "v1.0.0",
    )


async def test_python_without_dockerfile_uses_build_file() -> None:
    r = await _detect({"pyproject.toml": 'requires-python = ">=3.12"'})
    assert (r.language, r.version, r.source) == ("PYTHON", "3.12", "build_file")


async def test_python_dockerfile_wins_over_pyproject() -> None:
    r = await _detect({"pyproject.toml": 'requires-python = ">=3.9"', "Dockerfile": PY})
    assert (r.version, r.source) == ("3.12", "dockerfile")


async def test_frontend_nginx_runtime_uses_package_json() -> None:
    r = await _detect(
        {
            "package.json": json.dumps({"engines": {"node": ">=20"}}),
            "Dockerfile": "FROM node:20 AS b\nFROM nginx:1.27\n",
        }
    )
    assert (r.language, r.version, r.source) == ("NODE", "20", "build_file")


async def test_mixed_repo_uses_dockerfile_language() -> None:
    r = await _detect({"pom.xml": "<project/>", "package.json": "{}", "Dockerfile": AFM})
    assert (r.language, r.version, r.source) == ("JAVA", "21", "dockerfile")


async def test_mixed_repo_without_matching_runtime_unavailable() -> None:
    r = await _detect({"pom.xml": "<project/>", "package.json": "{}", "Dockerfile": "FROM nginx"})
    assert r.status == "UNAVAILABLE"
    assert r.language is None


async def test_language_without_certain_version() -> None:
    r = await _detect({"pyproject.toml": 'requires-python = ">=3.9,<3.13"'})
    assert (r.status, r.language, r.version, r.source) == ("OK", "PYTHON", None, None)


async def test_no_build_file_unavailable() -> None:
    r = await _detect({"Dockerfile": AFM, "README.md": "x"})
    assert r.status == "UNAVAILABLE"
    assert r.language is None


async def test_unresolved_tag_unavailable() -> None:
    r = await _detect({"pom.xml": "<project/>"}, version="9.9.9")
    assert (r.status, r.ref) == ("UNAVAILABLE", None)


async def test_http_error_becomes_error_status() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(500))
    async with httpx.AsyncClient(transport=t) as c:
        r = await detect_stack(EnrichGitHub(c, 0), "repo", "1.0.0")
    assert r.status == "ERROR"
    assert r.error


async def test_budget_exhaustion_propagates() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    async with httpx.AsyncClient(transport=t) as c:
        with pytest.raises(GitHubBudgetExhaustedError):
            await detect_stack(EnrichGitHub(c, 0), "repo", "1.0.0")
