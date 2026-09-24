"""OKF v0.2 page contract (docs/specs/okf-frontmatter/spec.md).

Every test here pins one acceptance criterion of the OKF front-matter
contract: key order, the retired names, typed links, the relation graph,
visibility, forget semantics, verify's conformance checks, and the
round-trip through an independent YAML implementation.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import yaml

from memex.application.memory import Memex
from memex.application.verify import verify
from memex.domain.errors import WikiStoreError
from memex.domain.models import SemanticLink, WriteInput
from memex.domain.reserved import OKF_VERSION
from memex.infrastructure.store.import_export import ImportExport
from memex.infrastructure.store.wiki_store import _OKF_KEYS, WikiStore


@pytest.fixture
def memex(data_dir: Path) -> Memex:
    """A Memex over an isolated, empty store."""
    from memex.infrastructure.config import MemexConfig

    return Memex(MemexConfig(data_dir=data_dir))


@pytest.fixture(scope="session")
def reference_linter() -> Path:
    """The real OKF reference linter, resolved once per session.

    Session scope, not a module-level constant: pytest sets up a
    session-scoped fixture before any test's function-scoped fixtures, so
    this resolves against the developer's real ``HOME`` before the
    per-test ``_isolated_memex_env`` autouse fixture redirects it — and an
    import-time stat never risks failing collection for the whole file in
    an environment where ``HOME`` is unset.
    """
    return Path.home() / "code/okf-wiki/bootstrap/skills/okf-lint/scripts/lint-bundle.py"


def _front_matter(path: Path) -> dict[str, object]:
    block = path.read_text(encoding="utf-8").split("---\n")[1]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict)
    return parsed


def _exporter(memex: Memex) -> ImportExport:
    return ImportExport(memex.wiki_store, memex.index_manager, memex.link_manager)


def _write(memex: Memex, **kwargs: object) -> Path:
    defaults: dict[str, object] = {"type": "entity", "title": "Page", "body": "Body text."}
    node = memex.write(WriteInput(**{**defaults, **kwargs}))  # type: ignore[arg-type]
    assert node.file_path is not None
    return Path(node.file_path)


class TestPageShape:
    def test_page_key_order(self, memex: Memex) -> None:
        """AC-0001: okf_version first, then the OKF fields, then Memex fields."""
        keys = list(_front_matter(_write(memex)))
        assert keys[: len(_OKF_KEYS)] == list(_OKF_KEYS)
        assert keys[0] == "okf_version"
        assert keys[len(_OKF_KEYS) :][0] == "id"

    def test_okf_version_declared(self, memex: Memex) -> None:
        assert _front_matter(_write(memex))["okf_version"] == OKF_VERSION

    @pytest.mark.parametrize("retired", ["created", "updated", "valid_to", "expires_at"])
    def test_retired_names_are_not_okf_fields(self, retired: str) -> None:
        """AC-0002: the OKF block never carries a retired Memex name."""
        assert retired not in _OKF_KEYS

    def test_unknown_key_is_a_malformed_page(self, tmp_path: Path, memex: Memex) -> None:
        """AC-0002: a key outside the accepted set fails the page read."""
        page = _write(memex)
        page.write_text(page.read_text().replace("resource: null", 'made_up: "x"'))
        store = WikiStore(memex.data_dir)
        with pytest.raises(WikiStoreError, match="unknown front matter keys"):
            store.read("page")

    def test_timestamp_moves_every_write_and_created_never_does(self, memex: Memex) -> None:
        """`timestamp` moves on every write; `updated_at` moves only when the
        body's content hash changes; `created` never changes across writes
        to the same page (WikiStore.write). Both writes below target the
        same slug, so this proves the claim about one page, not luck across
        two different ones."""
        path = _write(memex)
        slug = path.stem
        first = _front_matter(path)

        same_body = memex.wiki_store.read(slug)
        assert same_body is not None
        rewritten = memex.wiki_store.write(same_body)
        assert rewritten.file_path is not None
        unchanged = _front_matter(Path(rewritten.file_path))
        assert unchanged["created"] == first["created"]
        assert unchanged["timestamp"] >= first["timestamp"]  # type: ignore[operator]
        assert unchanged["updated_at"] == first["updated_at"]

        edited = memex.wiki_store.read(slug)
        assert edited is not None
        edited.body = "Body text, edited."
        time.sleep(1)  # cross a whole-second boundary so updated_at strictly moves
        stored = memex.wiki_store.write(edited)
        assert stored.file_path is not None
        after_edit = _front_matter(Path(stored.file_path))
        assert after_edit["created"] == first["created"]
        assert after_edit["updated_at"] > unchanged["updated_at"]  # type: ignore[operator]


class TestTypedLinks:
    def test_declared_and_default_relations(self, memex: Memex) -> None:
        """AC-0003 / AC-0010: a bare target defaults to relates-to."""
        data = _front_matter(_write(memex, links=["alpha", "beta:depends-on"]))
        assert data["links"] == [
            {"target": "alpha", "rel": "relates-to"},
            {"target": "beta", "rel": "depends-on"},
        ]

    def test_link_without_target_is_rejected(self) -> None:
        """AC-0003: a link entry missing target is rejected at the boundary."""
        with pytest.raises(ValueError, match="missing required field 'target'"):
            SemanticLink.parse({"rel": "depends-on"})

    def test_link_with_unknown_member_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported fields"):
            SemanticLink.parse({"target": "a", "rel": "b", "colour": "red"})

    def test_link_with_non_string_member_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            SemanticLink.parse({"target": "a", "rel": 7})

    def test_relation_fields_are_normalized(self, memex: Memex) -> None:
        """AC-0004: relation fields normalize exactly as tags do."""
        data = _front_matter(
            _write(memex, parent=" Estate ", supersedes=["Old-Notes"], depends_on=["B", "a"])
        )
        assert data["parent"] == "estate"
        assert data["supersedes"] == ["old-notes"]
        assert data["depends_on"] == ["b", "a"]

    def test_body_references_stay_out_of_front_matter(self, memex: Memex) -> None:
        """Body `[[slug]]` is a graph edge, never a declared relation."""
        data = _front_matter(_write(memex, body="See [[other-page]]."))
        assert data["links"] == []


class TestLinkGraph:
    def test_relations_carry_their_rel(self, memex: Memex) -> None:
        """AC-0005: every relation source projects into the graph with its rel."""
        memex.write(
            WriteInput(
                type="entity",
                title="Page",
                body="See [[mentioned]].",
                links=["alpha:depends-on"],
                parent="estate",
                supersedes=["old"],
            )
        )
        rows = memex.index_manager.connection.execute(
            "SELECT target_slug, rel FROM wiki_links WHERE source_slug = 'page' ORDER BY rel"
        ).fetchall()
        assert [(row["target_slug"], row["rel"]) for row in rows] == [
            ("alpha", "depends-on"),
            ("mentioned", "mentions"),
            ("estate", "parent"),
            ("old", "supersedes"),
        ]

    def test_one_target_two_relations_is_listed_once(self, memex: Memex) -> None:
        """A target reached by two relations is not duplicated for readers."""
        memex.write(
            WriteInput(
                type="entity",
                title="Page",
                body="See [[alpha]].",
                links=["alpha:depends-on", "alpha:relates-to"],
            )
        )
        assert memex.link_manager.get_outgoing("page") == ["alpha"]


class TestVisibility:
    def test_expired_page_is_hidden(self, memex: Memex) -> None:
        """AC-0006: a page past valid_until is out of recall."""
        memex.write(
            WriteInput(
                type="entity",
                title="Gone",
                body="unique-marker",
                valid_until="2020-01-01T00:00:00Z",
            )
        )
        assert memex.recall("unique-marker").hits == []

    def test_future_page_is_hidden(self, memex: Memex) -> None:
        """AC-0006: a page before valid_from is out of recall."""
        memex.write(
            WriteInput(
                type="entity",
                title="Later",
                body="unique-marker",
                valid_from="2999-01-01T00:00:00Z",
            )
        )
        assert memex.recall("unique-marker").hits == []

    def test_stale_after_does_not_hide(self, memex: Memex) -> None:
        """AC-0006: stale_after is advisory and never excludes a page."""
        memex.write(
            WriteInput(
                type="entity", title="Old", body="unique-marker", stale_after="2020-01-01T00:00:00Z"
            )
        )
        assert [hit.slug for hit in memex.recall("unique-marker").hits] == ["old"]


class TestForgetModes:
    def test_soft_retires_now(self, memex: Memex) -> None:
        """AC-0007: soft ends the validity window immediately."""
        memex.write(WriteInput(type="entity", title="Page", body="unique-marker"))
        memex.forget("page", mode="soft")
        node = memex.wiki_store.read("page")
        assert node is not None and node.valid_until is not None
        assert memex.recall("unique-marker").hits == []

    def test_decay_retires_one_half_life_out(self, memex: Memex) -> None:
        """AC-0007: decay ends it one configured half-life from now."""
        memex.write(WriteInput(type="entity", title="Page", body="unique-marker"))
        memex.forget("page", mode="decay")
        node = memex.wiki_store.read("page")
        assert node is not None and node.valid_until is not None
        # Still inside its window, so still recallable.
        assert [hit.slug for hit in memex.recall("unique-marker").hits] == ["page"]

    def test_both_modes_write_valid_until(self, memex: Memex) -> None:
        memex.write(WriteInput(type="entity", title="A", body="x"))
        memex.write(WriteInput(type="entity", title="B", body="y"))
        memex.forget("a", mode="soft")
        memex.forget("b", mode="decay")
        for slug in ("a", "b"):
            data = _front_matter(Path(memex.wiki_store.get_path(slug, "entity")))
            assert data["valid_until"] is not None
            assert data["stale_after"] is None


class TestVerifyConformance:
    def test_verify_names_okf_defects(self, memex: Memex) -> None:
        """AC-0008: each OKF graph and temporal defect is its own named check."""
        memex.write(WriteInput(type="entity", title="Orphan", body="b", parent="nowhere"))
        memex.write(
            WriteInput(
                type="entity",
                title="Inverted",
                body="b",
                valid_from="2030-01-01T00:00:00Z",
                valid_until="2020-01-01T00:00:00Z",
            )
        )
        report = verify(memex)
        failed = {str(check["check"]) for check in report.checks if not check["ok"]}
        assert {"okf-parent-resolves", "okf-relations-resolve", "okf-validity-ordered"} <= failed

    def test_verify_details_carry_only_slugs(self, memex: Memex) -> None:
        """AC-0009: a diagnostic names pages, never their content."""
        memex.write(
            WriteInput(
                type="entity",
                title="Secret Page",
                body="ghp_0123456789abcdefghijklmnopqrstuvwxyzAB",
                parent="nowhere",
            )
        )
        report = verify(memex)
        details = " ".join(str(check["detail"]) for check in report.checks)
        assert "secret-page" in details
        assert "ghp_" not in details
        assert str(memex.data_dir) not in details

    def test_clean_store_passes_every_okf_check(self, memex: Memex) -> None:
        memex.write(WriteInput(type="entity", title="Target", body="b"))
        memex.write(WriteInput(type="entity", title="Source", body="b", links=["target"]))
        report = verify(memex)
        okf = [check for check in report.checks if str(check["check"]).startswith("okf-")]
        assert len(okf) == 5
        assert all(check["ok"] for check in okf)


class TestExternalConformance:
    def test_independent_yaml_parser_agrees(self, memex: Memex) -> None:
        """AC-0012: an independent YAML reader sees the same keys and values."""
        from memex.domain.frontmatter import parse_front_matter

        page = _write(
            memex,
            links=["alpha:depends-on"],
            tags=["a", "b"],
            description='A "quoted" signpost',
            parent="estate",
        )
        ours, _body = parse_front_matter(page.read_text(encoding="utf-8"))
        theirs = _front_matter(page)
        assert list(ours) == list(theirs)
        assert ours == theirs

    def test_export_carries_okf_names(self, memex: Memex, tmp_path: Path) -> None:
        """AC-0011: export emits every front-matter key under its OKF name."""
        memex.write(WriteInput(type="entity", title="Page", body="b", links=["alpha"]))
        archive = tmp_path / "export.json"
        _exporter(memex).export(archive)
        entry = json.loads(archive.read_text())["nodes"][0]
        assert {"timestamp", "updated_at", "valid_until", "stale_after", "links"} <= set(entry)
        assert entry["links"] == [{"target": "alpha", "rel": "relates-to"}]
        assert "updated" not in entry and "valid_to" not in entry

    def test_export_import_preserves_every_value(self, memex: Memex, tmp_path: Path) -> None:
        """AC-0011: an import into an empty store reproduces the field values."""
        memex.write(
            WriteInput(
                type="entity",
                title="Page",
                body="b",
                links=["alpha:depends-on"],
                parent="estate",
                supersedes=["old"],
                valid_from="2020-01-01T00:00:00Z",
            )
        )
        archive = tmp_path / "export.json"
        _exporter(memex).export(archive)
        original = memex.wiki_store.read("page")

        from memex.infrastructure.config import MemexConfig

        other = Memex(MemexConfig(data_dir=tmp_path / "second"))
        _exporter(other).import_file(archive)
        restored = other.wiki_store.read("page")

        assert original is not None and restored is not None
        # An import is a write, so the write instants legitimately move.
        assert restored.timestamp >= original.timestamp
        for name in (
            "created",
            "valid_from",
            "valid_until",
            "stale_after",
            "parent",
            "supersedes",
            "implements",
            "depends_on",
            "links",
        ):
            assert getattr(restored, name) == getattr(original, name), name


class TestReviewedCriteria:
    """Criteria added during spec review; each names its AC."""

    def test_updated_at_moves_only_on_body_change(self, memex: Memex) -> None:
        """AC-0004: a metadata-only write leaves updated_at alone."""
        page = _write(memex, body="original body")
        # Backdate the stored page so a same-second rewrite is still visible.
        page.write_text(page.read_text().replace('updated_at: "20', 'updated_at: "19'), "utf-8")
        backdated = str(_front_matter(page)["updated_at"])

        node = memex.wiki_store.read("page")
        assert node is not None
        node.tags = ["metadata-only"]
        memex.wiki_store.write(node)
        assert _front_matter(page)["updated_at"] == backdated

        node = memex.wiki_store.read("page")
        assert node is not None
        node.body = "a different body"
        memex.wiki_store.write(node)
        assert _front_matter(page)["updated_at"] != backdated

    def test_link_rejection_message_carries_no_page_content(self, memex: Memex) -> None:
        """AC-0007: the error names the field, never the page."""
        with pytest.raises(ValueError) as caught:
            memex.write(
                WriteInput(
                    type="entity",
                    title="Confidential title",
                    body="ghp_0123456789abcdefghijklmnopqrstuvwxyzAB",
                    links=[{"target": "a", "rel": "b", "colour": "red"}],
                )
            )
        message = str(caught.value)
        assert "colour" in message
        assert "Confidential title" not in message
        assert "ghp_" not in message

    def test_duplicate_target_appears_once_in_recall_and_backlinks(self, memex: Memex) -> None:
        """AC-0011: every reader-facing list de-duplicates by target."""
        memex.write(WriteInput(type="entity", title="Alpha", body="target body"))
        memex.write(
            WriteInput(
                type="entity",
                title="Source",
                body="unique-marker and [[alpha]]",
                links=["alpha:depends-on", "alpha:relates-to"],
            )
        )
        hit = memex.recall("unique-marker").hits[0]
        assert hit.links == ["alpha"]
        assert memex.link_manager.get_backlinks("alpha") == ["source"]
        assert memex.link_manager.get_link_graph()["source"] == ["alpha"]

    def test_include_expired_returns_pages_outside_the_window(self, memex: Memex) -> None:
        """AC-0014: the opt-in flag disables both validity exclusions."""
        memex.write(
            WriteInput(
                type="entity",
                title="Past",
                body="unique-marker",
                valid_until="2020-01-01T00:00:00Z",
            )
        )
        memex.write(
            WriteInput(
                type="entity",
                title="Future",
                body="unique-marker",
                valid_from="2999-01-01T00:00:00Z",
            )
        )
        assert memex.recall("unique-marker").hits == []
        opened = {hit.slug for hit in memex.recall("unique-marker", include_expired=True).hits}
        assert opened == {"past", "future"}

    def test_recall_hit_has_no_field_named_updated(self, memex: Memex) -> None:
        """AC-0024: the retired name is gone from the public hit shape."""
        memex.write(WriteInput(type="entity", title="Page", body="unique-marker"))
        hit = memex.recall("unique-marker").hits[0]
        assert not hasattr(hit, "updated")
        assert hit.created and hit.timestamp and hit.updated_at

    def test_schema_version_is_six(self) -> None:
        """AC-0029."""
        from memex.infrastructure.search.index_manager import SCHEMA_VERSION

        assert SCHEMA_VERSION == "6"

    def test_store_passes_the_okf_reference_linter(
        self, memex: Memex, tmp_path: Path, reference_linter: Path
    ) -> None:
        """AC-0028: the real OKF linter, when the reference checkout is present."""
        import subprocess

        linter = reference_linter
        if not linter.exists():
            pytest.skip("OKF reference linter not available on this machine")
        memex.write(WriteInput(type="entity", title="Target", body="b", description="When X."))
        memex.write(
            WriteInput(
                type="entity", title="Source", body="b", description="When Y.", links=["target"]
            )
        )
        memex.rebuild_index()
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(linter), str(memex.wiki_store.wiki_dir)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout
        assert "0 error(s), 0 warning(s)" in result.stdout


class TestAdapterRelations:
    def test_mcp_write_accepts_both_link_forms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-0021: the MCP tool takes 'target' and 'target:rel'."""
        from memex import mcp_server

        monkeypatch.setenv("MEMEX_DATA_DIR", str(tmp_path / "mcp-okf-home"))
        mcp_server._reset()
        try:
            result = mcp_server.memex_write(
                type="entity",
                title="Page",
                body="b",
                scope="global",
                links=["alpha", "beta:depends-on"],
                parent="estate",
            )
            page = Path(str(result["file_path"]))
            data = _front_matter(page)
            assert data["links"] == [
                {"target": "alpha", "rel": "relates-to"},
                {"target": "beta", "rel": "depends-on"},
            ]
            assert data["parent"] == "estate"
        finally:
            mcp_server._reset()

    def test_consolidation_links_get_a_relation(self, memex: Memex) -> None:
        """AC-0023: a bare consolidation slug becomes a relates-to link."""
        candidate = WriteInput(
            type="summary",
            title="Distilled",
            body="b",
            links=["alpha", "beta:depends-on"],
        )
        node = memex.write(candidate)
        assert [link.to_mapping() for link in node.links] == [
            {"target": "alpha", "rel": "relates-to"},
            {"target": "beta", "rel": "depends-on"},
        ]
