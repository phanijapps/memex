"""Generated directory navigation: OKF-style disposable index.md views.

Pins AC-0015..AC-0020 for the agentic-frontmatter-search spec: deterministic
generated indexes, reserved-filename exclusion, best-effort refresh after
page mutations, bounded diagnostics, and repair through explicit rebuild.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import cast

import pytest

from memex.application.memory import Memex
from memex.application.verify import verify
from memex.domain.models import WikiNode, WriteInput
from memex.infrastructure.config import ConfigLoader
from memex.infrastructure.store.navigation import (
    NavigationError,
    NavigationGenerator,
    NavigationReport,
)

PROJECT = "a" * 24


def _memex(data_dir: Path) -> Memex:
    return Memex(ConfigLoader().load(data_dir=data_dir))


def _write(memex: Memex, title: str, body: str = "content", **kwargs: object) -> str:
    node = memex.write(WriteInput(type="entity", title=title, body=body, **kwargs))  # type: ignore[arg-type]
    return node.slug


def _page_dir(memex: Memex, slug: str) -> Path:
    node = memex.wiki_store.read(slug)
    assert node is not None and node.file_path
    return Path(node.file_path).parent


def _regenerate(memex: Memex) -> None:
    report = memex.rebuild_index(force=True)
    assert not report.errors, report.errors


# --- Generator: shape, determinism, escaping (AC-0015, AC-0016) ---


def test_regenerate_writes_root_and_descendant_indexes(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Alpha entity", description="An alpha thing")
    memex.write(
        WriteInput(type="procedure", title="Beta procedure page", body="steps", description="")
    )
    _regenerate(memex)
    docs = memex.wiki_store.wiki_dir

    root = (docs / "index.md").read_text(encoding="utf-8")
    assert root.startswith('---\nokf_version: "0.2"\n---\n')
    assert "# index" in root
    assert "[global/](global/index.md)" in root

    scope = (docs / "global" / "index.md").read_text(encoding="utf-8")
    assert not scope.startswith("---")
    assert scope.splitlines()[0] == "# global"
    assert "[entities/](entities/index.md)" in scope
    assert "[procedures/](procedures/index.md)" in scope

    entities = (docs / "global" / "entities" / "index.md").read_text(encoding="utf-8")
    assert entities.splitlines()[0] == "# global/entities"
    assert "## Entities" in entities
    assert "[Alpha entity](alpha-entity.md) — An alpha thing" in entities
    assert "[Beta procedure page](beta-procedure-page.md)" not in entities


def test_project_scoped_pages_get_index_chains(data_dir: Path) -> None:
    memex = _memex(data_dir)
    memex.write(
        WriteInput(
            type="entity",
            title="Project fact",
            body="b",
            scope="project",
            project_id="f" * 24,
            project_label="Acme",
            project_locator="acme",
        )
    )
    _regenerate(memex)
    docs = memex.wiki_store.wiki_dir
    projects = (docs / "projects" / "index.md").read_text(encoding="utf-8")
    assert "[acme/](acme/index.md)" in projects
    acme = (docs / "projects" / "acme" / "entities" / "index.md").read_text(encoding="utf-8")
    assert "[Project fact](project-fact.md)" in acme


def test_entries_without_description_omit_suffix(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Plain page")
    _regenerate(memex)
    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in entities.splitlines() if "Plain page" in ln)
    assert line == "- [Plain page](plain-page.md)"


def test_ordering_is_deterministic_by_type_and_slug(data_dir: Path) -> None:
    memex = _memex(data_dir)
    for title in ("zeta", "alpha", "mid"):
        _write(memex, title)
    memex.write(WriteInput(type="preference", title="a-pref", body="b"))
    _regenerate(memex)
    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    entries = [ln for ln in entities.splitlines() if ln.startswith("- [")]
    assert [ln.split("](")[0][3:] for ln in entries] == ["alpha", "mid", "zeta"]
    prefs = (memex.wiki_store.wiki_dir / "global" / "preferences" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "## Preferences" in prefs

    scope = (memex.wiki_store.wiki_dir / "global" / "index.md").read_text(encoding="utf-8")
    subdirs = [
        ln for ln in scope.splitlines() if ln.startswith("- [") and ln.endswith("/index.md)")
    ]
    assert subdirs.index("- [entities/](entities/index.md)") < subdirs.index(
        "- [preferences/](preferences/index.md)"
    )


def test_regenerate_is_byte_identical_on_repeat(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Stable page", description="stable")
    _regenerate(memex)
    before = {
        p.relative_to(memex.wiki_store.wiki_dir).as_posix(): p.read_bytes()
        for p in memex.wiki_store.wiki_dir.rglob("index.md")
    }
    assert before
    _regenerate(memex)
    after = {
        p.relative_to(memex.wiki_store.wiki_dir).as_posix(): p.read_bytes()
        for p in memex.wiki_store.wiki_dir.rglob("index.md")
    }
    assert after == before


def test_hostile_titles_and_descriptions_render_inert(data_dir: Path) -> None:
    memex = _memex(data_dir)
    slug = _write(
        memex,
        "Evil [link](http://x) <script>alert(1)</script>",
        description="<b>bold</b> `code` *em* _it_ [x](y)",
    )
    _regenerate(memex)
    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in entities.splitlines() if "Evil" in ln)
    # The generator's own page link is the entry link; every other bracket in
    # the line is backslash-escaped inert text (CommonMark renders it as prose).
    assert f"]({slug}.md)" in line
    stripped = line.replace("- [", "", 1).replace(f"]({slug}.md)", "", 1)
    assert "[" not in stripped.replace("\\[", "").replace("\\]", "")
    assert "]" not in stripped.replace("\\[", "").replace("\\]", "")
    assert "<script>" not in entities
    assert "<b>" not in entities


def test_link_escape_outside_docs_root_is_rejected(data_dir: Path) -> None:
    memex = _memex(data_dir)
    node = memex.wiki_store.read(_write(memex, "Outside"))
    assert node is not None
    node.file_path = str(data_dir / "elsewhere.md")
    generator = NavigationGenerator(memex.wiki_store.wiki_dir)
    with pytest.raises(NavigationError):
        generator.regenerate([node])


# --- Reserved files: structural exclusion and legacy safety (AC-0018, AC-0019) ---


def test_structural_reserved_files_are_invisible_to_memory_surfaces(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    slug = _write(memex, "Real page")
    docs = memex.wiki_store.wiki_dir
    entities = docs / "global" / "entities"

    hostile = "See [[real-page]] and secrets on disk\n\n- [x](../../etc/passwd)\n"
    (entities / "index.md").write_text(hostile, encoding="utf-8")
    (entities / "log.md").write_text("# log\n\n- entry\n", encoding="utf-8")
    _regenerate(memex)  # must not raise on the hostile files

    # Structural index.md was replaced by the generator; the log is untouched.
    assert (entities / "log.md").read_text(encoding="utf-8") == "# log\n\n- entry\n"

    errors: list[str] = []
    nodes = memex.wiki_store.scan_all(errors)
    assert [n.slug for n in nodes] == [slug]
    assert errors == []
    assert memex.wiki_store.read("index") is None
    assert memex.wiki_store.read("log") is None

    # Not in FTS, links, or export.
    count = memex.index_manager.count()
    assert count == 1
    links = memex.link_manager.get_link_graph()
    assert links == {}
    export = memex.import_export.export()
    exported = cast("list[dict[str, object]]", export["nodes"])
    assert [n["slug"] for n in exported] == [slug]

    entities_index = (entities / "index.md").read_text(encoding="utf-8")
    assert "[Real page](real-page.md)" in entities_index
    assert "[[real-page]]" not in entities_index


def test_log_md_is_preserved_byte_for_byte_across_regeneration(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Page one")
    entities = memex.wiki_store.wiki_dir / "global" / "entities"
    log_bytes = b"# log\n\n## 2026-09-21\n\n- Creation\n"
    (entities / "log.md").write_bytes(log_bytes)
    _regenerate(memex)
    _regenerate(memex)
    assert (entities / "log.md").read_bytes() == log_bytes


def test_legacy_page_at_reserved_slug_stays_usable_and_blocks_generation(
    data_dir: Path,
) -> None:
    from memex.domain.models import WikiNode

    memex = _memex(data_dir)
    legacy = memex.wiki_store.write(
        WikiNode(
            type="entity",
            title="Legacy index page",
            body="legacy body",
            id="11111111-1111-1111-1111-111111111111",
        )
    )
    legacy_path = Path(legacy.file_path or "")
    legacy_bytes = legacy_path.read_bytes()
    # Place the page at the reserved name by hand (pre-existing store shape).
    target = legacy_path.with_name("index.md")
    target.write_bytes(legacy_bytes)
    legacy_path.unlink()

    assert memex.wiki_store.read("index") is not None
    assert [n.slug for n in memex.wiki_store.list()] == ["index"]

    report = memex.rebuild_index(force=True)
    assert target.read_bytes() == legacy_bytes

    # Verify reports the collision without failing the health of the page set.
    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    assert "collision" in str(nav["detail"])
    assert verdict.checks[0]["ok"] is True  # wiki still parses

    # The rebuild surfaces the collision as a bounded report entry too.
    assert any("collision" in e and "index.md" in e for e in report.navigation_defects)


def test_rebuild_report_separates_navigation_defects_from_node_errors(data_dir: Path) -> None:
    from memex.domain.models import WikiNode as Node

    memex = _memex(data_dir)
    _write(memex, "Report page")
    # A legacy page at the reserved index path blocks generation there.
    legacy = memex.wiki_store.write(
        Node(
            type="entity",
            title="Legacy blocker",
            body="b",
            id="44444444-4444-4444-4444-444444444444",
        )
    )
    assert legacy.file_path
    source = Path(legacy.file_path)
    target = source.with_name("index.md")
    target.write_bytes(source.read_bytes())
    source.unlink()

    report = memex.rebuild_index(force=True)
    assert report.errors == []
    assert report.nodes_errored == 0
    assert any("collision" in d and "index.md" in d for d in report.navigation_defects)


def test_new_writes_never_allocate_reserved_slugs(data_dir: Path) -> None:
    memex = _memex(data_dir)
    store = memex.wiki_store

    def fresh_store_write(title: str) -> str:
        from memex.domain.models import WikiNode

        node = store.write(WikiNode(type="entity", title=title, body="b", id=""))
        return node.slug

    assert fresh_store_write("Index") == "index-2"
    assert fresh_store_write("Log") == "log-2"


def test_structural_index_in_project_dir_does_not_break_project_lookup(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    project_id = "a" * 24
    memex.write(
        WriteInput(
            type="entity",
            title="Proj page",
            body="b",
            scope="project",
            project_id=project_id,
            project_label="Proj",
            project_locator="proj",
        )
    )
    _regenerate(memex)
    # A second write to the same project must resolve its directory even with
    # a structural index.md present.
    memex.write(
        WriteInput(
            type="entity",
            title="Proj page two",
            body="b",
            scope="project",
            project_id=project_id,
            project_label="Proj",
        )
    )
    nodes = [n for n in memex.wiki_store.list() if n.project_id == project_id]
    assert len(nodes) == 2


# --- Refresh after mutations (AC-0017) ---


def test_write_refreshes_affected_navigation_chain(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "First page", description="first")
    docs = memex.wiki_store.wiki_dir
    root = docs / "index.md"
    entities = docs / "global" / "entities" / "index.md"
    assert root.exists() and entities.exists()
    assert "[First page](first-page.md) — first" in entities.read_text(encoding="utf-8")

    _write(memex, "Second page", description="second")
    assert "[Second page](second-page.md) — second" in entities.read_text(encoding="utf-8")
    # Atomic replacement leaves no temp files.
    assert not list(docs.rglob("*.tmp"))


def test_deleting_last_page_removes_index_and_updates_ancestors(data_dir: Path) -> None:
    memex = _memex(data_dir)
    slug = _write(memex, "Only page")
    docs = memex.wiki_store.wiki_dir
    entities_index = docs / "global" / "entities" / "index.md"
    assert entities_index.exists()
    memex.forget(slug, mode="hard")
    assert not entities_index.exists()
    # An empty store keeps no navigation at all (same as a fresh store).
    assert not list(docs.rglob("index.md"))


def test_transcript_capture_refreshes_navigation(data_dir: Path) -> None:
    from memex.domain.models import IngestTranscriptInput, TurnStreamEntry

    memex = _memex(data_dir)
    memex.ingest_transcript(
        IngestTranscriptInput(
            session_id="sess-1",
            turns=[TurnStreamEntry(role="user", content="hello", turn=1)],
        )
    )
    docs = memex.wiki_store.wiki_dir
    episodes = (docs / "global" / "episodes" / "index.md").read_text(encoding="utf-8")
    assert "[Session sess-1](sess-1.md)" in episodes
    assert (docs / "global" / "index.md").exists()
    assert (docs / "index.md").exists()


def test_consolidation_refreshes_navigation(data_dir: Path) -> None:
    import json

    from memex.application.ports import LLMResponse
    from memex.domain.models import ConsolidateInput, IngestTranscriptInput, TurnStreamEntry

    memex = _memex(data_dir)
    memex.ingest_transcript(
        IngestTranscriptInput(
            session_id="sess-c",
            turns=[TurnStreamEntry(role="user", content="prefer ruff over flake8", turn=1)],
        )
    )

    class SingleNodeLLM:
        def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResponse:
            return LLMResponse(
                text=json.dumps(
                    [
                        {
                            "type": "entity",
                            "title": "Distilled fact",
                            "body": "clean consolidated body",
                            "description": "one line signpost",
                            "importance": 0.8,
                        }
                    ]
                ),
                prompt_tokens=1,
                completion_tokens=1,
            )

    memex._llm = SingleNodeLLM()
    report = memex.consolidate(ConsolidateInput(mode="full"))
    assert report.nodes_created

    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "[Distilled fact](distilled-fact.md) — one line signpost" in entities


def test_import_refreshes_navigation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    document: dict[str, object] = {
        "nodes": [
            {
                "slug": "imported-page",
                "type": "entity",
                "title": "Imported page",
                "body": "b",
                "description": "imported desc",
            }
        ]
    }
    result = memex.import_export.import_data(document)
    assert result["imported"] == 1
    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "[Imported page](imported-page.md) — imported desc" in entities


def test_write_refresh_survives_unrelated_malformed_page(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Good page", description="d")
    unrelated = memex.wiki_store.wiki_dir / "global" / "preferences" / "broken.md"
    unrelated.write_text("not front matter at all", encoding="utf-8")
    # The write's own chain refresh must neither parse nor depend on the
    # unrelated malformed page elsewhere in the store.
    _write(memex, "Second page", description="d2")
    entities = (memex.wiki_store.wiki_dir / "global" / "entities" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "[Second page](second-page.md) — d2" in entities


def test_lifecycle_and_merge_refresh_navigation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    slug = _write(memex, "Pending page", description="before")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    assert "— before" in entities_index.read_text(encoding="utf-8")

    node = memex.wiki_store.read(slug)
    assert node is not None and node.file_path
    path = Path(node.file_path)
    text = path.read_text(encoding="utf-8").replace("before", "after")
    path.write_text(text, encoding="utf-8")

    other = _write(memex, "Merge target", description="target")
    memex.merge(other, slug)
    merged_index = entities_index.read_text(encoding="utf-8")
    assert "— after" in merged_index


def test_open_does_not_generate_navigation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Page while open")
    docs = memex.wiki_store.wiki_dir
    for index in docs.rglob("index.md"):
        index.unlink()
    memex.close()
    fresh = _memex(data_dir)
    try:
        assert not list(fresh.wiki_store.wiki_dir.rglob("index.md"))
    finally:
        fresh.close()


def test_rebuild_flag_controls_navigation_regeneration(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Rebuild page")
    docs = memex.wiki_store.wiki_dir
    entities_index = docs / "global" / "entities" / "index.md"
    entities_index.unlink()

    # The open-upgrade form (suppressed regeneration) never touches navigation.
    memex.rebuild_index(force=True, regenerate_navigation=False)
    assert not entities_index.exists()

    # The explicit rebuild path regenerates it, with or without --force.
    memex.rebuild_index(force=False)
    assert entities_index.exists()


def test_navigation_refresh_failure_never_fails_the_mutation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Baseline")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    entities_index.unlink()

    from memex.infrastructure.store import navigation as nav_module

    original = nav_module.NavigationGenerator.refresh_page

    def exploding(
        self: NavigationGenerator,
        page_path: Path,
        node: object,
        scan_dir: object,
    ) -> NavigationReport:
        raise OSError("disk exploded /home/secret-user")

    nav_module.NavigationGenerator.refresh_page = exploding  # type: ignore[method-assign]
    try:
        node = memex.write(WriteInput(type="entity", title="Still works", body="b"))
        assert node.slug == "still-works"
        assert memex.wiki_store.read("still-works") is not None
    finally:
        nav_module.NavigationGenerator.refresh_page = original  # type: ignore[method-assign]

    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    assert nav["ok"] is False
    assert "missing" in str(nav["detail"])

    _regenerate(memex)
    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    assert nav["ok"] is True


def test_recall_available_with_indexes_missing_and_regeneration_restores(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    _write(memex, "Searchable page", description="needlè word")
    docs = memex.wiki_store.wiki_dir
    for index in docs.rglob("index.md"):
        index.unlink()
    hits = memex.recall("needlè").hits
    assert [h.slug for h in hits] == ["searchable-page"]
    _regenerate(memex)
    assert (docs / "index.md").exists()


# --- Verify integration (AC-0020) ---


def test_verify_reports_stale_orphan_and_missing_with_bounded_details(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    _write(memex, "Verified page", description="v")
    _regenerate(memex)
    docs = memex.wiki_store.wiki_dir

    # Stale: rewrite the index with wrong content.
    entities_index = docs / "global" / "entities" / "index.md"
    entities_index.write_text("# stale\n", encoding="utf-8")
    # Orphan: structural index in a directory with no pages below it.
    orphan_dir = docs / "global" / "preferences"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "index.md").write_text("# orphan\n", encoding="utf-8")
    # Missing: remove the scope index.
    (docs / "global" / "index.md").unlink()

    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    detail = str(nav["detail"])
    assert nav["ok"] is False
    for token in ("missing", "stale", "orphan"):
        assert token in detail
    # Bounded: docs-relative POSIX paths only, never absolute or host text.
    assert "global/entities/index.md" in detail
    assert str(data_dir) not in detail
    assert "/home/" not in detail
    assert "Verified page" not in detail


def test_navigation_diagnostics_never_carry_memory_content_or_secrets(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    secret = "sk-proj-1234567890abcdefghij"  # noqa: S105 - fixture, never real
    _write(
        memex,
        "Title with sk-proj-1234567890abcdefghij inside",
        body="body",
        description="desc with sk-proj-1234567890abcdefghij",
    )
    report = memex.rebuild_index(force=True)
    for message in (*report.errors, *report.navigation_defects):
        assert secret not in message
    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    assert secret not in str(nav["detail"])
    assert "Title with" not in str(nav["detail"])


# --- Watcher integration (AC-0018) ---


def test_generator_writes_do_not_feed_back_into_memory_reindex(data_dir: Path) -> None:
    from memex.infrastructure.store.watcher import IndexWatcher

    memex = _memex(data_dir)
    _write(memex, "Watched page")
    watcher = IndexWatcher(
        memex.wiki_store.wiki_dir,
        memex.index_manager,
        memex.wiki_store,
        link_mgr=memex.link_manager,
        navigation=memex.navigation,
    )
    assert watcher.check() == []
    _regenerate(memex)
    assert watcher.check() == []
    assert watcher.reindex_changed() == 0


def test_external_edit_refreshes_navigation(data_dir: Path) -> None:
    from memex.infrastructure.store.watcher import IndexWatcher

    memex = _memex(data_dir)
    slug = _write(memex, "Edited page", description="old text")
    watcher = IndexWatcher(
        memex.wiki_store.wiki_dir,
        memex.index_manager,
        memex.wiki_store,
        link_mgr=memex.link_manager,
        navigation=memex.navigation,
    )
    node = memex.wiki_store.read(slug)
    assert node is not None and node.file_path
    path = Path(node.file_path)
    text = path.read_text(encoding="utf-8").replace("old text", "new text")
    path.write_text(text, encoding="utf-8")

    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    assert watcher.reindex_changed() == 1
    assert "new text" in entities_index.read_text(encoding="utf-8")

    # External deletion removes the obsolete index through the watcher path.
    path.unlink()
    watcher.reindex_changed()
    assert not entities_index.exists()


# --- Backup carries the disposable views harmlessly (informational) ---


def test_backup_includes_and_restore_rebuilds_navigation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Backed page", description="bk")
    _regenerate(memex)
    archive = data_dir / "backup.tar.gz"
    memex.backup(archive)
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert any(n.endswith("global/entities/index.md") for n in names)

    memex.restore(archive)
    docs = memex.wiki_store.wiki_dir
    assert (docs / "global" / "entities" / "index.md").exists()
    assert "[Backed page](backed-page.md) — bk" in (
        docs / "global" / "entities" / "index.md"
    ).read_text(encoding="utf-8")


def test_navigation_report_only_contains_bounded_fields(data_dir: Path) -> None:
    from dataclasses import asdict

    memex = _memex(data_dir)
    _write(memex, "Bounded", description="bounded desc")
    generator = NavigationGenerator(memex.wiki_store.wiki_dir)
    report = generator.regenerate(memex.wiki_store.scan_all())
    for change in report.changes:
        assert change.category in {"written", "removed", "collision", "write_failed"}
        rel = Path(change.path)
        assert not rel.is_absolute()
        assert ".." not in rel.parts
        assert "\\" not in change.path
    assert json.dumps([asdict(change) for change in report.changes[:1]])


def test_undecodable_reserved_file_is_untouchable_collision(
    data_dir: Path,
) -> None:
    """A non-UTF-8 reserved file never classifies structural (no overwrite)."""
    from memex.domain.reserved import classify_reserved

    memex = _memex(data_dir)
    _write(memex, "Alpha entity")
    target = _page_dir(memex, "alpha-entity") / "index.md"
    target.write_bytes(b"\xff\xfe not utf8")
    assert classify_reserved(target) == "not_reserved"
    errors: list[str] = []
    report = memex.navigation.regenerate(memex.wiki_store.scan_all(errors))
    assert errors  # reported as a malformed page, never a crash
    assert report.by_category("collision")
    assert target.read_bytes() == b"\xff\xfe not utf8"


def test_symlinked_reserved_file_never_read_for_classification(
    data_dir: Path,
) -> None:
    """A symlink at a reserved name classifies not_reserved without reading it."""
    from memex.domain.reserved import classify_reserved

    memex = _memex(data_dir)
    _write(memex, "Alpha entity")
    page = _page_dir(memex, "alpha-entity") / "alpha-entity.md"
    link = _page_dir(memex, "alpha-entity") / "index.md"
    link.unlink()  # remove the generated structural index at that path
    link.symlink_to(page)
    assert classify_reserved(link) == "not_reserved"
    errors: list[str] = []
    nodes = memex.wiki_store.scan_all(errors)
    assert [n.slug for n in nodes] == ["alpha-entity"]  # real page still scanned
    assert errors  # the symlinked reserved name reports as an unsafe page path


def test_refresh_partial_log_is_bounded_and_excludes_routine_removals(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only genuine defects log; routine removals stay silent (round-5 pin)."""
    import logging

    memex = _memex(data_dir)
    memex.logger.propagate = True  # caplog visibility; production keeps False
    slug = _write(memex, "Doomed page")
    memex.logger.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger="memex"):
        memex.forget(slug, mode="hard")  # routine removal: no warning
    assert not [r for r in caplog.records if "navigation_refresh" in r.getMessage()]

    entities = memex.wiki_store.wiki_dir / "global" / "entities"
    entities.mkdir(parents=True, exist_ok=True)
    (entities / "index.md").write_text(
        "---\n"
        'id: "legacy-1"\n'
        'type: "entity"\n'
        'title: "Legacy blocker"\n'
        "tags: []\n"
        "importance: 0.5\n"
        'created: "2026-09-21T00:00:00Z"\n'
        'updated: "2026-09-21T00:00:00Z"\n'
        "access_count: 0\n"
        "links: []\n"
        'content_hash: "sha256:00"\n'
        'status: "active"\n'
        'scope: "global"\n'
        "---\n\nbody\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="memex"):
        memex._refresh_navigation(str(entities / "doomed-page.md"))
    partial = [r for r in caplog.records if "status=partial" in r.getMessage()]
    assert partial and "collision=1" in partial[0].getMessage()
    assert "Legacy" not in partial[0].getMessage()  # bounded: no memory content


# --- Incremental single-page refresh (incremental-navigation spec) ---


def _oracle_clean(memex: Memex) -> None:
    """Every index.md on disk equals the full render; none is missing or orphaned."""
    errors: list[str] = []
    nodes = memex.wiki_store.scan_all(errors)
    assert errors == []
    assert memex.navigation.diagnose(nodes) == []


def _update(
    memex: Memex, slug: str, *, title: str | None = None, description: str | None = None
) -> None:
    node = memex.wiki_store.read(slug)
    assert node is not None
    if title is not None:
        node.title = title
    if description is not None:
        node.description = description
    stored = memex.wiki_store.write(node)
    memex._refresh_navigation(stored.file_path)


def _read_spy(monkeypatch: pytest.MonkeyPatch, docs: Path) -> list[Path]:
    """Record every ``*.md`` under ``docs`` opened through ``Path.read_text``."""
    opened: list[Path] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix == ".md" and self.is_relative_to(docs):
            opened.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    return opened


def test_randomized_mutation_sequence_matches_full_render_after_every_step(
    data_dir: Path,
) -> None:
    import random

    memex = _memex(data_dir)
    rng = random.Random(20260923)  # noqa: S311 - reproducible sequence, not security
    live: dict[str, str] = {}  # slug -> type
    words = ["alpha", "beta", "gamma", "delta", "kappa", "omega", "Evil [x](y)", "under_score"]
    for step in range(120):
        roll = rng.random()
        if live and roll < 0.25:
            slug = rng.choice(sorted(live))
            memex.forget(slug, mode="hard")
            del live[slug]
        elif live and roll < 0.55:
            slug = rng.choice(sorted(live))
            _update(
                memex,
                slug,
                title=f"{rng.choice(words)} {step}",
                description=rng.choice(["", f"{rng.choice(words)} desc {step}"]),
            )
        else:
            node_type = rng.choice(["entity", "preference"])
            node = memex.write(
                WriteInput(
                    type=node_type,
                    title=f"{rng.choice(words)} {rng.randint(0, 99)}",
                    body="b",
                    description=rng.choice(["", f"{rng.choice(words)} signpost"]),
                )
            )
            live[node.slug] = node_type
        _oracle_clean(memex)
    assert live  # the sequence exercised both directories and left pages behind
    assert {*live.values()} == {"entity", "preference"}


def test_single_page_refresh_opens_a_constant_number_of_files(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memex = _memex(data_dir)
    docs = memex.wiki_store.wiki_dir
    entities = docs / "global" / "entities"

    def opened_by_one_write(title: str) -> set[Path]:
        opened = _read_spy(monkeypatch, docs)
        node = memex.write(WriteInput(type="entity", title=title, body="b", description="d"))
        monkeypatch.undo()
        assert node.file_path
        return {*opened}

    for i in range(4):
        _write(memex, f"seed {i}")
    small = opened_by_one_write("probe small")
    for i in range(4, 200):
        _write(memex, f"seed {i}")
    large = opened_by_one_write("probe large")

    assert len(small) == len(large) == 2
    assert large == {entities / "probe-large.md", entities / "index.md"}
    # Ancestors list child directories only; a sibling write never opens them.
    assert (docs / "global" / "index.md") not in large
    assert (docs / "index.md") not in large
    _oracle_clean(memex)


def test_single_page_refresh_never_scans_the_directory(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "seed one")
    calls: list[Path] = []
    original = memex.wiki_store.scan_dir

    def counting_scan_dir(directory: Path, errors: list[str] | None = None) -> list[WikiNode]:
        calls.append(directory)
        return original(directory, errors)

    memex.wiki_store.scan_dir = counting_scan_dir  # type: ignore[method-assign]
    slug = _write(memex, "seed two", description="two")
    _update(memex, slug, title="seed two renamed")
    memex.forget(slug, mode="hard")
    assert calls == []
    _oracle_clean(memex)


def test_missing_index_falls_back_to_full_render(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "first")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    entities_index.unlink()
    _write(memex, "second", description="two")
    assert "[first](first.md)" in entities_index.read_text(encoding="utf-8")
    _oracle_clean(memex)


def test_foreign_structural_index_falls_back_to_full_render(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "first")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    entities_index.write_text("# hand written\n\n- [x](first.md)\n", encoding="utf-8")
    _write(memex, "second")
    assert entities_index.read_text(encoding="utf-8").startswith("# global/entities\n")
    _oracle_clean(memex)


def test_ambiguous_row_falls_back_to_full_render(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "first")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    entities_index.write_text(
        "# global/entities\n\n## Entities\n- [first](first.md)\n\n"
        "## Preferences\n- [first](first.md)\n",
        encoding="utf-8",
    )
    _update(memex, "first", description="renamed")
    text = entities_index.read_text(encoding="utf-8")
    assert text.count("(first.md)") == 1
    assert "## Preferences" not in text
    _oracle_clean(memex)


def test_delete_of_unlisted_page_falls_back_to_full_render(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "first")
    slug = _write(memex, "second")
    entities_index = memex.wiki_store.wiki_dir / "global" / "entities" / "index.md"
    entities_index.write_text(
        "# global/entities\n\n## Entities\n- [first](first.md)\n", encoding="utf-8"
    )
    memex.forget(slug, mode="hard")
    _oracle_clean(memex)


def test_legacy_page_at_index_path_is_untouched_by_single_page_refresh(data_dir: Path) -> None:
    from memex.domain.models import WikiNode

    memex = _memex(data_dir)
    _write(memex, "first")
    entities = memex.wiki_store.wiki_dir / "global" / "entities"
    legacy = memex.wiki_store.write(
        WikiNode(
            type="entity", title="Legacy", body="legacy", id="22222222-2222-2222-2222-222222222222"
        )
    )
    legacy_bytes = Path(legacy.file_path or "").read_bytes()
    (entities / "index.md").write_bytes(legacy_bytes)
    Path(legacy.file_path or "").unlink()

    report = memex.navigation.refresh_page(
        entities / "first.md", memex.wiki_store.read("first"), memex.wiki_store.scan_dir
    )
    assert (entities / "index.md").read_bytes() == legacy_bytes
    assert [c.path for c in report.by_category("collision")] == ["global/entities/index.md"]


def test_deleting_last_page_of_one_type_dir_rewrites_only_the_first_populated_ancestor(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memex = _memex(data_dir)
    _write(memex, "entity page")
    pref = memex.write(WriteInput(type="preference", title="pref page", body="b"))
    docs = memex.wiki_store.wiki_dir
    opened = _read_spy(monkeypatch, docs)
    memex.forget(pref.slug, mode="hard")
    monkeypatch.undo()
    assert not (docs / "global" / "preferences" / "index.md").exists()
    assert (docs / "index.md") not in {*opened}
    assert "[preferences/]" not in (docs / "global" / "index.md").read_text(encoding="utf-8")
    _oracle_clean(memex)


def test_verify_navigation_stays_consistent_through_incremental_refreshes(
    data_dir: Path,
) -> None:
    memex = _memex(data_dir)
    first = _write(memex, "first", description="one")
    second = _write(memex, "second")
    _update(memex, first, title="first renamed", description="")
    memex.forget(second, mode="hard")
    memex.write(WriteInput(type="procedure", title="steps", body="b", description="how"))
    verdict = verify(memex)
    nav = next(c for c in verdict.checks if c["check"] == "navigation-consistent")
    assert nav["ok"] is True, nav["detail"]


def test_rebuild_index_still_regenerates_every_index(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "first", description="one")
    docs = memex.wiki_store.wiki_dir
    for index in docs.rglob("index.md"):
        index.write_text("# wrong\n", encoding="utf-8")
    _regenerate(memex)
    _oracle_clean(memex)


def test_orphan_index_falls_back_to_full_render(data_dir: Path) -> None:
    """A bare heading with no rows means the directory is only now gaining pages."""
    memex = _memex(data_dir)
    docs = memex.wiki_store.wiki_dir
    (docs / "global" / "entities" / "index.md").write_text("# global/entities\n", encoding="utf-8")
    _write(memex, "first")
    assert "[entities/](entities/index.md)" in (docs / "global" / "index.md").read_text(
        encoding="utf-8"
    )
    _oracle_clean(memex)


def test_unparsable_mutated_page_falls_back_to_full_render(data_dir: Path) -> None:
    """A page the store cannot parse leaves no stale row: the chain render drops it."""
    memex = _memex(data_dir)
    _write(memex, "first")
    slug = _write(memex, "second", description="two")
    page = memex.wiki_store.wiki_dir / "global" / "entities" / f"{slug}.md"
    page.write_text('---\ntype: "entity"\nbogus: 1\n---\nbody\n', encoding="utf-8")
    memex._refresh_navigation(str(page))
    index_text = (page.parent / "index.md").read_text(encoding="utf-8")
    assert "(first.md)" in index_text
    assert "(second.md)" not in index_text
    errors: list[str] = []
    nodes = memex.wiki_store.scan_all(errors)
    assert len(errors) == 1
    assert memex.navigation.diagnose(nodes) == []


def test_declared_types_get_their_own_heading_after_builtins(data_dir: Path) -> None:
    """Each declared type gets its own directory and canonical heading; the
    project index only links to it, exactly as a built-in type's does.

    A directory holds pages of exactly one type (built-in or declared) under
    the store's per-type layout, so ``render``'s NODE_TYPES-then-alphabetical
    ordering contract is exercised separately below, against synthetic nodes
    sharing one directory.
    """
    memex = _memex(data_dir)
    memex.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    memex.wiki_store.declare_type("access-matrix", scope="project", project_id=PROJECT)
    memex.write(
        WriteInput(type="entity", title="Kafka", body="b", scope="project", project_id=PROJECT)
    )
    memex.write(
        WriteInput(
            type="decision", title="Choose Kafka", body="b", scope="project", project_id=PROJECT
        )
    )
    memex.write(
        WriteInput(
            type="access-matrix", title="Who edits", body="b", scope="project", project_id=PROJECT
        )
    )
    project_dir = memex.wiki_store.get_path("kafka").parent.parent
    assert "## Entities" in (project_dir / "entities" / "index.md").read_text()
    assert "## Access-matrix" in (project_dir / "access-matrix" / "index.md").read_text()
    assert "## Decision" in (project_dir / "decision" / "index.md").read_text()
    assert (
        "- [Choose Kafka](choose-kafka.md)" in (project_dir / "decision" / "index.md").read_text()
    )
    _oracle_clean(memex)


def test_render_orders_builtins_then_declared_types_alphabetically(data_dir: Path) -> None:
    """render() lists NODE_TYPES sections first, then other present types
    alphabetically; a real single-page splice against that shape preserves it.
    """
    memex = _memex(data_dir)
    _write(memex, "Kafka")
    directory = memex.wiki_store.wiki_dir / "global" / "entities"
    synthetic = [
        WikiNode(
            type="entity",
            title="Kafka",
            body="b",
            id="1",
            slug="kafka",
            file_path=str(directory / "kafka.md"),
        ),
        WikiNode(
            type="decision",
            title="Choose Kafka",
            body="b",
            id="2",
            slug="choose-kafka",
            file_path=str(directory / "choose-kafka.md"),
        ),
        WikiNode(
            type="access-matrix",
            title="Who edits",
            body="b",
            id="3",
            slug="who-edits",
            file_path=str(directory / "who-edits.md"),
        ),
    ]
    text = memex.navigation.render(directory, synthetic)
    assert text.index("## Entities") < text.index("## Access-matrix") < text.index("## Decision")
    (directory / "index.md").write_text(text, encoding="utf-8")

    _write(memex, "Second")
    spliced = (directory / "index.md").read_text(encoding="utf-8")
    assert (
        spliced.index("## Entities")
        < spliced.index("## Access-matrix")
        < spliced.index("## Decision")
    )
    assert "[Kafka](kafka.md)" in spliced
    assert "[Second](second.md)" in spliced
    assert "[Who edits](who-edits.md)" in spliced
    assert "[Choose Kafka](choose-kafka.md)" in spliced


def test_declared_type_rows_survive_the_splice_oracle(data_dir: Path) -> None:
    memex = _memex(data_dir)
    memex.wiki_store.declare_type("rule", scope="project", project_id=PROJECT)
    for i in range(4):
        memex.write(
            WriteInput(
                type="rule", title=f"Rule {i}", body="b", scope="project", project_id=PROJECT
            )
        )
    memex.forget("rule-1", mode="hard")
    _oracle_clean(memex)
