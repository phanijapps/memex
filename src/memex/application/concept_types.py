"""Declaring, listing, removing, and suggesting project concept types."""

from __future__ import annotations

from collections import Counter

from memex.domain.errors import WikiStoreError
from memex.domain.types import CATALOGUE, validate_type_name
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.store.navigation import NavigationGenerator
from memex.infrastructure.store.wiki_store import TypeDeclaration, WikiStore


class ConceptTypes:
    """Project-level type lifecycle over the store's directory declarations."""

    def __init__(
        self, store: WikiStore, index: IndexManager, navigation: NavigationGenerator
    ) -> None:
        self._store = store
        self._index = index
        self._navigation = navigation

    def add(self, name: str, *, project_id: str, description: str = "") -> TypeDeclaration:
        if name in CATALOGUE:
            raise WikiStoreError(f"{name!r} is a catalogue type; run memex types enable")
        return self._store.declare_type(
            name, scope="project", project_id=project_id, description=description
        )

    def enable(self, name: str, *, project_id: str) -> TypeDeclaration:
        if name not in CATALOGUE:
            raise WikiStoreError(f"{name!r} is not a catalogue type; run memex types add")
        return self._store.declare_type(
            name, scope="project", project_id=project_id, description=CATALOGUE[name].answers
        )

    def remove(self, name: str, *, project_id: str, force: bool = False) -> dict[str, object]:
        declared = self._store.declared_types(scope="project", project_id=project_id).get(name)
        if declared is None or declared.kind == "builtin":
            raise WikiStoreError(f"type {name!r} is not declared for this project")
        pages = [n for n in self._store.scan_dir(declared.directory) if n.status != "archived"]
        if pages and not force:
            raise WikiStoreError(
                f"type {name!r} holds {len(pages)} page(s); pass --force to archive them first"
            )
        for node in pages:
            node.status = "archived"
            stored = self._store.write(node)
            self._index.update_record(stored)
        self._store.append_log(declared.directory, "remove", name, "user", f"archived={len(pages)}")
        self._navigation.refresh(declared.directory, self._store.scan_dir)
        return {"removed": name, "archived": len(pages)}

    def suggest(self, *, project_id: str, min_pages: int = 3) -> list[tuple[str, int]]:
        """Tags that recur across pages and are not types: concepts the store strains toward."""
        known = set(self._store.declared_types(scope="project", project_id=project_id))
        counts: Counter[str] = Counter()
        for node in self._store.scan_all():
            if node.project_id != project_id:
                continue
            for tag in node.tags:
                try:
                    validate_type_name(tag, custom=True)
                except ValueError:
                    continue
                if tag not in known:
                    counts[tag] += 1
        return sorted(
            ((t, c) for t, c in counts.items() if c >= min_pages), key=lambda r: (-r[1], r[0])
        )

    # Defined last: naming this method ``list`` shadows the builtin ``list``
    # type for every later annotation in this class body under
    # ``from __future__ import annotations`` (mypy resolves the postponed
    # string against the class namespace as it stands at that point).
    def list(self, *, scope: str, project_id: str | None) -> list[TypeDeclaration]:
        return list(self._store.declared_types(scope=scope, project_id=project_id).values())
