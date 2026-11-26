"""Importa da Git: un feature file (qualsiasi branch) nella bozza di una feature.

Il file è solo il punto di partenza: la PR dell'hub lo porterà su `main` nello stesso percorso e,
se il branch di partenza non è `main`, una PR di pulizia lo toglierà da quel branch.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import settings

_BLOB = re.compile(r"^/([^/]+/[^/]+)/blob/(.+)$")


class ImportUrlInvalid(Exception):  # noqa: N818
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_blob_url(url: str) -> tuple[str, str]:
    """`(repo, resto)` da `https://github.com/<repo>/blob/<branch>/<percorso>`."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in (
        "github.com",
        "www.github.com",
    ):
        raise ImportUrlInvalid("Serve un link GitHub del tipo https://github.com/…/blob/…")
    m = _BLOB.match(unquote(parsed.path))
    if not m:
        raise ImportUrlInvalid("Il link deve puntare a un file (…/blob/<branch>/<percorso>)")
    repo, rest = m.group(1), m.group(2).strip("/")
    # branch e percorso finiscono negli URL delle API GitHub: niente segmenti vuoti, `.`, `..`
    if any(seg in ("", ".", "..") for seg in rest.split("/")) or any(
        ch in rest for ch in "\\?#"
    ) or any(ord(ch) < 32 for ch in rest):
        raise ImportUrlInvalid("Il link contiene un percorso non valido")
    # TL_REPO vuoto (modalità Git locale): si accetta il repository del link
    if settings.tl_repo and repo.lower() != settings.tl_repo.lower():
        raise ImportUrlInvalid(f"Il file deve stare nel repository {settings.tl_repo}")
    if not rest.endswith(".feature"):
        raise ImportUrlInvalid("Il link deve puntare a un file .feature")
    return repo, rest


async def resolve_blob(client: Any, url: str) -> tuple[str, str]:
    """`(branch, percorso)`: i branch possono contenere `/`, si prova dal più lungo."""
    _repo, rest = parse_blob_url(url)
    parts = rest.split("/")
    for i in range(len(parts) - 1, 0, -1):
        branch, path = "/".join(parts[:i]), "/".join(parts[i:])
        if await client.branch_exists(branch):
            return branch, path
    raise ImportUrlInvalid("Branch del link non trovato nel repository")


# ── importazione nella bozza ──────────────────────────────────────────────────


class ImportFileNotFound(Exception):  # noqa: N818
    pass


class ImportParseError(Exception):  # noqa: N818
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


async def import_from_git(
    db: Any,
    feature_id: Any,
    revision_id: Any,
    url: str,
    lock_version: int,
    actor_email: str,
    client: Any,
) -> Any:
    """Sostituisce gli scenari della bozza con quelli del file indicato. Un commit.

    Rete prima dei lock (nessuna transazione aperta durante la lettura del file); poi lock di
    feature e revisione, controlli di stato e `lock_version`, scrittura.
    """
    from datetime import UTC, datetime

    from sqlalchemy import delete, exists, func, select

    from app.models.test_lifecycle import (
        TlScenario,
        TlScenarioAlias,
        TlScenarioContent,
        TlScope,
    )
    from app.schemas.test_lifecycle import TlImportGitResult
    from app.services.test_lifecycle import features as features_svc
    from app.services.test_lifecycle import history
    from app.services.test_lifecycle.actors import resolve_actor
    from app.services.test_lifecycle.category_tags import known_tags
    from app.services.test_lifecycle.feature_parser import normalize_text, parse_feature_file
    from app.services.test_lifecycle.migration import (
        _POLARITY,
        _feature_tags,
        _scenarios_from_file,
        is_identity_tag,
        is_keepable,
        test_type_of,
    )
    from app.services.test_lifecycle.tags import TEST_TYPES

    await resolve_actor(db, actor_email)  # 403 prima della rete
    # feature-level tags are pushed onto the scenarios only when they are catalog, status or flaky
    allowed = await known_tags(db)
    await db.rollback()

    branch, path = await resolve_blob(client, url)
    file = await client.get_file(path, branch)
    if file is None:
        raise ImportFileNotFound(path)
    text = normalize_text(file.text)
    parsed = parse_feature_file(text, allowed_extra=())
    if parsed.errors:
        raise ImportParseError(parsed.errors)
    if not parsed.scenarios:
        raise ImportParseError(["Il file non contiene scenari"])
    feature_tags = _feature_tags(text)
    scenario_names = [t.lstrip("@") for sc in parsed.scenarios for t in sc.all_tags]
    feature_names = [t.lstrip("@") for t in feature_tags]
    # Reported, not imported: feature-level tags outside the catalog, and scenario tags that
    # could never be saved again (format). Test types and identities are not "ignored".
    ignored = sorted(
        {
            n
            for n in scenario_names + feature_names
            if not is_identity_tag(n)
            and n.lower() not in _POLARITY
            and n not in TEST_TYPES
            and (
                (n in scenario_names and not is_keepable(n))
                or (n in feature_names and n not in scenario_names and n not in allowed)
            )
        }
    )
    file_test_type = test_type_of(feature_tags)

    actor = await resolve_actor(db, actor_email)
    feature = await features_svc._lock_feature(db, feature_id)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    if feature.working_revision_id != rev.id:
        raise features_svc.RevisionLockedError(rev.status)
    features_svc._require_draft(rev)
    features_svc._require_lock(rev, lock_version)
    scope = await db.get(TlScope, feature.scope_id)

    replaced = (
        await db.execute(
            select(func.count())
            .select_from(TlScenarioContent)
            .where(TlScenarioContent.revision_id == rev.id)
        )
    ).scalar_one()
    await db.execute(delete(TlScenarioContent).where(TlScenarioContent.revision_id == rev.id))
    # identità usate solo da questa bozza: via (quelle della pubblicata restano)
    await db.execute(
        delete(TlScenario).where(
            TlScenario.feature_id == feature.id,
            ~exists().where(TlScenarioContent.scenario_id == TlScenario.id),
        )
    )
    used = (
        await db.execute(
            select(func.max(TlScenario.number)).where(TlScenario.feature_id == feature.id)
        )
    ).scalar_one()
    start = (used or 0) + 1
    specs = _scenarios_from_file(
        parsed, feature_tags, set(allowed), scope.code, feature.number, start=start, keep_all=True
    )
    if file_test_type is not None:
        feature.test_type = file_test_type
    aliases = set((await db.execute(select(TlScenarioAlias.alias))).scalars())
    for position, spec in enumerate(specs, start=1):
        scenario = TlScenario(feature_id=feature.id, number=spec["number"], tag=spec["tag"])
        db.add(scenario)
        await db.flush()
        db.add(
            TlScenarioContent(
                revision_id=rev.id,
                scenario_id=scenario.id,
                position=position,
                title=spec["title"][:500],
                kind=spec["kind"],
                polarity=spec["polarity"],
                extra_tags=spec["extra_tags"],
                steps=spec["steps"],
                examples=spec["examples"],
                detail_link=spec["detail_link"],
            )
        )
        old = spec["old"]
        if old and old not in aliases and old != spec["tag"].lstrip("@"):
            db.add(TlScenarioAlias(scenario_id=scenario.id, alias=old))
            aliases.add(old)
    feature.next_scenario_number = start + len(specs)
    rev.description = parsed.description
    rev.background_steps = parsed.background_steps
    rev.lock_version += 1
    feature.import_source = {
        "url": url.strip(),
        "branch": branch,
        "path": path,
        "blob_sha": file.blob_sha,
        "imported_at": datetime.now(UTC).isoformat(),
        "cleanup": {
            "state": "pending",
            "pr_number": None,
            "pr_url": None,
            "error": None,
            "note": None,
        },
    }
    history.record_event(
        db,
        feature.id,
        "tl.feature.imported",
        actor,
        {
            "url": url.strip(),
            "branch": branch,
            "path": path,
            "scenarios": len(specs),
            "replaced": replaced,
        },
    )
    await db.commit()
    db.expunge_all()  # oggetti scaduti dal commit: il dettaglio li rilegge da zero
    detail = await features_svc.get_feature_detail(db, feature_id, actor_email)
    return TlImportGitResult(feature=detail, ignored_tags=ignored, replaced=replaced)


# ── PR di pulizia sul branch di partenza ──────────────────────────────────────


def cleanup_branch(code: str) -> str:
    return f"qa-hub/cleanup-{code.lower()}"


async def open_cleanup_pr(db: Any, feature_id: Any, client: Any, *, force: bool = False) -> None:
    """Dopo la PR dell'hub: PR verso il branch di partenza che rimuove il file importato.

    Niente da fare senza provenienza, con branch di partenza = `main`, o se la pulizia è già
    aperta / non necessaria. Senza `force` serve una PR dell'hub aperta (il test non sparisce
    mai prima che la sua nuova versione sia pronta). Esito ed errori in `import_source.cleanup`.
    """
    from sqlalchemy import select

    from app.models.test_lifecycle import TlFeatureRevision
    from app.services.test_lifecycle import features as features_svc
    from app.services.test_lifecycle.git_client import TlGitError

    feature = await features_svc._lock_feature(db, feature_id)
    source = dict(feature.import_source or {})
    state = (source.get("cleanup") or {}).get("state")
    code = feature.code
    if not source or state in ("open", "not_needed"):
        await db.rollback()
        return
    if not force:
        hub_pr = (
            await db.execute(
                select(TlFeatureRevision.id).where(
                    TlFeatureRevision.feature_id == feature.id,
                    TlFeatureRevision.pr_state == "open",
                )
            )
        ).first()
        if hub_pr is None:
            await db.rollback()
            return
    branch, path = source["branch"], source["path"]
    await db.rollback()  # nessuna transazione aperta durante la rete

    outcome: dict[str, Any] = {"pr_number": None, "pr_url": None, "error": None, "note": None}
    if branch == settings.tl_base_branch:
        note = "Il file viene da main: la PR dell'hub lo sostituisce"
        outcome.update(state="not_needed", note=note)
    else:
        try:
            if not await client.branch_exists(branch):
                outcome.update(state="not_needed", note=f"Il branch {branch} non esiste più")
            elif await client.get_file(path, branch) is None:
                outcome.update(state="not_needed", note=f"Il file non è più sul branch {branch}")
            else:
                clean = cleanup_branch(code)
                await client.create_branch(clean, await client.head_sha(branch))
                current = await client.get_file(path, clean)
                if current is not None:
                    await client.delete_file(
                        path, clean, f"chore(qa-hub): rimuovi {code}, ora gestito da QA Hub",
                        current.blob_sha,
                    )
                pull = await client.find_open_pull(clean)
                if pull is None:
                    pull = await client.create_pull(
                        clean, branch, f"Rimuovi {code}: ora gestito da QA Hub",
                        f"Il feature file `{path}` è stato importato in QA Hub come **{code}**.\n"
                        "La versione gestita dall'hub arriva su main con la PR dell'hub: questa PR "
                        "lo toglie dal branch, così le due PR non vanno in conflitto.\n",
                    )
                outcome.update(state="open", pr_number=pull.number, pr_url=pull.url)
        except TlGitError as exc:
            outcome.update(state="error", error=exc.message[:1000])

    feature = await features_svc._lock_feature(db, feature_id)
    source = dict(feature.import_source or {})
    source["cleanup"] = outcome
    feature.import_source = source
    await db.commit()
