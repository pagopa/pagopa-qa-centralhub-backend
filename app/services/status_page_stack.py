from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from app.services.status_page_github import (
    EnrichGitHub,
    GitHubBudgetExhaustedError,
    contents_path,
    resolve_tag,
)

# Reliable data or nothing: every parser returns ``None`` when the version is not certain.

_JAVA_IMAGES = ("eclipse-temurin", "amazoncorretto", "openjdk")
_OTHER_IMAGES = {"python": "PYTHON", "node": "NODE", "rust": "RUST", "golang": "GO"}
_MIN_TAG_PARTS = 2


def _normalize_java(raw: str) -> str | None:
    """``1.8`` -> ``8``; ``17`` -> ``17``; anything else is not certain."""
    match = re.fullmatch(r"(?:1\.)?(\d+)(?:\.\d+)*", raw.strip())
    return match.group(1) if match else None


def _major_minor(raw: str) -> str | None:
    match = re.match(r"(\d+)\.(\d+)(?!\d)", raw)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _major(raw: str) -> str | None:
    match = re.match(r"(\d+)(?!\d)", raw)
    return match.group(1) if match else None


def _last_from_image(text: str) -> tuple[str, str] | None:
    """(name, tag) of the image in the last ``FROM`` line, or None."""
    joined = re.sub(r"\\[ \t]*\r?\n", " ", text)
    image: str | None = None
    for line in joined.splitlines():
        stripped = line.strip()
        if not re.match(r"FROM\s", stripped, re.IGNORECASE):
            continue
        tokens = [t for t in stripped.split()[1:] if not t.startswith("--")]
        image = tokens[0] if tokens else None
    if not image or "$" in image:
        return None
    image = image.split("@", 1)[0]
    name, _, tag = image.rpartition(":")
    if not name or "/" in tag:  # no tag (a registry port is not a tag)
        name, tag = image, ""
    return name.rsplit("/", 1)[-1].lower(), tag.lower()


def runtime_from_dockerfile(text: str) -> tuple[str, str | None] | None:
    """Language and version of the image in the last ``FROM`` line (the runtime stage)."""
    parsed = _last_from_image(text)
    if parsed is None:
        return None
    name, tag = parsed
    if name.startswith(_JAVA_IMAGES) or "openjdk" in name:
        in_name = re.search(r"openjdk(\d+)", name)
        if in_name:
            return "JAVA", in_name.group(1)
        match = re.match(r"(?:jdk-|jre-)?(\d+)(?!\d)", tag)
        return "JAVA", match.group(1) if match else None
    language = _OTHER_IMAGES.get(name)
    if language is None:
        return None
    if language == "NODE":
        return language, _major(tag)
    return language, _major_minor(tag)


def java_from_pom(text: str) -> str | None:
    for tag in ("java.version", "maven.compiler.release", "maven.compiler.source"):
        match = re.search(rf"<{re.escape(tag)}>\s*([^<\s]+)\s*</{re.escape(tag)}>", text)
        if match:
            version = _normalize_java(match.group(1))
            if version:
                return version
    return None


def java_from_gradle(text: str) -> str | None:
    match = re.search(r"JavaLanguageVersion\.of\(\s*[\"']?(\d+)[\"']?\s*\)", text)
    if match:
        return match.group(1)
    match = re.search(r"sourceCompatibility\s*=?\s*JavaVersion\.VERSION_(\d+)(?:_(\d+))?", text)
    if match:
        return _normalize_java(
            f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)
        )
    match = re.search(r"sourceCompatibility\s*=?\s*[\"']?(\d+(?:\.\d+)?)[\"']?", text)
    return _normalize_java(match.group(1)) if match else None


def python_from_pyproject(text: str) -> str | None:
    match = re.search(r"^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    if not match:
        return None
    spec = re.fullmatch(r"(?:>=|~=|\^|==)\s*(\d+\.\d+)(?:\.(?:\d+|\*))?", match.group(1).strip())
    return spec.group(1) if spec else None


def python_from_version_file(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            match = re.fullmatch(r"(\d+\.\d+)(?:\.\d+)?", line)
            return match.group(1) if match else None
    return None


def node_from_package(text: str) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    engines = data.get("engines") if isinstance(data, dict) else None
    spec = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(spec, str):
        return None
    match = re.fullmatch(r"(?:>=|\^|~|=)?\s*v?(\d+)(?:\.(?:\d+|x|\*)){0,2}", spec.strip())
    return match.group(1) if match else None


def node_from_nvmrc(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            match = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", line)
            return match.group(1) if match else None
    return None


def rust_from_cargo(text: str) -> str | None:
    match = re.search(r"^\s*rust-version\s*=\s*[\"'](\d+\.\d+)(?:\.\d+)?[\"']", text, re.MULTILINE)
    return match.group(1) if match else None


def rust_from_toolchain(text: str) -> str | None:
    match = re.search(r"^\s*channel\s*=\s*[\"'](\d+\.\d+)(?:\.\d+)?[\"']", text, re.MULTILINE)
    if match:
        return match.group(1)
    plain = re.fullmatch(r"\s*(\d+\.\d+)(?:\.\d+)?\s*", text)
    return plain.group(1) if plain else None


def go_from_mod(text: str) -> str | None:
    match = re.search(r"^go\s+(\d+\.\d+)(?:\.\d+)?\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


# language -> (build files that prove it)
_LANGUAGE_FILES: dict[str, tuple[str, ...]] = {
    "JAVA": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "PYTHON": ("pyproject.toml", "requirements.txt"),
    "NODE": ("package.json",),
    "RUST": ("Cargo.toml",),
    "GO": ("go.mod",),
}
# language -> ordered (file, parser) used to read the version when the runtime has none
_VERSION_PARSERS = {
    "JAVA": (
        ("pom.xml", java_from_pom),
        ("build.gradle", java_from_gradle),
        ("build.gradle.kts", java_from_gradle),
    ),
    "PYTHON": (
        ("pyproject.toml", python_from_pyproject),
        (".python-version", python_from_version_file),
    ),
    "NODE": (("package.json", node_from_package), (".nvmrc", node_from_nvmrc)),
    "RUST": (
        ("Cargo.toml", rust_from_cargo),
        ("rust-toolchain.toml", rust_from_toolchain),
        ("rust-toolchain", rust_from_toolchain),
    ),
    "GO": (("go.mod", go_from_mod),),
}


@dataclass
class StackResult:
    status: str
    language: str | None = None
    version: str | None = None
    source: str | None = None
    ref: str | None = None
    error: str | None = None


async def detect_stack(gh: EnrichGitHub, repository: str, repo_version: str) -> StackResult:
    """Language and version of ``repository`` at the release tag of ``repo_version``."""
    try:
        return await _detect(gh, repository, repo_version)
    except GitHubBudgetExhaustedError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        return StackResult(status="ERROR", error=f"{type(exc).__name__}: {exc}"[:500])


async def _detect(gh: EnrichGitHub, repository: str, repo_version: str) -> StackResult:
    tag = await resolve_tag(gh, repository, repo_version)
    if tag is None:
        return StackResult(status="UNAVAILABLE")
    listing = await gh.list_dir(contents_path(repository), tag)
    names = {str(item.get("name")) for item in listing or [] if item.get("type", "file") == "file"}
    languages = [lang for lang, files in _LANGUAGE_FILES.items() if any(f in names for f in files)]
    if not languages:
        return StackResult(status="UNAVAILABLE", ref=tag)

    runtime: tuple[str, str | None] | None = None
    if "Dockerfile" in names:
        dockerfile = await gh.get_text(contents_path(repository, "Dockerfile"), tag)
        runtime = runtime_from_dockerfile(dockerfile) if dockerfile else None

    if len(languages) == 1:
        language = languages[0]
    elif runtime is not None and runtime[0] in languages:
        language = runtime[0]
    else:
        return StackResult(status="UNAVAILABLE", ref=tag)

    if runtime is not None and runtime[0] == language and runtime[1] is not None:
        return StackResult("OK", language, runtime[1], "dockerfile", tag)
    for file_name, parser in _VERSION_PARSERS[language]:
        if file_name not in names:
            continue
        content = await gh.get_text(contents_path(repository, file_name), tag)
        version = parser(content) if content else None
        if version:
            return StackResult("OK", language, version, "build_file", tag)
    return StackResult("OK", language, None, None, tag)
