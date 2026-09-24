"""Ranked recall over the generated ``index.md`` navigation files.

A dependency-free engine that needs no SQLite index: it parses the page
rows the navigation generator writes (title, link, description), ranks
them by weighted term overlap, and reads only the front matter of the pages
it returns. Page bodies are never opened, so the engine covers titles and
descriptions only, and one search costs a scan linear in the total size of
the ``index.md`` files plus one bounded front-matter read per examined
candidate.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from memex.domain.errors import IndexManagerError, WikiStoreError
from memex.domain.frontmatter import parse_front_matter
from memex.domain.models import RecallHit, RecallResult, WikiNode, utc_now_iso
from memex.domain.reserved import classify_reserved_text
from memex.domain.types import TYPE_DIRS
from memex.infrastructure.search.bm25_retriever import _query_tokens, _stable_dedupe
from memex.infrastructure.search.index_manager import check_slug
from memex.infrastructure.store.wiki_store import _node_from_dict

SEARCH_ENGINE = "navigation-index-md"
FALLBACK_SEARCH_ENGINE = "navigation-index-md-fallback"

_INDEX_NAME = "index.md"
_TITLE_WEIGHT = 2.0
_DESCRIPTION_WEIGHT = 1.0
_TEXT_TOKENS = re.compile(r"[a-z0-9]+")
# A generated row: the renderer escapes "]" inside text, so an unescaped
# "](" can only open the link, and " — " after the link can only open the
# description.
_ROW = re.compile(
    r"^- \[(?P<title>(?:\\.|[^\\\]])*)\]\((?P<link>[^)\s]+)\)(?: — (?P<description>.*))?$"
)
_HEADING = re.compile(r"^## (?P<name>.+)$")
_UNESCAPE = re.compile(r"\\(.)")
_HEADING_TYPES = {TYPE_DIRS[node_type].capitalize(): node_type for node_type in TYPE_DIRS}
# Generous ceiling over the ~1 KB a stored page's front matter occupies; a
# page that never closes its front matter is abandoned, not read through.
_FRONT_MATTER_MAX_BYTES = 16 * 1024


@dataclass(slots=True, frozen=True)
class _Row:
    """One page row parsed from a navigation index; no page has been opened."""

    title: str
    description: str
    path: Path
    slug: str
    node_type: str
    scope: str
    project_dir: str | None


class NavigationSearch:
    """Weighted term-overlap ranking over navigation rows (higher score is better)."""

    def __init__(
        self,
        wiki_dir: Path,
        *,
        default_top_k: int = 10,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._wiki_dir = wiki_dir
        self._default_top_k = default_top_k
        self._clock = clock

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        node_type: str | None = None,
        time_range: tuple[str, str] | None = None,
        tags: list[str] | None = None,
        include_expired: bool = False,
        include_inactive: bool = False,
        scope: str = "global",
        project_id: str | None = None,
    ) -> RecallResult:
        """Rank navigation rows by query overlap and fill hits from front matter.

        Candidates are walked best-first; each one's front matter is read
        once to apply the ``tags`` filter and the recall visibility rules
        (active status, validity window) until ``top_k`` hits are found.
        Rows are matched on title and description only. Global scope spans
        global and project pages, as the FTS5 retriever does; project scope
        narrows to one project. ``time_range`` needs the index and is
        rejected. ``total_indexed`` counts every parsed row.
        """
        if time_range is not None:
            raise ValueError(f"time_range is not supported by the {SEARCH_ENGINE} engine")
        limit = top_k if top_k is not None else self._default_top_k
        if not 1 <= limit <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if scope == "project" and not project_id:
            raise ValueError("project_id is required for project recall")
        if scope not in ("global", "project"):
            raise ValueError("scope must be 'global' or 'project'")
        started = time.perf_counter()
        tokens = self._tokens(query)
        rows = self._rows()
        ranked = sorted(
            ((score, row) for row in rows if (score := self._score(row, tokens)) > 0),
            key=lambda item: (-item[0], item[1].slug, item[1].path),
        )
        now = self._clock()
        # Locators are opaque directory names, so a project directory's id
        # is learned from the first candidate page read there.
        project_ids: dict[str | None, str | None] = {}
        hits: list[RecallHit] = []
        for score, row in ranked:
            if node_type is not None and row.node_type != node_type:
                continue
            if scope == "project" and (
                row.scope != "project" or project_ids.get(row.project_dir, project_id) != project_id
            ):
                continue
            node = self._front_matter_node(row)
            if node is None:
                continue
            if scope == "project":
                project_ids.setdefault(row.project_dir, node.project_id)
                if node.project_id != project_id:
                    continue
            if not self._visible(node, now, include_expired, include_inactive):
                continue
            if tags and any(tag not in node.tags for tag in tags):
                continue
            hits.append(self._hit(row, node, tokens, score, len(hits) + 1))
            if len(hits) == limit:
                break
        return RecallResult(
            query=query,
            hits=hits,
            total_indexed=len(rows),
            search_engine=SEARCH_ENGINE,
            search_time_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    @staticmethod
    def _tokens(query: str) -> list[str]:
        return _stable_dedupe(_query_tokens(query))

    @staticmethod
    def _score(row: _Row, tokens: list[str]) -> float:
        title = set(_TEXT_TOKENS.findall(row.title.lower()))
        description = set(_TEXT_TOKENS.findall(row.description.lower()))
        return sum(
            (_TITLE_WEIGHT if token in title else 0.0)
            + (_DESCRIPTION_WEIGHT if token in description else 0.0)
            for token in tokens
        )

    def _rows(self) -> list[_Row]:
        """Every page row of every structural index, in path order.

        A legacy page at an index path is a memory, not navigation: its
        text is never parsed for rows.
        """
        rows: list[_Row] = []
        for index in sorted(self._wiki_dir.rglob(_INDEX_NAME)):
            if index.is_symlink():
                continue
            try:
                text = index.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if classify_reserved_text(index.name, text) != "structural":
                continue
            rows.extend(self._parse_index(index, text))
        return rows

    def _parse_index(self, index: Path, text: str) -> list[_Row]:
        """Page rows of one index whose directory sits in a known scope layout.

        The index is read once by the caller, which also classified it. The
        directory is resolved once; each row's link is then checked
        lexically (one plain ``slug.md`` component), so a row costs no
        filesystem call.
        """
        directory = index.parent
        try:
            parts = directory.resolve(strict=False).relative_to(self._wiki_dir.resolve()).parts
        except (OSError, ValueError):
            return []
        if len(parts) == 2 and parts[0] == "global":
            scope, project_dir = "global", None
        elif len(parts) == 3 and parts[0] == "projects":
            scope, project_dir = "project", parts[1]
        else:
            return []
        rows: list[_Row] = []
        node_type: str | None = None
        for line in text.splitlines():
            heading = _HEADING.match(line)
            if heading is not None:
                node_type = _HEADING_TYPES.get(heading.group("name"))
                continue
            if node_type is None or parts[-1] != TYPE_DIRS[node_type]:
                continue
            match = _ROW.match(line)
            if match is None:
                continue
            slug = self._link_slug(match.group("link"))
            if slug is None:
                continue
            rows.append(
                _Row(
                    title=_UNESCAPE.sub(r"\1", match.group("title")),
                    description=_UNESCAPE.sub(r"\1", match.group("description") or ""),
                    path=directory / f"{slug}.md",
                    slug=slug,
                    node_type=node_type,
                    scope=scope,
                    project_dir=project_dir,
                )
            )
        return rows

    @staticmethod
    def _link_slug(link: str) -> str | None:
        """The slug of a link to a sibling page; None for any other link shape."""
        if not link.endswith(".md"):
            return None
        slug = link[: -len(".md")]
        try:
            return slug if check_slug(slug) == slug else None
        except IndexManagerError:
            return None

    def _front_matter_node(self, row: _Row) -> WikiNode | None:
        """Parse a page's front matter; a malformed or unreadable page is skipped."""
        text = _read_front_matter(row.path)
        if text is None:
            return None
        try:
            data, _body = parse_front_matter(text)
            node = _node_from_dict(data, "")
        except (WikiStoreError, ValueError):
            return None
        node.slug = row.slug
        node.file_path = str(row.path)
        return node

    @staticmethod
    def _visible(node: WikiNode, now: str, include_expired: bool, include_inactive: bool) -> bool:
        if not include_expired:
            if node.valid_from is not None and node.valid_from > now:
                return False
            if node.valid_until is not None and node.valid_until <= now:
                return False
        return include_inactive or node.status == "active"

    @staticmethod
    def _hit(row: _Row, node: WikiNode, tokens: list[str], score: float, rank: int) -> RecallHit:
        description = set(_TEXT_TOKENS.findall(row.description.lower()))
        if any(token in description for token in tokens):
            snippet, source = row.description, "description"
        else:
            snippet, source = row.title, "title"
        return RecallHit(
            slug=row.slug,
            status=node.status,
            file_path=str(row.path),
            title=row.title,
            description=row.description,
            node_type=row.node_type,
            importance=node.importance,
            score=score,
            rank=rank,
            snippet=snippet,
            snippet_source=source,
            tags=list(node.tags),
            created=node.created,
            timestamp=node.timestamp,
            updated_at=node.updated_at,
            last_access=node.last_access,
            transcript_ref=node.transcript_ref,
            links=sorted({link.target for link in node.links}),
            scope=node.scope,
            project_id=node.project_id,
            project_label=node.project_label,
        )


def _read_front_matter(path: Path) -> str | None:
    """The front matter block with its closing delimiter; the body is never read.

    Returns None for a symlink, an unreadable file, a page without front
    matter, or one whose front matter exceeds the byte ceiling.
    """
    if path.is_symlink():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
            if first != "---\n":
                return None
            lines = [first]
            size = len(first)
            for line in handle:
                lines.append(line)
                size += len(line.encode("utf-8"))
                if line.rstrip("\n") == "---":
                    return "".join(lines)
                if size > _FRONT_MATTER_MAX_BYTES:
                    return None
    except (OSError, UnicodeDecodeError):
        return None
    return None


__all__ = ["FALLBACK_SEARCH_ENGINE", "SEARCH_ENGINE", "NavigationSearch"]
