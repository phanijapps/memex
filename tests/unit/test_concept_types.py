"""Project-level concept types (docs/specs/project-concept-types/spec.md)."""

from __future__ import annotations

import pytest

from memex.domain import types as T


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
        "name", ["entity", "entities", "episode", "episodes", "index", "log", "rule", "decision"]
    )
    def test_collisions_with_builtin_catalogue_and_reserved(self, name: str) -> None:
        # Review Focus 4: a custom type may never shadow a built-in name, a
        # built-in directory, a catalogue name, or a reserved filename.
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
