"""`memex types` (docs/specs/project-concept-types/spec.md AC-0001, AC-0002, AC-0010, AC-0011)."""

import json
from pathlib import Path
from typing import Any

import pytest

from memex import cli

PROJECT = "a" * 24


def _run(capsys: pytest.CaptureFixture[str], data_dir: Path, *args: str) -> tuple[int, Any]:
    code = cli.main(["--data-dir", str(data_dir), *args])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def test_add_enable_list(capsys: pytest.CaptureFixture[str], data_dir: Path) -> None:
    assert (
        _run(
            capsys,
            data_dir,
            "types",
            "add",
            "access-matrix",
            "--project-id",
            PROJECT,
            "--description",
            "Who may edit.",
        )[0]
        == 0
    )
    assert _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)[0] == 0
    code, listed = _run(capsys, data_dir, "types", "list", "--project-id", PROJECT)
    assert code == 0
    by_name = {row["name"]: row for row in listed}
    assert (
        by_name["access-matrix"]["kind"] == "custom"
        and by_name["access-matrix"]["description"] == "Who may edit."
    )
    assert by_name["decision"]["kind"] == "catalogue"
    assert by_name["entity"]["kind"] == "builtin"


def test_add_rejects_bad_and_colliding_names(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    errors: dict[str, str] = {}
    for bad in ("Decision", "entities", "log", "rule"):
        code = cli.main(["--data-dir", str(data_dir), "types", "add", bad, "--project-id", PROJECT])
        assert code != 0, bad
        errors[bad] = capsys.readouterr().err
    # "rule" is a catalogue name passed to `add`: refused with the enable hint.
    assert "memex types enable" in errors["rule"]
    # "Decision" fails the shape rule (must match [a-z][a-z0-9-]{0,63}).
    assert "[a-z" in errors["Decision"]
    # "entities" and "log" collide with a built-in directory / reserved name.
    assert "already" in errors["entities"]
    assert "already" in errors["log"]


def test_add_rejects_multiline_description_and_creates_no_directory(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, _ = _run(
        capsys,
        data_dir,
        "types",
        "add",
        "story-map",
        "--project-id",
        PROJECT,
        "--description",
        "a\nb",
    )
    assert code != 0
    assert not list((data_dir / "docs").rglob("story-map"))


def test_enable_rejects_bad_shape_before_any_message(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code = cli.main(
        ["--data-dir", str(data_dir), "types", "enable", "Bad Name", "--project-id", PROJECT]
    )
    err = capsys.readouterr().err
    assert code != 0
    assert "[a-z" in err


def test_write_to_declared_type_then_remove_refuses_and_force_archives(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)
    code, written = _run(
        capsys,
        data_dir,
        "write",
        "--type",
        "decision",
        "--title",
        "Choose Kafka",
        "--body",
        "b",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    assert code == 0 and written["file_path"].endswith("/decision/choose-kafka.md")
    code, _ = _run(capsys, data_dir, "types", "remove", "decision", "--project-id", PROJECT)
    assert code != 0
    code, result = _run(
        capsys, data_dir, "types", "remove", "decision", "--project-id", PROJECT, "--force"
    )
    assert code == 0 and result == {"removed": "decision", "archived": 1}


def test_write_undeclared_type_fails_before_writing(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, _ = _run(
        capsys,
        data_dir,
        "write",
        "--type",
        "decision",
        "--title",
        "X",
        "--body",
        "b",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    assert code != 0
    assert not list((data_dir / "docs").rglob("x.md"))


def test_recall_filters_by_declared_type(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)
    _run(
        capsys,
        data_dir,
        "write",
        "--type",
        "decision",
        "--title",
        "Choose Kafka",
        "--body",
        "unique-marker",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    _run(
        capsys,
        data_dir,
        "write",
        "--type",
        "entity",
        "--title",
        "Kafka",
        "--body",
        "unique-marker",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    code, result = _run(
        capsys,
        data_dir,
        "recall",
        "unique-marker",
        "--type",
        "decision",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    assert code == 0 and [h["slug"] for h in result["hits"]] == ["choose-kafka"]


def test_mcp_write_rejects_undeclared_type(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memex import mcp_server

    monkeypatch.setenv("MEMEX_DATA_DIR", str(data_dir))
    mcp_server._reset()
    try:
        result = mcp_server.memex_write(
            type="decision", title="X", body="b", scope="project", project_id=PROJECT
        )
        assert "error" in result and "undeclared type" in str(result["error"])
        assert not list((data_dir / "docs").rglob("x.md"))
    finally:
        mcp_server._reset()


def test_mcp_write_names_the_withdrawn_remedy(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)
    _run(
        capsys,
        data_dir,
        "write",
        "--type",
        "decision",
        "--title",
        "Choose Kafka",
        "--body",
        "b",
        "--scope",
        "project",
        "--project-id",
        PROJECT,
    )
    _run(capsys, data_dir, "types", "remove", "decision", "--project-id", PROJECT, "--force")

    from memex import mcp_server

    monkeypatch.setenv("MEMEX_DATA_DIR", str(data_dir))
    mcp_server._reset()
    try:
        result = mcp_server.memex_write(
            type="decision", title="Choose Redis", body="b", scope="project", project_id=PROJECT
        )
        assert "error" in result and "withdrawn" in str(result["error"])
    finally:
        mcp_server._reset()


def test_suggest_counts_tags_that_are_not_types(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    for i in range(3):
        _run(
            capsys,
            data_dir,
            "write",
            "--type",
            "entity",
            "--title",
            f"Story {i}",
            "--body",
            "b",
            "--tags",
            "user-stories,entity",
            "--scope",
            "project",
            "--project-id",
            PROJECT,
        )
    code, rows = _run(capsys, data_dir, "types", "suggest", "--project-id", PROJECT)
    assert code == 0 and rows == [{"tag": "user-stories", "pages": 3}]
