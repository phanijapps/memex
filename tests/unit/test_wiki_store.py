from pathlib import Path

import pytest

from memex.domain.errors import WikiStoreError
from memex.domain.models import WikiNode
from memex.infrastructure.store.wiki_store import WikiStore, hash_body


def make_node(**overrides: object) -> WikiNode:
    fields: dict[str, object] = {
        "type": "entity",
        "title": "Ruff linter",
        "body": "Ruff is a fast Python linter.",
        "id": "id-0001",
    }
    fields.update(overrides)
    return WikiNode(**fields)  # type: ignore[arg-type]


class TestWriteRead:
    def test_write_assigns_slug_id_and_path(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        stored = store.write(make_node(id=""))
        assert stored.slug == "ruff-linter"
        assert stored.id
        assert stored.file_path == str(data_dir / "docs/global/entities/ruff-linter.md")
        assert stored.content_hash == hash_body(stored.body)

    def test_description_round_trips_through_front_matter(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node(description="What this page holds and when to use it"))
        page = data_dir / "docs/global/entities/ruff-linter.md"
        text = page.read_text(encoding="utf-8")
        assert 'description: "What this page holds and when to use it"' in text

        reread = store.read("ruff-linter")
        assert reread is not None
        assert reread.description == "What this page holds and when to use it"
        assert reread.body == "Ruff is a fast Python linter."

        # A direct Markdown edit of only the description survives read-back.
        page.write_text(
            text.replace(
                'description: "What this page holds and when to use it"',
                'description: "Hand-edited signpost"',
            ),
            encoding="utf-8",
        )
        edited = store.read("ruff-linter")
        assert edited is not None
        assert edited.description == "Hand-edited signpost"
        assert edited.body == "Ruff is a fast Python linter."

    def test_page_without_description_reads_as_empty(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node(description="temporary"))
        page = data_dir / "docs/global/entities/ruff-linter.md"
        text = page.read_text(encoding="utf-8")
        page.write_text(text.replace('description: "temporary"\n', ""), encoding="utf-8")

        reread = store.read("ruff-linter")
        assert reread is not None
        assert reread.description == ""

    def test_update_preserves_stored_fields(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        stored = store.write(make_node(id="other", slug="ruff-linter", body="updated body"))
        assert stored.id == "id-0001"
        assert stored.body == "updated body"
        assert stored.slug == "ruff-linter"
        assert store.list() and len(store.list()) == 1

    def test_project_pages_are_isolated_by_project_namespace(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        first = store.write(
            make_node(
                id="",
                scope="project",
                project_id="a" * 24,
                project_label="One",
                project_locator="git-memex",
            )
        )
        second = store.write(
            make_node(
                id="",
                scope="project",
                project_id="b" * 24,
                project_label="Two",
                project_locator="git-other",
            )
        )

        assert first.slug == second.slug == "ruff-linter"
        assert first.file_path == str(data_dir / "docs/projects/git-memex/entities/ruff-linter.md")
        assert second.file_path == str(data_dir / "docs/projects/git-other/entities/ruff-linter.md")
        assert f'project_id: "{"a" * 24}"' in Path(first.file_path).read_text(encoding="utf-8")

    def test_project_write_without_locator_uses_existing_directory(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        first = store.write(
            make_node(
                id="",
                scope="project",
                project_id="a" * 24,
                project_label="One",
                project_locator="git-memex",
            )
        )
        second = store.write(
            make_node(
                id="",
                title="Second",
                scope="project",
                project_id="a" * 24,
                project_label="One",
            )
        )

        assert first.file_path is not None
        assert second.file_path == str(data_dir / "docs/projects/git-memex/entities/second.md")

    def test_project_write_without_existing_directory_falls_back_to_id(
        self, data_dir: Path
    ) -> None:
        store = WikiStore(data_dir)

        stored = store.write(
            make_node(id="", scope="project", project_id="a" * 24, project_label="One")
        )

        assert stored.file_path == str(
            data_dir / "docs/projects" / ("a" * 24) / "entities/ruff-linter.md"
        )

    def test_project_read_can_select_duplicate_slug_by_project_id(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        first_project_id = "a" * 24
        second_project_id = "b" * 24
        first = store.write(
            make_node(
                id="",
                scope="project",
                project_id=first_project_id,
                project_label="One",
                project_locator="git-memex",
            )
        )
        second = store.write(
            make_node(
                id="",
                scope="project",
                project_id=second_project_id,
                project_label="Two",
                project_locator="git-other",
            )
        )

        assert first.slug == second.slug == "ruff-linter"
        read_back = store.read(
            "ruff-linter", "entity", scope="project", project_id=second_project_id
        )
        assert read_back is not None and read_back.file_path == second.file_path

    def test_duplicate_same_project_key_refuses_lookup_and_scan(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        project_id = "a" * 24
        stored = store.write(
            make_node(
                id="",
                scope="project",
                project_id=project_id,
                project_label="One",
                project_locator="git-memex",
            )
        )
        duplicate = data_dir / "docs/projects" / project_id / "entities/ruff-linter.md"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_text(Path(stored.file_path or "").read_text(encoding="utf-8"))

        with pytest.raises(WikiStoreError, match="ambiguous wiki page namespace"):
            store.read("ruff-linter", "entity", scope="project", project_id=project_id)
        with pytest.raises(WikiStoreError, match="ambiguous wiki page namespace"):
            store.scan_all([])

    def test_readable_project_directory_rejects_different_project_id(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(
            make_node(
                id="",
                scope="project",
                project_id="a" * 24,
                project_label="One",
                project_locator="git-memex",
            )
        )

        with pytest.raises(WikiStoreError, match="belongs to a different project"):
            store.write(
                make_node(
                    id="",
                    scope="project",
                    project_id="b" * 24,
                    project_label="Two",
                    project_locator="git-memex",
                )
            )

        assert not (data_dir / "docs/projects/git-memex/entities/ruff-linter-2.md").exists()

    def test_legacy_project_refuses_owned_proposed_locator(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        legacy_id = "a" * 24
        store.write(make_node(id="", scope="project", project_id=legacy_id))
        store.write(
            make_node(
                id="",
                scope="project",
                project_id="b" * 24,
                project_locator="git-memex",
            )
        )

        with pytest.raises(WikiStoreError, match="belongs to a different project"):
            store.write(
                make_node(
                    id="",
                    title="Another page",
                    scope="project",
                    project_id=legacy_id,
                    project_locator="git-memex",
                )
            )

        assert not (data_dir / "docs/projects" / legacy_id / "entities/another-page.md").exists()

    def test_scoped_get_path_without_type_honors_project_id(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node(id="", scope="project", project_id="a" * 24))
        second = store.write(make_node(id="", scope="project", project_id="b" * 24))

        assert store.get_path("ruff-linter", scope="project", project_id="b" * 24) == Path(
            second.file_path or ""
        )

    def test_symlinked_project_page_is_not_read_or_scanned(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        store = WikiStore(data_dir)
        stored = store.write(make_node(id="", scope="project", project_id="a" * 24))
        outside = tmp_path / "outside.md"
        outside.write_text(Path(stored.file_path or "").read_text(encoding="utf-8"))
        page = Path(stored.file_path or "")
        page.unlink()
        page.symlink_to(outside)

        with pytest.raises(WikiStoreError, match="symlink"):
            store.read("ruff-linter", scope="project", project_id="a" * 24)
        with pytest.raises(WikiStoreError, match="symlink"):
            store.scan_all()

    def test_readable_project_directory_rejects_unsafe_locator(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)

        with pytest.raises(WikiStoreError, match="project locator"):
            store.write(
                make_node(
                    id="",
                    scope="project",
                    project_id="a" * 24,
                    project_label="One",
                    project_locator="../escape",
                )
            )

    def test_readable_project_directory_rejects_symlinked_projects_dir(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        store = WikiStore(data_dir)
        outside = tmp_path / "outside"
        outside.mkdir()
        projects = data_dir / "docs/projects"
        projects.parent.mkdir(parents=True, exist_ok=True)
        projects.symlink_to(outside, target_is_directory=True)

        with pytest.raises(WikiStoreError, match="symlink"):
            store.write(
                make_node(
                    id="",
                    scope="project",
                    project_id="a" * 24,
                    project_label="One",
                    project_locator="git-memex",
                )
            )

    def test_initialization_rejects_symlinked_docs_without_writing(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        data_dir.mkdir(parents=True)
        (data_dir / "docs").symlink_to(outside, target_is_directory=True)

        with pytest.raises(WikiStoreError, match="symlink"):
            WikiStore(data_dir)

        assert list(outside.iterdir()) == []

    def test_initialization_rejects_symlinked_global_without_writing(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        global_dir = data_dir / "docs/global"
        global_dir.parent.mkdir(parents=True)
        global_dir.symlink_to(outside, target_is_directory=True)

        with pytest.raises(WikiStoreError, match="symlink"):
            WikiStore(data_dir)

        assert list(outside.iterdir()) == []

    def test_readable_project_directory_rejects_project_dir_symlink_escape(
        self, data_dir: Path, tmp_path: Path
    ) -> None:
        store = WikiStore(data_dir)
        outside = tmp_path / "outside"
        outside.mkdir()
        project_dir = data_dir / "docs/projects/git-memex"
        project_dir.parent.mkdir(parents=True, exist_ok=True)
        project_dir.symlink_to(outside, target_is_directory=True)

        with pytest.raises(WikiStoreError, match="symlink"):
            store.write(
                make_node(
                    id="",
                    scope="project",
                    project_id="a" * 24,
                    project_label="One",
                    project_locator="git-memex",
                )
            )

    def test_read_missing_returns_none(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        assert store.read("nope") is None

    def test_malformed_page_raises(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        bad = data_dir / "docs/global/entities/broken.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("not front matter at all\n", encoding="utf-8")
        with pytest.raises(WikiStoreError):
            store.read("broken")


class TestDeleteMove:
    def test_delete_removes_file(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        store.delete("ruff-linter")
        assert not store.exists("ruff-linter")

    def test_delete_missing_raises(self, data_dir: Path) -> None:
        with pytest.raises(WikiStoreError, match="cannot delete"):
            WikiStore(data_dir).delete("ghost")

    def test_move_changes_type_dir(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        node = store.move("ruff-linter", "preference")
        assert node.file_path is not None
        assert node.file_path.endswith("docs/global/preferences/ruff-linter.md")
        assert store.read("ruff-linter") is not None

    def test_move_keeps_project_namespace(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node(scope="project", project_id="a" * 24, project_label="One"))
        node = store.move("ruff-linter", "preference")
        assert node.file_path is not None
        assert node.file_path.endswith(f"docs/projects/{'a' * 24}/preferences/ruff-linter.md")


class TestGuardrails:
    def test_glob_slug_cannot_select_or_delete_a_page(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        stored = store.write(make_node())

        with pytest.raises(WikiStoreError, match="invalid wiki slug"):
            store.read("*")
        with pytest.raises(WikiStoreError, match="invalid wiki slug"):
            store.delete("*")

        assert Path(stored.file_path or "").exists()

    def test_get_path_unknown_type(self, data_dir: Path) -> None:
        with pytest.raises(WikiStoreError, match="undeclared type"):
            WikiStore(data_dir).get_path("x", "folder")

    def test_list_bad_shape_type_rejected(self, data_dir: Path) -> None:
        with pytest.raises(WikiStoreError, match=r"\[a-z\]"):
            WikiStore(data_dir).list("Folder")

    def test_list_undeclared_type_matches_no_pages(self, data_dir: Path) -> None:
        # list spans every scope and project; a declaration is a per-project
        # fact, so a shape-valid but undeclared type name matches nothing.
        assert WikiStore(data_dir).list("folder") == []

    def test_move_missing_slug(self, data_dir: Path) -> None:
        with pytest.raises(WikiStoreError, match="cannot move"):
            WikiStore(data_dir).move("ghost", "entity")

    def test_move_unknown_type(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        with pytest.raises(WikiStoreError, match="undeclared type"):
            store.move("ruff-linter", "folder")

    def test_write_unknown_type(self, data_dir: Path) -> None:
        with pytest.raises(ValueError, match="type"):
            WikiStore(data_dir).write(make_node(type="folder"))


class TestListScan:
    def test_list_filters_by_type(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node(title="Tool X"))
        store.write(make_node(id="id-2", title="Prefer dark mode", type="preference"))
        assert [node.title for node in store.list("entity")] == ["Tool X"]
        assert {node.type for node in store.list()} == {"entity", "preference"}

    def test_scan_all_collects_errors(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        bad = data_dir / "docs/global/entities/broken.md"
        bad.write_text("garbage\n", encoding="utf-8")
        errors: list[str] = []
        nodes = store.scan_all(errors)
        assert len(nodes) == 1
        assert len(errors) == 1

    def test_atomic_write_leaves_no_temp_files(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        store.write(make_node())
        leftovers = list((data_dir / "docs/entities").glob("*.tmp"))
        assert leftovers == []


class TestFrontMatterValidation:
    def write_raw(self, data_dir: Path, text: str) -> None:
        target = data_dir / "docs/global/entities/raw.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_unknown_key_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            'importance: 0.5\nmystery: "x"\n---\nbody',
        )
        with pytest.raises(WikiStoreError, match="unknown front matter"):
            WikiStore(data_dir).read("raw")

    def test_missing_required_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n---\nbody',
        )
        with pytest.raises(WikiStoreError, match="required"):
            WikiStore(data_dir).read("raw")

    def test_missing_okf_version_rejected(self, data_dir: Path) -> None:
        """A page without the format marker is not an OKF v0.2 concept."""
        self.write_raw(data_dir, '---\nid: "a"\ntype: "entity"\ntitle: "t"\n---\nbody')
        with pytest.raises(WikiStoreError, match="unsupported okf_version"):
            WikiStore(data_dir).read("raw")

    def test_non_numeric_importance_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            'importance: "high"\n---\nbody',
        )
        with pytest.raises(WikiStoreError, match="importance"):
            WikiStore(data_dir).read("raw")

    def test_non_int_access_count_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            'importance: 0.5\naccess_count: "many"\n---\nbody',
        )
        with pytest.raises(WikiStoreError, match="access_count"):
            WikiStore(data_dir).read("raw")

    def test_non_list_tags_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            'importance: 0.5\ntags: "tool"\n---\nbody',
        )
        with pytest.raises(WikiStoreError, match="tags"):
            WikiStore(data_dir).read("raw")

    def test_non_string_list_items_rejected(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            "importance: 0.5\nlinks: [1, 2]\n---\nbody",
        )
        with pytest.raises(WikiStoreError, match="links"):
            WikiStore(data_dir).read("raw")

    def test_invalid_type_rejected(self, data_dir: Path) -> None:
        # Membership (built-in vs. declared) is the store's job at write time;
        # a stored page's type is only checked for shape on read (Task 3).
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "Folder"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            "importance: 0.5\n---\nbody",
        )
        with pytest.raises(WikiStoreError, match="node type"):
            WikiStore(data_dir).read("raw")

    def test_string_field_must_be_string(self, data_dir: Path) -> None:
        self.write_raw(
            data_dir,
            '---\nokf_version: "0.2"\nid: "a"\ntype: "entity"\ntitle: "t"\n'
            'created: "2026-01-01T00:00:00Z"\ntimestamp: "2026-01-01T00:00:00Z"\n'
            'updated_at: "2026-01-01T00:00:00Z"\n'
            "importance: 0.5\nlast_access: 5\n---\nbody",
        )
        with pytest.raises(WikiStoreError, match="last_access"):
            WikiStore(data_dir).read("raw")


class TestInvalidStoredDescriptionIsAMalformedPage:
    """A hand-edited invalid description reports as a scan error, not a crash."""

    def test_overlong_stored_description_reports_as_error(self, data_dir: Path) -> None:
        store = WikiStore(data_dir)
        node = WikiNode(
            type="entity",
            title="Edit me",
            body="body",
            id="id-edit-me",
        )
        stored = store.write(node)
        path = Path(stored.file_path or "")
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('title: "Edit me"', f'title: "Edit me"\ndescription: "{"x" * 513}"'),
            encoding="utf-8",
        )
        errors: list[str] = []
        nodes = store.scan_all(errors)
        assert [n.slug for n in nodes] == []
        assert len(errors) == 1 and "description" in errors[0]
