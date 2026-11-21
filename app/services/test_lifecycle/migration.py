"""Migrazione da Confluence (fase 5): analisi delle pagine FEAT e dei feature file.

L'analisi legge tutto prima di scrivere (nessuna transazione aperta durante la rete), poi
ricostruisce le righe di migrazione conservando decisioni e correzioni delle righe con la stessa
chiave (`page_id`, altrimenti `file_path`). Non scrive mai su Confluence né su Git.
"""

from __future__ import annotations

import posixpath
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureStatus,
    TlMigrationItem,
    TlMigrationScope,
    TlMigrationState,
    TlMigrationTag,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlMigrationItemOut,
    TlMigrationScopeOut,
    TlMigrationSummary,
    TlMigrationTagOut,
)
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.category_tags import category_tags
from app.services.test_lifecycle.confluence_page import FeatPage, map_status, parse_feat_page
from app.services.test_lifecycle.feature_parser import ParsedFeature, parse_feature_file

LOCK_KEY = "tl_migration"
_CODE = re.compile(r"FEAT_(\d{3})_([A-Za-z][A-Za-z0-9]*)")
_TITLE_PREFIX = re.compile(r"^\s*FEAT_\d{3}_[A-Za-z][A-Za-z0-9]*\s*[-–—_:]?\s*")
_HUB_IDENTITY = re.compile(r"^[A-Za-z0-9]+_\d{2,3}$|^[A-Za-z0-9_]*_\d{3}_\d{2}$")
_POLARITY = {"positivo", "negativo"}
TITLE_THRESHOLD = 0.6
OVERRIDABLE = ("scope_code", "number", "title", "status", "owner_id", "reviewer_id", "file_path")

PROBLEM_MESSAGES = {
    "number_conflict": "Numero già usato nello stesso ambito",
    "page_without_file": "Pagina senza feature file su Git",
    "file_without_page": "Feature file senza pagina Confluence",
    "multiple_files": "Più feature file con lo stesso codice: abbinato il più simile",
    "duplicate_identity_tag": "Tag identità usato anche in un altro file",
    "parse_error": "Feature file non leggibile",
    "owner_not_found": "Owner non trovato tra gli utenti dell'hub",
    "reviewer_not_found": "Reviewer non trovato tra gli utenti dell'hub",
    "reviewer_is_team": "Il reviewer è un gruppo: va scelta una persona",
    "fixture_or_copy": "Probabile fixture o copia (tas_*, docs/use-cases)",
    "no_code": "Codice FEAT assente: scegliere ambito e numero",
    "draft_with_file": (
        "Pagina in bozza ma file già su Git: il file sarà allineato all'approvazione"
    ),
    "changed_since_analysis": "Pagina o file cambiati dopo l'analisi: rianalizzare e confermare",
    "jira_key_not_found": "Chiave Jira inesistente",
}


class MigrationInProgress(Exception):  # noqa: N818
    pass


# ── lettura delle fonti ───────────────────────────────────────────────────────


@dataclass
class _Page:
    id: str
    title: str
    version: int
    url: str
    data: FeatPage


@dataclass
class _File:
    path: str
    blob_sha: str
    text: str
    parsed: ParsedFeature
    code: str | None
    feature_tags: list[str] = field(default_factory=list)


def code_parts(code: str | None) -> tuple[str, int] | None:
    m = _CODE.search(code or "")
    return (m.group(2), int(m.group(1))) if m else None


def is_identity_tag(tag: str) -> bool:
    name = tag.lstrip("@")
    return name.upper().startswith("FEAT_") or bool(_HUB_IDENTITY.match(name))


def _norm_text(value: str) -> str:
    return " ".join(re.sub(r"[^0-9a-zà-ù ]", " ", value.lower()).split())


def title_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm_text(a), _norm_text(b)).ratio()


def page_title(title: str) -> str:
    return _TITLE_PREFIX.sub("", title).strip() or title


def _feature_tags(text: str) -> list[str]:
    tags: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Funzionalità:", "Feature:", "Caratteristica:")):
            return tags
        if stripped.startswith("@"):
            tags += [t for t in stripped.split() if t.startswith("@")]
    return []


def _file_code(path: str, text: str, tags: list[str]) -> str | None:
    for t in tags:
        m = _CODE.search(t)
        if m:
            return m.group(0)
    m = _CODE.search(posixpath.basename(path))
    return m.group(0) if m else None


def _blob_path(url: str | None) -> str | None:
    if not url or "/blob/" not in url:
        return None
    rest = url.split("/blob/", 1)[1]
    return rest.split("/", 1)[1] if "/" in rest else None


async def _read_pages(confluence: Any) -> list[_Page]:
    out = []
    for ref in await confluence.list_feat_pages(settings.tl_confluence_root_page_id):
        content = await confluence.get_page(ref.id)
        out.append(
            _Page(
                ref.id,
                content.title,
                content.version,
                content.url,
                parse_feat_page(content.storage),
            )
        )
    return out


async def _read_files(git: Any) -> list[_File]:
    head = await git.head_sha(settings.tl_base_branch)
    out = []
    for path in await git.list_tree(head):
        if not path.endswith(".feature"):
            continue
        file = await git.get_file(path, head)
        if file is None:
            continue
        tags = _feature_tags(file.text)
        out.append(
            _File(
                path,
                file.blob_sha,
                file.text,
                parse_feature_file(file.text, allowed_extra=()),
                _file_code(path, file.text, tags),
                tags,
            )
        )
    return out


# ── abbinamento ───────────────────────────────────────────────────────────────


def match_pages_files(
    pages: list[_Page], files: list[_File]
) -> list[tuple[_Page | None, _File | None, list[str]]]:
    """(pagina, file, problemi di abbinamento): link, poi codice, poi titolo."""
    by_path = {f.path: f for f in files}
    used: set[str] = set()
    pairs: dict[str, tuple[_File | None, list[str]]] = {}
    for p in pages:  # 1. link "Feature File"
        path = _blob_path(p.data.feature_file_url)
        if path in by_path and path not in used:
            pairs[p.id] = (by_path[path], [])
            used.add(path)
    for p in pages:  # 2. codice
        if p.id in pairs:
            continue
        code = (p.data.code or "").lower()
        cands = [f for f in files if f.path not in used and f.code and f.code.lower() == code]
        if cands:
            name = p.data.doc_name or p.title
            best = max(cands, key=lambda f: title_score(name, f.parsed.title or f.path))
            pairs[p.id] = (best, ["multiple_files"] if len(cands) > 1 else [])
            used.add(best.path)
    scored = sorted(  # 3. titolo
        (
            (title_score(page_title(p.title), f.parsed.title), p.id, f.path)
            for p in pages
            if p.id not in pairs
            for f in files
            if f.path not in used
        ),
        reverse=True,
    )
    for score, pid, path in scored:
        if score < TITLE_THRESHOLD or pid in pairs or path in used:
            continue
        pairs[pid] = (by_path[path], [])
        used.add(path)
    out: list[tuple[_Page | None, _File | None, list[str]]] = [
        (p, *pairs.get(p.id, (None, []))) for p in pages
    ]
    out += [(None, f, []) for f in files if f.path not in used]
    return out


# ── persone ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _UserRef:
    id: uuid.UUID
    email: str
    name: str


class _People:
    def __init__(self, users: list[_UserRef]) -> None:
        self.by_email = {u.email.lower(): u for u in users}
        self.by_name = {" ".join(u.name.lower().split()): u for u in users}

    def find(self, email: str | None, name: str | None) -> _UserRef | None:
        if email and email.lower() in self.by_email:
            return self.by_email[email.lower()]
        key = " ".join((name or "").lower().split())
        return self.by_name.get(key) if key else None


async def _resolve(confluence: Any, accounts: list[str]) -> list[tuple[str, str, str | None]]:
    out = []
    for acc in accounts:
        user = await confluence.get_user(acc)
        out.append((acc, user.display_name, user.email))
    return out


# ── costruzione delle proposte ────────────────────────────────────────────────


def _problem(code: str, detail: str | None = None) -> dict[str, str]:
    message = PROBLEM_MESSAGES[code]
    return {"code": code, "message": f"{message}: {detail}" if detail else message}


@dataclass
class _Proposal:
    key: tuple[str | None, str | None]  # (page_id, file_path)
    page: _Page | None
    file: _File | None
    scope_code: str
    number: int | None
    title: str
    status: TlFeatureStatus
    proposal: dict[str, Any]
    problems: list[dict[str, str]]


async def _propose(
    page: _Page | None,
    file: _File | None,
    match_problems: list[str],
    confluence: Any,
    people: _People,
) -> _Proposal:
    problems = [_problem(c) for c in match_problems]
    code = (page.data.code if page else None) or (file.code if file else None)
    parts = code_parts(code)
    scope_code, number = (parts[0], parts[1]) if parts else ("", None)
    if parts is None:
        problems.append(_problem("no_code"))
    if page:
        title = page_title(page.title)
        status = map_status(page.data.status_text)
    else:
        title = (file.parsed.title if file else "") or posixpath.basename(file.path if file else "")
        status = TlFeatureStatus.IMPLEMENTED
    prop: dict[str, Any] = {
        "page_url": page.url if page else None,
        "area": page.data.area if page else None,
        "repo_folder": posixpath.dirname(file.path) if file else None,
        "description": page.data.description
        if page
        else (file.parsed.description if file else None),
        "requirements_url": page.data.requirements_url if page else None,
        "jira_design_key": page.data.jira_design if page else None,
        "jira_impl_key": page.data.jira_impl if page else None,
        "product_issue_key": page.data.product_key if page else None,
        "impl_estimate_days": page.data.estimate_days if page else None,
        "background_steps": page.data.background_steps if page else None,
        "scenarios": [{"title": s.title, "steps": s.steps} for s in page.data.scenarios]
        if page
        else [],
        "note": page.data.note if page else "",
        "owner_id": None,
        "reviewer_id": None,
        "owner_name": None,
        "reviewer_name": None,
    }
    if page is None:
        problems.append(_problem("file_without_page"))
    if file is None:
        problems.append(_problem("page_without_file"))
    else:
        prop["file_title"] = file.parsed.title
        prop["file_scenarios"] = len(file.parsed.scenarios)
        prop["feature_tags"] = file.feature_tags
        prop["old_tags"] = [
            t.lstrip("@") for sc in file.parsed.scenarios for t in sc.all_tags if is_identity_tag(t)
        ] + [t.lstrip("@") for t in file.feature_tags if is_identity_tag(t)]
        if file.parsed.errors:
            problems.append(_problem("parse_error", "; ".join(file.parsed.errors)[:300]))
        if file.path.startswith("docs/use-cases/") or "/tas_" in file.path:
            problems.append(_problem("fixture_or_copy"))
        if status == TlFeatureStatus.DRAFT and page is not None:
            problems.append(_problem("draft_with_file"))
    if page:
        owners = await _resolve(confluence, page.data.owner_accounts[:1])
        if owners:
            acc, name, email = owners[0]
            prop["owner_name"] = name or acc
            user = people.find(email, name)
            if user:
                prop["owner_id"] = str(user.id)
            else:
                problems.append(_problem("owner_not_found", name or acc))
        if page.data.reviewer_label:
            prop["reviewer_name"] = page.data.reviewer_label
            problems.append(_problem("reviewer_is_team", page.data.reviewer_label))
        else:
            others = [
                a for a in page.data.reviewer_accounts if a not in page.data.owner_accounts[:1]
            ]
            reviewers = await _resolve(confluence, others[:1])
            if reviewers:
                acc, name, email = reviewers[0]
                prop["reviewer_name"] = name or acc
                user = people.find(email, name)
                if user:
                    prop["reviewer_id"] = str(user.id)
                else:
                    problems.append(_problem("reviewer_not_found", name or acc))
    return _Proposal(
        key=(page.id if page else None, file.path if file else None),
        page=page,
        file=file,
        scope_code=scope_code,
        number=number,
        title=title[:500],
        status=status,
        proposal=prop,
        problems=problems,
    )


def _canonical_scopes(proposals: list[_Proposal], existing: list[str]) -> None:
    """Stessa capitalizzazione per lo stesso ambito (FdR3/Fdr3): prima l'hub, poi le pagine."""
    canon = {c.lower(): c for c in existing}
    for p in sorted(proposals, key=lambda p: p.page is None):
        if p.scope_code:
            canon.setdefault(p.scope_code.lower(), p.scope_code)
    for p in proposals:
        if p.scope_code:
            p.scope_code = canon[p.scope_code.lower()]


def effective(item: TlMigrationItem) -> dict[str, Any]:
    """Valori da usare: colonne e proposta, con le correzioni dell'utente sopra."""
    eff: dict[str, Any] = {
        "scope_code": item.scope_code,
        "number": item.number,
        "title": item.title,
        "status": item.status.value if isinstance(item.status, TlFeatureStatus) else item.status,
        "owner_id": item.proposal.get("owner_id"),
        "reviewer_id": item.proposal.get("reviewer_id"),
        "file_path": item.file_path,
    }
    eff.update({k: v for k, v in (item.overrides or {}).items() if k in OVERRIDABLE})
    return eff


def _cross_problems(
    items: list[TlMigrationItem], hub_numbers: set[tuple[str, int]], files: list[_File]
) -> None:
    """Problemi che dipendono dalle altre righe: numeri doppi e tag identità condivisi."""
    counts: dict[tuple[str, int], int] = {}
    for it in items:
        eff = effective(it)
        if it.decision == "excluded" or eff["number"] is None or it.executed_at:
            continue
        key = (str(eff["scope_code"]).lower(), int(eff["number"]))
        counts[key] = counts.get(key, 0) + 1
    tag_files: dict[str, set[str]] = {}
    for f in files:
        for sc in f.parsed.scenarios:
            for t in sc.all_tags:
                if is_identity_tag(t):
                    tag_files.setdefault(t.lower(), set()).add(f.path)
    shared = {p for paths in tag_files.values() if len(paths) > 1 for p in paths}
    for it in items:
        if it.executed_at:
            continue
        probs = [
            p for p in it.problems if p["code"] not in ("number_conflict", "duplicate_identity_tag")
        ]
        eff = effective(it)
        if it.decision != "excluded" and eff["number"] is not None:
            key = (str(eff["scope_code"]).lower(), int(eff["number"]))
            if counts.get(key, 0) > 1 or key in hub_numbers:
                probs.append(_problem("number_conflict"))
        if eff["file_path"] in shared:
            probs.append(_problem("duplicate_identity_tag"))
        it.problems = probs


async def _hub_numbers(db: AsyncSession) -> set[tuple[str, int]]:
    rows = await db.execute(
        select(func.lower(TlScope.code), TlFeature.number).join(
            TlScope, TlScope.id == TlFeature.scope_id
        )
    )
    return {(c, n) for c, n in rows.all()}


async def recompute_problems(db: AsyncSession) -> None:
    """Ricalcola i problemi incrociati (dopo una correzione). Il chiamante fa commit."""
    items = list((await db.execute(select(TlMigrationItem))).scalars())
    _cross_problems(items, await _hub_numbers(db), [])
    # i tag identità condivisi dipendono dai file: si conservano dall'ultima analisi
    for it in items:
        if it.proposal.get("shared_identity") and not any(
            p["code"] == "duplicate_identity_tag" for p in it.problems
        ):
            it.problems = [*it.problems, _problem("duplicate_identity_tag")]


async def _set_state(db: AsyncSession, **values: Any) -> None:
    state = await db.get(TlMigrationState, 1)
    if state is None:
        state = TlMigrationState(id=1)
        db.add(state)
    for k, v in values.items():
        setattr(state, k, v)


async def analyze(db: AsyncSession, confluence: Any, git: Any) -> TlMigrationSummary:
    async with try_advisory_lock(db, LOCK_KEY) as got:
        if not got:
            raise MigrationInProgress()
        try:
            return await _analyze_locked(db, confluence, git)
        except Exception as exc:
            await db.rollback()
            await _set_state(db, status="error", error=str(exc)[:1000])
            await db.commit()
            raise


async def _analyze_locked(db: AsyncSession, confluence: Any, git: Any) -> TlMigrationSummary:
    rows = await db.execute(select(User.id, User.email, User.name).where(User.is_active.is_(True)))
    people = _People([_UserRef(i, e, n) for i, e, n in rows.all()])
    existing_scopes = list((await db.execute(select(TlScope.code))).scalars())
    await db.rollback()  # nessuna transazione aperta durante la rete

    pages = await _read_pages(confluence)
    files = await _read_files(git)
    proposals = [
        await _propose(p, f, probs, confluence, people)
        for p, f, probs in match_pages_files(pages, files)
    ]
    _canonical_scopes(proposals, existing_scopes)

    rows = list((await db.execute(select(TlMigrationItem))).scalars())
    by_page = {r.page_id: r for r in rows if r.page_id}
    by_file = {r.file_path: r for r in rows if r.file_path}
    keep_ids: set[uuid.UUID] = set()
    plan: list[tuple[TlMigrationItem | None, _Proposal]] = []
    for prop in proposals:
        page_id, path = prop.key
        row = by_page.get(page_id) if page_id else None
        if row is None and path:
            row = by_file.get(path)
            if row is not None and row.page_id and row.page_id != page_id:
                row = None
        plan.append((row, prop))
        if row is not None:
            keep_ids.add(row.id)
    # righe non più presenti (o fuse): via, se non eseguite; poi si libera il percorso
    for r in rows:
        if r.id not in keep_ids and r.executed_at is None:
            await db.delete(r)
    await db.flush()
    for row, prop in plan:
        if row is not None and row.executed_at is not None:
            continue
        if row is None:
            row = TlMigrationItem(decision="pending", overrides={})
            db.add(row)
        page_id, path = prop.key
        row.page_id = page_id
        row.page_title = prop.page.title if prop.page else None
        row.page_version = prop.page.version if prop.page else None
        row.file_path = path
        row.file_blob_sha = prop.file.blob_sha if prop.file else None
        row.scope_code = prop.scope_code
        row.number = prop.number
        row.title = prop.title
        row.status = prop.status
        row.proposal = prop.proposal
        row.problems = prop.problems
    await db.flush()
    items = list((await db.execute(select(TlMigrationItem))).scalars())
    _cross_problems(items, await _hub_numbers(db), files)
    for it in items:
        if not it.executed_at:
            shared = any(p["code"] == "duplicate_identity_tag" for p in it.problems)
            it.proposal = {**it.proposal, "shared_identity": shared}

    # tag non identità (di scenario e di feature): conteggi, decisioni conservate
    counts: dict[str, int] = {}
    for f in files:
        names = [t for sc in f.parsed.scenarios for t in sc.all_tags] + f.feature_tags
        for t in names:
            name = t.lstrip("@")
            if not is_identity_tag(t) and name.lower() not in _POLARITY:
                counts[name] = counts.get(name, 0) + 1
    tags = {t.name: t for t in (await db.execute(select(TlMigrationTag))).scalars()}
    for name, n in counts.items():
        if name in tags:
            tags[name].occurrences = n
        else:
            db.add(TlMigrationTag(name=name, occurrences=n, decision="keep"))
    for name, tag in tags.items():
        if name not in counts:
            await db.delete(tag)
    await _set_state(db, status="idle", error=None, analyzed_at=datetime.now(UTC))
    await db.commit()
    return await summary(db)


# ── riepilogo e viste ─────────────────────────────────────────────────────────


def item_out(item: TlMigrationItem) -> TlMigrationItemOut:
    return TlMigrationItemOut(
        id=item.id,
        page_id=item.page_id,
        page_title=item.page_title,
        page_url=item.proposal.get("page_url"),
        file_path=item.file_path,
        scope_code=item.scope_code,
        number=item.number,
        title=item.title,
        status=item.status,
        decision=item.decision,
        problems=item.problems,
        proposal=item.proposal,
        overrides=item.overrides,
        effective=effective(item),
        feature_id=item.feature_id,
        executed_at=item.executed_at,
    )


async def summary(db: AsyncSession) -> TlMigrationSummary:
    state = await db.get(TlMigrationState, 1)
    items = list((await db.execute(select(TlMigrationItem))).scalars())
    scopes_db = {s.scope_code: s for s in (await db.execute(select(TlMigrationScope))).scalars()}
    per: dict[str, TlMigrationScopeOut] = {}
    for it in items:
        code = str(effective(it)["scope_code"] or "")
        out = per.setdefault(code, TlMigrationScopeOut(scope_code=code))
        out.items += 1
        if it.executed_at:
            out.executed += 1
        elif it.decision == "confirmed":
            out.confirmed += 1
        elif it.decision == "excluded":
            out.excluded += 1
        else:
            out.pending += 1
        if it.problems and not it.executed_at and it.decision != "excluded":
            out.with_problems += 1
    for code, s in scopes_db.items():
        out = per.setdefault(code, TlMigrationScopeOut(scope_code=code))
        out.executed_at, out.pr_number, out.pr_url = s.executed_at, s.pr_number, s.pr_url
        out.pr_state, out.pr_error, out.confluence_errors = (
            s.pr_state,
            s.pr_error,
            s.confluence_errors,
        )
    allowed = set(await category_tags(db))
    tags = [
        TlMigrationTagOut(
            name=t.name, occurrences=t.occurrences, decision=t.decision, category=t.name in allowed
        )
        for t in (await db.execute(select(TlMigrationTag).order_by(TlMigrationTag.name))).scalars()
    ]
    return TlMigrationSummary(
        status=state.status if state else "idle",
        analyzed_at=state.analyzed_at if state else None,
        error=state.error if state else None,
        confluence_write=settings.tl_migration_confluence_write,
        scopes=sorted(per.values(), key=lambda s: s.scope_code.lower()),
        tags=tags,
    )


async def wipe_unexecuted(db: AsyncSession) -> None:
    """Solo per strumenti di test/manutenzione: rimuove le righe non eseguite."""
    await db.execute(delete(TlMigrationItem).where(TlMigrationItem.executed_at.is_(None)))
