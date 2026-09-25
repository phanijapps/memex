"""Viz dashboard route tests (T1 + T2)."""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from memex import Memex
from memex.domain.models import WikiNode, WriteInput
from memex.infrastructure.config import MemexConfig as Config
from memex.infrastructure.web.server import VizHandler, serve


@pytest.fixture(scope="module")
def viz_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path]]:
    """Seed a store and start a real HTTP server for the module."""
    data_dir = tmp_path_factory.mktemp("viz")
    memex = Memex(Config(data_dir=data_dir))
    memex.write(WriteInput(type="entity", title="Alpha entity", body="alpha search content"))
    for number in range(21):
        memex.write(
            WriteInput(
                type="entity",
                title=f"Paginated memory {number:02d}",
                body=f"pagination fixture {number}",
            )
        )
    memex.write(
        WriteInput(
            type="entity",
            title="Project alpha",
            body="alpha project search content",
            scope="project",
            project_id="a" * 24,
            project_label="memex",
        )
    )
    memex.write(WriteInput(type="preference", title="Beta pref", body="beta preference body"))
    node = WikiNode(
        type="entity",
        title="Gamma pending",
        body="gamma content",
        id="g1",
        status="pending",
        source="consolidation",
        harness="codex",
    )
    stored = memex.wiki_store.write(node)
    memex.index_manager.update_record(stored)
    # Seed a transcript with token usage
    meta = {
        "session_id": "sess-viz",
        "started_at": "2026-09-16T10:00:00Z",
        "turn_count": 3,
        "token_usage": {"total_tokens": 1234},
        "harness": "codex",
    }
    (data_dir / "transcripts/2026-09-16").mkdir(parents=True, exist_ok=True)
    (data_dir / "transcripts/2026-09-16/sess-viz.meta.json").write_text(json.dumps(meta))
    turns = [
        {"role": "user", "content": "fix the login bug", "ts": "2026-09-16T10:00:01Z"},
        {
            "role": "agent",
            "content": "Reading the auth module first.",
            "ts": "2026-09-16T10:00:02Z",
        },
        {
            "role": "tool",
            "tool_name": "read",
            "query": "src/auth.py",
            "result": "def login(): ...",
            "ts": "2026-09-16T10:00:03Z",
        },
        {"role": "user", "content": "looks good", "ts": "2026-09-16T10:01:00Z"},
    ]
    (data_dir / "transcripts/2026-09-16/sess-viz.jsonl").write_text(
        "\n".join(json.dumps(t) for t in turns), encoding="utf-8"
    )
    older = data_dir / "transcripts/2026-09-15"
    older.mkdir()
    (older / "sess-older.meta.json").write_text(
        json.dumps(
            {
                "session_id": "sess-older",
                "started_at": "2026-09-15T09:00:00Z",
                "turn_count": 3,
                "harness": "claude",
                "project_label": "OtherProject",
            }
        ),
        encoding="utf-8",
    )
    (older / "sess-older.jsonl").write_text(
        "\n".join(
            json.dumps(turn)
            for turn in [
                {"role": "user", "content": "<script>alert(1)</script>", "ts": "09:00"},
                {
                    "role": "agent",
                    "content": "**A useful answer** <img src=x onerror=alert(1)>",
                    "ts": "09:01",
                },
                {
                    "role": "tool",
                    "tool_name": "read",
                    "query": "<svg onload=alert(1)>",
                    "result": "<script>" + "x" * 2200,
                    "ts": "09:02",
                },
            ]
        ),
        encoding="utf-8",
    )
    # Seed an episode page for the sessions list
    ep = WikiNode(
        type="episode",
        title="Session sess-viz",
        body="test session",
        id="e1",
        session_id="sess-viz",
        transcript_ref="transcripts/2026-09-16/sess-viz.jsonl",
    )
    memex.wiki_store.write(ep)
    memex.index_manager.update_record(ep)
    memex.close()

    class BoundVizHandler(VizHandler):
        memex: Memex | None = Memex(Config(data_dir=data_dir))

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoundVizHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield port, data_dir
    server.shutdown()
    server.server_close()


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


class TestShellPage:
    def test_shell_renders(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/")
        assert code == 200
        assert "memex" in body
        assert 'class="brand" href="/" aria-current="page" aria-label="Memex home"' in body
        assert '<svg viewBox="0 0 64 64" aria-hidden="true"' in body
        assert "<span>memex</span>" in body
        assert 'src="/htmx.js"' in body
        assert 'href="/style.css"' in body
        assert 'href="/view/memories"' in body
        assert 'href="/view/sessions"' in body

    def test_direct_views_boot_the_matching_fragment(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, memories = _get(port, "/view/memories?page=2&type=entity")
        _, replay = _get(port, "/view/session/sess-viz")
        assert "Page 2 of" in memories
        assert "fix the login bug" in replay
        assert 'href="/style.css"' in replay

    def test_launcher_honors_isolated_data_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMEX_DATA_DIR", str(tmp_path))
        with patch("memex.infrastructure.web.server.ThreadingHTTPServer") as server_type:
            with patch("webbrowser.open"):
                server_type.return_value.serve_forever.side_effect = KeyboardInterrupt
                serve(port=7172)
            handler_type = server_type.call_args.args[1]
            assert handler_type.memex.data_dir == tmp_path

    @pytest.mark.parametrize(
        ("route", "expected"),
        [
            ("/view/search?q=alpha", "Alpha entity"),
            ("/view/sessions", "2026-09-16"),
            ("/view/tokens", "1,234"),
            ("/view/page/alpha-entity", "alpha search content"),
            ("/view/page/project-alpha?project=" + "a" * 24, "alpha project search content"),
        ],
    )
    def test_direct_views_render_without_htmx(
        self, viz_server: tuple[int, Path], route: str, expected: str
    ) -> None:
        port, _ = viz_server
        code, body = _get(port, route)
        assert code == 200
        assert '<main id="main">' in body
        assert expected in body

    def test_direct_search_scope_controls_have_a_swap_target(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, _ = viz_server
        _, body = _get(port, "/view/search?q=alpha")
        assert '<div id="search-results">' in body
        assert 'hx-target="#search-results"' in body


class TestHTMXServed:  # AC-0007
    def test_real_htmx(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/htmx.js")
        assert code == 200
        assert len(body) > 10000
        assert "htmx" in body


class TestOverview:  # AC-0001
    def test_stat_cards(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/overview")
        assert code == 200
        assert "Memory pages" in body
        assert "Pending approval" in body


class TestPagesFilter:  # AC-0002
    def test_entity_filter(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/pages?type=entity")
        assert code == 200
        assert "Paginated memory" in body
        assert "Beta pref" not in body

    def test_preference_filter(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/pages?type=preference")
        assert code == 200
        assert "Beta pref" in body
        assert "Alpha entity" not in body

    def test_project_scope_is_visible_without_an_identifier(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, _ = viz_server
        code, body = _get(port, "/pages?project=" + "a" * 24)
        assert code == 200
        assert "Project alpha" in body
        assert "memex" in body
        assert "file:" not in body

    def test_pagination_preserves_the_type_selection(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        first_code, first_body = _get(port, "/pages?type=entity&page=1")
        second_code, second_body = _get(port, "/pages?type=entity&page=2")
        assert first_code == second_code == 200
        assert "Page 1 of 2" in first_body
        assert "Page 2 of 2" in second_body
        assert "page=2" in first_body
        assert "Paginated memory" in second_body

    def test_twenty_card_pages_do_not_overlap(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, first = _get(port, "/pages?scope=global&page=1")
        _, second = _get(port, "/pages?scope=global&page=2")
        first_titles = set(re.findall(r"Paginated memory \d{2}", first))
        second_titles = set(re.findall(r"Paginated memory \d{2}", second))
        assert len(re.findall(r'class="card"', first)) == 20
        assert not first_titles.intersection(second_titles)

    def test_scope_change_resets_page_and_keeps_type(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, body = _get(port, "/pages?type=entity&page=2&scope=global")
        assert "scope=project&amp;project=" in body
        assert "type=entity" in body
        assert "project=aaaaaaaaaaaaaaaaaaaaaaaa&amp;page=2" not in body

    def test_invalid_page_clamps_to_valid_range(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, invalid = _get(port, "/pages?page=oops")
        _, large = _get(port, "/pages?page=999")
        assert "Page 1 of" in invalid
        assert "Page 2 of 2" in large

    def test_all_scope_includes_project_and_global(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, all_pages = _get(port, "/pages?scope=best&page=2")
        _, global_pages = _get(port, "/pages?scope=global&page=2")
        assert "Project alpha" in all_pages or "Project alpha" in _get(port, "/pages?scope=best")[1]
        assert "Project alpha" not in global_pages

    @pytest.mark.parametrize(
        ("route", "message"),
        [
            ("/pages?type=unrecognized", "Unknown memory type"),
            ("/pages?scope=unrecognized", "Unknown memory scope"),
            ("/pages?scope=project&project=missing", "Unknown project scope"),
            (
                "/pages?scope=project&project=aaaaaaaaaaaaaaaaaaaaaaaa&type=preference",
                "No memories match this selection",
            ),
        ],
    )
    def test_selection_empty_states(
        self, viz_server: tuple[int, Path], route: str, message: str
    ) -> None:
        port, _ = viz_server
        _, body = _get(port, route)
        assert message in body


class TestSearch:  # AC-0003
    def test_search_returns_results(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/search?q=alpha")
        assert code == 200
        assert "Alpha entity" in body

    def test_search_selects_a_project_namespace(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/search?q=alpha&project=" + "a" * 24)
        assert code == 200
        assert "Project alpha" in body
        assert "Alpha entity" not in body

    def test_best_search_falls_back_to_all_memory(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, body = _get(port, "/search?q=beta&scope=best&project=" + "a" * 24)
        assert "Beta pref" in body

    def test_best_search_prefers_selected_project_hit(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, body = _get(port, "/search?q=alpha&scope=best&project=" + "a" * 24)
        assert "Project alpha" in body
        assert "Alpha entity" not in body

    def test_invalid_scope_has_a_visible_empty_state(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, body = _get(port, "/search?q=alpha&scope=invalid")
        assert "Unknown search scope" in body

    @pytest.mark.parametrize(
        ("route", "message"),
        [
            ("/search", "Enter a search term"),
            ("/search?q=alpha&scope=project", "Choose a project"),
            ("/search?q=alpha&scope=best&project=missing", "Unknown project scope"),
            ("/search?q=nonexistent", "No memories match"),
        ],
    )
    def test_search_empty_states(
        self, viz_server: tuple[int, Path], route: str, message: str
    ) -> None:
        port, _ = viz_server
        _, body = _get(port, route)
        assert message in body


class TestHealth:  # AC-0004
    def test_health_fragment(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/health")
        assert code == 200
        assert "pages" in body
        assert "pending" in body


class TestSessions:  # AC-0005
    def test_sessions_table(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/sessions")
        assert code == 200
        assert "sess-viz" in body
        assert 'class="session-day"' in body
        assert 'class="session-card"' in body

    def test_groups_newest_date_then_project_and_harness(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, _ = viz_server
        _, body = _get(port, "/sessions")
        assert body.index("2026-09-16") < body.index("2026-09-15")
        assert "OtherProject · claude" in body
        assert "Global or unscoped · codex" in body

    def test_group_uses_capture_directory_date_not_metadata_date(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, data_dir = viz_server
        path = data_dir / "transcripts/2026-09-16/sess-viz.meta.json"
        original = path.read_bytes()
        meta = json.loads(original)
        meta["started_at"] = "2026-01-01T10:00:00Z"
        path.write_text(json.dumps(meta), encoding="utf-8")
        try:
            _, body = _get(port, "/sessions")
            assert body.index("2026-09-16") < body.index("sess-viz")
        finally:
            path.write_bytes(original)

    def test_malformed_metadata_has_a_degraded_state(self, viz_server: tuple[int, Path]) -> None:
        port, data_dir = viz_server
        path = data_dir / "transcripts/2026-09-16/sess-viz.meta.json"
        original = path.read_bytes()
        path.write_text('{"session_id":"sess-viz","turn_count":"invalid"}', encoding="utf-8")
        try:
            _, sessions = _get(port, "/sessions")
            _, overview = _get(port, "/overview")
            assert "1 session metadata record(s) unreadable" in sessions
            assert "sess-older" in sessions
            assert "1 session metadata record(s) unreadable" in overview
            assert "sess-older" in overview
        finally:
            path.write_bytes(original)

    def test_external_metadata_symlink_is_ignored(
        self, viz_server: tuple[int, Path], tmp_path: Path
    ) -> None:
        port, data_dir = viz_server
        external = tmp_path / "outside.meta.json"
        external.write_text(
            json.dumps(
                {
                    "session_id": "outside-session",
                    "started_at": "2026-09-16T09:00:00Z",
                    "turn_count": 1,
                    "token_usage": {"total_tokens": 999999},
                }
            ),
            encoding="utf-8",
        )
        link = data_dir / "transcripts/2026-09-16/outside-session.meta.json"
        link.symlink_to(external)
        try:
            assert "outside-session" not in _get(port, "/sessions")[1]
            assert "999,999" not in _get(port, "/tokens")[1]
        finally:
            link.unlink()


class TestTokens:  # AC-0006
    def test_token_chart(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/tokens")
        assert code == 200
        assert "<svg" in body
        assert "<rect" in body
        assert "1,234" in body

    @pytest.mark.parametrize("raw", ["Infinity", "NaN", "-1", "true", "1000000000001"])
    def test_invalid_token_total_does_not_break_chart(
        self, viz_server: tuple[int, Path], raw: str
    ) -> None:
        port, data_dir = viz_server
        path = data_dir / "transcripts/2026-09-16/bad-token.meta.json"
        path.write_text(
            '{"session_id":"bad-token","token_usage":{"total_tokens":' + raw + "}}",
            encoding="utf-8",
        )
        try:
            code, body = _get(port, "/tokens")
            assert code == 200
            assert "1,234" in body
            assert "bad-token" not in body
        finally:
            path.unlink()


class TestSessionDetail:
    def test_malformed_jsonl_line_is_skipped_without_losing_later_turns(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, data_dir = viz_server
        path = data_dir / "transcripts/2026-09-16/sess-viz.jsonl"
        original = path.read_bytes()
        path.write_bytes(
            original
            + b'\n{"role":"user","content":\n'
            + b'{"role":"agent","content":"after malformed line"}\n'
        )
        try:
            _, body = _get(port, "/session/sess-viz")
            assert "fix the login bug" in body
            assert "after malformed line" in body
        finally:
            path.write_bytes(original)

    def test_session_detail_renders_turns(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/session/sess-viz")
        assert code == 200
        assert "fix the login bug" in body
        assert "Reading the auth module" in body
        assert "def login(): ..." in body

    def test_tool_content_only_turn_is_visible_and_escaped(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, data_dir = viz_server
        path = data_dir / "transcripts/2026-09-16/sess-viz.jsonl"
        original = path.read_bytes()
        path.write_bytes(original + b'\n{"role":"tool","content":"<script>tool output</script>"}\n')
        try:
            _, body = _get(port, "/session/sess-viz")
            assert "&lt;script&gt;tool output&lt;/script&gt;" in body
            assert "<script>tool output</script>" not in body
        finally:
            path.write_bytes(original)

    def test_session_detail_shows_meta(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/session/sess-viz")
        assert code == 200
        assert "3 turns" in body
        assert "codex" in body

    def test_session_detail_missing(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/session/no-such-session")
        assert code == 200
        assert "not found" in body.lower()

    def test_session_detail_rejects_path_like_identifier(
        self, viz_server: tuple[int, Path]
    ) -> None:
        port, _ = viz_server
        code, body = _get(port, "/session/%2E%2E%2Fsecret")
        assert code == 200
        assert "not found" in body.lower()

    def test_session_detail_rejects_symlink_outside_transcripts(
        self, viz_server: tuple[int, Path], tmp_path: Path
    ) -> None:
        port, data_dir = viz_server
        secret = tmp_path / "outside.jsonl"
        secret.write_text('{"role":"user","content":"outside secret"}', encoding="utf-8")
        link = data_dir / "transcripts/2026-09-16/sess-outside.jsonl"
        link.symlink_to(secret)
        try:
            _, body = _get(port, "/session/sess-outside")
            assert "Session not found" in body
            assert "outside secret" not in body
        finally:
            link.unlink()

    def test_replay_escapes_roles_and_marks_truncation(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, body = _get(port, "/session/sess-older")
        assert "AI assistant" in body
        assert "User" in body and "Tool" in body
        assert "&lt;script&gt;" in body
        assert "<script>" not in body
        assert "<strong>A useful answer</strong>" in body
        assert "Show more result" in body
        assert "Truncated after 2,000 characters" in body


class TestPageDetail:
    def test_page_detail_renders_markdown(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/page/alpha-entity")
        assert code == 200
        assert "alpha search content" in body

    def test_page_detail_pending_badge(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/page/gamma-pending")
        assert code == 200
        assert "pending" in body
        assert "codex" in body

    def test_page_detail_missing(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/page/no-such-page")
        assert code == 200
        assert "not found" in body.lower()

    def test_project_page_detail_uses_project_identity(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        _, project = _get(port, "/page/project-alpha?project=" + "a" * 24)
        _, global_result = _get(port, "/page/project-alpha")
        assert "alpha project search content" in project
        assert "Page not found" in global_result


def test_dashboard_get_requests_do_not_mutate_the_store(viz_server: tuple[int, Path]) -> None:
    port, data_dir = viz_server
    before = {
        path.relative_to(data_dir): path.read_bytes() if path.is_file() else None
        for path in data_dir.rglob("*")
        if path.is_file() or path.is_dir()
    }
    for route in (
        "/",
        "/overview",
        "/pages?page=2",
        "/search?q=alpha",
        "/sessions",
        "/session/sess-viz",
        "/tokens",
        "/health",
        "/view/memories?type=entity&page=2",
        "/view/search?q=alpha",
        "/view/page/project-alpha?project=" + "a" * 24,
        "/view/session/sess-viz",
        "/session/no-such-session",
    ):
        assert _get(port, route)[0] == 200
    after = {
        path.relative_to(data_dir): path.read_bytes() if path.is_file() else None
        for path in data_dir.rglob("*")
        if path.is_file() or path.is_dir()
    }
    assert before == after


class TestShellAndFallback:
    def test_root_shell(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/")
        assert code == 200
        assert "memex" in body.lower()

    def test_style_css(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/style.css")
        assert code == 200
        assert "body" in body

    def test_unknown_route_fallback(self, viz_server: tuple[int, Path]) -> None:
        port, _ = viz_server
        code, body = _get(port, "/definitely-not-a-route")
        assert code == 200
        assert "Not found" in body


def test_memories_filter_accepts_dynamic_concept_types(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A project-declared shelf (book) is a first-class filter on /pages."""
    data_dir = tmp_path_factory.mktemp("viz-dynamic-type")
    memex = Memex(Config(data_dir=data_dir))
    memex.wiki_store.declare_type(
        "book", scope="project", project_id="a" * 24, description="catalog records"
    )
    from memex.domain.models import WriteInput

    memex.write(
        WriteInput(
            type="book",
            title="Pride and Prejudice",
            body="Title: Pride and Prejudice",
            scope="project",
            project_id="a" * 24,
            project_label="memex",
        )
    )
    memex.close()

    class BoundVizHandler(VizHandler):
        memex: Memex | None = Memex(Config(data_dir=data_dir))

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoundVizHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    try:
        status, body = _get(port, "/pages?type=book")
        assert status == 200
        assert "Pride and Prejudice" in body
        assert ">Book<" in body
        assert "Unknown memory type" in _get(port, "/pages?type=nosuch")[1]
    finally:
        server.shutdown()
        server.server_close()
