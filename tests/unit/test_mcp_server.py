import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from memex import mcp_server
from memex.domain.operations import RecallResultDict, TaskRecallResultDict
from memex.infrastructure.search.bm25_retriever import MAX_QUERY_BYTES, MAX_QUERY_TOKENS
from memex.infrastructure.workspace_context import ProjectContext
from memex.mcp_server import (
    memex_forget,
    memex_provenance,
    memex_recall,
    memex_write,
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data_dir = tmp_path / "mcp-home"
    monkeypatch.setenv("MEMEX_DATA_DIR", str(data_dir))
    mcp_server._reset()
    yield data_dir
    mcp_server._reset()


def test_only_agent_tools_registered() -> None:
    import asyncio

    server = mcp_server.build_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    expected = {
        "memex_write",
        "memex_recall",
        "memex_consolidate",
        "memex_forget",
        "memex_provenance",
    }
    assert names == expected


EXPECTED_HINTS = {
    "memex_write": {"read_only": False, "destructive": False, "idempotent": True},
    "memex_recall": {"read_only": True, "destructive": False, "idempotent": False},
    "memex_consolidate": {"read_only": False, "destructive": False, "idempotent": False},
    "memex_forget": {"read_only": False, "destructive": True, "idempotent": False},
    "memex_provenance": {"read_only": True, "destructive": False, "idempotent": True},
}


def test_wire_descriptions_are_call_contracts() -> None:
    """The description an agent client sees must be a usable contract:
    non-trivial, dedented, and explicit about the error-result shape."""
    import asyncio

    server = mcp_server.build_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == set(EXPECTED_HINTS)

    for name, tool in tools.items():
        assert len(tool.description or "") >= 250, f"{name}: description too light"
        assert '"error"' in tool.description, f"{name}: error contract not documented"
        assert "    " not in tool.description, f"{name}: indentation leaked onto the wire"
        assert "Args:" not in tool.description, f"{name}: maintainer pyguide header on wire"
        assert (tool.title or "").startswith("Memex:"), f"{name}: missing display title"

        annotations = tool.annotations
        assert annotations is not None, f"{name}: no annotations"
        expected = EXPECTED_HINTS[name]
        assert annotations.read_only_hint == expected["read_only"], name
        assert annotations.destructive_hint == expected["destructive"], name
        assert annotations.idempotent_hint == expected["idempotent"], name
        assert annotations.open_world_hint is False, name


def test_wire_registry_matches_registered_tools() -> None:
    """Every registered tool has a shared description; CLI-only operations remain."""
    import asyncio

    from memex.domain.operations import OPERATION_DESCRIPTIONS

    server = mcp_server.build_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names <= set(OPERATION_DESCRIPTIONS)


@pytest.mark.parametrize(
    "expected",
    (f"{MAX_QUERY_BYTES:,} UTF-8 bytes", f"{MAX_QUERY_TOKENS} searchable tokens"),
)
def test_registered_recall_description_pins_query_work_caps(expected: str) -> None:
    """Clients see the production query boundary in the registered contract."""
    import asyncio

    server = mcp_server.build_server()
    recall = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "memex_recall")

    assert expected in recall.description


def test_registered_write_description_explains_scope_choice() -> None:
    import asyncio

    server = mcp_server.build_server()
    write = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "memex_write")

    assert "workspace architecture" in write.description
    assert 'scope="project"' in write.description
    assert 'scope="global"' in write.description
    assert "MCP writes require scope" in write.description
    assert "derive the project identity" in write.description
    assert "server process's working directory" in write.description


def test_registered_recall_description_explains_derived_project_identity() -> None:
    import asyncio

    server = mcp_server.build_server()
    recall = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "memex_recall")

    assert "omit project_id to derive identity" in recall.description
    assert "server process's working directory" in recall.description


def test_tool_docstrings_follow_pyguide() -> None:
    """Source docstrings are maintainer docs (pyguide), not wire text."""
    functions = [
        mcp_server.memex_write,
        mcp_server.memex_recall,
        mcp_server.memex_consolidate,
        mcp_server.memex_forget,
        mcp_server.memex_provenance,
    ]
    for fn in functions:
        assert fn.__doc__ and "Returns:" in fn.__doc__, fn.__name__
        assert "When to use:" not in fn.__doc__, f"{fn.__name__}: wire text leaked into source"
        assert "Args:" in fn.__doc__, fn.__name__


def test_write_recall_forget_flow() -> None:
    written = memex_write(type="entity", title="MCP entity", body="via mcp tool", scope="global")
    assert written["slug"] == "mcp-entity"

    recalled = cast(RecallResultDict, memex_recall("mcp"))
    assert [hit["slug"] for hit in recalled["hits"]] == ["mcp-entity"]

    forgotten = memex_forget("mcp-entity")
    assert forgotten["forgotten"] is True


def test_task_recall_derives_project_and_uses_existing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "project_identity", lambda cwd: ("a" * 24, "memex"))
    written = memex_write(
        type="entity",
        title="MCP project layout",
        body="project layout page",
        scope="project",
        project_id="a" * 24,
    )
    for top_k in (8, 9):
        task = cast(
            TaskRecallResultDict,
            memex_recall("Repair lookup", questions=["project layout"], top_k=top_k),
        )
        assert task["sources"] == [written["file_path"]]
        assert task["unanswered_questions"] == []
        assert "hits" not in task
    assert "hits" in memex_recall("project layout", scope="project", project_id="a" * 24)


@pytest.mark.parametrize("scope", ["global", "unknown"])
def test_task_recall_rejects_non_project_scope(scope: str) -> None:
    assert memex_recall("goal", questions=["query"], scope=scope) == {
        "error": "invalid arguments for this operation"
    }


def test_task_recall_explicit_project_id_overrides_derived_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "project_identity", lambda cwd: ("a" * 24, "memex"))
    written = memex_write(
        type="entity",
        title="Private beta",
        body="beta only page",
        scope="project",
        project_id="b" * 24,
    )
    task = cast(
        TaskRecallResultDict,
        memex_recall("Find beta", questions=["beta only"], project_id="b" * 24),
    )
    assert task["sources"] == [written["file_path"]]


def test_project_write_uses_derived_readable_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )

    written = memex_write(
        type="preference",
        title="MCP project",
        body="via mcp tool",
        scope="project",
    )

    assert str(written["file_path"]).endswith("docs/projects/git-memex/preferences/mcp-project.md")


def test_project_write_with_explicit_id_uses_id_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "project_context",
        lambda cwd: ProjectContext("a" * 24, "memex", "git-memex"),
    )

    written = memex_write(
        type="preference",
        title="MCP project",
        body="via mcp tool",
        scope="project",
        project_id="b" * 24,
    )

    assert str(written["file_path"]).endswith(
        f"docs/projects/{'b' * 24}/preferences/mcp-project.md"
    )


def test_errors_are_sanitized() -> None:

    assert memex_forget("totally-unknown-slug") == {"error": "memory node not found"}
    assert memex_write(type="Bogus", title="x", body="y", scope="global") == {
        "error": "invalid arguments for this operation"
    }
    assert memex_recall("???") == {"error": "invalid arguments for this operation"}


def test_recall_oversized_query_uses_sanitized_error() -> None:
    query = "leaksecret " + ("é" * MAX_QUERY_BYTES)

    result = memex_recall(query)

    assert result == {"error": "invalid arguments for this operation"}
    assert "leaksecret" not in json.dumps(result)


def test_provenance_tool() -> None:
    memex_write(type="entity", title="MCP entity", body="via mcp tool", scope="global")
    provenance = memex_provenance("mcp-entity")
    assert provenance["confidence"] == "none"

    assert memex_provenance("ghost") == {"error": "memory node not found"}


def test_tool_exception_fallbacks_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal failures surface as sanitized errors, never raw traces."""
    import memex.mcp_server as srv

    class _Boom:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("secret path /root/x in store")

    monkeypatch.setattr(srv, "_get_memex", lambda: _Boom())

    assert "error" in memex_recall("anything")
    assert "error" in srv.memex_consolidate(mode="full")
    assert "error" in srv.memex_provenance("ghost")
    # The sanitized error must not leak the internal exception message
    assert "secret path" not in json.dumps(memex_recall("anything"))
