"""On-demand HTMX dashboard for the memex memory store.

`memex viz` starts a localhost HTTP server that renders existing surfaces
(docs pages, session metadata, run log, index stats) as an interactive
dashboard. Read-only: the filesystem is the truth, this is a projection.

Zero new dependencies: stdlib http.server + vendored HTMX 2.0.4 +
hand-written CSS + server-rendered SVG charts. Stops when the process stops.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlparse

from memex.application.memory import Memex
from memex.domain.models import SessionSummary, WikiNode
from memex.infrastructure.web.components import DASHBOARD_CSS, PAGE_SHELL, escape, scope_controls
from memex.infrastructure.web.explorer import (
    MemoryPage,
    MemorySelection,
    direct_url,
    fragment_url,
    parse_page,
    render_pager,
)
from memex.infrastructure.web.markdown import render_markdown
from memex.infrastructure.web.sessions import SessionView, render_replay, render_session_groups

DEFAULT_PORT = 7171
SEARCH_SCOPES = {"best", "project", "global"}
MAX_CHART_TOKENS = 1_000_000_000_000

_HTMX_PATH = Path(__file__).parent / "assets" / "htmx.min.js"
_HTMX_BYTES: bytes = (
    _HTMX_PATH.read_bytes() if _HTMX_PATH.exists() else b"console.error('htmx not found');"
)

_ENRICHED_MARKER = re.compile(r"<!-- enriched -->\s*", re.DOTALL)


def _esc(text: object) -> str:
    """HTML-escape any value at the interpolation boundary."""
    return escape(text)


def _clean_snippet(snippet: str) -> str:
    """Escape content while preserving safe <mark> tags from FTS5."""
    escaped = html.escape(snippet)
    # Re-inject the mark tags that html.escape destroyed
    return escaped.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")


def _strip_enriched(body: str) -> str:
    """Remove the <!-- enriched --> marker and extract the summary below it."""
    match = re.search(r"<!-- enriched -->\s*\n*(.*?)(?:\n---|\Z)", body, re.DOTALL)
    if match:
        return match.group(1).strip()
    return _ENRICHED_MARKER.sub("", body).strip()


def _type_badge(node_type: str, status: str) -> str:
    if status != "active":
        return f'<span class="badge {status}">{_esc(status)}</span>'
    return f'<span class="badge {node_type}">{_esc(node_type)}</span>'


def _meta_line(node: WikiNode | SimpleNamespace) -> str:
    parts = [_esc(getattr(node, "type", ""))]
    if getattr(node, "tags", None):
        parts.append(" ".join(f"#{_esc(t)}" for t in node.tags[:4]))
    updated = str(getattr(node, "timestamp", "") or "")[:10]
    if updated:
        parts.append(updated)
    importance = getattr(node, "importance", None)
    if importance is not None:
        parts.append(f"imp {_esc(importance)}")
    return f'<span class="meta"><span>{'</span><span class="sep">·</span><span>'.join(parts)}</span></span>'


class VizHandler(BaseHTTPRequestHandler):
    memex: Memex | None = None

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)

        if route == "/htmx.js":
            self._bytes(_HTMX_BYTES, "application/javascript")
        elif route == "/style.css":
            self._text(DASHBOARD_CSS, "text/css")
        elif route in {
            "/",
            "/view/overview",
            "/view/memories",
            "/view/search",
            "/view/sessions",
            "/view/tokens",
        } or route.startswith(("/view/page/", "/view/session/")):
            self._text(self._shell(route, qs), "text/html")
        elif route == "/overview":
            self._text(self._frag_overview(), "text/html")
        elif route == "/pages":
            scope = qs.get("scope", ["project" if "project" in qs else "best"])[0]
            selection = MemorySelection(
                qs.get("type", [None])[0],
                scope,
                qs.get("project", [None])[0],
                parse_page(qs.get("page", [None])[0]),
            )
            self._text(self._frag_pages(selection), "text/html")
        elif route == "/search":
            self._text(
                self._frag_search(
                    qs.get("q", [""])[0],
                    qs.get("scope", ["project" if qs.get("project", [""])[0] else "best"])[0],
                    qs.get("project", [None])[0],
                ),
                "text/html",
            )
        elif route == "/sessions":
            self._text(self._frag_sessions(), "text/html")
        elif route == "/tokens":
            self._text(self._frag_tokens(), "text/html")
        elif route.startswith("/session/"):
            session_id = route.removeprefix("/session/")
            self._text(self._frag_session_detail(session_id), "text/html")
        elif route.startswith("/page/"):
            slug = route.removeprefix("/page/")
            self._text(self._frag_page_detail(slug, qs.get("project", [None])[0]), "text/html")
        elif route == "/health":
            self._text(self._frag_health(), "text/html")
        else:
            self._text('<div class="empty">Not found</div>', "text/html", 200)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _bytes(self, payload: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            # Browsers can cancel a fragment swap while the response is in flight.
            return

    def _text(self, body: str, ctype: str, code: int = 200) -> None:
        self._bytes(body.encode(), ctype, code)

    def _m(self) -> Memex:
        if self.memex is None:
            raise RuntimeError("viz handler not initialized")
        return self.memex

    def _projects(self) -> dict[str, str]:
        """Project ids mapped to their safe display labels.

        Served from the disposable index, not a full-store parse: the
        dashboard stays fast on large stores (a filesystem scan of tens of
        thousands of pages takes the better part of a minute; this query is
        milliseconds). MIN(slug) keeps the label choice deterministic the
        way the old slug-sorted scan was.
        """
        rows = (
            self._m()
            .index_manager.connection.execute(
                "SELECT project_id, project_label, MIN(slug) AS first_slug"
                " FROM wiki_index WHERE scope = 'project' AND project_id != ''"
                " GROUP BY project_id, project_label"
            )
            .fetchall()
        )
        projects: dict[str, str] = {}
        first_slug: dict[str, str] = {}
        for row in rows:
            project_id = str(row["project_id"])
            slug = str(row["first_slug"])
            if project_id not in first_slug or slug < first_slug[project_id]:
                first_slug[project_id] = slug
                projects[project_id] = str(row["project_label"] or "Project")
        return projects

    def _paginate_index(self, selection: MemorySelection) -> MemoryPage:
        """Index-backed pagination: identical ordering to `paginate`, no scan.

        Mirrors explorer.paginate's key (timestamp, slug, project_id) and its
        scope semantics, so the Memories page renders the same order it
        always did — in milliseconds instead of a full-store parse.
        """
        from memex.infrastructure.web.explorer import MEMORY_PAGE_SIZE

        clauses: list[str] = []
        params: list[object] = []
        if selection.node_type:
            clauses.append("node_type = ?")
            params.append(selection.node_type)
        if selection.scope == "global":
            clauses.append("scope = 'global'")
        elif selection.scope == "project":
            clauses.append("project_id = ?")
            params.append(selection.project_id or "")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        count_row = self._index_rows(
            "SELECT COUNT(*) AS n FROM wiki_index"  # noqa: S608 - static clauses, bound values
            + where,
            tuple(params),
        )[0]
        total = int(count_row["n"])
        pages = max(1, (total + MEMORY_PAGE_SIZE - 1) // MEMORY_PAGE_SIZE)
        page = min(selection.page, pages)
        rows = self._index_rows(
            "SELECT node_type, status, slug, title, body, description, scope,"  # noqa: S608 - static clauses, bound values
            " project_id, project_label, tags, timestamp, importance, transcript_ref,"
            " source, harness FROM wiki_index"
            + where
            + " ORDER BY timestamp DESC, slug DESC, project_id DESC LIMIT ? OFFSET ?",
            (*params, MEMORY_PAGE_SIZE, (page - 1) * MEMORY_PAGE_SIZE),
        )
        return MemoryPage([self._row_node(row) for row in rows], page, pages, total)

    def _row_node(self, row: sqlite3.Row) -> SimpleNamespace:
        """Attribute-compatible page view over one index row (no parsing)."""
        return SimpleNamespace(
            type=str(row["node_type"]),
            status=str(row["status"]),
            slug=str(row["slug"]),
            title=str(row["title"]),
            body=str(row["body"]),
            description=str(row["description"]),
            scope=str(row["scope"]),
            project_id=str(row["project_id"]) or None,
            project_label=str(row["project_label"]) if row["project_label"] else None,
            tags=json.loads(str(row["tags"])),
            timestamp=str(row["timestamp"]),
            importance=float(row["importance"]),
            transcript_ref=row["transcript_ref"],
            source=row["source"],
            harness=row["harness"],
        )

    def _index_rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return self._m().index_manager.connection.execute(sql, params).fetchall()

    def _sessions(self) -> tuple[list[SessionSummary], int] | None:
        try:
            return self._m().transcript_hook.list_sessions_report()
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _shell(self, route: str, qs: dict[str, list[str]]) -> str:
        known = {
            "/": "/overview",
            "/view/overview": "/overview",
            "/view/memories": "/pages",
            "/view/search": "/search",
            "/view/sessions": "/sessions",
            "/view/tokens": "/tokens",
        }
        fragment = known.get(route)
        if fragment is None:
            fragment = route.removeprefix("/view")
        if fragment == "/pages":
            params = {
                key: values[0]
                for key, values in qs.items()
                if key in {"type", "scope", "project", "page"} and values
            }
            if params:
                fragment += "?" + urlencode(params)
        elif fragment.startswith("/page/") and qs.get("project"):
            fragment += "?" + urlencode({"project": qs["project"][0]})
        if fragment == "/overview":
            initial = self._frag_overview()
        elif fragment.startswith("/pages"):
            scope = qs.get("scope", ["project" if "project" in qs else "best"])[0]
            initial = self._frag_pages(
                MemorySelection(
                    qs.get("type", [None])[0],
                    scope,
                    qs.get("project", [None])[0],
                    parse_page(qs.get("page", [None])[0]),
                )
            )
        elif fragment.startswith("/search"):
            initial = '<div id="search-results">' + self._frag_search(
                qs.get("q", [""])[0],
                qs.get("scope", ["project" if qs.get("project", [""])[0] else "best"])[0],
                qs.get("project", [None])[0],
            )
            initial += "</div>"
        elif fragment == "/sessions":
            initial = self._frag_sessions()
        elif fragment == "/tokens":
            initial = self._frag_tokens()
        elif fragment.startswith("/session/"):
            initial = self._frag_session_detail(route.removeprefix("/view/session/"))
        else:
            initial = self._frag_page_detail(
                route.removeprefix("/view/page/"), qs.get("project", [None])[0]
            )
        shell = PAGE_SHELL.replace("@@INITIAL_CONTENT@@", initial)
        active_href = "/" if route == "/view/overview" else route
        return shell.replace(
            f'href="{active_href}"', f'href="{active_href}" aria-current="page"', 1
        )

    def _index_stats(self) -> tuple[int, int, int]:
        """(total, pending, stale-ish) counts for the dashboard, cheaply.

        Below a page threshold the stale count is exact (the facade's
        per-page freshness check); above it the store is too large for that
        on a dashboard tick, so the index's own file-count bookkeeping stands
        in and the UI still points at `memex verify` for the real gate.
        """
        conn = self._m().index_manager.connection
        total_row = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending"
            " FROM wiki_index"
        ).fetchone()
        total, pending = int(total_row["n"]), int(total_row["pending"] or 0)
        threshold = 2_000
        if total <= threshold:
            return total, pending, int(str(self._m().status().get("index_stale_rows", 0) or 0))
        recorded = self._m().index_manager.get_meta("wiki_file_count")
        stale = 0 if recorded is not None and int(recorded) == total else 1
        return total, pending, stale

    def _zero_yield_streak(self) -> int:
        from memex.infrastructure.run_log import read_runs, zero_yield_streak

        return zero_yield_streak(read_runs(self._m().data_dir))

    def _frag_overview(self) -> str:
        total, pending, stale = self._index_stats()

        health = '<span class="dot ok"></span>' if stale == 0 else '<span class="dot warn"></span>'

        # Recent non-episode memories (the interesting ones) — served from
        # the index so a 78k-page store renders as fast as a 7-page one.
        rows = self._index_rows(
            "SELECT node_type, status, slug, title, body, description, scope,"
            " project_id, project_label, tags, timestamp, importance, transcript_ref,"
            " source, harness"
            " FROM wiki_index WHERE node_type != 'episode'"
            " ORDER BY timestamp DESC, slug DESC LIMIT 8"
        )
        nodes = [self._row_node(row) for row in rows]
        projects = self._projects()
        cards = "".join(self._card(n) for n in nodes)
        memories = (
            f'<div class="cards">{cards}</div>'
            if cards
            else '<div class="empty">No distilled memories yet — run <code>memex consolidate</code></div>'
        )
        token_chart = self._frag_tokens()
        builtin_labels = {
            "entity": "Entities",
            "preference": "Preferences",
            "procedure": "Procedures",
            "summary": "Summaries",
            "episode": "Episodes",
        }
        type_counts = "".join(
            f'<div class="kpi"><div class="value">{count}</div>'
            f'<div class="label">{builtin_labels.get(kind, kind.replace("-", " ").title())}</div></div>'
            for kind, count in self._type_counts()[:5]
        )
        options = "".join(
            f'<option value="{_esc(identifier)}">{_esc(label)}</option>'
            for identifier, label in sorted(projects.items(), key=lambda item: item[1].casefold())
        )
        session_report = self._sessions()
        sessions, unreadable = session_report if session_report is not None else ([], 0)
        recent_sessions = sorted(
            sessions, key=lambda session: session.started_at or "", reverse=True
        )[:3]
        session_cards = (
            "".join(
                f'<div class="card"><h3><a href="/view/session/{_esc(session.session_id)}" '
                f'hx-get="/session/{_esc(session.session_id)}" hx-target="#panel-body">'
                f"{_esc(session.session_id[:18])}</a></h3>"
                f'<span class="meta">{_esc(str(session.started_at or "Unknown date")[:16])} · '
                f"{_esc(session.turn_count)} turns</span></div>"
                for session in recent_sessions
                if self._m().transcript_hook.is_valid_session_id(session.session_id)
            )
            or '<div class="empty">No captured sessions yet</div>'
        )
        if session_report is None:
            session_cards = '<div class="error">Session metadata unavailable</div>'
        elif unreadable:
            session_cards = (
                f'<div class="error">{unreadable} session metadata record(s) unreadable</div>'
                + session_cards
            )

        return f"""
<div class="statusbar" id="health" hx-get="/health" hx-trigger="every 5s" hx-swap="innerHTML">
  {health}
  <span class="metric"><b>{total}</b><span>pages</span></span>
  <span class="metric"><b>{pending}</b><span>pending</span></span>
  <span class="metric"><b>{stale}</b><span>stale rows</span></span>
  <span class="spacer"></span>
  <span class="subtle">auto-refresh 5s</span>
</div>
<div class="kpis">
  <div class="kpi"><div class="value">{total}</div><div class="label">Memory pages</div></div>
  <div class="kpi"><div class="value">{len(projects)}</div><div class="label">Projects</div></div>
  <div class="kpi"><div class="value">{pending}</div><div class="label">Pending approval</div></div>
  <div class="kpi"><div class="value">{stale}</div><div class="label">Stale index rows</div></div>
  <div class="kpi"><div class="value">{self._zero_yield_streak()}</div><div class="label" title="Consecutive consolidations that produced zero new nodes">Zero-yield streak</div></div>
</div>
<form class="search-bar" action="/view/search" method="get" hx-get="/search"
  hx-trigger="input changed delay:300ms, change, submit" hx-target="#search-results">
  <input type="search" placeholder="Search memories…" aria-label="Search memories"
    autocomplete="off" name="q">
  <select name="scope" aria-label="Search scope"><option value="best">Best match · all</option>
    <option value="global">Global only</option><option value="project">Project</option></select>
  <select name="project" aria-label="Search project"><option value="">Choose project</option>{options}</select>
  <button type="submit">Search</button>
</form>
<div id="search-results"></div>
<div class="section">Memory types</div><div class="kpis">{type_counts}</div>
<div class="section">Recent memories</div>
{memories}
<div class="section">Recent sessions <a href="/view/sessions">View all</a></div>
<div class="cards">{session_cards}</div>
<div class="section">Token consumption</div>
{token_chart}
"""

    def _frag_health(self) -> str:
        total, pending, stale = self._index_stats()
        from memex.infrastructure.run_log import read_runs, zero_yield_streak

        streak = zero_yield_streak(read_runs(self._m().data_dir))
        dot = '<span class="dot ok"></span>' if stale == 0 else '<span class="dot warn"></span>'
        link = " · Run memex verify in the CLI" if stale > 0 else ""
        return (
            f'{dot}<span class="metric"><b>{total}</b><span>pages</span></span>'
            f'<span class="metric"><b>{pending}</b><span>pending</span></span>'
            f'<span class="metric"><b>{stale}</b><span>stale</span></span>'
            f'<span class="metric"><b>{streak}</b><span>zero-yield</span></span>'
            f'<span class="spacer"></span><span class="subtle">5s</span>{link}'
        )

    def _card(self, node: WikiNode | SimpleNamespace) -> str:
        body_raw = _strip_enriched(str(getattr(node, "body", "")))
        badge = _type_badge(getattr(node, "type", ""), getattr(node, "status", "active"))
        slug = getattr(node, "slug", "")
        page_url = "/page/" + quote(slug, safe="")
        if node.scope == "project" and node.project_id:
            page_url += "?" + urlencode({"project": node.project_id})
        safe_url = _esc(page_url)
        direct_url = _esc("/view" + page_url)
        title_html = (
            f'<a href="{direct_url}" hx-get="{safe_url}" '
            f'hx-target="#panel-body" hx-swap="innerHTML">'
            f"{_esc(getattr(node, 'title', ''))}</a>"
            if slug
            else _esc(getattr(node, "title", ""))
        )
        return (
            f'<div class="card"><h3>{title_html} {badge}</h3>'
            f'<div class="body">{_esc(body_raw[:200])}</div>'
            f'{_meta_line(node)}<span class="meta">'
            f"{_esc(node.project_label or 'Project') if node.scope == 'project' else 'Global'}"
            "</span></div>"
        )

    def _type_counts(self) -> list[tuple[str, int]]:
        """Every shelf present in the index with its page count, biggest first.

        Dynamic concept types (a project's `book`, `domain`, …) appear here
        exactly like the seeded five — the dashboard never hardcodes names.
        """
        rows = self._index_rows(
            "SELECT node_type AS kind, COUNT(*) AS n FROM wiki_index"
            " GROUP BY node_type ORDER BY n DESC, kind ASC"
        )
        return [(str(row["kind"]), int(row["n"])) for row in rows]

    def _frag_pages(self, selection: MemorySelection) -> str:
        known = dict(self._type_counts())
        if selection.node_type and selection.node_type not in known:
            return '<div class="empty">Unknown memory type</div>'
        projects = self._projects()
        if selection.scope not in SEARCH_SCOPES:
            return '<div class="empty">Unknown memory scope</div>'
        if selection.scope == "project" and selection.project_id not in projects:
            return '<div class="empty">Unknown project scope</div>'
        result = self._paginate_index(selection)
        selected = selection.project_id if selection.scope == "project" else selection.scope
        route = "/pages"
        if selection.node_type:
            route += "?" + urlencode({"type": selection.node_type})
        scopes = scope_controls(projects, selected, route)
        builtin_labels = {
            "entity": "Entities",
            "preference": "Preferences",
            "procedure": "Procedures",
            "summary": "Summaries",
            "episode": "Episodes",
        }
        filters = []
        shown: list[tuple[str | None, str, int]] = [(None, "All types", sum(known.values()))]
        shown += [
            (node_type, builtin_labels.get(node_type, node_type.replace("-", " ").title()), count)
            for node_type, count in known.items()
        ][:9]
        for node_type, label, _count in shown:
            target = MemorySelection(node_type, selection.scope, selection.project_id)
            fragment = _esc(fragment_url(target, 1))
            direct = _esc(direct_url(target, 1))
            current = ' aria-current="true"' if selection.node_type == node_type else ""
            filters.append(
                f'<a href="{direct}" hx-get="{fragment}" hx-target="#main" '
                f'hx-push-url="{direct}"{current}>{label}</a>'
            )
        filter_html = '<div class="filters" aria-label="Memory type">' + "".join(filters) + "</div>"
        page_html = render_pager(result, selection)
        cards = "".join(self._card(node) for node in result.nodes)
        content = (
            f'<div class="cards">{cards}</div>'
            if cards
            else '<div class="empty">No memories match this selection</div>'
        )
        return (
            '<h1 class="page-heading">Memories</h1>'
            + scopes
            + filter_html
            + page_html
            + content
            + page_html
        )

    def _frag_search(self, q: str, scope: str = "best", project_id: str | None = None) -> str:
        if not q.strip():
            return '<div class="empty">Enter a search term to find memory</div>'
        if scope not in SEARCH_SCOPES:
            return '<div class="empty">Unknown search scope</div>'
        projects = self._projects()
        if scope == "project" and project_id not in projects:
            return '<div class="empty">Choose a project to search</div>'
        if project_id and project_id not in projects:
            return '<div class="empty">Unknown project scope</div>'
        try:
            if scope in {"project", "best"} and project_id:
                result = self._m().retriever.retrieve_without_access(
                    q,
                    top_k=20,
                    scope="project",
                    project_id=project_id,
                )
                if scope == "best" and not result.hits:
                    result = self._m().retriever.retrieve_without_access(q, top_k=20)
            else:
                result = self._m().retriever.retrieve_without_access(q, top_k=20)
        except ValueError:
            return '<div class="empty">Enter a searchable word</div>'
        except Exception:
            return (
                '<div class="error">Search unavailable. Run memex rebuild-index from the CLI.</div>'
            )
        hits = result.hits
        if not hits:
            return f'<div class="empty">No memories match “{_esc(q)}”</div>'
        cards = []
        for hit in hits[:20]:
            snippet = _clean_snippet(hit.snippet[:250] if hit.snippet else "")
            page_url = "/page/" + quote(hit.slug, safe="")
            if hit.scope == "project" and hit.project_id:
                page_url += "?" + urlencode({"project": hit.project_id})
            safe_url = _esc(page_url)
            direct_url = _esc("/view" + page_url)
            label = _esc(hit.project_label or "Project") if hit.scope == "project" else "Global"
            cards.append(
                f'<div class="card"><h3><a href="{direct_url}" hx-get="{safe_url}" '
                f'hx-target="#panel-body">{_esc(hit.title)}</a></h3>'
                f'<div class="body">{snippet}</div>'
                f'<span class="meta">{label} · {_esc(hit.node_type)}</span></div>'
            )
        selected = project_id if scope == "project" else scope
        controls = scope_controls(projects, selected, "/search?" + urlencode({"q": q}))
        return controls + '<div class="cards">' + "".join(cards) + "</div>"

    def _frag_sessions(self) -> str:
        session_report = self._sessions()
        if session_report is None:
            return '<div class="error">Session metadata unavailable</div>'
        sessions, unreadable = session_report
        if not sessions:
            return (
                f'<div class="error">{unreadable} session metadata record(s) unreadable</div>'
                if unreadable
                else '<div class="empty">No sessions captured yet</div>'
            )
        # Episode lookups from the index, not a full-store parse: the
        # session id is the transcript file stem (transcripts/<date>/<id>.jsonl);
        # only pages lacking a transcript_ref fall back to one-file parses.
        episodes: dict[str, SimpleNamespace] = {}
        for row in self._index_rows(
            "SELECT file_path, scope, project_label, transcript_ref"
            " FROM wiki_index WHERE node_type = 'episode'"
        ):
            ref = row["transcript_ref"]
            session_id = Path(str(ref)).stem if ref else None
            if session_id is None:
                page = self._m().wiki_store.read_path(Path(str(row["file_path"])))
                session_id = page.session_id
            if session_id:
                episodes[session_id] = SimpleNamespace(
                    session_id=session_id,
                    scope=str(row["scope"]),
                    project_label=str(row["project_label"]) if row["project_label"] else None,
                    harness="",
                )
        views = []
        for session in sessions:
            if not self._m().transcript_hook.is_valid_session_id(session.session_id):
                continue
            meta = self._session_metadata(session.session_id)
            episode = episodes.get(session.session_id)
            label = episode.project_label if episode and episode.scope == "project" else None
            metadata_label = meta.get("project_label")
            if (
                not label
                and isinstance(metadata_label, str)
                and 0 < len(metadata_label) <= 80
                and not any(character in metadata_label for character in "/\\:@")
            ):
                label = metadata_label
            harness = str(
                meta.get("harness") or (episode.harness if episode else "") or "Unknown harness"
            )
            day = Path(session.file_path).parent.name
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = str(session.started_at or "Unknown date")[:10]
            views.append(
                SessionView(
                    session.session_id,
                    day,
                    str(session.started_at or ""),
                    session.turn_count,
                    session.episode_slug,
                    label or "Global or unscoped",
                    harness,
                )
            )
        warning = (
            f'<div class="error">{unreadable} session metadata record(s) unreadable</div>'
            if unreadable
            else ""
        )
        return warning + (
            render_session_groups(views) if views else '<div class="empty">No valid sessions</div>'
        )

    def _session_metadata(self, session_id: str) -> dict[str, object]:
        """Read optional session display metadata without exposing its path."""
        path = self._m().transcript_hook.get_transcript_path(session_id).with_suffix(".meta.json")
        if not self._confined_transcript(path):
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _confined_transcript(self, path: Path) -> bool:
        root = self._m().transcript_hook.transcripts_dir.resolve()
        return path.resolve().is_relative_to(root)

    def _frag_session_detail(self, session_id: str) -> str:
        if not self._m().transcript_hook.is_valid_session_id(session_id):
            return '<div class="empty">Session not found</div>'
        jsonl_path = self._m().transcript_hook.get_transcript_path(session_id)
        if not self._confined_transcript(jsonl_path) or not jsonl_path.exists():
            return '<div class="empty">Session not found</div>'
        turns: list[dict[str, object]] = []
        try:
            for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("role") in {"user", "agent", "tool"}:
                    turns.append(entry)
        except OSError:
            return '<div class="empty">Cannot read transcript</div>'
        return render_replay(turns, self._session_metadata(session_id))

    def _frag_page_detail(self, slug: str, project_id: str | None = None) -> str:
        """Rendered Markdown view of a single memory page."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return '<div class="empty">Page not found</div>'
        if project_id is not None and project_id not in self._projects():
            return '<div class="empty">Page not found</div>'
        row = self._index_rows(
            "SELECT node_type, status, slug, title, body, description, scope,"
            " project_id, project_label, tags, timestamp, importance, transcript_ref,"
            " source, harness"
            " FROM wiki_index WHERE slug = ? AND project_id = ?"
            " ORDER BY scope LIMIT 1",
            (slug, project_id or ""),
        )
        node = self._row_node(row[0]) if row else None
        if node is None:
            return '<div class="empty">Page not found</div>'

        rendered = render_markdown(node.body)
        badge = _type_badge(node.type, node.status)

        # Provenance
        provenance = ""
        if node.transcript_ref:
            transcript_status = (
                "Transcript retired"
                if node.transcript_ref.startswith("retired:")
                else "Transcript reference"
            )
            provenance = (
                f'<div class="section">Provenance</div>'
                f'<p class="subtle">{transcript_status}: <code>{_esc(node.transcript_ref)}</code></p>'
            )

        # Meta line
        meta_parts = [_esc(node.type), f"importance {_esc(node.importance)}"]
        if node.tags:
            meta_parts.append(" ".join(f"#{_esc(t)}" for t in node.tags))
        if node.timestamp:
            meta_parts.append(f"updated {_esc(str(node.timestamp)[:10])}")
        if node.source:
            meta_parts.append(f"source {_esc(node.source)}")
        if node.harness:
            meta_parts.append(f"harness {_esc(node.harness)}")

        return (
            f'<div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem">{badge}'
            f'<span class="subtle">{" · ".join(meta_parts)}</span></div>'
            f"{rendered}"
            f"{provenance}"
        )

    def _frag_tokens(self) -> str:
        """Token consumption chart with proper axes, labels, and gridlines."""
        metas = []
        for meta_path in sorted(self._m().transcript_hook.transcripts_dir.rglob("*.meta.json")):
            if not self._confined_transcript(meta_path):
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    metas.append(meta)
            except (OSError, json.JSONDecodeError):
                continue
        if not metas:
            return '<div class="empty">No token data — captures write usage to meta.json</div>'
        metas.sort(key=lambda m: str(m.get("started_at") or ""))

        # Extract valid token data
        data: list[tuple[str, int, str]] = []  # (label, tokens, started_at)
        for m in metas:
            try:
                usage = m.get("token_usage", {})
                if not isinstance(usage, dict):
                    continue
                raw = usage.get("total_tokens", 0)
                if (
                    isinstance(raw, bool)
                    or not isinstance(raw, (int, float))
                    or not 0 <= raw <= MAX_CHART_TOKENS
                ):
                    continue
                sid = str(m.get("session_id", "?"))
                started = str(m.get("started_at", "") or "")
                label = started[:10] if len(started) >= 10 else sid[:10]
                data.append((label, int(raw), started))
            except (AttributeError, TypeError, ValueError):
                continue

        if not data:
            return '<div class="empty">No valid token data found</div>'

        # Aggregate by day if too many bars
        if len(data) > 40:
            daily: dict[str, int] = {}
            for label, tokens, _ in data:
                daily[label] = daily.get(label, 0) + tokens
            data = [(d, t, d) for d, t in sorted(daily.items())]

        # Chart geometry
        n = len(data)
        max_tokens = max(t for _, t, _ in data) or 1
        chart_w = 800
        chart_h = 260
        margin = {"top": 30, "right": 20, "bottom": 50, "left": 80}
        plot_w = chart_w - margin["left"] - margin["right"]
        plot_h = chart_h - margin["top"] - margin["bottom"]
        bar_w = max(plot_w // max(n, 1) - 2, 3)
        bar_gap = max(plot_w // max(n, 1) - bar_w, 1)

        def _fmt_tokens(v: int) -> str:
            if v >= 1_000_000:
                return f"{v / 1_000_000:.1f}M"
            if v >= 1_000:
                return f"{v / 1_000:.0f}k"
            return str(v)

        def _y_pos(tokens: int) -> float:
            return margin["top"] + plot_h - (tokens / max_tokens) * plot_h

        # Bars
        bars = []
        for i, (label, tokens, _started) in enumerate(data):
            x = margin["left"] + i * (bar_w + bar_gap)
            h = (tokens / max_tokens) * plot_h
            y = margin["top"] + plot_h - h
            if tokens == 0:
                # Zero-token: 1px baseline tick
                bars.append(
                    f'<rect class="bar zero" x="{x:.1f}" y="{margin["top"] + plot_h - 1:.1f}" '
                    f'width="{bar_w}" height="1" fill="var(--text-dim)"><title>{_esc(label)}: 0 tokens</title></rect>'
                )
            else:
                tooltip = f"{_esc(label)}: {tokens:,} tokens ({_fmt_tokens(tokens)})"
                bars.append(
                    f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" rx="2">'
                    f"<title>{tooltip}</title></rect>"
                )

        # Y-axis gridlines + labels (5 ticks: 0, 25%, 50%, 75%, max)
        gridlines = []
        for i in range(5):
            value = int(max_tokens * i / 4)
            y = _y_pos(value)
            gridlines.append(
                f'<line class="grid-line" x1="{margin["left"]}" y1="{y:.1f}" '
                f'x2="{margin["left"] + plot_w}" y2="{y:.1f}"/>'
            )
            gridlines.append(
                f'<text class="axis-label" x="{margin["left"] - 10}" y="{y + 4:.1f}" '
                f'text-anchor="end">{_fmt_tokens(value)}</text>'
            )

        # Y-axis line
        gridlines.append(
            f'<line x1="{margin["left"]}" y1="{margin["top"]}" x2="{margin["left"]}" '
            f'y2="{margin["top"] + plot_h}" stroke="var(--border)" stroke-width="1"/>'
        )

        # X-axis labels (show ~5-8 labels max)
        x_labels = []
        step = 1 if n <= 8 else max(n // 6, 1)
        for i in range(0, n, step):
            x = margin["left"] + i * (bar_w + bar_gap) + bar_w // 2
            label = data[i][0]
            # Rotate if labels are long
            if len(label) > 6:
                x_labels.append(
                    f'<text class="axis-label" x="{x:.0f}" y="{margin["top"] + plot_h + 18}" '
                    f'text-anchor="end" transform="rotate(-35 {x:.0f} {margin["top"] + plot_h + 18})">'
                    f"{_esc(label)}</text>"
                )
            else:
                x_labels.append(
                    f'<text class="axis-label" x="{x:.0f}" y="{margin["top"] + plot_h + 18}" '
                    f'text-anchor="middle">{_esc(label)}</text>'
                )

        # X-axis line
        gridlines.append(
            f'<line x1="{margin["left"]}" y1="{margin["top"] + plot_h}" '
            f'x2="{margin["left"] + plot_w}" y2="{margin["top"] + plot_h}" '
            f'stroke="var(--border)" stroke-width="1"/>'
        )

        # Axis titles
        axis_titles = (
            f'<text class="axis-title" x="{margin["left"] + plot_w // 2}" y="{chart_h - 5}" '
            f'text-anchor="middle">{"Date" if len(data) > 8 else "Session"}</text>'
            f'<text class="axis-title" x="{-chart_h // 2 + 15}" y="14" text-anchor="middle" '
            f'transform="rotate(-90)">Tokens</text>'
        )

        # Chart title
        title = (
            f'<text class="chart-title" x="{chart_w // 2}" y="18" text-anchor="middle">'
            f"Token Consumption ({_fmt_tokens(sum(t for _, t, _ in data))} total)</text>"
        )

        svg = (
            f'<div class="chart-box">'
            f'<svg viewBox="0 0 {chart_w} {chart_h}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" aria-label="Token consumption per session, {_fmt_tokens(max_tokens)} max">'
            f"<defs><style>"
            f".bar{{fill:var(--accent);opacity:.75;rx:2;transition:opacity .15s}} "
            f".bar:hover{{opacity:1}} "
            f".zero{{fill:var(--text-dim);opacity:.4}} "
            f".grid-line{{stroke:var(--border);stroke-width:.5;opacity:.4}} "
            f".axis-label{{fill:var(--text-dim);font-size:10px;font-family:var(--mono)}} "
            f".axis-title{{fill:var(--text-muted);font-size:11px;font-weight:500}} "
            f".chart-title{{fill:var(--text);font-size:12px;font-weight:600}}"
            f"</style></defs>"
            f"{title}"
            f"{''.join(gridlines)}"
            f"{''.join(x_labels)}"
            f"{''.join(axis_titles)}"
            f"{''.join(bars)}"
            f"</svg></div>"
        )
        return svg


def serve(data_dir: Path | None = None, port: int = DEFAULT_PORT) -> None:
    """Start the viz server. Blocks until interrupted."""
    import webbrowser

    from memex.infrastructure.config import ConfigLoader

    config = ConfigLoader().load(data_dir=data_dir)

    class BoundVizHandler(VizHandler):
        memex = Memex(config)

    server = ThreadingHTTPServer(("127.0.0.1", port), BoundVizHandler)
    url = f"http://localhost:{port}"
    print(f"memex viz → {url} (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if BoundVizHandler.memex is not None:
            BoundVizHandler.memex.close()
        server.server_close()
