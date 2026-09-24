"""Project-level concept types (docs/specs/project-concept-types/spec.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.application.decay import RecencyDecay
from memex.domain import types as T
from memex.domain.errors import WikiStoreError
from memex.domain.models import TaskRecallInput, WikiNode, WriteInput
from memex.domain.types import TYPE_DIRS
from memex.infrastructure.store.wiki_store import WikiStore


class TestTypeNames:
    @pytest.mark.parametrize("name", ["decision", "access-matrix", "a", "x" * 64])
    def test_valid_shape_returns_name(self, name: str) -> None:
        assert T.validate_type_name(name) == name

    @pytest.mark.parametrize(
        "name", ["Decision", "access matrix", "-lead", "9lives", "x" * 65, "", "a_b"]
    )
    def test_bad_shape_is_rejected_not_normalized(self, name: str) -> None:
        # Review Focus 3: never lowercase or strip silently.
        with pytest.raises(ValueError, match=r"\[a-z\]\[a-z0-9-\]\{0,63\}"):
            T.validate_type_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "entity",
            "entities",
            "episode",
            "episodes",
            "index",
            "log",
            "rule",
            "decision",
            "subdirectories",
        ],
    )
    def test_collisions_with_builtin_catalogue_and_reserved(self, name: str) -> None:
        # Review Focus 4: a custom type may never shadow a built-in name, a
        # built-in directory, a catalogue name, a reserved filename, or the
        # generated "Subdirectories" navigation heading.
        with pytest.raises(ValueError, match="already"):
            T.validate_type_name(name, custom=True)


class TestKinds:
    @pytest.mark.parametrize("name", ["episode", "summary"])
    def test_memory_types(self, name: str) -> None:
        assert T.type_kind(name) == "memory"

    @pytest.mark.parametrize(
        "name", ["entity", "procedure", "preference", "rule", "policy", "access-matrix"]
    )
    def test_knowledge_types(self, name: str) -> None:
        assert T.type_kind(name) == "knowledge"

    def test_catalogue_never_decays_builtins_do(self) -> None:
        assert all(not T.decays(name) for name in T.CATALOGUE)
        assert T.decays("entity") and T.decays("episode")
        assert T.decays("access-matrix")  # custom shelf types keep memory aging


class TestDirectoriesAndHeadings:
    def test_builtins_keep_plural_directories(self) -> None:
        assert T.type_directory("entity") == "entities"
        assert T.type_for_directory("entities") == "entity"

    def test_declared_types_are_their_own_directory(self) -> None:
        assert T.type_directory("decision") == "decision"
        assert T.type_for_directory("access-matrix") == "access-matrix"

    def test_headings_round_trip(self) -> None:
        for name in ["entity", "episode", "decision", "access-matrix"]:
            assert T.type_for_heading(T.heading_for(name)) == name
        assert T.heading_for("access-matrix") == "Access-matrix"

    def test_type_directory_names(self) -> None:
        assert T.is_type_directory_name("entities") and T.is_type_directory_name("access-matrix")
        assert not T.is_type_directory_name("index") and not T.is_type_directory_name("Scratch")


class TestInitialStatus:
    @pytest.mark.parametrize("source", [None, "user", "agent", "transcript", "consolidation"])
    def test_memory_is_always_active(self, source: str | None) -> None:
        assert T.initial_status("episode", source, "manual") == "active"
        assert T.initial_status("summary", source, "auto") == "active"

    def test_person_written_knowledge_is_active(self) -> None:
        assert T.initial_status("rule", "user", "manual") == "active"

    @pytest.mark.parametrize("source", ["agent", "transcript", "consolidation", None])
    def test_model_written_knowledge_is_pending_under_manual(self, source: str | None) -> None:
        assert T.initial_status("rule", source, "manual") == "pending"

    def test_auto_makes_model_knowledge_active(self) -> None:
        assert T.initial_status("rule", "consolidation", "auto") == "active"

    def test_unknown_policy_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"manual|auto"):
            T.initial_status("rule", "user", "sometimes")


PROJECT = "a" * 24


class TestModelsAcceptShapeValidTypes:
    def test_write_input_accepts_a_declared_looking_type(self) -> None:
        node = WriteInput(
            type="decision", title="Choose X", body="b", scope="project", project_id=PROJECT
        )
        assert node.type == "decision"

    def test_wiki_node_accepts_a_custom_type(self) -> None:
        node = WikiNode(
            type="access-matrix", title="t", body="b", id="1", scope="project", project_id=PROJECT
        )
        assert node.type == "access-matrix"

    def test_bad_shape_still_rejected_at_the_model(self) -> None:
        with pytest.raises(ValueError, match=r"\[a-z\]"):
            WriteInput(type="Decision", title="t", body="b")

    def test_task_recall_filter_accepts_declared_types(self) -> None:
        request = TaskRecallInput(goal="g", questions=["q"], project_id=PROJECT, node_type="rule")
        assert request.node_type == "rule"


PROJECT_B = "b" * 24


def _store(data_dir: Path) -> WikiStore:
    return WikiStore(data_dir)


def _page(
    store: WikiStore, node_type: str, title: str, *, project_id: str = PROJECT, **kw: object
) -> WikiNode:
    return store.write(
        WikiNode(
            type=node_type,
            title=title,
            body="b",
            id="",
            scope="project",
            project_id=project_id,
            **kw,  # type: ignore[arg-type]
        )
    )


class TestDeclaration:
    def test_enable_catalogue_type_creates_directory_and_log(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type(
            "decision", scope="project", project_id=PROJECT, description="Why we chose."
        )
        assert declared.kind == "catalogue" and declared.directory.is_dir()
        log = (declared.directory / "log.md").read_text()
        assert " declare decision by user: Why we chose." in log
        assert not log.startswith("---")  # body-only, so it stays structural

    def test_add_custom_type(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type("access-matrix", scope="project", project_id=PROJECT)
        assert declared.kind == "custom"
        assert store.declared_types(scope="project", project_id=PROJECT)["access-matrix"].pages == 0

    def test_list_shows_builtins_catalogue_custom_and_counts(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("rule", scope="project", project_id=PROJECT)
        _page(store, "rule", "Never force push")
        _page(store, "entity", "Kafka")
        types = store.declared_types(scope="project", project_id=PROJECT)
        assert types["entity"].kind == "builtin" and types["entity"].pages == 1
        assert types["rule"].kind == "catalogue" and types["rule"].pages == 1
        assert set(types) >= {"entity", "preference", "procedure", "summary", "episode", "rule"}

    def test_draft_declaration_is_marked_and_logged(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type(
            "story-map",
            scope="project",
            project_id=PROJECT,
            actor="consolidation",
            draft=True,
            description="sessions=s1,s2 pages=3",
        )
        assert declared.kind == "draft"
        assert (
            " propose story-map by consolidation: sessions=s1,s2 pages=3"
            in (declared.directory / "log.md").read_text()
        )

    def test_stray_directory_without_log_is_refused(self, data_dir: Path) -> None:
        # Review Focus 1.
        store = _store(data_dir)
        _page(store, "entity", "Seed")  # creates the project directory
        project_dir = store.get_path("seed").parent.parent
        (project_dir / "scratch").mkdir()
        with pytest.raises(WikiStoreError, match="exists without a declaration"):
            store.declare_type("scratch", scope="project", project_id=PROJECT)
        assert "scratch" not in store.declared_types(scope="project", project_id=PROJECT)

    def test_scope_root_containers_are_not_type_directories(self, data_dir: Path) -> None:
        # Regression: docs/global and docs/projects are scope-root
        # containers, not type directories, even though each is a real
        # directory on disk — the flat-layout allowance for pre-scoping
        # data must not swallow them.
        store = _store(data_dir)
        _page(store, "entity", "Seed")  # creates docs/projects/<id>/...
        assert store._is_type_dir(store.wiki_dir / "global") is False
        assert store._is_type_dir(store.wiki_dir / "projects") is False
        (store.wiki_dir / "entities").mkdir()
        assert store._is_type_dir(store.wiki_dir / "entities") is True

    def test_global_scope_refuses_non_builtin(self, data_dir: Path) -> None:
        # Review Focus 5.
        with pytest.raises(WikiStoreError, match="global scope"):
            _store(data_dir).declare_type("rule", scope="global", project_id=None)

    def test_declare_type_refuses_the_subdirectories_heading(self, data_dir: Path) -> None:
        # A custom type named "subdirectories" would render the same
        # "## Subdirectories" heading the generator uses for child links.
        store = _store(data_dir)
        with pytest.raises(WikiStoreError, match="already"):
            store.declare_type("subdirectories", scope="project", project_id=PROJECT)


class TestWritesRespectDeclarations:
    def test_write_to_enabled_type_lands_in_its_directory(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        node = _page(store, "decision", "Choose Kafka")
        assert Path(node.file_path or "").parent.name == "decision"
        read = store.read("choose-kafka")
        assert read is not None and read.type == "decision"

    def test_undeclared_type_is_rejected_before_writing(self, data_dir: Path) -> None:
        store = _store(data_dir)
        with pytest.raises(WikiStoreError, match=r"undeclared type 'decision'.*memex types"):
            _page(store, "decision", "Choose Kafka")
        assert not list((data_dir / "docs").rglob("choose-kafka.md"))

    def test_declaration_is_per_project(self, data_dir: Path) -> None:
        # Review Focus 2.
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        with pytest.raises(WikiStoreError, match="undeclared type"):
            _page(store, "decision", "Elsewhere", project_id=PROJECT_B)

    def test_scan_move_and_find_cover_declared_directories(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        store.declare_type("access-matrix", scope="project", project_id=PROJECT)
        _page(store, "decision", "Choose Kafka")
        assert [n.slug for n in store.scan_all()] == ["choose-kafka"]
        moved = store.move("choose-kafka", "access-matrix")
        assert Path(moved.file_path or "").parent.name == "access-matrix"
        assert store.read("choose-kafka") is not None
        with pytest.raises(WikiStoreError, match="undeclared type"):
            store.move("choose-kafka", "policy")

    def test_five_type_store_is_unchanged(self, data_dir: Path) -> None:
        store = _store(data_dir)
        node = store.write(WikiNode(type="entity", title="Global thing", body="b", id=""))
        assert Path(node.file_path or "").parts[-3:-1] == ("global", "entities")
        assert store.declared_types(scope="global", project_id=None).keys() == set(TYPE_DIRS)


def test_catalogue_pages_never_decay_builtins_do(data_dir: Path) -> None:
    store = _store(data_dir)
    store.declare_type("policy", scope="project", project_id=PROJECT)
    old = "2020-01-01T00:00:00Z"
    _page(store, "policy", "Prefer boring tech", importance=1.0, created=old, last_access=old)
    _page(store, "entity", "Kafka", importance=1.0, created=old, last_access=old)
    changes = RecencyDecay(half_life_days=30).apply_decay(store)
    assert [slug for slug, _old, _new in changes] == ["kafka"]
    policy = store.read("prefer-boring-tech")
    assert policy is not None and policy.importance == 1.0
