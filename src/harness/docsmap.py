"""Docs map: which doc pages the agent used, which version they came from, what happened next.

Sources (domains, version scheme, sitemap) come from the tool pack. The outcome of a visit
is the result of the next tool command after it (e.g. the next ``dagger`` invocation).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from harness.events import Event, EventKind
from harness.models import DocVisit
from harness.pack import DocsSource

URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")
LOCAL_DOC_MARKERS = ("/.claude/skills/", "/.codex/skills/", "/.claude/CLAUDE.md", "/.codex/AGENTS.md")


def classify(url: str, sources: list[DocsSource]) -> tuple[str, str | None]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    for src in sources:
        if not any(host == d or host.endswith("." + d) for d in src.domains):
            continue
        hostpath = host + parsed.path
        if src.path_prefixes and not any(hostpath.startswith(p) for p in src.path_prefixes):
            continue
        for rule in src.versions:
            m = re.search(rule.pattern, url)
            if m:
                return src.name, m.expand(rule.version)
        return src.name, src.default_version
    return "other", None


def _urls(ev: Event) -> Iterable[str]:
    if ev.url:
        yield ev.url
    elif ev.kind == EventKind.shell and ev.command:
        # A heredoc body is file content being written (e.g. a CI step that curls), not a fetch.
        cmd = ev.command.split("<<", 1)[0]
        if re.search(r"\b(curl|wget|http)\b", cmd):
            yield from URL_RE.findall(cmd)
    elif ev.kind == EventKind.mcp_call and ev.input:
        for v in ev.input.values():
            if isinstance(v, str):
                yield from URL_RE.findall(v)


def build(events: list[Event], sources: list[DocsSource], outcome_command: str | None) -> list[DocVisit]:
    visits: list[DocVisit] = []
    for i, ev in enumerate(events):
        found: list[DocVisit] = []
        for url in _urls(ev):
            url = url.rstrip(".,;\\")
            name, version = classify(url, sources)
            found.append(DocVisit(event=ev.id, url=url, source=name, doc_version=version, kind="fetch"))
        if ev.kind == EventKind.web_search and not ev.url and (ev.query or "").strip():
            found.append(DocVisit(event=ev.id, url=f"search:{ev.query}", source="search", kind="search"))
        if ev.kind == EventKind.file_read:
            for p in ev.paths:
                if any(m in p for m in LOCAL_DOC_MARKERS):
                    found.append(DocVisit(event=ev.id, url=f"file:{p}", source="local", kind="local"))
        if not found:
            continue
        outcome, outcome_ev = _outcome(events[i + 1 :], outcome_command)
        for v in found:
            v.outcome, v.outcome_event = outcome, outcome_ev
        visits.extend(found)
    return visits


def _outcome(after: list[Event], command: str | None) -> tuple[str, str | None]:
    if not command:
        return "none", None
    pat = re.compile(rf"(^|[\s;&|(]){re.escape(command)}\b")
    for ev in after:
        if ev.kind == EventKind.shell and ev.command and pat.search(ev.command):
            ok = ev.exit_code == 0 if ev.exit_code is not None else not ev.is_error
            return ("success" if ok else "error"), ev.id
    return "none", None


def fetch_sitemap(url: str, limit: int = 20_000) -> list[str]:
    """All page URLs in a sitemap (following one level of sitemap indexes)."""
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if root.tag.endswith("sitemapindex"):
        pages: list[str] = []
        for loc in root.findall("s:sitemap/s:loc", ns):
            pages.extend(fetch_sitemap(loc.text.strip(), limit))
            if len(pages) >= limit:
                break
        return pages[:limit]
    return [loc.text.strip() for loc in root.findall("s:url/s:loc", ns)][:limit]


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}"


def sitemap_cache(cache_dir: Path, source: DocsSource) -> Path:
    return cache_dir / "sitemaps" / f"{source.name}.txt"
