"""Pagine Confluence e feature file di prova per i test di migrazione (fase 5)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete

from app.models.test_lifecycle import (
    TlMigrationItem,
    TlMigrationScope,
    TlMigrationState,
    TlMigrationTag,
)
from tests._db import TestSession


def page(
    code: str,
    name: str,
    status: str = "WIP",
    *,
    owner: str | None = None,
    reviewer: str | None = None,
    team: bool = False,
    file_url: str | None = None,
    scenarios: list[tuple[str, str, str, str]] = (),
    jira: str | None = None,
) -> str:
    """Storage minimo di una pagina FEAT."""
    rows = [
        ("ID documento", code),
        ("Nome documento", name),
        ("Ambito", code.split("_")[2]),
        ("Stato del documento", status),
    ]
    html = ["<table><tbody>"]
    html += [f"<tr><td><p><strong>{k}</strong></p></td><td><p>{v}</p></td></tr>" for k, v in rows]
    if owner:
        html.append(
            f'<tr><td><p>Owner</p></td><td><p><ac:link><ri:user ri:account-id="{owner}" />'
            "</ac:link></p></td></tr>"
        )
    if reviewer or team:
        inner = '<a href="https://x.test/people/team/t1">qa_team</a>' if team else ""
        if reviewer:
            inner += f'<ac:link><ri:user ri:account-id="{reviewer}" /></ac:link>'
        html.append(f"<tr><td><p>Reviewer</p></td><td><p>{inner}</p></td></tr>")
    html.append("</tbody></table><h1>Dettagli</h1><table><tbody>")
    if file_url:
        html.append(
            f'<tr><td><p>Feature File</p></td><td><p><a href="{file_url}">f</a></p></td></tr>'
        )
    if jira:
        html.append(f"<tr><td><p>Jira Progettazione (QA)</p></td><td><p>{jira}</p></td></tr>")
    html.append("</tbody></table>")
    for i, (title, given, when, then) in enumerate(scenarios, start=1):
        html.append(
            f"<h2>Scenario {i:02d}: {title}</h2><table><tbody>"
            "<tr><th><p>Given</p></th><th><p>When</p></th><th><p>Then</p></th></tr>"
            f"<tr><td><p>{given}</p></td><td><p>{when}</p></td><td><p>{then}</p></td></tr>"
            "</tbody></table>"
        )
    return "".join(html)


def feature_file(
    feature_tag: str | None,
    title: str,
    scenarios: list[tuple[str, str]],
    header: str = "# language: it",
) -> str:
    """Feature file legacy: (tag scenario, titolo) con step fissi."""
    lines = [header]
    if feature_tag:
        lines.append(feature_tag)
    lines.append(f"Funzionalità: {title}")
    lines.append("")
    for tags, sc_title in scenarios:
        lines += [
            f"  {tags}",
            f"  Scenario: {sc_title}",
            "    Dato un sistema",
            "    Quando agisco",
            "    Allora funziona",
            "",
        ]
    return "\n".join(lines)


def unique_scope() -> str:
    return "M" + uuid.uuid4().hex[:6].upper()


async def _wipe() -> None:
    async with TestSession() as s:
        await s.execute(delete(TlMigrationItem))
        await s.execute(delete(TlMigrationScope))
        await s.execute(delete(TlMigrationTag))
        state = await s.get(TlMigrationState, 1)
        if state is not None:
            state.status, state.error, state.analyzed_at = "idle", None, None
        await s.commit()


@pytest.fixture
async def clean_migration() -> AsyncIterator[None]:
    """Le tabelle di migrazione sono globali: ogni test parte e finisce pulito."""
    await _wipe()
    yield
    await _wipe()
