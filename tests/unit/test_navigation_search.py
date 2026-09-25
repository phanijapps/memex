"""Dependency-free recall over generated ``index.md`` navigation files.

Pins the navigation-search spec: rows parsed from real generated indexes,
deterministic weighted overlap ranking, filters, the zero-row FTS5 fallback,
reserved-file handling, front-matter-only page reads, and the CLI flag.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from memex import cli
from memex.application.memory import Memex
from memex.domain.models import WriteInput
from memex.infrastructure.config import ConfigLoader
from memex.infrastructure.search.bm25_retriever import MAX_QUERY_TOKENS
from memex.infrastructure.store.navigation_search import (
    FALLBACK_SEARCH_ENGINE,
    SEARCH_ENGINE,
    NavigationSearch,
    _read_front_matter,
)

PROJECT_ID = "a" * 24


def _memex(data_dir: Path) -> Memex:
    return Memex(ConfigLoader().load(data_dir=data_dir))


def _write(memex: Memex, title: str, **kwargs: Any) -> str:
    kwargs.setdefault("type", "entity")
    kwargs.setdefault("body", "plain body words")
    return memex.write(WriteInput(title=title, **kwargs)).slug


def _search(memex: Memex) -> NavigationSearch:
    return NavigationSearch(memex.wiki_store.wiki_dir)


# --- Row parsing against real generated indexes ---


def test_rows_parsed_from_generated_indexes_fill_hit_fields(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(
        memex,
        "Kafka [broker] settings",
        description="Retention *and* partitions",
        tags=["infra", "kafka"],
        importance=0.8,
    )
    _write(
        memex,
        "Kafka project note",
        type="procedure",
        description="project-only steps",
        scope="project",
        project_id=PROJECT_ID,
        project_label="Proj A",
        project_locator="proj-a-dir",
    )

    result = _search(memex).search("kafka", scope="global")

    assert result.search_engine == SEARCH_ENGINE
    assert result.total_indexed == 2
    # Global recall spans global and project pages, as the FTS5 retriever does.
    assert [hit.slug for hit in result.hits] == ["kafka-broker-settings", "kafka-project-note"]
    hit = result.hits[0]
    assert hit.rank == 1
    assert hit.title == "Kafka [broker] settings"
    assert hit.description == "Retention *and* partitions"
    assert hit.node_type == "entity"
    assert hit.scope == "global"
    assert hit.project_id is None
    assert hit.tags == ["infra", "kafka"]
    assert hit.importance == 0.8
    assert hit.status == "active"
    assert hit.created and hit.timestamp and hit.updated_at
    assert (
        Path(hit.file_path)
        == memex.wiki_store.wiki_dir / "global/entities/kafka-broker-settings.md"
    )

    project = _search(memex).search("kafka", scope="project", project_id=PROJECT_ID)
    assert [hit.slug for hit in project.hits] == ["kafka-project-note"]
    assert project.hits[0].node_type == "procedure"
    assert project.hits[0].project_id == PROJECT_ID
    assert project.hits[0].project_label == "Proj A"


def test_rows_without_description_match_on_title_only(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Bare title page")

    result = _search(memex).search("bare title")

    assert [hit.slug for hit in result.hits] == ["bare-title-page"]
    assert result.hits[0].description == ""
    assert result.hits[0].snippet_source == "title"


# --- Ranking: weights, determinism, tie-break ---


def test_title_match_ranks_above_description_match(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Zulu kafka", description="unrelated")
    _write(memex, "Alpha thing", description="kafka notes live here")
    _write(memex, "Omega other", description="nothing shared")

    result = _search(memex).search("kafka")

    assert [hit.slug for hit in result.hits] == ["zulu-kafka", "alpha-thing"]
    assert result.hits[0].score > result.hits[1].score
    assert result.hits[0].snippet_source == "title"
    assert result.hits[1].snippet_source == "description"
    assert result.hits[1].snippet == "kafka notes live here"


def test_equal_scores_tie_break_by_slug_and_results_are_deterministic(data_dir: Path) -> None:
    memex = _memex(data_dir)
    for title in ("Kafka delta", "Kafka charlie", "Kafka bravo"):
        _write(memex, title)

    first = _search(memex).search("kafka")
    second = _search(memex).search("kafka")

    assert [hit.slug for hit in first.hits] == ["kafka-bravo", "kafka-charlie", "kafka-delta"]
    assert [hit.rank for hit in first.hits] == [1, 2, 3]
    assert [(hit.slug, hit.score) for hit in second.hits] == [
        (hit.slug, hit.score) for hit in first.hits
    ]


def test_top_k_and_query_bounds(data_dir: Path) -> None:
    memex = _memex(data_dir)
    for title in ("Kafka one", "Kafka two", "Kafka three"):
        _write(memex, title)
    search = _search(memex)

    assert len(search.search("kafka", top_k=2).hits) == 2
    with pytest.raises(ValueError, match="top_k"):
        search.search("kafka", top_k=0)
    with pytest.raises(ValueError, match="searchable"):
        search.search("!!!")
    with pytest.raises(ValueError, match="too many"):
        search.search(" ".join(f"t{i}" for i in range(MAX_QUERY_TOKENS + 1)))


# --- Filters ---


def test_node_type_and_tags_filters(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity", tags=["infra"])
    _write(memex, "Kafka procedure", type="procedure", tags=["infra", "runbook"])
    search = _search(memex)

    assert [hit.slug for hit in search.search("kafka", node_type="procedure").hits] == [
        "kafka-procedure"
    ]
    assert [hit.slug for hit in search.search("kafka", tags=["infra"]).hits] == [
        "kafka-entity",
        "kafka-procedure",
    ]
    assert [hit.slug for hit in search.search("kafka", tags=["infra", "runbook"]).hits] == [
        "kafka-procedure"
    ]
    assert search.search("kafka", tags=["missing"]).hits == []


def test_scope_filter_requires_project_id_and_rejects_unknown_scope(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity")
    search = _search(memex)

    with pytest.raises(ValueError, match="project_id"):
        search.search("kafka", scope="project")
    with pytest.raises(ValueError, match="scope"):
        search.search("kafka", scope="team")


def test_visibility_follows_status_and_validity_window(data_dir: Path) -> None:
    memex = _memex(data_dir)
    archived = _write(memex, "Kafka archived")
    retired = _write(memex, "Kafka retired")
    _write(memex, "Kafka active")
    memex.forget(archived, mode="archive")
    memex.forget(retired, mode="soft")
    search = _search(memex)

    assert [hit.slug for hit in search.search("kafka").hits] == ["kafka-active"]
    assert [hit.slug for hit in search.search("kafka", include_inactive=True).hits] == [
        "kafka-active",
        "kafka-archived",
    ]
    assert [hit.slug for hit in search.search("kafka", include_expired=True).hits] == [
        "kafka-active",
        "kafka-retired",
    ]


def test_time_range_is_rejected_naming_the_engine(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity")

    with pytest.raises(ValueError, match=SEARCH_ENGINE):
        _search(memex).search("kafka", time_range=("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"))
    with pytest.raises(ValueError, match=SEARCH_ENGINE):
        memex.recall(
            "kafka",
            engine="navigation",
            time_range=("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),
        )


# --- Reserved files and page reads ---


def test_legacy_index_page_rows_are_never_parsed(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity")
    legacy = memex.wiki_store.wiki_dir / "global/procedures/index.md"
    real = memex.wiki_store.wiki_dir / "global/entities/kafka-entity.md"
    front_matter = real.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    legacy.write_text(
        front_matter.replace('type: "entity"', 'type: "procedure"').replace(
            'title: "Kafka entity"', 'title: "Legacy kafka page"'
        )
        + "\n---\n\n- [Phantom kafka](phantom.md) — never a row\n",
        encoding="utf-8",
    )

    result = _search(memex).search("kafka phantom")

    assert [hit.slug for hit in result.hits] == ["kafka-entity"]
    assert result.total_indexed == 1


def test_only_front_matter_of_chosen_pages_is_read(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memex = _memex(data_dir)
    body = "kafka body words " * 2000
    _write(memex, "Kafka entity", body=body)
    _write(memex, "Kafka second", body=body)
    _write(memex, "Unrelated page", body=body)
    docs = memex.wiki_store.wiki_dir
    page = docs / "global/entities/kafka-entity.md"
    front_matter_end = page.read_text(encoding="utf-8").index("\n---\n", 4) + len("\n---\n")
    consumed: dict[Path, int] = {}
    real_open = Path.open

    class _Counting:
        def __init__(self, path: Path, handle: Any) -> None:
            self._path, self._handle = path, handle

        def readline(self, *args: Any) -> str:
            line: str = self._handle.readline(*args)
            consumed[self._path] = consumed.get(self._path, 0) + len(line)
            return line

        def read(self, *args: Any) -> str:
            text: str = self._handle.read(*args)
            consumed[self._path] = consumed.get(self._path, 0) + len(text)
            return text

        def __iter__(self) -> Any:
            for line in self._handle:
                consumed[self._path] = consumed.get(self._path, 0) + len(line)
                yield line

        def __enter__(self) -> _Counting:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        return _Counting(self, real_open(self, *args, **kwargs))

    def forbid_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name != "index.md":
            raise AssertionError(f"read_text on a page: {self.relative_to(docs)}")
        text: str = real_read_text(self, *args, **kwargs)
        return text

    real_read_text = Path.read_text
    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", forbid_read_text)

    result = NavigationSearch(docs).search("kafka", top_k=1)

    assert [hit.slug for hit in result.hits] == ["kafka-entity"]
    pages_read = {path.relative_to(docs).as_posix() for path in consumed if path.name != "index.md"}
    assert pages_read == {"global/entities/kafka-entity.md"}
    assert consumed[page] < len(body)
    assert consumed[page] <= front_matter_end


def test_project_scope_opens_each_candidate_page_once(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memex = _memex(data_dir)
    for title in ("Kafka one", "Kafka two"):
        _write(
            memex,
            title,
            scope="project",
            project_id=PROJECT_ID,
            project_label="Proj A",
            project_locator="proj-a-dir",
        )
    docs = memex.wiki_store.wiki_dir
    opened: dict[str, int] = {}
    real_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name != "index.md":
            key = self.relative_to(docs).as_posix()
            opened[key] = opened.get(key, 0) + 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    result = NavigationSearch(docs).search("kafka", top_k=1, scope="project", project_id=PROJECT_ID)

    assert [hit.slug for hit in result.hits] == ["kafka-one"]
    assert opened == {"projects/proj-a-dir/entities/kafka-one.md": 1}


def test_front_matter_ceiling_counts_bytes_not_characters(tmp_path: Path) -> None:
    # 9,000 two-byte characters: 18,000 bytes, under the ceiling when counted in characters.
    multibyte = tmp_path / "multibyte.md"
    multibyte.write_text('---\ntitle: "' + "\u00e9" * 9_000 + '"\n---\nbody\n', encoding="utf-8")
    ascii_page = tmp_path / "ascii.md"
    ascii_page.write_text('---\ntitle: "' + "e" * 9_000 + '"\n---\nbody\n', encoding="utf-8")

    assert _read_front_matter(multibyte) is None
    assert (
        _read_front_matter(ascii_page) == ascii_page.read_text(encoding="utf-8")[: -len("body\n")]
    )


# --- Facade: engine parameter and zero-row fallback ---


def test_recall_engine_parameter_selects_navigation(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity")

    default = memex.recall("kafka")
    navigation = memex.recall("kafka", engine="navigation")

    assert default.search_engine == "semantic-and-fallback-fts5"
    assert navigation.search_engine == SEARCH_ENGINE
    assert [hit.slug for hit in navigation.hits] == ["kafka-entity"]


def test_recall_serves_navigation_when_index_has_zero_rows(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka entity", description="broker settings")
    _write(memex, "Other page")
    with memex.index_manager.connection:
        memex.index_manager.connection.execute("DELETE FROM wiki_index")
    assert memex.index_manager.count() == 0

    result = memex.recall("kafka")

    assert result.search_engine == FALLBACK_SEARCH_ENGINE
    assert [hit.slug for hit in result.hits] == ["kafka-entity"]
    assert result.total_indexed == 2


def test_default_recall_and_fallback_return_the_same_pages(data_dir: Path) -> None:
    memex = _memex(data_dir)
    _write(memex, "Kafka global")
    _write(
        memex,
        "Kafka project",
        scope="project",
        project_id=PROJECT_ID,
        project_label="Proj A",
        project_locator="proj-a-dir",
    )
    fts5 = {hit.slug for hit in memex.recall("kafka").hits}
    navigation = {hit.slug for hit in memex.recall("kafka", engine="navigation").hits}
    with memex.index_manager.connection:
        memex.index_manager.connection.execute("DELETE FROM wiki_index")

    fallback = memex.recall("kafka")

    assert fallback.search_engine == FALLBACK_SEARCH_ENGINE
    assert (
        fts5
        == navigation
        == {hit.slug for hit in fallback.hits}
        == {
            "kafka-global",
            "kafka-project",
        }
    )


def test_recall_with_time_range_on_empty_index_warns_without_the_query(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    memex = _memex(data_dir)
    memex.logger.propagate = True  # caplog visibility; production keeps False
    _write(memex, "Kafka entity")
    with memex.index_manager.connection:
        memex.index_manager.connection.execute("DELETE FROM wiki_index")

    with caplog.at_level(logging.WARNING, logger="memex"):
        result = memex.recall("kafka", time_range=("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"))

    assert result.search_engine == "semantic-and-fallback-fts5"
    assert result.hits == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["operation=recall engine=fts5 indexed_rows=0 reason=time_range hits=0"]


def test_recall_fallback_honors_max_tokens_and_top_k(data_dir: Path) -> None:
    memex = _memex(data_dir)
    for title in ("Kafka one", "Kafka two"):
        _write(memex, title)
    with memex.index_manager.connection:
        memex.index_manager.connection.execute("DELETE FROM wiki_index")

    result = memex.recall("kafka", top_k=1, max_tokens=64)

    assert result.search_engine == FALLBACK_SEARCH_ENGINE
    assert [(hit.slug, hit.rank) for hit in result.hits] == [("kafka-one", 1)]


def test_recall_stays_on_fts5_when_the_store_holds_no_pages(data_dir: Path) -> None:
    memex = _memex(data_dir)

    result = memex.recall("kafka")

    assert result.search_engine == "semantic-and-fallback-fts5"
    assert result.hits == []


# --- CLI ---


def test_cli_engine_flag_defaults_to_fts5_and_accepts_navigation(
    data_dir: Path, capture: dict[str, str]
) -> None:
    base = ["--data-dir", str(data_dir)]
    assert (
        cli.main([*base, "write", "--type", "entity", "--title", "Kafka entity", "--body", "b"])
        == 0
    )

    assert cli.main([*base, "recall", "kafka"]) == 0
    assert json.loads(capture["out"])["search_engine"] == "semantic-and-fallback-fts5"

    assert cli.main([*base, "recall", "kafka", "--engine", "navigation"]) == 0
    payload = json.loads(capture["out"])
    assert payload["search_engine"] == SEARCH_ENGINE
    assert [hit["slug"] for hit in payload["hits"]] == ["kafka-entity"]

    with pytest.raises(SystemExit):
        cli.main([*base, "recall", "kafka", "--engine", "grep"])


def test_navigation_engine_reports_declared_types(data_dir: Path) -> None:
    memex = _memex(data_dir)
    memex.wiki_store.declare_type("decision", scope="project", project_id=PROJECT_ID)
    memex.write(
        WriteInput(
            type="decision",
            title="Choose Kafka",
            body="b",
            description="When picking the broker.",
            scope="project",
            project_id=PROJECT_ID,
        )
    )
    memex.rebuild_index()
    hits = memex.recall(
        "picking the broker", engine="navigation", scope="project", project_id=PROJECT_ID
    ).hits
    assert [(h.slug, h.node_type) for h in hits] == [("choose-kafka", "decision")]
