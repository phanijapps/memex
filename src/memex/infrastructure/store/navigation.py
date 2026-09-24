"""Generated OKF-style directory navigation over the memory tree.

``index.md`` files are disposable navigation views derived entirely from
memory pages: deterministic bytes, no timestamps, rebuilt on demand. The
root index carries ``okf_version: "0.2"`` front matter; descendant indexes
are body-only. ``log.md`` is never generated, never modified, never removed.
"""

from __future__ import annotations

import itertools
import os
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from memex.domain.errors import MemexError
from memex.domain.models import NODE_TYPES, WikiNode
from memex.domain.reserved import OKF_VERSION, classify_reserved_text, is_structural
from memex.domain.types import TYPE_DIRS

_INDEX_NAME = "index.md"
_ESCAPE_CHARS = frozenset("\\`*_[]<>")
_SUBDIR_HEADING = "Subdirectories"
_SECTION_TYPES = {TYPE_DIRS[node_type].capitalize(): node_type for node_type in NODE_TYPES}

# Entry lines of one index keyed by node type, then by slug: the parsed
# form of an index that both the full render and the single-page splice
# serialize through one composer, so their bytes cannot diverge.
Entries = dict[str, dict[str, str]]

# Bounded per-directory page listing (WikiStore.scan_dir shaped); refresh
# parses only the mutated chain's own pages through it, never the store.
ScanDir = Callable[[Path, list[str] | None], list[WikiNode]]


class NavigationError(MemexError):
    """Bounded navigation failure; never carries memory content."""


@dataclass(slots=True)
class NavigationChange:
    """One bounded navigation outcome: category plus docs-relative path."""

    category: str
    path: str


@dataclass(slots=True)
class NavigationReport:
    """Outcomes of a regeneration or refresh run. Bounded fields only."""

    CATEGORIES: ClassVar[tuple[str, ...]] = ("written", "removed", "collision", "write_failed")

    changes: list[NavigationChange] = field(default_factory=list)

    def by_category(self, category: str) -> list[NavigationChange]:
        return [change for change in self.changes if change.category == category]

    def category_counts(self) -> dict[str, int]:
        return {category: len(self.by_category(category)) for category in self.CATEGORIES}


def _escape(text: str) -> str:
    """Render page text as inert Markdown: no markup, no HTML passthrough."""
    return "".join("\\" + ch if ch in _ESCAPE_CHARS else ch for ch in text)


def _entry_link(line: str) -> str | None:
    """The link target of one generated entry line, or None when malformed.

    Escaped title text never holds a bare ``]``, so the first unescaped
    ``](`` after ``- [`` opens the generator's own link unambiguously.
    """
    if not line.startswith("- ["):
        return None
    pos = 3
    while pos < len(line) and line[pos] != "]":
        pos += 2 if line[pos] == "\\" else 1
    end = line.find(")", pos + 2)
    if line[pos : pos + 2] != "](" or end == -1:
        return None
    return line[pos + 2 : end]


class NavigationGenerator:
    """Deterministic ``index.md`` generation for the docs tree."""

    def __init__(self, wiki_dir: Path) -> None:
        self._wiki_dir = wiki_dir
        self._tmp_seq = itertools.count()

    def regenerate(self, nodes: list[WikiNode]) -> NavigationReport:
        """Rewrite every needed index; remove obsolete generated ones.

        Obsolete indexes are removed only when they are structural files; a
        legacy page or any unrecognized file at an index path is reported as
        a collision and never touched.
        """
        self._guard_nodes_inside(nodes)
        report = NavigationReport()
        self._sweep_stale_tmps()
        needed = self._needed_dirs()
        for directory in sorted(needed):
            self._write_index(directory, self.render(directory, nodes), report)
        for target in sorted(self._wiki_dir.rglob(_INDEX_NAME)):
            if target.parent in needed:
                continue
            self._remove_index(target.parent, report)
        return report

    def refresh_page(
        self, page_path: Path, node: WikiNode | None, scan_dir: ScanDir
    ) -> NavigationReport:
        """Refresh navigation after one page write (``node``) or delete (None).

        Splices the page's own entry into its directory index in sorted
        position, so the cost is the index plus the page, never the
        siblings. Ancestor indexes list child directories only and are
        touched solely when this directory stops holding pages. Falls back
        to :meth:`refresh` when the index is missing, is not generator
        shaped, or lists the page ambiguously; a legacy page at the index
        path stays untouched and reports a collision, as everywhere else.
        """
        directory = page_path.parent
        if node is not None:
            self._guard_nodes_inside([node])
        if not self._inside(page_path):
            return NavigationReport()
        target = directory / _INDEX_NAME
        text = self._structural_text(target)
        if text is None:
            return self.refresh(directory, scan_dir)
        parsed = self._parse_index(directory, text)
        if parsed is None:
            return self.refresh(directory, scan_dir)
        entries, children = parsed
        slug = page_path.stem
        listed_under = [node_type for node_type, rows in entries.items() if slug in rows]
        if len(listed_under) > 1 or (node is None and not listed_under):
            return self.refresh(directory, scan_dir)
        if not children and not any(entries.values()):
            # An orphan index: the directory is only now gaining pages, so
            # its ancestors must learn about it through the chain refresh.
            return self.refresh(directory, scan_dir)
        for node_type in listed_under:
            del entries[node_type][slug]
        if node is not None:
            entries.setdefault(node.type, {})[slug] = self._entry(directory, page_path, node)
        report = NavigationReport()
        if children or any(entries.values()):
            self._write_index(directory, self._compose(directory, entries, children), report)
            return report
        self._remove_index(directory, report)
        for ancestor in self._chain(directory)[1:]:
            if self._subtree_has_pages(ancestor):
                self._write_index(ancestor, self.render(ancestor, scan_dir(ancestor, [])), report)
                break
            self._remove_index(ancestor, report)
        return report

    def refresh(self, changed_dir: Path, scan_dir: ScanDir) -> NavigationReport:
        """Refresh indexes on the whole ancestor chain of one mutated directory.

        Parses only pages directly inside the chain's directories through
        ``scan_dir``, so one mutation's refresh cost never scales with the
        whole store; page existence elsewhere is a filesystem fact, not a
        parse. Best effort: filesystem failures become ``write_failed``
        entries and never raise, so an authoritative page mutation stays
        successful.
        """
        report = NavigationReport()
        if not self._inside(changed_dir):
            return report
        scan_errors: list[str] = []
        for directory in self._chain(changed_dir):
            if self._subtree_has_pages(directory):
                pages = scan_dir(directory, scan_errors)
                self._write_index(directory, self.render(directory, pages), report)
            else:
                self._remove_index(directory, report)
        return report

    def diagnose(self, nodes: list[WikiNode]) -> list[NavigationChange]:
        """Compare the expected tree with on-disk indexes (read-only).

        Categories: ``missing`` (needed index absent), ``stale`` (bytes
        differ), ``orphan`` (structural index where no pages remain), and
        ``collision`` (legacy page blocking a needed index path).
        """
        changes: list[NavigationChange] = []
        needed = self._needed_dirs()
        for directory in sorted(needed):
            target = directory / _INDEX_NAME
            rel = self._rel(target)
            if not target.exists():
                changes.append(NavigationChange("missing", rel))
                continue
            if not is_structural(target):
                changes.append(NavigationChange("collision", rel))
                continue
            if target.read_text(encoding="utf-8") != self.render(directory, nodes):
                changes.append(NavigationChange("stale", rel))
        for target in sorted(self._wiki_dir.rglob(_INDEX_NAME)):
            if target.parent in needed or not is_structural(target):
                continue
            changes.append(NavigationChange("orphan", self._rel(target)))
        return changes

    def render(self, directory: Path, nodes: list[WikiNode]) -> str:
        """Deterministic bytes of one directory's index.

        Page entries are listed under node-type headings (``NODE_TYPES``
        order) sorted by slug, then child-directory links sorted by name.
        Every link is relative to this index's directory and resolved under
        the docs root before use.
        """
        if not self._inside(directory):
            raise NavigationError("navigation directory escapes the docs root")
        entries: Entries = {}
        for node in nodes:
            if node.file_path and Path(node.file_path).parent == directory:
                entry = self._entry(directory, Path(node.file_path), node)
                entries.setdefault(node.type, {})[node.slug] = entry
        children = sorted(
            child.name
            for child in directory.iterdir()
            if child.is_dir() and self._subtree_has_pages(child)
        )
        return self._compose(directory, entries, children)

    def _entry(self, directory: Path, page_path: Path, node: WikiNode) -> str:
        entry = f"- [{_escape(node.title)}]({self._link(directory, page_path)})"
        if node.description:
            entry += f" — {_escape(node.description)}"
        return entry

    def _heading(self, directory: Path) -> str:
        if directory == self._wiki_dir:
            return "# index"
        return f"# {directory.relative_to(self._wiki_dir).as_posix()}"

    def _compose(self, directory: Path, entries: Entries, children: list[str]) -> str:
        lines = [self._heading(directory)]
        for node_type in NODE_TYPES:
            rows = entries.get(node_type)
            if not rows:
                continue
            lines.append("")
            lines.append(f"## {TYPE_DIRS[node_type].capitalize()}")
            lines.extend(rows[slug] for slug in sorted(rows))
        if children:
            lines.append("")
            lines.append(f"## {_SUBDIR_HEADING}")
            lines.extend(f"- [{child}/]({child}/{_INDEX_NAME})" for child in children)
        body = "\n".join(lines) + "\n"
        if directory == self._wiki_dir:
            return f'---\nokf_version: "{OKF_VERSION}"\n---\n{body}'
        return body

    def _parse_index(self, directory: Path, text: str) -> tuple[Entries, list[str]] | None:
        """Invert :meth:`_compose`; None for anything the generator never wrote.

        Entry lines are kept verbatim so a re-composed index reproduces the
        full render byte for byte; only each line's link is inspected.
        """
        prefix = f'---\nokf_version: "{OKF_VERSION}"\n---\n' if directory == self._wiki_dir else ""
        if not text.startswith(prefix) or not text.endswith("\n"):
            return None
        lines = text[len(prefix) : -1].split("\n")
        if lines[0] != self._heading(directory):
            return None
        entries: Entries = {}
        children: list[str] = []
        section_order = [*_SECTION_TYPES, _SUBDIR_HEADING]
        pos = 1
        while pos < len(lines):
            if lines[pos] != "" or pos + 2 > len(lines) or not lines[pos + 1].startswith("## "):
                return None
            heading = lines[pos + 1][3:]
            if heading not in section_order:
                return None
            section_order = section_order[section_order.index(heading) + 1 :]
            pos += 2
            rows_start = pos
            while pos < len(lines) and lines[pos] != "":
                line = lines[pos]
                link = _entry_link(line)
                if link is None:
                    return None
                if heading == _SUBDIR_HEADING:
                    child = link.removesuffix(f"/{_INDEX_NAME}")
                    if line != f"- [{child}/]({child}/{_INDEX_NAME})":
                        return None
                    children.append(child)
                else:
                    slug = link.removesuffix(".md")
                    rows = entries.setdefault(_SECTION_TYPES[heading], {})
                    if link != f"{slug}.md" or "/" in slug or slug in rows:
                        return None
                    rows[slug] = line
                pos += 1
            if pos == rows_start:
                return None
        return entries, children

    def _needed_dirs(self) -> set[Path]:
        """Directories needing an index: ancestors of page-holding directories.

        A page is a non-structural ``*.md`` in a type-directory position; the
        same predicate governs generation, refresh, and child links so the
        surfaces cannot disagree. An empty store needs none: navigation
        exists to disclose pages, and requiring a root index would fail
        ``verify`` on every fresh store until an explicit rebuild runs.
        """
        needed: set[Path] = set()
        for type_name in TYPE_DIRS.values():
            for path in self._wiki_dir.rglob(f"{type_name}/*.md"):
                if is_structural(path):
                    continue
                needed.add(self._wiki_dir)
                current = path.parent
                while current != self._wiki_dir:
                    needed.add(current)
                    current = current.parent
        return needed

    def _sweep_stale_tmps(self) -> None:
        """Remove index tmp files stranded by a hard kill mid-write."""
        for target in self._wiki_dir.rglob(f"{_INDEX_NAME}.*.tmp"):
            try:
                target.unlink()
            except OSError:
                continue  # bounded best effort; the file is inert to memory

    def _structural_text(self, target: Path) -> str | None:
        """The generator-owned index text at ``target``, or None when absent,
        unreadable, or not structural (a legacy page, a symlink)."""
        if target.is_symlink():
            return None
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if classify_reserved_text(target.name, text) != "structural":
            return None
        return text

    def _write_index(self, directory: Path, text: str, report: NavigationReport) -> None:
        target = directory / _INDEX_NAME
        rel = self._rel(target)
        if target.exists() and not is_structural(target):
            report.changes.append(NavigationChange("collision", rel))
            return
        # Process- and thread-unique temp name: concurrent index writes in
        # one directory cannot interleave each other's write/replace pair.
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{next(self._tmp_seq)}.tmp")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            report.changes.append(NavigationChange("write_failed", rel))
            return
        report.changes.append(NavigationChange("written", rel))

    def _remove_index(self, directory: Path, report: NavigationReport) -> None:
        target = directory / _INDEX_NAME
        if not target.exists():
            return
        rel = self._rel(target)
        if not is_structural(target):
            report.changes.append(NavigationChange("collision", rel))
            return
        try:
            target.unlink()
        except OSError:
            report.changes.append(NavigationChange("write_failed", rel))
            return
        report.changes.append(NavigationChange("removed", rel))

    def _link(self, directory: Path, page_path: Path) -> str:
        resolved = page_path.resolve(strict=False)
        root = self._wiki_dir.resolve(strict=False)
        try:
            page_rel = resolved.relative_to(root)
            dir_rel = directory.resolve(strict=False).relative_to(root)
        except ValueError:
            raise NavigationError("navigation link target escapes the docs root") from None
        link = posixpath.relpath(page_rel.as_posix(), dir_rel.as_posix())
        if link.startswith(".."):
            raise NavigationError("navigation link target escapes the docs root")
        return link

    def _subtree_has_pages(self, directory: Path) -> bool:
        """Filesystem fact: does this subtree hold a memory page?

        A page is a non-structural ``*.md`` either directly in ``directory``
        when it is itself a type directory, or in a type-directory position
        below it. Existence only — no page parsing, so refresh cost stays
        bounded by directory walking rather than content.
        """
        if not directory.is_dir():
            return False
        if directory.name in TYPE_DIRS.values() and any(
            not is_structural(path) for path in directory.glob("*.md")
        ):
            return True
        return any(
            not is_structural(path)
            for type_name in TYPE_DIRS.values()
            for path in directory.rglob(f"{type_name}/*.md")
        )

    def _chain(self, changed_dir: Path) -> list[Path]:
        """The changed directory and every ancestor up to the docs root."""
        chain: list[Path] = []
        current = changed_dir
        while current != self._wiki_dir:
            chain.append(current)
            current = current.parent
        chain.append(self._wiki_dir)  # the root index always belongs to the tree
        return chain

    def _guard_nodes_inside(self, nodes: list[WikiNode]) -> None:
        """Reject caller-supplied pages whose paths escape the docs root."""
        for node in nodes:
            if node.file_path and not self._inside(Path(node.file_path)):
                raise NavigationError("navigation target escapes the docs root")

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self._wiki_dir.resolve(strict=False))
        except ValueError:
            return False
        return True

    def _rel(self, path: Path) -> str:
        return path.relative_to(self._wiki_dir).as_posix()


__all__ = [
    "NavigationChange",
    "NavigationError",
    "NavigationGenerator",
    "NavigationReport",
]
