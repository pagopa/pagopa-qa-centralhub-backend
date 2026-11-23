"""Lettura delle pagine FEAT di Confluence (fase 5): metadati e scenari.

Funzione pura sul formato storage della pagina. Riconosce anche la forma HTML convertita
(menzioni `data-user-id`, stato `data-type="status"`), così regge pagine scritte in modi diversi.
Non solleva mai: quello che non riesce a interpretare finisce in `note`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.models.test_lifecycle import TlFeatureStatus

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_FEAT_CODE = re.compile(r"(?<![A-Za-z0-9])FEAT_\d{3}_[A-Za-z][A-Za-z0-9]*(?![A-Za-z0-9])")
_SCENARIO = re.compile(r"^\s*Scenario\s*(\d+)\s*[:\-–—]\s*(.+?)\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
_KEYWORD_RE = re.compile(r"^(Dato|Data|Dati|Date|Quando|Allora|E|Ma)\b")
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = _HEADINGS | {"table", "ul", "ol", "p"}
_VOID = {"br", "hr", "img", "col", "ri:user", "ri:page", "ri:attachment"}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        parts: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag in ("p", "li", "br", "tr", "td", "th", "div"):
                parts.append(" " + c.text() + " ")
            elif c.tag == "ac:parameter":
                continue  # parametri delle macro: letti a parte
            else:
                parts.append(c.text())
        return " ".join("".join(parts).split())

    def find_all(self, tag: str) -> Iterator[_Node]:
        for c in self.children:
            if isinstance(c, _Node):
                if c.tag == tag:
                    yield c
                yield from c.find_all(tag)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


@dataclass
class PageScenario:
    number: int
    title: str
    steps: str


@dataclass
class FeatPage:
    code: str | None = None
    doc_name: str | None = None
    area: str | None = None
    status_text: str | None = None
    owner_accounts: list[str] = field(default_factory=list)
    reviewer_accounts: list[str] = field(default_factory=list)
    reviewer_label: str | None = None
    requirements_url: str | None = None
    jira_design: str | None = None
    jira_impl: str | None = None
    product_key: str | None = None
    estimate_days: float | None = None
    description: str | None = None
    feature_file_url: str | None = None
    background_steps: str | None = None
    scenarios: list[PageScenario] = field(default_factory=list)
    note: str = ""


def map_status(text: str | None) -> TlFeatureStatus:
    norm = " ".join((text or "").lower().split())
    if "test suite implemented" in norm:
        return TlFeatureStatus.IMPLEMENTED
    if norm.startswith("approvato"):
        return TlFeatureStatus.DESIGNED
    return TlFeatureStatus.DRAFT


def _blocks(node: _Node) -> Iterator[_Node]:
    """Blocchi in ordine di documento (titoli, tabelle, liste, paragrafi), anche dentro i layout."""
    for c in node.children:
        if not isinstance(c, _Node):
            continue
        if c.tag in _BLOCKS:
            yield c
        else:
            yield from _blocks(c)


def _rows(table: _Node) -> list[list[_Node]]:
    return [
        [c for c in tr.children if isinstance(c, _Node) and c.tag in ("td", "th")]
        for tr in table.find_all("tr")
    ]


def _label(text: str) -> str:
    return " ".join(text.lower().replace(":", " ").split())


def _accounts(node: _Node) -> list[str]:
    out: list[str] = []
    for n in [node, *_descendants(node)]:
        acc = n.attrs.get("ri:account-id") or (
            n.attrs.get("data-user-id") if n.attrs.get("data-type") == "mention" else None
        )
        if acc and acc not in out:
            out.append(acc)
    return out


def _descendants(node: _Node) -> Iterator[_Node]:
    for c in node.children:
        if isinstance(c, _Node):
            yield c
            yield from _descendants(c)


def _status(node: _Node) -> str:
    for n in _descendants(node):
        if n.tag == "ac:structured-macro" and n.attrs.get("ac:name") == "status":
            for p in n.find_all("ac:parameter"):
                if p.attrs.get("ac:name") == "title":
                    return " ".join("".join(c for c in p.children if isinstance(c, str)).split())
        if n.attrs.get("data-type") == "status":
            return n.text()
    return node.text()


def _jira_key(node: _Node) -> str | None:
    candidates: list[str] = []
    for n in _descendants(node):
        if n.tag == "ac:parameter" and n.attrs.get("ac:name") == "key":
            candidates.append("".join(c for c in n.children if isinstance(c, str)))
        if n.attrs.get("href"):
            candidates.append(n.attrs["href"])
    candidates.append(node.text())
    for c in candidates:
        m = _JIRA_KEY.search(c)
        if m:
            return m.group(1)
    return None


def _hrefs(node: _Node) -> list[str]:
    return [
        n.attrs["href"] for n in _descendants(node) if n.attrs.get("href", "").startswith("http")
    ]


def _team(node: _Node) -> str | None:
    for n in _descendants(node):
        if "/people/team/" in n.attrs.get("href", ""):
            return n.text() or None
    return None


def _items(cell: _Node) -> list[str]:
    lis = list(cell.find_all("li"))
    texts = [li.text() for li in lis] if lis else [
        p.text() for p in cell.find_all("p")
    ] or [cell.text()]
    out = []
    for t in texts:
        t = re.sub(r"^[-•*]\s*", "", t.strip())
        if t:
            out.append(t)
    return out


def _with_keyword(items: list[str], first: str) -> list[str]:
    out = []
    for i, item in enumerate(items):
        if _KEYWORD_RE.match(item):
            out.append(item)
        else:
            out.append(f"{first if i == 0 else 'E'} {item}")
    return out


def _scenario_steps(table: _Node) -> str | None:
    rows = _rows(table)
    if not rows:
        return None
    header = [_label(c.text()) for c in rows[0]]
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        if "given" in h or "precondizion" in h:
            cols.setdefault("given", i)
        elif "when" in h or "step" in h or "action" in h:
            cols.setdefault("when", i)
        elif "then" in h or "esito" in h:
            cols.setdefault("then", i)
    if not cols:
        return None
    lines: list[str] = []
    for row in rows[1:]:
        for key, first in (("given", "Dato"), ("when", "Quando"), ("then", "Allora")):
            idx = cols.get(key)
            if idx is not None and idx < len(row):
                lines += _with_keyword(_items(row[idx]), first)
    return "\n".join(lines) or None


def _kv(page: FeatPage, label: str, row: list[_Node]) -> None:
    value = row[-1]
    texts = [_label(c.text()) for c in row]
    if "test automatici" in texts:
        m = _NUMBER.search(row[-1].text())
        if m:
            page.estimate_days = float(m.group(1).replace(",", "."))
        return
    if label.startswith("id documento"):
        m = _FEAT_CODE.search(value.text())
        page.code = m.group(0) if m else (value.text() or None)
    elif label.startswith("nome documento"):
        page.doc_name = value.text() or None
    elif label == "ambito":
        page.area = value.text() or None
    elif label.startswith("stato del documento"):
        page.status_text = _status(value) or None
    elif label == "owner":
        page.owner_accounts = _accounts(value)
    elif label == "reviewer":
        page.reviewer_accounts = _accounts(value)
        page.reviewer_label = _team(value)
    elif label.startswith("link requisiti"):
        urls = [u for u in _hrefs(value) if "inserire" not in value.text().lower()]
        page.requirements_url = urls[0] if urls else None
    elif label.startswith("jira progettazione"):
        page.jira_design = _jira_key(value)
    elif label.startswith("jira implementazione"):
        page.jira_impl = _jira_key(value)
    elif label.startswith("epic / story") or label.startswith("epic/story"):
        page.product_key = _jira_key(value)
    elif label.startswith("feature file"):
        files = [u for u in _hrefs(value) if "/blob/" in u and u.endswith(".feature")]
        page.feature_file_url = files[0] if files else None


def parse_feat_page(storage: str) -> FeatPage:
    page = FeatPage()
    builder = _TreeBuilder()
    try:
        builder.feed(storage or "")
        builder.close()
    except Exception as exc:  # noqa: BLE001 - una pagina illeggibile non blocca la migrazione
        page.note = f"Pagina non interpretabile: {exc}"
        return page

    notes: list[str] = []
    after_details = False
    in_background = False
    current: tuple[int, str] | None = None  # scenario in attesa della sua tabella
    pending_text: list[str] = []

    def flush_unparsed() -> None:
        nonlocal current, pending_text
        if current is not None:
            notes.append(
                "\n".join([f"Scenario {current[0]:02d}: {current[1]}", *pending_text]).strip()
            )
        current = None
        pending_text = []

    for block in _blocks(builder.root):
        text = block.text()
        if block.tag in _HEADINGS:
            flush_unparsed()
            in_background = False
            m = _SCENARIO.match(text)
            if m:
                current = (int(m.group(1)), m.group(2))
            elif _label(text).startswith("contesto"):
                in_background = True
            elif _label(text) == "dettagli":
                after_details = True
            elif after_details and page.description is None and text:
                page.description = text
            continue
        if block.tag == "table":
            if current is not None:
                steps = _scenario_steps(block)
                if steps:
                    page.scenarios.append(PageScenario(current[0], current[1], steps))
                    current = None
                    pending_text = []
                continue
            for row in _rows(block):
                if len(row) >= 2:
                    _kv(page, _label(row[0].text()), row)
            continue
        if in_background and block.tag in ("ul", "ol"):
            items = [re.sub(r"^[-•*]\s*", "", li.text()) for li in block.find_all("li")]
            steps = _with_keyword([i for i in items if i], "Dato")
            page.background_steps = "\n".join(steps) or None
            continue
        if current is not None and text:
            pending_text.append(text)
        elif after_details and page.description is None and block.tag == "p" and text:
            page.description = text
    flush_unparsed()
    page.note = "\n\n".join(n for n in notes if n)
    if not re.fullmatch(_FEAT_CODE.pattern, page.code or ""):
        # alcune pagine (es. WISP) mettono un altro identificativo: il codice è nel nome
        m = _FEAT_CODE.search(page.doc_name or "")
        if m:
            page.code = m.group(0)
    return page
