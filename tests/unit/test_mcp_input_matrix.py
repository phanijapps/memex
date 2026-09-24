"""Regression matrix for MCP tool input handling.

Each row pins one failure mode observed in the loose-typing review:
schema violations must die at the SDK layer with a field-precise
message, domain violations must return sanitized error data, and no
input may ever escape as UnexpectedToolError.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from memex import mcp_server


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data_dir = tmp_path / "mcp-matrix-home"
    monkeypatch.setenv("MEMEX_DATA_DIR", str(data_dir))
    mcp_server._reset()
    yield data_dir
    mcp_server._reset()


def call(server: Any, tool: str, args: dict[str, Any]) -> tuple[str, Any]:
    """Invoke a tool; classify the outcome as 'result' or 'sdk-rejected'."""
    try:
        result = asyncio.run(server.call_tool(tool, args))
    except ToolError as exc:
        assert not isinstance(exc, UnexpectedToolError), (
            f"{tool} escaped to UnexpectedToolError: {exc.__cause__!r}"
        )
        return "sdk-rejected", str(exc)
    data = result.model_dump()
    return "result", data.get("structured_content")


@pytest.fixture
def server() -> Any:
    return mcp_server.build_server()


VALID_WRITE = {"type": "entity", "title": "Matrix node", "body": "b", "scope": "global"}


class TestSchemaChannel:
    """Wrong shapes and out-of-contract values die at the SDK layer,
    with the offending field named in the message."""

    @pytest.mark.parametrize(
        ("args", "field"),
        [
            ({"type": "folder", "title": "x", "body": "y"}, "type"),
            ({"type": "entity", "title": "x", "body": "y", "importance": 2}, "importance"),
            ({"type": "entity", "title": "x", "body": "y", "importance": "high"}, "importance"),
            ({"type": "entity", "title": "x", "body": "y", "tags": "tool"}, "tags"),
            ({"type": "entity", "title": "x"}, "body"),
            ({"type": "entity", "title": "x", "body": "y"}, "scope"),
            ({"type": "entity", "title": "x", "body": "y", "scope": "other"}, "scope"),
        ],
    )
    def test_write_rejections_name_the_field(
        self, server: Any, args: dict[str, Any], field: str
    ) -> None:
        channel, message = call(server, "memex_write", args)
        assert channel == "sdk-rejected"
        assert field in message

    def test_recall_top_k_bounds(self, server: Any) -> None:
        for bad in (0, 101, 999):
            channel, message = call(server, "memex_recall", {"query": "x", "top_k": bad})
            assert channel == "sdk-rejected", message
            assert "top_k" in message

    def test_recall_node_type_shape(self, server: Any) -> None:
        channel, payload = call(
            server,
            "memex_recall",
            {
                "query": "x",
                "node_type": "Folder",
                "questions": ["q"],
                "project_id": "a" * 24,
            },
        )
        assert channel == "result"
        assert payload == {"error": "invalid arguments for this operation"}

    def test_forget_mode_enum(self, server: Any) -> None:
        channel, message = call(server, "memex_forget", {"slug": "x", "mode": "explode"})
        assert channel == "sdk-rejected"
        assert "mode" in message


class TestDomainChannel:
    """Valid shapes with invalid semantics return sanitized error data."""

    def test_write_ok(self, server: Any) -> None:
        channel, payload = call(server, "memex_write", VALID_WRITE)
        assert channel == "result"
        assert payload["slug"] == "matrix-node"

    def test_project_write_derives_identity(self, server: Any, isolated_env: Path) -> None:
        channel, payload = call(
            server,
            "memex_write",
            {**VALID_WRITE, "title": "Workspace architecture", "scope": "project"},
        )
        assert channel == "result"
        assert Path(payload["file_path"]).is_relative_to(isolated_env / "docs/projects")
        assert not (isolated_env / "docs/global/entities/workspace-architecture.md").exists()

    def test_recall_empty_query(self, server: Any) -> None:
        channel, payload = call(server, "memex_recall", {"query": "???"})
        assert channel == "result"
        assert payload == {"error": "invalid arguments for this operation"}

    def test_task_recall_returns_flat_structured_content(self, server: Any) -> None:
        project_id = "a" * 24
        _, written = call(
            server,
            "memex_write",
            {
                "type": "entity",
                "title": "Project layout",
                "body": "project layout page",
                "scope": "project",
                "project_id": project_id,
            },
        )
        channel, payload = call(
            server,
            "memex_recall",
            {"query": "Repair lookup", "questions": ["project layout"], "project_id": project_id},
        )
        assert channel == "result"
        assert payload["sources"] == [written["file_path"]]
        assert "context" in payload and "result" not in payload

    def test_forget_unknown_slug(self, server: Any) -> None:
        channel, payload = call(server, "memex_forget", {"slug": "ghost"})
        assert channel == "result"
        assert payload == {"error": "memory node not found"}


class TestTranscriptCliRegression:
    """The remaining manual transcript path accepts turns without ts."""

    def test_cli_accepts_ts_less_jsonl(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        from memex import cli

        turns = tmp_path / "turns.jsonl"
        turns.write_text(json.dumps({"role": "user", "content": "no ts", "turn": 1}) + "\n")
        code = cli.main(
            [
                "--data-dir",
                str(tmp_path / "cli-home"),
                "ingest-transcript",
                "--session-id",
                "sess-cli-nots",
                "--turns-file",
                str(turns),
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["turn_count"] == 1


class TestWireSchemas:
    """The advertised schemas must carry the domain enums and bounds."""

    @staticmethod
    def schemas(server: Any) -> dict[str, Any]:
        tools = asyncio.run(server.list_tools())

        def dump(schema: Any) -> dict[str, Any]:
            data: dict[str, Any] = schema.model_dump() if hasattr(schema, "model_dump") else schema
            return data

        return {
            tool.name: {"input": dump(tool.input_schema), "output": dump(tool.output_schema)}
            for tool in tools
        }

    def test_write_input_schema(self, server: Any) -> None:
        schema = self.schemas(server)["memex_write"]["input"]
        props = schema["properties"]
        assert "scope" in schema["required"]
        assert set(props["scope"]["enum"]) == {"global", "project"}
        assert props["type"]["type"] == "string" and "enum" not in props["type"]
        assert props["importance"]["minimum"] == 0
        assert props["importance"]["maximum"] == 1

    def test_recall_input_schema(self, server: Any) -> None:
        props = self.schemas(server)["memex_recall"]["input"]["properties"]
        assert props["top_k"]["minimum"] == 1
        assert props["top_k"]["maximum"] == 100

    def test_output_schemas_are_not_vacuous(self, server: Any) -> None:
        schemas = self.schemas(server)
        write_out = schemas["memex_write"]["output"]
        assert {"slug", "file_path", "error"} <= set(write_out["properties"])
        recall_out = schemas["memex_recall"]["output"]
        assert {"hits", "total_indexed", "error", "context", "sources"} <= set(
            recall_out["properties"]
        )
