"""Filesystem CRUD for wiki Markdown pages (spec §7 Utility 1).

Pages live at ``docs/{type_dir}/{slug}.md`` with strict front matter.
Reads are side-effect free; access counting lives in the index layer.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from memex.domain.errors import WikiStoreError
from memex.domain.frontmatter import parse_front_matter, serialize_front_matter
from memex.domain.models import (
    NODE_TYPES,
    PAGE_STATUSES,
    SemanticLink,
    WikiNode,
    new_id,
    utc_now_iso,
)
from memex.domain.reserved import OKF_VERSION, RESERVED_SLUGS, is_structural
from memex.domain.slugs import derive_slug, unique_slug
from memex.domain.types import TYPE_DIRS

_SAFE_COMPONENT = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")

# OKF v0.2 fields first, in the order the OKF reference implementation writes
# them, then the Memex extension fields. The reader accepts exactly this set:
# an unknown key is a malformed page, and the retired names `created`,
# `updated`, `valid_to`, and `expires_at` are unknown.
_OKF_KEYS: tuple[str, ...] = (
    "okf_version",
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "timestamp",
    "valid_from",
    "valid_until",
    "stale_after",
    "updated_at",
    "parent",
    "supersedes",
    "implements",
    "depends_on",
    "links",
)

_MEMEX_KEYS: tuple[str, ...] = (
    "id",
    "importance",
    "access_count",
    "last_access",
    "created",
    "content_hash",
    "status",
    "occurred_at",
    "source",
    "harness",
    "confidence",
    "scope",
    "project_id",
    "project_label",
    "session_id",
    "transcript_ref",
)

_FRONT_MATTER_KEYS: tuple[str, ...] = _OKF_KEYS + _MEMEX_KEYS

_STR_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "resource",
    "timestamp",
    "updated_at",
    "last_access",
    "stale_after",
    "valid_from",
    "valid_until",
    "transcript_ref",
    "session_id",
    "content_hash",
)


def hash_body(body: str) -> str:
    """SHA-256 of the Markdown body, prefixed for inspectability."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


NodeList = list[WikiNode]
PathList = list[Path]
StrList = list[str]
NamespaceKey = tuple[str, str, str, str]


class _MissingWikiPage(WikiStoreError):
    """Private sentinel so missing pages don't mask ambiguous pages."""


class WikiStore:
    """CRUD over the wiki directory tree. The filesystem is the truth."""

    LEGACY_DIR = "wiki"  # pre-0.2 layout; migrated in place, never deleted
    PAGES_DIR = "docs"

    def __init__(self, data_dir: Path, *, slug_algo: str = "kebab") -> None:
        self.data_dir = data_dir
        legacy = data_dir / self.LEGACY_DIR
        docs = data_dir / self.PAGES_DIR
        if legacy.is_symlink() or docs.is_symlink():
            raise WikiStoreError("wiki path component is a symlink")
        try:
            docs.resolve(strict=False).relative_to(data_dir.resolve(strict=False))
        except ValueError as exc:
            raise WikiStoreError("wiki path escapes the data directory") from exc
        if legacy.is_dir() and not docs.exists():
            legacy.rename(docs)  # one-time migration to the docs layout
        self.wiki_dir = docs
        self.slug_algo = slug_algo
        global_dir = self.wiki_dir / "global"
        for component in (global_dir, *(global_dir / name for name in TYPE_DIRS.values())):
            if component.is_symlink():
                raise WikiStoreError(f"wiki path component is a symlink: {component}")
        for type_dir in TYPE_DIRS.values():
            (global_dir / type_dir).mkdir(parents=True, exist_ok=True)

    def get_path(
        self,
        slug: str,
        node_type: str | None = None,
        *,
        scope: str | None = None,
        project_id: str | None = None,
        project_locator: str | None = None,
    ) -> Path:
        """Path for a slug; searches type dirs when node_type is omitted."""
        self._validate_page_slug(slug)
        if node_type is not None:
            if node_type not in TYPE_DIRS:
                raise WikiStoreError(f"unknown node type: {node_type!r}")
            return (
                self._type_dir(node_type, scope or "global", project_id, project_locator)
                / f"{slug}.md"
            )
        path = self._find_existing_path(slug, scope=scope, project_id=project_id)
        if path is None:
            raise _MissingWikiPage(f"no wiki page found for slug: {slug!r}")
        return path

    def get_slug_from_path(self, path: Path) -> str:
        return path.stem

    def exists(self, slug: str) -> bool:
        try:
            self.get_path(slug)
        except WikiStoreError:
            return False
        return True

    def read(
        self,
        slug: str,
        node_type: str | None = None,
        *,
        scope: str | None = None,
        project_id: str | None = None,
    ) -> WikiNode | None:
        """Parse a wiki page. Returns None when the slug does not exist."""
        path = self._find_existing_path(slug, node_type, scope=scope, project_id=project_id)
        if path is None:
            return None
        return self.read_path(path)

    def write(self, node: WikiNode) -> WikiNode:
        """Persist a node and return it as stored.

        A new node (empty ``slug``) gets a derived, collision-suffixed slug.
        Writing to an existing slug updates it: ``id``, ``created``,
        ``access_count``, and ``last_access`` are preserved from the stored
        page; every other field comes from the input node (spec §7 Utility 1).

        The two OKF timestamps carry different promises, as the format
        intends: ``timestamp`` is refreshed on every write, while
        ``updated_at`` moves only when the body actually changes.
        """
        if node.type not in TYPE_DIRS:
            raise WikiStoreError(f"unknown node type: {node.type!r}")
        slug = node.slug or self._new_slug(
            node.title, node.id, node.scope, node.project_id, node.project_locator
        )
        existing_path = self.get_path(
            slug,
            node.type,
            scope=node.scope,
            project_id=node.project_id,
            project_locator=node.project_locator,
        )
        existing = self.read_path(existing_path) if existing_path.exists() else None

        stored = node
        stored.slug = slug
        now = utc_now_iso()
        content_hash = hash_body(stored.body)
        if existing is not None:
            stored.id = existing.id
            stored.created = existing.created
            stored.access_count = existing.access_count
            stored.last_access = existing.last_access
            stored.updated_at = (
                existing.updated_at if content_hash == existing.content_hash else now
            )
        else:
            stored.id = node.id or new_id()
            if not stored.created:
                stored.created = now
            stored.updated_at = now
        stored.timestamp = now
        stored.content_hash = content_hash

        path = self.get_path(
            stored.slug,
            stored.type,
            scope=stored.scope,
            project_id=stored.project_id,
            project_locator=stored.project_locator,
        )
        self._atomic_write(path, serialize_front_matter(node_front_matter(stored), stored.body))
        stored.file_path = str(path)
        return stored

    def list(self, node_type: str | None = None) -> NodeList:
        """All nodes, optionally filtered by type. Sorted by slug."""
        nodes = self.scan_all()
        if node_type is not None:
            if node_type not in TYPE_DIRS:
                raise WikiStoreError(f"unknown node type: {node_type!r}")
            nodes = [node for node in nodes if node.type == node_type]
        return sorted(nodes, key=lambda node: node.slug)

    def scan_all(self, errors: StrList | None = None) -> NodeList:
        """Traverse every type directory. Malformed pages are skipped and,
        when ``errors`` is provided, reported as messages."""
        nodes: NodeList = []
        for type_dir in TYPE_DIRS.values():
            for path in sorted(self.wiki_dir.rglob(f"{type_dir}/*.md")):
                if is_structural(path):
                    continue  # generated index.md / optional OKF log.md
                try:
                    self._reject_unsafe_page_path(path)
                    nodes.append(self.read_path(path))
                except WikiStoreError as exc:
                    if errors is None:
                        raise
                    errors.append(str(exc))
        self._reject_duplicate_namespace_keys(nodes)
        return nodes

    def scan_dir(self, directory: Path, errors: StrList | None = None) -> NodeList:
        """Parse only the pages whose parent is exactly ``directory``.

        Same safeguards as :meth:`scan_all` (structural reserved files are
        skipped, paths are confinement-checked, malformed pages raise or
        append to ``errors``), at a cost bounded by one directory instead of
        the whole store. Non-type directories never hold pages and return
        an empty list without touching the filesystem.
        """
        if directory.name not in TYPE_DIRS.values() or not directory.is_dir():
            return []
        nodes: NodeList = []
        for path in sorted(directory.glob("*.md")):
            if is_structural(path):
                continue  # generated index.md / optional OKF log.md
            try:
                self._reject_unsafe_page_path(path)
                nodes.append(self.read_path(path))
            except WikiStoreError as exc:
                if errors is None:
                    raise
                errors.append(str(exc))
        self._reject_duplicate_namespace_keys(nodes)
        return nodes

    def _type_dir(
        self,
        node_type: str,
        scope: str,
        project_id: str | None,
        project_locator: str | None = None,
    ) -> Path:
        if scope == "global":
            return self.wiki_dir / "global" / TYPE_DIRS[node_type]
        if scope == "project" and project_id:
            project_dir = self._project_dir(project_id, project_locator)
            type_dir = project_dir / TYPE_DIRS[node_type]
            if type_dir.is_symlink():
                raise WikiStoreError(f"project path component is a symlink: {type_dir}")
            self._ensure_inside_projects(type_dir)
            return type_dir
        raise WikiStoreError("project_id is required for project scope")

    def delete(self, slug: str) -> Path:
        """Delete a page. Raises WikiStoreError when it does not exist."""
        try:
            path = self.get_path(slug)
        except _MissingWikiPage as exc:
            raise WikiStoreError(f"cannot delete, no wiki page for slug: {slug!r}") from exc
        path.unlink()
        return path

    def move(self, slug: str, new_type: str) -> WikiNode:
        """Move a page to a different node type directory."""
        if new_type not in TYPE_DIRS:
            raise WikiStoreError(f"unknown node type: {new_type!r}")
        node = self.read(slug)
        if node is None:
            raise WikiStoreError(f"cannot move, no wiki page for slug: {slug!r}")
        old_path = self.get_path(slug)
        node.type = new_type
        node.updated_at = utc_now_iso()
        destination = self.get_path(slug, new_type, scope=node.scope, project_id=node.project_id)
        self._atomic_write(destination, serialize_front_matter(node_front_matter(node), node.body))
        old_path.unlink()
        node.file_path = str(destination)
        return node

    def _new_slug(
        self,
        title: str,
        node_id: str,
        scope: str,
        project_id: str | None,
        project_locator: str | None = None,
    ) -> str:
        base = derive_slug(title, algo=self.slug_algo)
        if not base:
            base = (node_id or new_id())[:8]
        return unique_slug(base, self._taken_slugs(scope, project_id, project_locator))

    def _taken_slugs(
        self, scope: str, project_id: str | None, project_locator: str | None = None
    ) -> set[str]:
        if scope == "global":
            root = self.wiki_dir / "global"
        elif scope == "project" and project_id:
            root = self._project_dir(project_id, project_locator)
        else:
            return set(RESERVED_SLUGS)
        return {path.stem for path in root.rglob("*.md")} | set(RESERVED_SLUGS)

    def _project_dir(self, project_id: str, project_locator: str | None) -> Path:
        if project_locator is not None:
            projects = self._projects_dir()
            locator = self._validated_project_locator(project_locator)
            proposed = projects / locator
            self._reject_symlink(proposed)
            self._ensure_inside_projects(proposed)
            owner = self._project_id_in_dir(proposed)
            if owner is not None and owner != project_id:
                raise WikiStoreError(
                    f"project directory {locator!r} belongs to a different project_id"
                )
        existing = self._existing_project_dir(project_id)
        if existing is not None:
            return existing

        projects = self._projects_dir()
        locator = self._validated_project_locator(project_locator or project_id)
        project_dir = projects / locator
        self._reject_symlink(project_dir)
        self._ensure_inside_projects(project_dir)
        owner = self._project_id_in_dir(project_dir)
        if owner is not None and owner != project_id:
            raise WikiStoreError(f"project directory {locator!r} belongs to a different project_id")
        return project_dir

    def _existing_project_dir(self, project_id: str) -> Path | None:
        projects = self._projects_dir()
        if not projects.exists():
            return None
        matches: list[Path] = []
        for candidate in sorted(projects.iterdir()):
            if not candidate.is_dir():
                continue
            self._reject_symlink(candidate)
            self._ensure_inside_projects(candidate)
            if self._project_id_in_dir(candidate) == project_id:
                matches.append(candidate)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        self._reject_duplicate_project_keys(project_id, matches)
        legacy = projects / project_id
        if legacy in matches:
            return legacy
        names = ", ".join(path.name for path in matches)
        raise WikiStoreError(f"project_id {project_id!r} spans multiple directories: {names}")

    def _find_existing_path(
        self,
        slug: str,
        node_type: str | None = None,
        *,
        scope: str | None = None,
        project_id: str | None = None,
    ) -> Path | None:
        self._validate_page_slug(slug)
        matches: list[WikiNode] = []
        for path in sorted(self.wiki_dir.rglob(f"{slug}.md")):
            if path.stem != slug or path.parent.name not in TYPE_DIRS.values():
                continue
            if is_structural(path):
                continue  # generated navigation is not a memory page
            node = self.read_path(path)
            if node_type is not None and node.type != node_type:
                continue
            if scope is not None and node.scope != scope:
                continue
            if project_id is not None and node.project_id != project_id:
                continue
            matches.append(node)
        if not matches:
            return None
        self._reject_duplicate_namespace_keys(matches)
        if len(matches) > 1:
            keys = ", ".join(_format_namespace_key(_namespace_key(node)) for node in matches)
            raise WikiStoreError(f"ambiguous wiki page for slug {slug!r}: {keys}")
        file_path = matches[0].file_path
        if file_path is None:
            raise WikiStoreError(f"wiki page {slug!r} has no file path")
        return Path(file_path)

    def _reject_duplicate_project_keys(self, project_id: str, roots: PathList) -> None:
        nodes = [
            self.read_path(path)
            for root in roots
            for path in sorted(root.rglob("*.md"))
            if path.parent.name in TYPE_DIRS.values() and not is_structural(path)
        ]
        self._reject_duplicate_namespace_keys(
            [node for node in nodes if node.project_id == project_id]
        )

    def _reject_duplicate_namespace_keys(self, nodes: NodeList) -> None:
        seen: dict[NamespaceKey, WikiNode] = {}
        for node in nodes:
            key = _namespace_key(node)
            previous = seen.get(key)
            if previous is None:
                seen[key] = node
                continue
            raise WikiStoreError(
                "ambiguous wiki page namespace for "
                f"{_format_namespace_key(key)}: {previous.file_path}, {node.file_path}"
            )

    def _project_id_in_dir(self, project_dir: Path) -> str | None:
        if not project_dir.exists():
            return None
        project_ids: set[str] = set()
        for path in sorted(project_dir.rglob("*.md")):
            if is_structural(path):
                continue
            node = self.read_path(path)
            if node.scope == "project" and node.project_id:
                project_ids.add(node.project_id)
        if len(project_ids) > 1:
            raise WikiStoreError(f"project directory {project_dir.name!r} mixes project IDs")
        return next(iter(project_ids), None)

    def _projects_dir(self) -> Path:
        if self.wiki_dir.is_symlink():
            raise WikiStoreError(f"project path component is a symlink: {self.wiki_dir}")
        projects = self.wiki_dir / "projects"
        if projects.is_symlink():
            raise WikiStoreError(f"project path component is a symlink: {projects}")
        self._ensure_inside_data_dir(projects)
        return projects

    def _ensure_inside_projects(self, path: Path) -> None:
        self._ensure_inside(path, self._projects_dir(), "project path escapes docs/projects")

    def _ensure_inside_data_dir(self, path: Path) -> None:
        self._ensure_inside(path, self.data_dir, "wiki path escapes the data directory")

    def _ensure_inside(self, path: Path, root: Path, message: str) -> None:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise WikiStoreError(message) from exc

    def _reject_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise WikiStoreError(f"project path component is a symlink: {path}")

    def _validated_project_locator(self, locator: str) -> str:
        if _SAFE_COMPONENT.fullmatch(locator):
            return locator
        raise WikiStoreError(f"invalid project locator: {locator!r}")

    def read_path(self, path: Path) -> WikiNode:
        """Parse the page at one exact path inside the docs tree."""
        self._reject_unsafe_page_path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) else "not valid UTF-8"
            raise WikiStoreError(f"cannot read wiki page: {reason}") from exc
        try:
            data, body = parse_front_matter(text)
            node = _node_from_dict(data, body)
        except Exception as exc:
            raise WikiStoreError(f"{path.name}: {exc}") from exc
        node.slug = path.stem
        node.file_path = str(path)
        return node

    def _reject_unsafe_page_path(self, path: Path) -> None:
        if not path.is_relative_to(self.wiki_dir):
            raise WikiStoreError(f"wiki page path escapes docs: {path}")
        component = path
        while component != self.wiki_dir.parent:
            self._reject_symlink(component)
            component = component.parent
        self._ensure_inside(path, self.wiki_dir, "wiki page escapes docs")

    def _validate_page_slug(self, slug: str) -> None:
        if not _SAFE_COMPONENT.fullmatch(slug):
            raise WikiStoreError(f"invalid wiki slug: {slug!r}")

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise WikiStoreError(f"cannot write wiki page: {exc.strerror}") from exc


def _namespace_key(node: WikiNode) -> NamespaceKey:
    return (node.scope, node.project_id or "", node.type, node.slug)


def _format_namespace_key(key: NamespaceKey) -> str:
    scope, project_id, node_type, slug = key
    return f"scope={scope!r}, project_id={project_id!r}, type={node_type!r}, slug={slug!r}"


def node_front_matter(node: WikiNode) -> dict[str, object]:
    """Front matter in emission order: OKF v0.2 fields, then Memex fields."""
    return {
        "okf_version": OKF_VERSION,
        "type": node.type,
        "title": node.title,
        "description": node.description,
        "resource": node.resource,
        "tags": node.tags,
        "timestamp": node.timestamp,
        "valid_from": node.valid_from,
        "valid_until": node.valid_until,
        "stale_after": node.stale_after,
        "updated_at": node.updated_at,
        "parent": node.parent,
        "supersedes": node.supersedes,
        "implements": node.implements,
        "depends_on": node.depends_on,
        "links": [link.to_mapping() for link in node.links],
        "id": node.id,
        "created": node.created,
        "importance": node.importance,
        "access_count": node.access_count,
        "last_access": node.last_access,
        "content_hash": node.content_hash,
        "status": node.status,
        "occurred_at": node.occurred_at,
        "source": node.source,
        "harness": node.harness,
        "confidence": node.confidence,
        "scope": node.scope,
        "project_id": node.project_id,
        "project_label": node.project_label,
        "session_id": node.session_id,
        "transcript_ref": node.transcript_ref,
    }


def _str_value(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WikiStoreError(f"front matter field {key!r} must be a string or null")
    return value


def _require_str(data: dict[str, object], key: str) -> str:
    value = _str_value(data, key)
    if value is None:
        raise WikiStoreError(f"front matter field {key!r} is required")
    return value


def _list_value(data: dict[str, object], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WikiStoreError(f"front matter field {key!r} must be a list of strings")
    return value


def _status_value(data: dict[str, object]) -> str:
    value = data.get("status", "active")
    if isinstance(value, str) and value in PAGE_STATUSES:
        return value
    raise WikiStoreError(f"invalid page status: {value!r} (expected one of {PAGE_STATUSES})")


def _link_list(data: dict[str, object]) -> list[SemanticLink]:
    """Typed links from front matter, validated one entry at a time."""
    value = data.get("links", [])
    if not isinstance(value, list):
        raise WikiStoreError("front matter field 'links' must be a list")
    links: list[SemanticLink] = []
    for entry in value:
        try:
            links.append(SemanticLink.parse(entry))
        except ValueError as exc:
            raise WikiStoreError(f"front matter field 'links': {exc}") from exc
    return links


def _okf_version(data: dict[str, object]) -> None:
    value = data.get("okf_version")
    if value != OKF_VERSION:
        raise WikiStoreError(f"unsupported okf_version: {value!r} (expected {OKF_VERSION!r})")


def _node_from_dict(data: dict[str, object], body: str) -> WikiNode:
    unknown = set(data) - set(_FRONT_MATTER_KEYS)
    if unknown:
        raise WikiStoreError(f"unknown front matter keys: {sorted(unknown)}")
    _okf_version(data)
    node_type = data.get("type")
    if not isinstance(node_type, str) or node_type not in NODE_TYPES:
        raise WikiStoreError(f"invalid node type: {node_type!r}")
    importance = data.get("importance", 0.5)
    if not isinstance(importance, int | float):
        raise WikiStoreError("front matter field 'importance' must be numeric")
    access_count = data.get("access_count", 0)
    if not isinstance(access_count, int):
        raise WikiStoreError("front matter field 'access_count' must be an integer")
    return WikiNode(
        type=node_type,
        title=_require_str(data, "title"),
        body=body,
        id=_require_str(data, "id"),
        description=_str_value(data, "description") or "",
        resource=_str_value(data, "resource"),
        tags=_list_value(data, "tags"),
        timestamp=_require_str(data, "timestamp"),
        valid_from=_str_value(data, "valid_from"),
        valid_until=_str_value(data, "valid_until"),
        stale_after=_str_value(data, "stale_after"),
        updated_at=_require_str(data, "updated_at"),
        parent=_str_value(data, "parent"),
        supersedes=_list_value(data, "supersedes"),
        implements=_list_value(data, "implements"),
        depends_on=_list_value(data, "depends_on"),
        links=_link_list(data),
        created=_require_str(data, "created"),
        importance=float(importance),
        access_count=access_count,
        last_access=_str_value(data, "last_access"),
        content_hash=_str_value(data, "content_hash") or "",
        status=_status_value(data),
        occurred_at=_str_value(data, "occurred_at"),
        source=_str_value(data, "source"),
        harness=_str_value(data, "harness"),
        confidence=_str_value(data, "confidence"),
        scope=_str_value(data, "scope") or "global",
        project_id=_str_value(data, "project_id"),
        project_label=_str_value(data, "project_label"),
        session_id=_str_value(data, "session_id"),
        transcript_ref=_str_value(data, "transcript_ref"),
    )
