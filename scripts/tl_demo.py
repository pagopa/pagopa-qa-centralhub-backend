"""Prepara il Test Lifecycle per una demo (solo sviluppo).

Svuota i dati del Test Lifecycle, riporta Git allo stato di partenza e crea l'ambito "Demo"
con una feature per ogni momento del ciclo di vita, più una decina di run NRT finte.

Git, secondo TL_GIT_MODE:
- `local`: clone indicato da TL_LOCAL_REPO_PATH; PR simulate, nulla va su GitHub;
- `github`: solo un repository di prova (mai dell'organizzazione `pagopa`) che abbia i tag
  `demo-base` (stato iniziale di main) e `demo-pq827-base` (branch da importare). Le PR sono
  vere e la preparazione ne fa il merge via API.

Nulla viene scritto su Jira o Confluence: le feature già avanzate usano come storico le chiavi
Jira indicate qui sotto (devono esistere, vengono solo lette).

Uso:
    uv run python -m scripts.tl_demo prepare     # svuota e ricrea i dati della demo
    uv run python -m scripts.tl_demo clean       # svuota soltanto
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, text

from app.config import settings
from app.core.db import async_session
from app.models.test_lifecycle import TlFeature, TlFeatureRevision
from app.models.test_metrics import TestExecution, TestRun, TestSuite
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlCommentCreate,
    TlExamples,
    TlFeatureCreate,
    TlFeatureUpdate,
    TlRevisionUpdate,
    TlScenarioCreate,
    TlScenarioUpdate,
    TlScopeCreate,
    TlTransitionAction,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import git_sync, nrt, reviews, scopes, workflow
from app.services.test_lifecycle.git_client import TlGitClient
from app.services.test_lifecycle.local_git import _ok, _run, local_merge, local_reset, local_write

PRESENTER = "cristiano.sticca@pagopa.it"  # chi fa la demo: reviewer e owner implementazione
OWNER = "reviewer.prova@example.test"  # owner di progettazione delle feature preparate
OWNER_NAME = "Reviewer di prova"
DESIGN_KEY = "PQ-849"  # task Jira già esistenti, usati solo come storico
IMPL_KEY = "PQ-850"
SUITE_OBJECT = "qa-hub-demo"  # suite NRT finta, cancellata a ogni preparazione
IMPORT_BRANCH = "PQ-827-fdr-e2e-test-design"
IMPORT_PATH = (
    "src/integration/fdr3/feature/FEAT_011_FdR3_Recupero_FdR_Pubblicati_Organizzazione.feature"
)
BASE_TAG = "demo-base"  # repository GitHub di prova: stato iniziale di main
IMPORT_TAG = "demo-pq827-base"  # e del branch da importare

SCOPE = TlScopeCreate(
    code="Demo",
    name="Demo pagamenti",
    area="Demo",
    repo_folder="src/integration/demo/features",
    owner_team="pagopa-team-qa",
)
BACKGROUND = "Dato un ente creditore attivo\nE un avviso di pagamento valido"

# (titolo, tipo, polarità, step, esempi)
Scenario = tuple[str, str, str, str, dict[str, Any] | None]

FEATURES: dict[str, dict[str, Any]] = {
    "running": {
        "title": "Pagamento con carta",
        "description": "Come cittadino voglio pagare un avviso con la carta per saldare il debito.",
        "scenarios": [
            ("Pagamento con carta valida", "scenario", "positivo",
             "Quando il cittadino paga con una carta valida\nAllora il pagamento risulta eseguito\n"
             "E viene emessa la ricevuta", None),
            ("Carta scaduta rifiutata", "scenario", "negativo",
             "Quando il cittadino paga con una carta scaduta\nAllora il pagamento viene rifiutato\n"
             "E l'avviso resta da pagare", None),
            ("Importi ammessi", "outline", "positivo",
             "Quando il cittadino paga <importo> euro con una carta valida\n"
             "Allora il pagamento ha esito <esito>",
             {"headers": ["importo", "esito"], "rows": [["1", "OK"], ["9999", "OK"], ["0", "KO"]]}),
        ],
    },
    "versions": {
        "title": "Annullamento del pagamento",
        "description": "Come cittadino voglio annullare un pagamento non ancora concluso.",
        "scenarios": [
            ("Annullamento prima della conferma", "scenario", "positivo",
             "Quando il cittadino annulla il pagamento prima della conferma\n"
             "Allora il pagamento risulta annullato", None),
        ],
        "second_revision": [
            ("Annullamento dopo la conferma non ammesso", "scenario", "negativo",
             "Quando il cittadino annulla il pagamento dopo la conferma\n"
             "Allora l'annullamento viene rifiutato", None),
        ],
    },
    "drift": {
        "title": "Ricevuta in PDF",
        "description": "Come cittadino voglio scaricare la ricevuta del pagamento in PDF.",
        "scenarios": [
            ("Download della ricevuta", "scenario", "positivo",
             "Dato un pagamento eseguito\nQuando il cittadino scarica la ricevuta\n"
             "Allora riceve un file PDF", None),
            ("Ricevuta di un pagamento inesistente", "scenario", "negativo",
             "Quando il cittadino scarica la ricevuta di un pagamento inesistente\n"
             "Allora riceve un errore 404", None),
        ],
    },
    "designed": {
        "title": "Notifica email al pagatore",
        "description": "Come pagatore voglio ricevere una email quando il pagamento è concluso.",
        "scenarios": [
            ("Email inviata a pagamento eseguito", "scenario", "positivo",
             "Dato un pagamento eseguito\nQuando il sistema elabora l'esito\n"
             "Allora il pagatore riceve una email di conferma", None),
        ],
    },
    "in_review": {
        "title": "Pagamento con app IO",
        "description": "Come cittadino voglio pagare un avviso direttamente dall'app IO.",
        "scenarios": [
            ("Pagamento dall'app IO", "scenario", "positivo",
             "Quando il cittadino paga l'avviso dall'app IO\nAllora il pagamento risulta eseguito",
             None),
        ],
        "after_changes": [
            ("Avviso già pagato", "scenario", "negativo",
             "Dato un avviso già pagato\nQuando il cittadino prova a pagarlo dall'app IO\n"
             "Allora l'app mostra che l'avviso è già pagato", None),
        ],
    },
}


class _LocalGit:
    """Clone locale: PR simulate (`local_git`)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.base = settings.tl_base_branch or "main"

    async def reset(self) -> None:
        local_reset(self.repo, self.base)
        if _ok(self.repo, "rev-parse", "--verify", f"origin/{IMPORT_BRANCH}"):
            _run(self.repo, "branch", "-f", IMPORT_BRANCH, f"origin/{IMPORT_BRANCH}")

    async def merge(self, number: int) -> None:
        local_merge(self.repo, number)

    async def read(self, path: str) -> str:
        return _run(self.repo, "show", f"{self.base}:{path}")

    async def write(self, path: str, text: str, message: str) -> None:
        local_write(self.repo, self.base, path, text, message)


class _GitHubGit:
    """Repository GitHub di prova: PR vere, merge e modifiche dirette via API."""

    def __init__(self, client: TlGitClient) -> None:
        self.c = client
        self.base = settings.tl_base_branch or "main"

    async def _tag_sha(self, tag: str) -> str:
        resp = await self.c._request("GET", f"git/ref/tags/{tag}", ok_statuses=(404,))
        if resp.status_code == 404:
            sys.exit(f"Interrotto: {self.c.repo} senza il tag {tag}: non è un repository di demo.")
        return resp.json()["object"]["sha"]

    async def _set_branch(self, branch: str, sha: str) -> None:
        if await self.c.branch_exists(branch):
            await self.c.reset_branch(branch, sha)
        else:
            await self.c.create_branch(branch, sha)

    async def reset(self) -> None:
        base_sha, import_sha = await self._tag_sha(BASE_TAG), await self._tag_sha(IMPORT_TAG)
        pulls = await self.c._request("GET", "pulls", params={"state": "open", "per_page": "100"})
        for pr in pulls.json():
            await self.c._request("PATCH", f"pulls/{pr['number']}", json={"state": "closed"})
        refs = await self.c._request("GET", "git/matching-refs/heads/qa-hub/")
        for ref in refs.json():
            await self.c._request("DELETE", f"git/{ref['ref']}")
        await self._set_branch(self.base, base_sha)
        await self._set_branch(IMPORT_BRANCH, import_sha)

    async def merge(self, number: int) -> None:
        # subito dopo l'apertura GitHub può non aver ancora calcolato la mergeabilità
        for _ in range(15):
            resp = await self.c._request(
                "PUT", f"pulls/{number}/merge", json={"merge_method": "merge"},
                ok_statuses=(405, 409),
            )
            if resp.status_code == 200:
                return
            await asyncio.sleep(2)
        sys.exit(f"Merge della PR #{number} non riuscito: {resp.text[:200]}")

    async def read(self, path: str) -> str:
        found = await self.c.get_file(path, self.base)
        if found is None:
            sys.exit(f"{path} non trovato su {self.base}")
        return found.text

    async def write(self, path: str, text: str, message: str) -> None:
        found = await self.c.get_file(path, self.base)
        await self.c.put_file(path, text, self.base, message, found.blob_sha if found else None)


Git = _LocalGit | _GitHubGit


def _guard() -> Git:
    """Mai fuori dallo sviluppo locale, mai sul repository vero dei test."""
    host = urlparse(settings.database_url.replace("+asyncpg", "")).hostname
    if host not in ("localhost", "127.0.0.1", "::1", "db", "postgres"):
        sys.exit(f"Interrotto: il database non è locale ({host}).")
    if settings.tl_git_mode == "local":
        if not settings.tl_local_repo_path:
            sys.exit("Interrotto: TL_GIT_MODE=local senza TL_LOCAL_REPO_PATH.")
        return _LocalGit(settings.tl_local_repo_path)
    if settings.tl_repo.split("/", 1)[0].lower() == "pagopa":
        sys.exit(f"Interrotto: {settings.tl_repo} è di pagopa: usa un repository di prova.")
    client = git_client_mod.get_git_client()
    if not isinstance(client, TlGitClient):
        sys.exit("Interrotto: client GitHub non disponibile.")
    return _GitHubGit(client)


# ── pulizia ──────────────────────────────────────────────────────────────────

async def clean(git: Git) -> None:
    await git.reset()  # per primo: se il repository non è di demo si ferma qui
    async with async_session() as db:
        tables = (
            await db.execute(
                text(
                    "select table_name from information_schema.tables where table_schema='qachub'"
                    " and table_name like 'tl\\_%' and table_name not in"
                    " ('tl_category_tags','tl_migration_state')"
                )
            )
        ).scalars().all()
        await db.execute(text("TRUNCATE " + ", ".join(f"qachub.{t}" for t in tables) + " CASCADE"))
        await db.execute(
            text(
                "DELETE FROM qachub.tl_category_tags"
                " WHERE name NOT IN ('runnable','e2e','ui','skip')"
            )
        )
        await db.execute(
            text("UPDATE qachub.tl_migration_state SET status='idle', error=NULL, analyzed_at=NULL")
        )
        await db.execute(text("DELETE FROM audit_logs WHERE resource_type='tl_feature'"))
        await db.execute(
            text("DELETE FROM qachub.test_suites WHERE test_object=:o"), {"o": SUITE_OBJECT}
        )
        await db.commit()
    print(
        "Pulizia fatta: dati del Test Lifecycle, suite NRT della demo, "
        f"Git ({settings.tl_git_mode})."
    )


# ── preparazione ─────────────────────────────────────────────────────────────

async def _user(db: Any, email: str, name: str | None = None) -> uuid.UUID:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        if name is None:
            sys.exit(f"Utente {email} assente: accedi almeno una volta all'hub.")
        user = User(email=email, name=name, role="qa_analyst", is_active=True)
        db.add(user)
        await db.commit()
    return user.id


async def _revision(db: Any, feature_id: uuid.UUID) -> TlFeatureRevision:
    """Revisione di lavoro, o la pubblicata se non ce n'è una aperta (per riaprire)."""
    feature = await db.get(TlFeature, feature_id, populate_existing=True)
    rev_id = feature.working_revision_id or feature.published_revision_id
    rev = await db.get(TlFeatureRevision, rev_id, populate_existing=True)
    await db.commit()
    return rev


async def _fill(db: Any, feature_id: uuid.UUID, scenarios: list[Scenario]) -> None:
    for title, kind, polarity, steps, examples in scenarios:
        rev = await _revision(db, feature_id)
        sc = await features_svc.add_scenario(
            db, feature_id, rev.id,
            TlScenarioCreate(actor_email=OWNER, title=title, kind=kind, polarity=polarity),
        )
        rev = await _revision(db, feature_id)
        await features_svc.update_scenario(
            db, feature_id, rev.id, sc.id,
            TlScenarioUpdate(
                actor_email=OWNER, lock_version=rev.lock_version, steps=steps,
                examples=TlExamples(**examples) if examples else None,
            ),
        )


async def _transition(db: Any, feature_id: uuid.UUID, action: TlTransitionAction, actor: str,
                      note: str | None = None) -> None:
    rev = await _revision(db, feature_id)
    await workflow.apply_transition(db, feature_id, rev.id, action, actor, note)


async def _pr_number(db: Any, feature_id: uuid.UUID) -> int:
    feature = await db.get(TlFeature, feature_id, populate_existing=True)
    rev = await db.get(TlFeatureRevision, feature.working_revision_id, populate_existing=True)
    await db.commit()
    if not rev.pr_number:
        sys.exit(f"PR non creata per {feature.code}: {rev.pr_error}")
    return rev.pr_number


async def _sync(db: Any) -> None:
    await git_sync.run_sync(db, git_client_mod.get_git_client())


async def _create(db: Any, scope_id: uuid.UUID, spec: dict[str, Any], owner_id: uuid.UUID,
                  presenter_id: uuid.UUID, impl_key: str | None) -> uuid.UUID:
    fid = await features_svc.create_feature(
        db, TlFeatureCreate(scope_id=scope_id, title=spec["title"], design_owner_id=owner_id,
                            actor_email=OWNER),
    )
    await features_svc.update_feature(
        db, fid,
        TlFeatureUpdate(actor_email=OWNER, reviewer_id=presenter_id, impl_owner_id=presenter_id,
                        jira_design_key=DESIGN_KEY, jira_impl_key=impl_key),
    )
    rev = await _revision(db, fid)
    await features_svc.update_revision(
        db, fid, rev.id,
        TlRevisionUpdate(actor_email=OWNER, lock_version=rev.lock_version,
                         description=spec["description"], background_steps=BACKGROUND,
                         reverse_engineering=True, design_estimate_days=2, impl_estimate_days=3),
    )
    await _fill(db, fid, spec["scenarios"])
    return fid


async def _approve_and_merge(db: Any, git: Git, fid: uuid.UUID) -> None:
    await _transition(db, fid, TlTransitionAction.SUBMIT, OWNER)
    await _transition(db, fid, TlTransitionAction.APPROVE, PRESENTER)
    await git.merge(await _pr_number(db, fid))
    await _sync(db)


async def _nrt_runs(db: Any, feature_id: uuid.UUID) -> int:
    """Dodici run giornaliere: scenario 01 sempre ok, 02 un fallimento, 03 instabile."""
    feature = await db.get(TlFeature, feature_id, populate_existing=True)
    tags = sorted(
        r for (r,) in (
            await db.execute(text("select tag from qachub.tl_scenarios where feature_id=:f"),
                             {"f": feature_id})
        ).all()
    )
    titles = [s[0] for s in FEATURES["running"]["scenarios"]]
    suite = TestSuite(test_object=SUITE_OBJECT, test_type="NRT", test_object_version="1.0",
                      owner_team="pagopa-team-qa")
    db.add(suite)
    await db.flush()
    now = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
    for day in range(12, 0, -1):
        start = now - timedelta(days=day)
        statuses = [
            "PASSED",
            "FAILED" if day == 5 else "PASSED",
            "FAILED" if day % 3 == 0 else "PASSED",
        ]
        run = TestRun(
            suite_id=suite.id, env="UAT", trigger_type="CRON", test_origin="qa-hub-demo",
            revision_result="PENDING", revisioned=False,
            timestamp_start=start, timestamp_end=start + timedelta(minutes=4), duration_ms=240000,
            scenario_qty=3, passed_scenario=statuses.count("PASSED"),
            failed_scenario=statuses.count("FAILED"), broken_scenario=0, skipped_scenario=0,
        )
        db.add(run)
        await db.flush()
        for tag, title, status in zip(tags, titles, statuses, strict=True):
            db.add(TestExecution(
                run_id=run.id, status=status, scenario_name=title, duration_ms=80000,
                error_message="AssertionError: esito atteso OK" if status == "FAILED" else None,
                allure_report={"labels": [{"name": "tag", "value": tag.lstrip("@")},
                                          {"name": "feature", "value": feature.code}]},
            ))
    await db.commit()
    return await nrt.promote_running(db)


async def prepare(git: Git) -> None:
    await clean(git)

    async def _no_jira(*_: Any, **__: Any) -> None:  # nessun task Jira creato dalla preparazione
        return None

    workflow._jira_impl_best_effort = _no_jira  # type: ignore[assignment]

    async with async_session() as db:
        presenter_id = await _user(db, PRESENTER)
        owner_id = await _user(db, OWNER, OWNER_NAME)
        scope = await scopes.create_scope(db, SCOPE)
        scope_id = scope.id
        await db.commit()
        ids: dict[str, uuid.UUID] = {}

        # 1. In esecuzione: approvata, merge, run NRT
        ids["running"] = await _create(
            db, scope_id, FEATURES["running"], owner_id, presenter_id, IMPL_KEY
        )
        await _approve_and_merge(db, git, ids["running"])

        # 2. Implementata con due versioni
        fid = ids["versions"] = await _create(
            db, scope_id, FEATURES["versions"], owner_id, presenter_id, IMPL_KEY
        )
        await _approve_and_merge(db, git, fid)
        await _transition(db, fid, TlTransitionAction.REOPEN, OWNER)
        await _fill(db, fid, FEATURES["versions"]["second_revision"])
        await _approve_and_merge(db, git, fid)

        # 3. Implementata, poi modificata direttamente su Git: versione da Git + anomalia
        fid = ids["drift"] = await _create(
            db, scope_id, FEATURES["drift"], owner_id, presenter_id, IMPL_KEY
        )
        await _approve_and_merge(db, git, fid)
        feature = await db.get(TlFeature, fid, populate_existing=True)
        path = feature.git_path
        await db.commit()
        content = await git.read(path)
        content = content.replace("Allora riceve un file PDF", "Allora riceve un file PDF firmato")
        await git.write(path, content, "Ricevuta firmata (modifica diretta su main)")
        await _sync(db)
        content += (
            "\n  Scenario: Ricevuta in inglese\n"
            "    Dato un pagamento eseguito\n"
            "    Quando il cittadino scarica la ricevuta in inglese\n"
            "    Allora riceve un file PDF in inglese\n"
        )
        await git.write(path, content, "Nuovo scenario senza tag (modifica diretta su main)")
        await _sync(db)

        # 4. Progettata: approvata, PR aperta
        fid = ids["designed"] = await _create(
            db, scope_id, FEATURES["designed"], owner_id, presenter_id, IMPL_KEY
        )
        await _transition(db, fid, TlTransitionAction.SUBMIT, OWNER)
        await _transition(db, fid, TlTransitionAction.APPROVE, PRESENTER)

        # 5. In review dopo un giro di modifiche richieste (il task di implementazione manca:
        #    nasce dal vivo all'approvazione)
        fid = ids["in_review"] = await _create(
            db, scope_id, FEATURES["in_review"], owner_id, presenter_id, None
        )
        await _transition(db, fid, TlTransitionAction.SUBMIT, OWNER)
        await _transition(db, fid, TlTransitionAction.REQUEST_CHANGES, PRESENTER,
                          "Manca il caso dell'avviso già pagato.")
        await _fill(db, fid, FEATURES["in_review"]["after_changes"])
        await _transition(db, fid, TlTransitionAction.SUBMIT, OWNER)
        rev = await _revision(db, fid)
        await reviews.add_comment(
            db, fid, rev.id,
            TlCommentCreate(actor_email=OWNER, body="Aggiunto lo scenario dell'avviso già pagato."),
        )

        promoted = await _nrt_runs(db, ids["running"])

        rows = (
            await db.execute(
                select(TlFeature.code, TlFeature.status, TlFeature.id)
                .where(TlFeature.scope_id == scope_id).order_by(TlFeature.number)
            )
        ).all()
        await db.commit()
    print(f"Ambito {SCOPE.code} pronto ({promoted} feature in esecuzione):")
    for code, status, fid in rows:
        print(f"  {code:<18} {status.value:<12} /test-lifecycle/{fid}")
    print("Link per Importa da Git:")
    print(f"  https://github.com/{settings.tl_repo}/blob/{IMPORT_BRANCH}/{IMPORT_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tl_demo", description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=["prepare", "clean"])
    args = parser.parse_args(argv)
    git = _guard()
    asyncio.run(prepare(git) if args.cmd == "prepare" else clean(git))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
