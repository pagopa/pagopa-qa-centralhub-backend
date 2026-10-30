"""Facts about one repository, computed from its file tree and a few metadata files.

Pure functions: the caller lists the tree, asks :func:`wanted_files` which files to read
and passes their text to :func:`extract_repo`. The output has the same shape as the
``repos_extract.json`` produced by the discovery scripts, plus ``tas_ci``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

import structlog
import yaml

from app.services.ea.sync.rules import TAS_MARKERS

logger = structlog.get_logger(__name__)

HELM_ENVS = ("prod", "uat", "dev")
MAX_API_FILES = 12

_API_RX = re.compile(r"(openapi|swagger)[^/]*\.(json|ya?ml)$", re.I)
_UNIT_RX = re.compile(
    r"(src/test/|__tests__/|\.test\.(ts|tsx|js)$|\.spec\.(ts|tsx)$|_test\.go$|tests?/.*test_.*\.py$)"
)
_PERF_RX = re.compile(r"(performance-test|load-test|perf)", re.I)
_ROOT_FILES = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "build.sbt",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "README.md",
)


def _api_files(tree: Iterable[str]) -> list[str]:
    try:
        return [
            x for x in tree if _API_RX.search(x) and "node_modules" not in x and "test" not in x.lower()
        ][:MAX_API_FILES]
    except Exception:
        logger.exception("_api_files failed")
        raise


def _identity_files(tree: Iterable[str]) -> list[str]:
    try:
        return [
            x
            for x in tree
            if re.fullmatch(r"\.identity/[^/]+\.tf", x)
            or re.fullmatch(r"\.identity/env/[^/]+/terraform\.tfvars", x)
        ]
    except Exception:
        logger.exception("_identity_files failed")
        raise


def wanted_files(tree: list[str]) -> list[str]:
    """The files :func:`extract_repo` needs, among those present in ``tree``."""
    try:
        present = set(tree)
        out = [f for f in _ROOT_FILES if f in present]
        for env in HELM_ENVS:
            f = f"helm/values-{env}.yaml"
            if f in present:
                out.append(f)
                break
        out += _identity_files(tree)
        out += _api_files(tree)
        out += [x for x in tree if x.startswith(".github/workflows/")]
        return out
    except Exception:
        logger.exception("wanted_files failed")
        raise


def _helm(files: Mapping[str, str]) -> dict[str, Any]:
    try:
        for env in HELM_ENVS:
            text = files.get(f"helm/values-{env}.yaml")
            if text is None:
                continue
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                logger.exception("_helm failed")
                data = {}
            return data if isinstance(data, dict) else {}
        return {}
    except Exception:
        logger.exception("_helm failed")
        raise


def extract_repo(name: str, tree: list[str], files: Mapping[str, str]) -> dict[str, Any]:
    try:
        t = set(tree)
        rd = files.get
        info: dict[str, Any] = {"name": name}

        lang: list[str] = []
        fw: list[str] = []
        pom = rd("pom.xml", "")
        gr = rd("build.gradle", "") + rd("build.gradle.kts", "")
        pk = rd("package.json", "")
        if pom:
            lang.append("Java")
        if "spring-boot" in pom or "spring-boot" in gr:
            fw.append("Spring Boot")
        if "webflux" in pom + gr:
            fw.append("WebFlux")
        if "quarkus" in pom + gr:
            fw.append("Quarkus")
        if "azure-functions" in pom + gr or "host.json" in t:
            fw.append("Azure Functions")
        if gr:
            lang.append("Kotlin" if any(x.endswith(".kt") for x in tree) else "Java")
        if pk:
            ts = any(x.endswith((".ts", ".tsx")) for x in tree)
            lang.append("TypeScript" if ts else "JavaScript")
            for key, label in (
                ("react", "React"),
                ("next", "Next.js"),
                ("@mui", "MUI"),
                ("express", "Express"),
                ("@playwright", "Playwright"),
                ("cypress", "Cypress"),
                ("k6", "k6"),
                ("cucumber", "Cucumber"),
            ):
                if f'"{key}' in pk:
                    fw.append(label)
        sbt = rd("build.sbt", "")
        if "build.sbt" in t:
            lang.append("Scala")
        if "akka" in sbt.lower():
            fw.append("Akka")
        if "go.mod" in t:
            lang.append("Go")
        if "pyproject.toml" in t or "requirements.txt" in t or sum(x.endswith(".py") for x in tree) > 3:
            lang.append("Python")
        if "fastapi" in (rd("requirements.txt", "") + rd("pyproject.toml", "")).lower():
            fw.append("FastAPI")
        if sum(x.endswith(".tf") for x in tree) > 10:
            lang.append("Terraform")
        if any(x.endswith(".dart") for x in tree):
            lang.append("Dart")
        info["lang"] = sorted(set(lang))
        info["fw"] = sorted(set(fw))

        hv = _helm(files)
        mc = hv.get("microservice-chart", hv)
        if isinstance(mc, dict):
            info["ns"] = mc.get("namespace")
            ing = mc.get("ingress") or {}
            if isinstance(ing, dict) and ing.get("create"):
                info["ingress"] = [ing.get("host"), ing.get("path")]
            ec = mc.get("envConfig") or {}
            if isinstance(ec, dict):
                info["env_urls"] = sorted(
                    {str(v) for v in ec.values() if re.search(r"https?://", str(v))}
                )[:40]
                info["env_keys"] = sorted(str(k) for k in ec)[:120]
            img = mc.get("image") or {}
            info["image"] = img.get("repository") if isinstance(img, dict) else None

        idt = " ".join(rd(f, "") for f in _identity_files(tree))
        m = re.search(r'domain\s*=\s*"([^"]+)"', idt)
        info["id_domain"] = m.group(1) if m else None

        apis = []
        for x in _api_files(tree):
            s = rd(x, "")
            mt = re.search(r'"title"\s*:\s*"([^"]+)"', s) or re.search(
                r"^\s{2}title:\s*[\"']?([^\"'\n]+)", s, re.M
            )
            apis.append({"file": x, "title": mt.group(1).strip() if mt else None})
        info["apis"] = apis

        feat = [x for x in tree if x.endswith(".feature")]
        info["tests"] = {
            "unit_files": sum(1 for x in tree if _UNIT_RX.search(x)),
            "features": len(feat),
            "feature_dirs": sorted({os.path.dirname(x) for x in feat})[:10],
            "perf_files": sum(1 for x in tree if _PERF_RX.search(x)),
            "postman": sum(1 for x in tree if "postman" in x.lower() and x.endswith(".json")),
        }

        wf: dict[str, dict[str, Any]] = {}
        tas_ci = False
        for x in tree:
            if not x.startswith(".github/workflows/"):
                continue
            s = rd(x, "")
            nm = re.search(r"^name:\s*(.+)$", s, re.M)
            wf[os.path.basename(x)] = {
                "name": nm.group(1).strip().strip("\"'") if nm else None,
                "cron": bool(re.search(r"schedule:\s*\n\s*-\s*cron", s)),
                "sonar": "sonar" in s.lower(),
                "k6": "k6" in s.lower(),
                "it": bool(re.search(r"integration", s, re.I)),
                "deploy": bool(re.search(r"deploy", x + s[:400], re.I)),
            }
            tas_ci = tas_ci or any(marker in s for marker in TAS_MARKERS)
        info["workflows"] = wf
        info["workflow_order"] = list(wf)  # JSONB does not keep the key order of ``workflows``
        info["tas_ci"] = tas_ci

        co = rd("CODEOWNERS", "") + rd(".github/CODEOWNERS", "")
        info["owners"] = sorted(set(re.findall(r"@pagopa/[\w-]+", co)))
        md = rd("README.md", "")
        paras = [
            line.strip()
            for line in md.split("\n")
            if line.strip()
            and not line.startswith(("#", "[!", "![", "<", "|", "```", "---", "- [", "*"))
        ]
        info["readme"] = " ".join(paras[:3])[:500]
        info["readme_links"] = sorted(
            set(re.findall(r'https://pagopa\.atlassian\.net/wiki/[^\s)\]"]+', md))
        )[:10]
        info["dockerfile"] = "Dockerfile" in t
        info["helm"] = any(x.startswith("helm/") for x in tree)
        info["infra_tf"] = sum(x.endswith(".tf") for x in tree)
        info["nfiles"] = len(tree)
        return info
    except Exception:
        logger.exception("extract_repo failed")
        raise
