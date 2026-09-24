"""JSON import/export of wiki nodes (spec §7 Utility 15, §5.5)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from memex.domain.errors import IndexManagerError, WikiStoreError
from memex.domain.models import SemanticLink, WikiNode, utc_now_iso
from memex.domain.reserved import RESERVED_SLUGS
from memex.domain.scrub import scrub
from memex.domain.types import validate_type_name
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.search.link_manager import LinkManager
from memex.infrastructure.store.wiki_store import WikiStore

logger = logging.getLogger("memex")

EXPORT_VERSION = "1.0"


class ImportExport:
    """Export every wiki node as a JSON document; import them back."""

    def __init__(
        self,
        wiki_store: WikiStore,
        index_mgr: IndexManager,
        link_mgr: LinkManager,
        *,
        on_page_written: Callable[[Path], None] | None = None,
    ) -> None:
        self._store = wiki_store
        self._index = index_mgr
        self._links = link_mgr
        self._on_page_written = on_page_written

    def export(self, output_path: Path | None = None) -> dict[str, object]:
        document: dict[str, object] = {
            "version": EXPORT_VERSION,
            "exported_at": utc_now_iso(),
            "types": [
                {
                    "scope": "project",
                    "project_id": project_id,
                    "name": declaration.name,
                    "kind": declaration.kind,
                    "description": declaration.description,
                }
                for project_id, declaration in self._store.project_declarations()[0]
            ],
            "nodes": [self._node_to_json(node) for node in self._store.list()],
        }
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return document

    def import_file(self, input_path: Path) -> dict[str, object]:
        try:
            data = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read import file: {exc}") from exc
        return self.import_data(data)

    def import_data(self, data: dict[str, object]) -> dict[str, object]:
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError("import document must contain a 'nodes' array")
        imported = 0
        skipped: list[str] = []
        errors: list[str] = []
        type_entries = data.get("types")
        for entry in type_entries if isinstance(type_entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            project_id = str(entry.get("project_id", ""))
            try:
                validate_type_name(name)
            except ValueError:
                errors.append(f"type {name!r}: invalid name")
                continue
            try:
                if name in self._store.declared_types(scope="project", project_id=project_id):
                    continue
                self._store.declare_type(
                    name,
                    scope="project",
                    project_id=project_id,
                    description=str(entry.get("description", "")),
                    actor="import",
                    draft=entry.get("kind") == "draft",
                )
            except WikiStoreError as exc:
                errors.append(f"type {name!r}: {exc}")
        for item in nodes:
            if not isinstance(item, dict):
                errors.append("non-object node entry skipped")
                continue
            try:
                node = self._node_from_json(item)
            except (ValueError, WikiStoreError) as exc:
                raw_slug = item.get("slug")
                prefix = f"{raw_slug}: " if isinstance(raw_slug, str) and raw_slug else ""
                errors.append(f"{prefix}{exc}")
                continue
            slug = node.slug
            if not slug:
                skipped.append(str(item.get("title", "<untitled>")))
                continue
            try:
                stored = self._store.write(node)
                self._index.update_record(stored)
                self._links.sync_node(stored)
            except (ValueError, WikiStoreError, IndexManagerError) as exc:
                errors.append(f"{slug}: {exc}")
                continue
            if self._on_page_written is not None and stored.file_path:
                self._on_page_written(Path(stored.file_path))
            imported += 1
        logger.info("operation=import imported=%d errors=%d", imported, len(errors))
        return {"imported": imported, "skipped": skipped, "errors": errors}

    @staticmethod
    def _node_to_json(node: WikiNode) -> dict[str, object]:
        return {
            "slug": node.slug,
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
            "created": node.created,
            "importance": node.importance,
            "status": node.status,
            "body": node.body,
            "transcript_ref": node.transcript_ref,
            "scope": node.scope,
            "project_id": node.project_id,
            "project_label": node.project_label,
        }

    def _node_from_json(self, item: dict[str, object]) -> WikiNode:
        node_type = item.get("type")
        title = item.get("title")
        if not isinstance(node_type, str):
            raise ValueError(f"invalid node type: {node_type!r}")
        try:
            validate_type_name(node_type)
        except ValueError as exc:
            raise ValueError(f"invalid node type: {node_type!r}") from exc
        if not isinstance(title, str) or not title.strip():
            raise ValueError("node title must be a non-empty string")
        importance = item.get("importance", 0.5)
        if not isinstance(importance, int | float):
            raise ValueError("importance must be numeric")
        slug = item.get("slug")
        if isinstance(slug, str) and slug in RESERVED_SLUGS:
            raise ValueError(f"reserved slug {slug!r} cannot be imported; rename the page first")
        description = item.get("description")
        transcript_ref = item.get("transcript_ref")
        scope = item.get("scope", "global")
        project_id = item.get("project_id")
        project_label = item.get("project_label")
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string or null")
        if scope not in {"global", "project"}:
            raise ValueError("scope must be 'global' or 'project'")
        if scope == "project" and not isinstance(project_id, str):
            raise ValueError("project_id is required for project scope")
        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("project_id must be a string or null")
        if project_label is not None and not isinstance(project_label, str):
            raise ValueError("project_label must be a string or null")
        return WikiNode(
            type=node_type,
            title=title,
            # Descriptions are scrubbed at this persisting boundary like the
            # facade write path; bodies keep their pre-existing behavior.
            description=scrub(description)[0] if description else "",
            body=self._str(item, "body"),
            id=self._str(item, "id"),
            slug=str(slug) if isinstance(slug, str) and slug else "",
            tags=self._str_list(item, "tags"),
            importance=float(importance),
            resource=self._str(item, "resource") or None,
            created=self._str(item, "created") or utc_now_iso(),
            timestamp=self._str(item, "timestamp") or utc_now_iso(),
            updated_at=self._str(item, "updated_at") or utc_now_iso(),
            valid_from=self._str(item, "valid_from") or None,
            valid_until=self._str(item, "valid_until") or None,
            stale_after=self._str(item, "stale_after") or None,
            parent=self._str(item, "parent") or None,
            supersedes=self._str_list(item, "supersedes"),
            implements=self._str_list(item, "implements"),
            depends_on=self._str_list(item, "depends_on"),
            transcript_ref=transcript_ref if isinstance(transcript_ref, str) else None,
            links=self._link_list(item),
            scope=str(scope),
            project_id=project_id,
            project_label=project_label,
        )

    @staticmethod
    def _str(item: dict[str, object], key: str) -> str:
        value = item.get(key)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _link_list(item: dict[str, object]) -> list[SemanticLink]:
        """Typed links from an export entry; each entry is validated."""
        value = item.get("links", [])
        if not isinstance(value, list):
            raise ValueError("node field 'links' must be a list")
        return [SemanticLink.parse(entry) for entry in value]

    @staticmethod
    def _str_list(item: dict[str, object], key: str) -> list[str]:
        value = item.get(key, [])
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            raise ValueError(f"node field {key!r} must be a list of strings")
        return value
