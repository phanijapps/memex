"""Project-level concept types (docs/specs/project-concept-types/spec.md)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from memex.application.decay import RecencyDecay
from memex.application.memory import Memex
from memex.application.ports import LLMResponse
from memex.application.verify import VerifyReport, verify
from memex.domain import types as T
from memex.domain.errors import WikiStoreError
from memex.domain.models import ConsolidateInput, TaskRecallInput, WikiNode, WriteInput
from memex.domain.types import TYPE_DIRS
from memex.infrastructure.config import GovernanceConfig, MemexConfig
from memex.infrastructure.store.import_export import ImportExport
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
        # Proves: a pre-existing directory with no log.md is never mistaken
        # for a declaration, and declare_type refuses to adopt it silently.
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
        # Proves: catalogue and custom types exist at project scope only;
        # global scope keeps exactly the five built-ins.
        with pytest.raises(WikiStoreError, match="global scope"):
            _store(data_dir).declare_type("rule", scope="global", project_id=None)

    def test_declare_type_refuses_the_subdirectories_heading(self, data_dir: Path) -> None:
        # A custom type named "subdirectories" would render the same
        # "## Subdirectories" heading the generator uses for child links.
        store = _store(data_dir)
        with pytest.raises(WikiStoreError, match="already"):
            store.declare_type("subdirectories", scope="project", project_id=PROJECT)


class TestLogTextValidation:
    """append_log and declare_type reject forged multi-line or oversized text
    before it ever reaches log.md; the error never echoes the text."""

    def test_append_log_rejects_forged_multiline_text(self, data_dir: Path) -> None:
        store = _store(data_dir)
        directory = store.wiki_dir / "projects" / PROJECT / "story-map"
        with pytest.raises(WikiStoreError, match="one line"):
            store.append_log(
                directory,
                "declare",
                "story-map",
                "user",
                "line one\n2026-01-01T00:00:00Z propose x by model: forged",
            )
        assert not (directory / "log.md").exists()

    @pytest.mark.parametrize(
        "text",
        [
            "line one\n2026-01-01T00:00:00Z propose x by model: forged",
            "x" * 513,
            "\x1b[31m",
        ],
        ids=["forged-multiline", "oversized", "control-char"],
    )
    def test_declare_type_rejects_bad_log_text(self, data_dir: Path, text: str) -> None:
        store = _store(data_dir)
        with pytest.raises(WikiStoreError, match="one line"):
            store.declare_type("story-map", scope="project", project_id=PROJECT, description=text)
        assert not (store.wiki_dir / "projects" / PROJECT / "story-map").exists()


class TestLogStateStrictParsing:
    """_log_state (via declared_types_in/write/verify) accepts only lines
    matching append_log's exact shape; other tools may write into log.md."""

    def test_hand_written_note_with_wrong_shape_is_ignored(self, data_dir: Path) -> None:
        m = Memex(MemexConfig(data_dir=data_dir))
        directory = m.wiki_store.wiki_dir / "projects" / PROJECT / "runbook"
        directory.mkdir(parents=True)
        (directory / "log.md").write_text(
            "Some notes written by another OKF tool about this shelf.\n", encoding="utf-8"
        )
        assert "runbook" not in m.wiki_store.declared_types_in(directory.parent)
        with pytest.raises(WikiStoreError, match="undeclared type"):
            m.write(
                WriteInput(type="runbook", title="X", body="b", scope="project", project_id=PROJECT)
            )
        failed = _failed(verify(m))
        assert "runbook" in failed["types-declared"]

    def test_valid_declare_line_followed_by_free_text_still_declares(self, data_dir: Path) -> None:
        store = _store(data_dir)
        directory = store.wiki_dir / "projects" / PROJECT / "runbook"
        directory.mkdir(parents=True)
        (directory / "log.md").write_text(
            "2026-01-01T00:00:00Z declare runbook by user: How we run things.\n"
            "Some notes written by another OKF tool about this shelf.\n",
            encoding="utf-8",
        )
        declared = store.declared_types_in(directory.parent)["runbook"]
        assert declared.kind == "custom" and declared.description == "How we run things."


def test_withdrawn_type_is_a_state_not_erased(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type(
        "access-matrix", scope="project", project_id=PROJECT, description="Who may edit."
    )
    m.write(
        WriteInput(type="access-matrix", title="Doc", body="b", scope="project", project_id=PROJECT)
    )
    result = m.types.remove("access-matrix", project_id=PROJECT, force=True)
    assert result == {"removed": "access-matrix", "archived": 1}

    project_dir = m.wiki_store.wiki_dir / "projects" / PROJECT
    assert m.wiki_store.declared_types_in(project_dir)["access-matrix"].kind == "withdrawn"

    with pytest.raises(WikiStoreError, match="withdrawn"):
        m.write(
            WriteInput(
                type="access-matrix", title="Second", body="b", scope="project", project_id=PROJECT
            )
        )

    failed = _failed(verify(m))
    assert "types-match-directory" not in failed
    assert "types-declared" not in failed
    assert "draft-types-pending" not in failed

    doc = ImportExport(m.wiki_store, m.index_manager, m.link_manager).export()
    exported_types = doc["types"]
    assert isinstance(exported_types, list)
    assert not any(t["name"] == "access-matrix" for t in exported_types)

    with pytest.raises(WikiStoreError, match="already withdrawn"):
        m.types.remove("access-matrix", project_id=PROJECT)

    redeclared = m.wiki_store.declare_type("access-matrix", scope="project", project_id=PROJECT)
    assert redeclared.kind == "custom"


def test_hand_placed_global_custom_type_directory_is_invisible(data_dir: Path) -> None:
    # Regression: is_type_dir's global branch must not fall through to the
    # general type-name-shape check, or a stray hand-placed directory under
    # docs/global/ would be scanned, indexed, and given a navigation heading.
    from memex.domain.frontmatter import serialize_front_matter
    from memex.infrastructure.store.wiki_store import node_front_matter

    m = Memex(MemexConfig(data_dir=data_dir))
    stray = m.wiki_store.wiki_dir / "global" / "runbook"
    stray.mkdir(parents=True)
    node = WikiNode(
        type="runbook", title="X", body="Body.", id="11111111-1111-1111-1111-111111111111"
    )
    node.content_hash = "sha256:" + "0" * 64
    (stray / "x.md").write_text(
        serialize_front_matter(node_front_matter(node), node.body), encoding="utf-8"
    )

    assert m.wiki_store._is_type_dir(stray) is False
    assert all(n.slug != "x" for n in m.wiki_store.scan_all())

    report = m.rebuild_index(force=True)
    assert report.nodes_indexed == 0
    assert m.index_manager.get("x") is None
    index_path = m.wiki_store.wiki_dir / "global" / "index.md"
    if index_path.exists():
        assert "Runbook" not in index_path.read_text(encoding="utf-8")


def test_project_directories_skips_symlinked_child(data_dir: Path) -> None:
    store = _store(data_dir)
    _page(store, "entity", "Seed")  # creates a genuine project directory
    projects_dir = store.wiki_dir / "projects"
    real = next(iter(store.project_directories()))
    bogus = projects_dir / "bogus-symlink"
    bogus.symlink_to(real, target_is_directory=True)

    names = {p.name for p in store.project_directories()}
    assert "bogus-symlink" not in names
    assert real.name in names


def test_suggest_surfaces_unenabled_catalogue_name_but_not_after_enable(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    for i in range(3):
        m.write(
            WriteInput(
                type="entity",
                title=f"Item {i}",
                body="b",
                tags=["decision"],
                scope="project",
                project_id=PROJECT,
            )
        )
    suggestions = m.types.suggest(project_id=PROJECT)
    assert ("decision", 3) in suggestions

    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    suggestions_after = m.types.suggest(project_id=PROJECT)
    assert not any(tag == "decision" for tag, _count in suggestions_after)


def test_import_type_with_multiline_description_records_error(
    data_dir: Path, tmp_path: Path
) -> None:
    other = Memex(MemexConfig(data_dir=data_dir))
    archive = tmp_path / "bad-desc.json"
    archive.write_text(
        json.dumps(
            {
                "version": "1.0",
                "exported_at": "2026-01-01T00:00:00Z",
                "types": [
                    {
                        "scope": "project",
                        "project_id": PROJECT,
                        "name": "story-map",
                        "kind": "custom",
                        "description": "line one\nline two",
                    }
                ],
                "nodes": [],
            }
        )
    )
    io = ImportExport(other.wiki_store, other.index_manager, other.link_manager)
    result = io.import_file(archive)
    import_errors = result["errors"]
    assert isinstance(import_errors, list)
    assert any("story-map" in e for e in import_errors)
    assert "story-map" not in other.wiki_store.declared_types(scope="project", project_id=PROJECT)


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


class _FakeLLM:
    """Matches memex.application.ports.LLMClient: complete(system, user, *, max_tokens)."""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResponse:
        return LLMResponse(text=self.payload, prompt_tokens=1, completion_tokens=1)


def _memex_with(data_dir: Path, payload: str, approval: str = "manual") -> Memex:
    governance = GovernanceConfig(knowledge_approval=approval)
    m = Memex(MemexConfig(data_dir=data_dir, governance=governance))
    m._llm = _FakeLLM(payload)  # the facade's lazy client slot (memory.py:68, :828-833)
    return m


def _episode(m: Memex, sid: str, *, project_id: str | None = PROJECT) -> None:
    """A project episode, written directly: consolidation groups by the episode's namespace."""
    node = m.wiki_store.write(
        WikiNode(
            type="episode",
            title=f"Session {sid}",
            body="we chose kafka",
            id="",
            session_id=sid,
            scope="project" if project_id else "global",
            project_id=project_id,
        )
    )
    m.index_manager.update_record(node)


class TestConsolidationGovernance:
    def test_summary_is_active_and_entity_is_pending_by_default(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"summary","title":"Week","body":"b","tags":[],'
            '"importance":0.5,"links":[]},'
            '{"type":"entity","title":"Kafka","body":"b","tags":[],'
            '"importance":0.5,"links":[]}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        week = m.wiki_store.read("week")
        kafka = m.wiki_store.read("kafka")
        assert week is not None and week.status == "active"
        assert kafka is not None and kafka.status == "pending"

    def test_auto_makes_entity_active(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"entity","title":"Kafka","body":"b","tags":[],"importance":0.5,"links":[]}]'
        )
        m = _memex_with(data_dir, payload, approval="auto")
        _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        kafka = m.wiki_store.read("kafka")
        assert kafka is not None and kafka.status == "active"

    def test_decision_written_only_when_enabled(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"decision","title":"Choose Kafka","body":"b","tags":[],'
            '"importance":0.5,"links":[]}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1")
        report = m.consolidate(ConsolidateInput())
        assert report.nodes_created == [] and m.wiki_store.read("choose-kafka") is None
        m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
        m.consolidate(ConsolidateInput())
        node = m.wiki_store.read("choose-kafka")
        assert node is not None and node.status == "pending" and node.source == "consolidation"
        # Lands in the episode's namespace.
        assert node.scope == "project" and node.project_id == PROJECT

    def test_proposed_type_lands_as_a_draft(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"entity","title":"Who edits","body":"b","tags":[],'
            '"importance":0.5,"links":[],"proposed_type":"access-matrix"}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        types = m.wiki_store.declared_types(scope="project", project_id=PROJECT)
        assert types["access-matrix"].kind == "draft" and types["access-matrix"].pages == 1
        page = m.wiki_store.read("who-edits")
        assert page is not None and page.type == "access-matrix" and page.status == "pending"
        log = (types["access-matrix"].directory / "log.md").read_text()
        assert "propose access-matrix by consolidation: sessions=s1 pages=1" in log
        assert m.recall("who edits", include_inactive=False).hits == []

    def test_same_titled_candidates_each_become_a_page(self, data_dir: Path) -> None:
        # Two candidates that share a title still land as two pages: the
        # store slugs a title collision as "-2" rather than merging the two,
        # so the propose line's raw candidate count matches what the run
        # actually writes.
        payload = (
            '[{"type":"entity","title":"Who edits","body":"b1","tags":[],'
            '"importance":0.5,"links":[],"proposed_type":"access-matrix"},'
            '{"type":"entity","title":"Who edits","body":"b2","tags":[],'
            '"importance":0.5,"links":[],"proposed_type":"access-matrix"}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        types = m.wiki_store.declared_types(scope="project", project_id=PROJECT)
        log = (types["access-matrix"].directory / "log.md").read_text()
        assert "propose access-matrix by consolidation: sessions=s1 pages=2" in log
        assert types["access-matrix"].pages == 2
        who_edits = m.wiki_store.read("who-edits")
        who_edits_2 = m.wiki_store.read("who-edits-2")
        assert who_edits is not None and who_edits.status == "pending"
        assert who_edits_2 is not None and who_edits_2.status == "pending"

    def test_bad_proposed_type_is_dropped_not_created(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"entity","title":"X","body":"b","tags":[],'
            '"importance":0.5,"links":[],"proposed_type":"Entities"}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        x_node = m.wiki_store.read("x")
        assert x_node is not None and x_node.type == "entity"
        # Exact-name check, not a glob: on a case-insensitive filesystem
        # (macOS default) rglob("Entities") would also match the pre-existing
        # built-in "entities" directory and pass for the wrong reason.
        assert not any(p.name == "Entities" for p in (data_dir / "docs").rglob("*"))

    def test_global_episodes_never_produce_catalogue_or_drafts(self, data_dir: Path) -> None:
        payload = (
            '[{"type":"entity","title":"X","body":"b","tags":[],'
            '"importance":0.5,"links":[],"proposed_type":"story-map"}]'
        )
        m = _memex_with(data_dir, payload)
        _episode(m, "s1", project_id=None)
        m.consolidate(ConsolidateInput())
        node = m.wiki_store.read("x")
        assert node is not None and node.scope == "global" and node.type == "entity"
        assert not list((data_dir / "docs" / "global").glob("story-map"))


def _failed(report: VerifyReport) -> dict[str, str]:
    return {str(c["check"]): str(c["detail"]) for c in report.checks if not c["ok"]}


def test_verify_reports_type_directory_mismatch(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    node = m.write(
        WriteInput(type="decision", title="Choose", body="b", scope="project", project_id=PROJECT)
    )
    path = Path(node.file_path or "")
    path.write_text(path.read_text().replace('type: "decision"', 'type: "policy"'))
    failed = _failed(verify(m))
    assert "types-match-directory" in failed and "choose" in failed["types-match-directory"]


def test_verify_reports_undeclared_directory_and_non_pending_draft(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.write(WriteInput(type="entity", title="Seed", body="b", scope="project", project_id=PROJECT))
    project_dir = Path(m.wiki_store.get_path("seed")).parent.parent
    (project_dir / "mystery").mkdir()
    seed_path = Path(m.wiki_store.get_path("seed"))
    seed_text = seed_path.read_text()
    mystery_text = seed_text.replace('type: "entity"', 'type: "mystery"').replace("Seed", "Thing")
    (project_dir / "mystery" / "thing.md").write_text(mystery_text)
    m.wiki_store.declare_type(
        "story-map",
        scope="project",
        project_id=PROJECT,
        actor="consolidation",
        draft=True,
    )
    m.write(
        WriteInput(
            type="story-map",
            title="Map",
            body="b",
            scope="project",
            project_id=PROJECT,
            status="active",
        )
    )
    failed = _failed(verify(m))
    assert "mystery" in failed["types-declared"]
    assert "map" in failed["draft-types-pending"]
    for detail in failed.values():
        assert str(data_dir) not in detail


def test_verify_passes_for_declared_types_with_no_pages_yet(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    m.wiki_store.declare_type("access-matrix", scope="project", project_id=PROJECT)
    report = verify(m)
    failed = _failed(report)
    assert "types-declared" not in failed
    assert "types-match-directory" not in failed
    assert "draft-types-pending" not in failed
    assert all(check["ok"] for check in report.checks)


def test_declared_types_in_matches_declared_types(data_dir: Path) -> None:
    store = _store(data_dir)
    store.declare_type("decision", scope="project", project_id=PROJECT)
    _page(store, "decision", "Choose Kafka")
    project_dir = store.wiki_dir / "projects" / PROJECT  # PROJECT is 'a'*24, so this is the path
    declared_in = store.declared_types_in(project_dir)
    declared = store.declared_types(scope="project", project_id=PROJECT)
    assert declared_in == declared


def test_export_import_round_trips_declared_types(data_dir: Path, tmp_path: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    m.wiki_store.declare_type(
        "access-matrix", scope="project", project_id=PROJECT, description="Who may edit."
    )
    m.write(
        WriteInput(type="decision", title="Choose", body="b", scope="project", project_id=PROJECT)
    )
    archive = tmp_path / "e.json"
    doc = ImportExport(m.wiki_store, m.index_manager, m.link_manager).export(archive)
    exported_types = doc["types"]
    assert isinstance(exported_types, list)
    assert {
        "scope": "project",
        "project_id": PROJECT,
        "name": "access-matrix",
        "kind": "custom",
        "description": "Who may edit.",
    } in exported_types

    other = Memex(MemexConfig(data_dir=tmp_path / "second"))
    ImportExport(other.wiki_store, other.index_manager, other.link_manager).import_file(archive)
    types = other.wiki_store.declared_types(scope="project", project_id=PROJECT)
    assert types["decision"].kind == "catalogue"
    assert types["access-matrix"].description == "Who may edit."
    read = other.wiki_store.read("choose")
    assert read is not None and read.type == "decision"


def test_import_fails_on_page_of_undeclared_type(data_dir: Path, tmp_path: Path) -> None:
    other = Memex(MemexConfig(data_dir=data_dir))
    archive = tmp_path / "bad.json"
    archive.write_text(
        json.dumps(
            {
                "version": "1.0",
                "exported_at": "2026-01-01T00:00:00Z",
                "types": [],
                "nodes": [
                    {
                        "slug": "x",
                        "type": "decision",
                        "title": "X",
                        "body": "b",
                        "tags": [],
                        "importance": 0.5,
                        "created": "2026-01-01T00:00:00Z",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "links": [],
                        "scope": "project",
                        "project_id": PROJECT,
                    }
                ],
            }
        )
    )
    io = ImportExport(other.wiki_store, other.index_manager, other.link_manager)
    result = io.import_file(archive)
    import_errors = result["errors"]
    assert result["imported"] == 0
    assert isinstance(import_errors, list)
    assert any("x" in e and "undeclared" in e for e in import_errors)


def test_export_skips_project_whose_pages_disagree_on_id(data_dir: Path, tmp_path: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    m.write(
        WriteInput(type="decision", title="Choose", body="b", scope="project", project_id=PROJECT)
    )
    second = m.write(
        WriteInput(
            type="decision", title="Choose Two", body="b", scope="project", project_id=PROJECT
        )
    )
    other_id = "c" * 24
    second_path = Path(second.file_path or "")
    second_path.write_text(
        second_path.read_text(encoding="utf-8").replace(
            f'project_id: "{PROJECT}"', f'project_id: "{other_id}"'
        ),
        encoding="utf-8",
    )

    archive = tmp_path / "e.json"
    doc = ImportExport(m.wiki_store, m.index_manager, m.link_manager).export(archive)
    exported_types = doc["types"]
    assert isinstance(exported_types, list)
    assert not any(t.get("project_id") == PROJECT for t in exported_types)
    _, skipped = m.wiki_store.project_declarations()
    assert skipped == 1


def test_export_warns_when_a_declared_project_is_skipped(
    data_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.logger.propagate = True  # caplog visibility; production keeps False
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    archive = tmp_path / "e.json"

    with caplog.at_level(logging.WARNING, logger="memex"):
        ImportExport(m.wiki_store, m.index_manager, m.link_manager).export(archive)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "operation=export types_skipped=1" in warnings


def test_import_records_non_object_type_entry(data_dir: Path, tmp_path: Path) -> None:
    other = Memex(MemexConfig(data_dir=data_dir))
    archive = tmp_path / "bad-types.json"
    archive.write_text(
        json.dumps(
            {
                "version": "1.0",
                "exported_at": "2026-01-01T00:00:00Z",
                "types": [42],
                "nodes": [],
            }
        )
    )
    result = ImportExport(other.wiki_store, other.index_manager, other.link_manager).import_file(
        archive
    )
    import_errors = result["errors"]
    assert isinstance(import_errors, list)
    assert "non-object type entry skipped" in import_errors
    assert result["imported"] == 0
