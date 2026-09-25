"""Read-only selection and pagination for dashboard memory cards."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlencode

from memex.domain.models import WikiNode
from memex.infrastructure.web.components import escape

MEMORY_PAGE_SIZE = 20


@dataclass(frozen=True)
class MemorySelection:
    node_type: str | None = None
    scope: str = "best"
    project_id: str | None = None
    page: int = 1

    def parameters(self, page: int) -> dict[str, str]:
        params = {"page": str(page), "scope": self.scope}
        if self.node_type:
            params["type"] = self.node_type
        if self.project_id and self.scope == "project":
            params["project"] = self.project_id
        return params


@dataclass(frozen=True)
class MemoryPage:
    # WikiNode | light index-backed views (web.server._row_node)
    nodes: list[WikiNode | SimpleNamespace]
    page: int
    pages: int
    total: int


def parse_page(value: str | None) -> int:
    """Treat invalid and nonpositive query values as the first page."""
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def paginate(nodes: Sequence[WikiNode | SimpleNamespace], selection: MemorySelection) -> MemoryPage:
    """Filter before counting; use a deterministic tie-break for equal timestamps."""
    if selection.scope == "global":
        nodes = [node for node in nodes if node.scope == "global"]
    elif selection.scope == "project":
        nodes = [node for node in nodes if node.project_id == selection.project_id]
    ordered = sorted(
        nodes,
        key=lambda node: (str(node.timestamp or ""), node.slug, node.project_id or ""),
        reverse=True,
    )
    total = len(ordered)
    pages = max(1, (total + MEMORY_PAGE_SIZE - 1) // MEMORY_PAGE_SIZE)
    page = min(selection.page, pages)
    start = (page - 1) * MEMORY_PAGE_SIZE
    return MemoryPage(ordered[start : start + MEMORY_PAGE_SIZE], page, pages, total)


def fragment_url(selection: MemorySelection, page: int) -> str:
    return "/pages?" + urlencode(selection.parameters(page))


def direct_url(selection: MemorySelection, page: int) -> str:
    return "/view/memories?" + urlencode(selection.parameters(page))


def render_pager(result: MemoryPage, selection: MemorySelection) -> str:
    """Previous/next links work without HTMX and preserve every selection."""

    def link(target: int, label: str) -> str:
        fragment = escape(fragment_url(selection, target))
        direct = escape(direct_url(selection, target))
        style = ' class="next"' if label == "Next" else ""
        return (
            f'<a{style} href="{direct}" hx-get="{fragment}" hx-target="#main" '
            f'hx-push-url="{direct}">{label}</a>'
        )

    previous = (
        link(result.page - 1, "Previous")
        if result.page > 1
        else '<span class="disabled">Previous</span>'
    )
    following = (
        link(result.page + 1, "Next")
        if result.page < result.pages
        else '<span class="disabled">Next</span>'
    )
    return (
        '<nav class="pager" aria-label="Memory pages">'
        f'<span class="summary">Page {result.page} of {result.pages} · '
        f"{result.total} memories</span>"
        f'<span class="controls">{previous}{following}</span></nav>'
    )
