import json
from pathlib import Path

import pytest

from memex import cli
from memex.infrastructure.search.bm25_retriever import MAX_QUERY_BYTES
from memex.infrastructure.workspace_context import ProjectContext


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "cli-home"


def test_write_and_recall_roundtrip(data_dir: Path, capture: dict[str, str]) -> None:
    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "write",
            "--type",
            "entity",
            "--title",
            "CLI entity",
            "--body",
            "written from the command line",
            "--tags",
            "cli,test",
        ]
    )
    assert code == 0
    payload = json.loads(capture["out"])
    assert payload["slug"] == "cli-entity"

    code = cli.main(["--data-dir", str(data_dir), "recall", "command line"])
    assert code == 0
    result = json.loads(capture["out"])
    assert [hit["slug"] for hit in result["hits"]] == ["cli-entity"]


def test_watch_command_wires_navigation_and_refreshes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped `memex watch` command must refresh generated indexes
    after an external edit (AC-0017 watcher clause)."""
    from memex.application.memory import Memex
    from memex.domain.models import WriteInput
    from memex.infrastructure.config import MemexConfig
    from memex.infrastructure.store.watcher import IndexWatcher

    memex = Memex(MemexConfig(data_dir=data_dir))
    memex.write(WriteInput(type="entity", title="Watched", body="b", description="old text"))
    memex.close()
    page = data_dir / "docs" / "global" / "entities" / "watched.md"
    entities_index = data_dir / "docs" / "global" / "entities" / "index.md"
    assert "old text" in entities_index.read_text(encoding="utf-8")

    def start_and_reindex(self: IndexWatcher) -> None:
        """One synchronous poll cycle: external edit, then re-index."""
        page.write_text(
            page.read_text(encoding="utf-8").replace("old text", "new text"), encoding="utf-8"
        )
        self.reindex_changed()

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(IndexWatcher, "start_polling", start_and_reindex)
    monkeypatch.setattr("time.sleep", interrupt)
    code = cli.main(["--data-dir", str(data_dir), "watch"])
    assert code == 0
    assert "new text" in entities_index.read_text(encoding="utf-8")


def test_task_recall_derives_project_and_preserves_single_query_shape(
    data_dir: Path, capture: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )
    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "write",
                "--type",
                "entity",
                "--title",
                "Project layout",
                "--body",
                "project layout page",
                "--scope",
                "project",
            ]
        )
        == 0
    )
    for top_k in (8, 9):
        assert (
            cli.main(
                [
                    "--data-dir",
                    str(data_dir),
                    "recall",
                    "Repair lookup",
                    "--question",
                    "project layout",
                    "--top-k",
                    str(top_k),
                ]
            )
            == 0
        )
        task = json.loads(capture["out"])
        assert len(task["sources"]) == 1
        assert task["unanswered_questions"] == []
        assert task["rendered_tokens"] <= 4096

    assert cli.main(["--data-dir", str(data_dir), "recall", "project layout"]) == 0
    ordinary = json.loads(capture["out"])
    assert "hits" in ordinary and "context" not in ordinary


def test_task_recall_rejects_global_scope(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["--data-dir", str(data_dir), "recall", "goal", "--question", "query", "--scope", "global"]
    )
    assert code == 1
    assert "project scope" in capsys.readouterr().err


def test_task_recall_explicit_project_id_overrides_derived_identity(
    data_dir: Path, capture: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )
    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "write",
                "--type",
                "entity",
                "--title",
                "Private beta",
                "--body",
                "beta only page",
                "--scope",
                "project",
                "--project-id",
                "b" * 24,
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "recall",
                "Find beta",
                "--question",
                "beta only",
                "--project-id",
                "b" * 24,
            ]
        )
        == 0
    )
    assert len(json.loads(capture["out"])["sources"]) == 1


def test_project_write_uses_derived_readable_locator(
    data_dir: Path, capture: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )

    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "write",
            "--type",
            "preference",
            "--title",
            "Example",
            "--body",
            "project-scoped",
            "--scope",
            "project",
        ]
    )

    assert code == 0
    payload = json.loads(capture["out"])
    assert payload["file_path"].endswith("docs/projects/git-memex/preferences/example.md")


def test_project_write_with_explicit_id_uses_id_fallback(
    data_dir: Path, capture: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )

    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "write",
            "--type",
            "preference",
            "--title",
            "Example",
            "--body",
            "project-scoped",
            "--scope",
            "project",
            "--project-id",
            "b" * 24,
        ]
    )

    assert code == 0
    payload = json.loads(capture["out"])
    assert payload["file_path"].endswith(f"docs/projects/{'b' * 24}/preferences/example.md")


def test_recall_rejects_oversized_query_without_echoing_input(
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    query = "leaksecret " + ("é" * MAX_QUERY_BYTES)

    code = cli.main(["--data-dir", str(data_dir), "recall", query])

    assert code == 1
    stderr = capsys.readouterr().err
    assert f"exceeds {MAX_QUERY_BYTES} UTF-8 bytes" in stderr
    assert "leaksecret" not in stderr


def test_forget_missing_slug_errors(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--data-dir", str(data_dir), "forget", "ghost"])
    assert code == 1
    assert "no wiki page" in capsys.readouterr().err


def test_invalid_write_type_rejected(data_dir: Path) -> None:
    # `--type` is a free string now (validated for shape and declaration
    # downstream, not a closed argparse `choices=`), so an unknown type is a
    # domain rejection (exit 1), not an argparse SystemExit (exit 2).
    code = cli.main(
        ["--data-dir", str(data_dir), "write", "--type", "bogus", "--title", "x", "--body", "y"]
    )
    assert code == 1


def test_info(data_dir: Path, capture: dict[str, str]) -> None:
    cli.main(
        ["--data-dir", str(data_dir), "write", "--type", "entity", "--title", "Info", "--body", "b"]
    )
    code = cli.main(["--data-dir", str(data_dir), "info"])
    assert code == 0
    info = json.loads(capture["out"])
    assert info["wiki_file_counts"]["entity"] == 1
    assert info["index_total"] == 1


def test_ingest_transcript_cli(data_dir: Path, capture: dict[str, str], tmp_path: Path) -> None:
    turns_file = tmp_path / "turns.jsonl"
    turns_file.write_text(
        json.dumps({"role": "user", "content": "hello", "ts": "2026-09-15T10:00:00Z", "turn": 1})
        + "\n",
        encoding="utf-8",
    )
    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "ingest-transcript",
            "--session-id",
            "sess-cli",
            "--turns-file",
            str(turns_file),
        ]
    )
    assert code == 0
    report = json.loads(capture["out"])
    assert report["episode_node"] == "sess-cli"
    assert report["turn_count"] == 1


def test_ingest_bad_turns_file(
    data_dir: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "ingest-transcript",
            "--session-id",
            "sess-bad",
            "--turns-file",
            str(bad),
        ]
    )
    assert code == 1
    assert "bad.jsonl:1" in capsys.readouterr().err


def test_export_import_cli(data_dir: Path, capture: dict[str, str], tmp_path: Path) -> None:
    cli.main(
        ["--data-dir", str(data_dir), "write", "--type", "entity", "--title", "Ex", "--body", "b"]
    )
    export_path = tmp_path / "nodes.json"
    code = cli.main(["--data-dir", str(data_dir), "export", "--output", str(export_path)])
    assert code == 0
    assert export_path.exists()

    other_dir = tmp_path / "other-home"
    code = cli.main(["--data-dir", str(other_dir), "import", "--input", str(export_path)])
    assert code == 0
    payload = json.loads(capture["out"])
    assert payload["imported"] == 1
    assert (other_dir / "docs/global/entities/ex.md").exists()


def test_consolidate_requires_api_key(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEMEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMEX_LLM_PROVIDER", raising=False)
    code = cli.main(["--data-dir", str(data_dir), "consolidate", "--mode", "dry-run"])
    assert code == 1
    assert "api_key" in capsys.readouterr().err


def test_write_with_explicit_project_id_derives_label_from_context(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --project-id still needs no --project-label: the display name
    falls back to the derived context label, matching the MCP write path."""
    monkeypatch.setattr(
        "memex.infrastructure.workspace_context.project_context",
        lambda cwd: ProjectContext(
            project_id="b" * 24,
            label="memex",
            locator="git-memex",
        ),
    )
    code = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "write",
            "--type",
            "entity",
            "--title",
            "Labeled page",
            "--body",
            "body",
            "--scope",
            "project",
            "--project-id",
            "b" * 24,
        ]
    )
    assert code == 0
    from memex.application.memory import Memex
    from memex.infrastructure.config import MemexConfig

    node = Memex(MemexConfig(data_dir=data_dir)).wiki_store.read("labeled-page")
    assert node is not None
    assert node.project_label == "memex"
