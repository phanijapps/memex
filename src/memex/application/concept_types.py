"""Declaring, listing, removing, and suggesting project concept types."""

from __future__ import annotations

from collections import Counter

from memex.domain.errors import WikiStoreError
from memex.domain.reserved import RESERVED_SLUGS
from memex.domain.types import (
    CATALOGUE,
    SUBDIRECTORIES_HEADING,
    TYPE_DIRS,
    TypeDeclaration,
    validate_type_name,
)
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.store.navigation import NavigationGenerator
from memex.infrastructure.store.wiki_store import WikiStore

# Excluded from `suggest` alongside a project's declared types: directory
# names and reserved filenames a tag could never legally become.
_NEVER_SUGGESTIBLE = frozenset(
    {*TYPE_DIRS.values(), *RESERVED_SLUGS, SUBDIRECTORIES_HEADING.lower()}
)


class ConceptTypes:
    """Project-level type lifecycle over the store's directory declarations."""

    def __init__(
        self, store: WikiStore, index: IndexManager, navigation: NavigationGenerator
    ) -> None:
        self._store = store
        self._index = index
        self._navigation = navigation

    def add(
        self,
        name: str,
        *,
        project_id: str,
        description: str = "",
        project_locator: str | None = None,
    ) -> TypeDeclaration:
        try:
            validate_type_name(name, custom=True)
        except ValueError as exc:
            # A shape-invalid name never reaches an f-string; a catalogue
            # collision gets the more useful "enable" hint instead of the
            # generic collision message.
            if name in CATALOGUE:
                raise WikiStoreError(
                    f"{name!r} is a catalogue type; run memex types enable"
                ) from exc
            raise WikiStoreError(str(exc)) from exc
        return self._store.declare_type(
            name,
            scope="project",
            project_id=project_id,
            description=description,
            project_locator=project_locator,
        )

    def enable(
        self, name: str, *, project_id: str, project_locator: str | None = None
    ) -> TypeDeclaration:
        try:
            validate_type_name(name)  # shape only; never embeds a bad name below
        except ValueError as exc:
            raise WikiStoreError(str(exc)) from exc
        if name not in CATALOGUE:
            raise WikiStoreError(f"{name!r} is not a catalogue type; run memex types add")
        return self._store.declare_type(
            name,
            scope="project",
            project_id=project_id,
            description=CATALOGUE[name].answers,
            project_locator=project_locator,
        )

    def remove(
        self, name: str, *, project_id: str, force: bool = False, project_locator: str | None = None
    ) -> dict[str, object]:
        try:
            validate_type_name(name)  # shape only; never embeds a bad name below
        except ValueError as exc:
            raise WikiStoreError(str(exc)) from exc
        declared = self._store.declared_types(
            scope="project", project_id=project_id, project_locator=project_locator
        ).get(name)
        if declared is None or declared.kind == "builtin":
            raise WikiStoreError(f"type {name!r} is not declared for this project")
        if declared.kind == "withdrawn":
            raise WikiStoreError(f"type {name!r} is already withdrawn")
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

    def suggest(
        self, *, project_id: str, min_pages: int = 3, project_locator: str | None = None
    ) -> list[tuple[str, int]]:
        """Tags that recur and are not yet a type for this project.

        Shape-valid only; an un-enabled catalogue name (e.g. ``decision``)
        qualifies, since "not yet a type for this project" is exactly what
        it is until a person runs ``memex types enable``.
        """
        known = set(
            self._store.declared_types(
                scope="project", project_id=project_id, project_locator=project_locator
            )
        )
        counts: Counter[str] = Counter()
        for node in self._store.scan_all():
            if node.project_id != project_id:
                continue
            for tag in node.tags:
                try:
                    validate_type_name(tag)
                except ValueError:
                    continue
                if tag in known or tag in _NEVER_SUGGESTIBLE:
                    continue
                counts[tag] += 1
        return sorted(
            ((t, c) for t, c in counts.items() if c >= min_pages), key=lambda r: (-r[1], r[0])
        )

    # Defined last: naming this method ``list`` shadows the builtin ``list``
    # type for every later annotation in this class body under
    # ``from __future__ import annotations`` (mypy resolves the postponed
    # string against the class namespace as it stands at that point).
    def list(
        self, *, scope: str, project_id: str | None, project_locator: str | None = None
    ) -> list[TypeDeclaration]:
        return list(
            self._store.declared_types(
                scope=scope, project_id=project_id, project_locator=project_locator
            ).values()
        )
