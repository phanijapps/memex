"""Deterministic verification gate (integration layer L3).

``memex verify`` converts "should have used memory" into a failing exit
code. Health checks are always run; activity evidence is optional and
only enforced when requested via require flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from memex.application.memory import Memex
from memex.domain.models import WikiNode
from memex.domain.types import type_directory
from memex.infrastructure.store.navigation import NavigationChange
from memex.infrastructure.store.wiki_store import hash_body


@dataclass(slots=True)
class VerifyReport:
    """Outcome of a deterministic verification run."""

    ok: bool
    checks: list[dict[str, object]] = field(default_factory=list)
    recall_evidence: bool = False
    write_evidence: bool = False
    warnings: list[str] = field(default_factory=list)


def _check(name: str, passed: bool, detail: str = "") -> dict[str, object]:
    return {"check": name, "ok": passed, "detail": detail}


def verify(
    memex: Memex,
    *,
    since: str | None = None,
    require_recall: bool = False,
    require_write: bool = False,
) -> VerifyReport:
    """Run health checks and optional memory-activity evidence checks."""
    checks: list[dict[str, object]] = []

    scan_errors: list[str] = []
    nodes = memex.wiki_store.scan_all(scan_errors)
    checks.append(_check("wiki-parseable", not scan_errors, f"{len(scan_errors)} malformed pages"))

    stale = 0
    for node in nodes:
        row = memex.index_manager.get(
            node.slug,
            scope=node.scope,
            project_id=node.project_id or "",
            node_type=node.type,
        )
        if (
            row is None
            or str(row["content_hash"]) != hash_body(node.body)
            or str(row["description"] or "") != node.description
        ):
            stale += 1
    checks.append(_check("index-fresh", stale == 0, f"{stale} stale or missing rows"))

    broken: list[str] = []
    for node in nodes:
        broken.extend(
            memex.link_manager.validate_links(
                node.slug,
                source_scope=node.scope,
                source_project_id=node.project_id or "",
            )
        )
    checks.append(_check("links-resolve", not broken, f"{len(broken)} broken links"))

    checks.extend(okf_checks(nodes))
    checks.extend(type_checks(memex, nodes))

    navigation_changes = memex.navigation.diagnose(nodes)
    navigation_defects = [
        change for change in navigation_changes if change.category in {"missing", "stale", "orphan"}
    ]
    navigation_detail = _navigation_detail(navigation_changes)
    checks.append(_check("navigation-consistent", not navigation_defects, navigation_detail))

    warnings: list[str] = []
    recall_evidence = False
    write_evidence = False
    from memex.infrastructure.run_log import read_runs, zero_yield_streak

    streak = zero_yield_streak(read_runs(memex.data_dir))
    if streak >= 3:
        warnings.append(f"{streak} consecutive zero-yield consolidations")
    if since is not None:
        for row in memex.index_manager.get_all_records():
            if row["last_access"] and str(row["last_access"]) >= since:
                recall_evidence = True
                break
        write_evidence = any(node.updated_at >= since for node in nodes)
        if not recall_evidence:
            warnings.append(f"no recall activity recorded since {since}")
        if not write_evidence:
            warnings.append(f"no memory writes since {since}")

    ok = all(check["ok"] for check in checks)
    if require_recall and not recall_evidence:
        ok = False
    if require_write and not write_evidence:
        ok = False
    return VerifyReport(
        ok=ok,
        checks=checks,
        recall_evidence=recall_evidence,
        write_evidence=write_evidence,
        warnings=warnings,
    )


def _slug_detail(label: str, slugs: list[str]) -> str:
    """Bounded diagnostic: a count plus at most three page slugs, never content."""
    if not slugs:
        return f"0 {label}"
    shown = ", ".join(sorted(slugs)[:3])
    return f"{len(slugs)} {label}: {shown}"


def _undeclared_and_non_pending(
    memex: Memex, project_dir: Path, nodes_by_dir: dict[Path, list[WikiNode]]
) -> tuple[list[str], list[str]]:
    """Check declared types in a project and return undeclared dirs and non-pending pages.

    Returns (undeclared, non_pending) lists of directory paths and page slugs.
    """
    undeclared: list[str] = []
    non_pending: list[str] = []
    declared = memex.wiki_store.declared_types_in(project_dir)
    by_dir = {type_directory(name): decl for name, decl in declared.items()}
    for child in sorted(p for p in project_dir.iterdir() if p.is_dir() and not p.is_symlink()):
        decl = by_dir.get(child.name)
        if decl is None:
            undeclared.append(f"{project_dir.name}/{child.name}")
            continue
        if decl.kind == "draft":
            non_pending.extend(n.slug for n in nodes_by_dir.get(child, []) if n.status != "pending")
    return undeclared, non_pending


def type_checks(memex: Memex, nodes: list[WikiNode]) -> list[dict[str, object]]:
    """Project concept types: page type matches its directory, every
    directory is declared, and a draft type holds only pending pages."""
    mismatched = [
        n.slug
        for n in nodes
        if n.file_path and Path(n.file_path).parent.name != type_directory(n.type)
    ]
    # Build nodes indexed by directory for efficient draft-type checks
    nodes_by_dir: dict[Path, list[WikiNode]] = {}
    for n in nodes:
        if n.file_path:
            parent = Path(n.file_path).parent
            if parent not in nodes_by_dir:
                nodes_by_dir[parent] = []
            nodes_by_dir[parent].append(n)

    undeclared: list[str] = []
    non_pending: list[str] = []
    for project_dir in memex.wiki_store.project_directories():
        proj_undeclared, proj_non_pending = _undeclared_and_non_pending(
            memex, project_dir, nodes_by_dir
        )
        undeclared.extend(proj_undeclared)
        non_pending.extend(proj_non_pending)

    mismatch_label = "pages whose type differs from their directory"
    declare_label = "project directories without a declaration"
    pending_label = "non-pending pages inside draft types"
    return [
        _check(
            "types-match-directory",
            not mismatched,
            _slug_detail(mismatch_label, mismatched),
        ),
        _check(
            "types-declared",
            not undeclared,
            _slug_detail(declare_label, undeclared),
        ),
        _check(
            "draft-types-pending",
            not non_pending,
            _slug_detail(pending_label, non_pending),
        ),
    ]


def okf_checks(nodes: list[WikiNode]) -> list[dict[str, object]]:
    """OKF v0.2 graph and temporal conformance, as the OKF linter defines it.

    Mirrors the reference linter's v0.2 rules: a ``parent`` must resolve and
    must not cycle, relation targets should resolve, ``valid_from`` must not
    follow ``valid_until``, and ``stale_after`` belongs inside a closed
    validity window.
    """
    known = {node.slug for node in nodes}
    by_slug = {node.slug: node for node in nodes}

    unresolved_parents = [node.slug for node in nodes if node.parent and node.parent not in known]
    cyclic: list[str] = []
    for node in nodes:
        seen = {node.slug}
        current = by_slug.get(node.parent or "")
        while current is not None:
            if current.slug in seen:
                cyclic.append(node.slug)
                break
            seen.add(current.slug)
            current = by_slug.get(current.parent or "")

    unresolved_relations = [
        node.slug for node in nodes if any(target not in known for target, _rel in node.relations())
    ]
    inverted = [
        node.slug
        for node in nodes
        if node.valid_from and node.valid_until and node.valid_from > node.valid_until
    ]
    out_of_window = [
        node.slug
        for node in nodes
        if node.stale_after
        and node.valid_from
        and node.valid_until
        and not (node.valid_from <= node.stale_after <= node.valid_until)
    ]
    return [
        _check(
            "okf-parent-resolves",
            not unresolved_parents,
            _slug_detail("unresolved parents", unresolved_parents),
        ),
        _check("okf-parent-acyclic", not cyclic, _slug_detail("parent cycles", cyclic)),
        _check(
            "okf-relations-resolve",
            not unresolved_relations,
            _slug_detail("unresolved relation targets", unresolved_relations),
        ),
        _check(
            "okf-validity-ordered",
            not inverted,
            _slug_detail("pages with valid_from after valid_until", inverted),
        ),
        _check(
            "okf-stale-in-window",
            not out_of_window,
            _slug_detail("pages with stale_after outside the validity window", out_of_window),
        ),
    ]


def _navigation_detail(changes: list[NavigationChange]) -> str:
    """Bounded navigation summary: categories, counts, docs-relative paths."""
    if not changes:
        return "navigation current"
    counts = " ".join(
        f"{category}={sum(1 for change in changes if change.category == category)}"
        for category in ("missing", "stale", "orphan", "collision")
        if any(change.category == category for change in changes)
    )
    paths = sorted({change.path for change in changes})
    return f"{counts}: {'; '.join(paths)}"
