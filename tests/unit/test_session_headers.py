"""Session headers: extraction, transcript format, reader compatibility."""

import json
from pathlib import Path

import pytest

from memex import Memex
from memex.domain.models import IngestTranscriptInput, TranscriptLinkReport, TurnStreamEntry
from memex.infrastructure.config import MemexConfig as Config
from memex.infrastructure.harness.transcripts import (
    parse_codex_rollout,
    parse_transcript,
    read_transcript_turns,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
RICH = FIXTURES / "codex_rollout_rich.jsonl"


def parsed_session_usage() -> dict[str, int]:
    usage = parse_codex_rollout(RICH).session_usage
    assert usage is not None
    return usage


class TestCodexHeaderExtraction:
    def test_parsed_session_usage_not_in_header(self) -> None:
        parsed = parse_codex_rollout(RICH)
        assert parsed.session_usage == {
            "input_tokens": 71759,
            "cached_input_tokens": 102144,
            "output_tokens": 344,
            "total_tokens": 72103,
        }
        assert "token_usage" not in (parsed.header.meta if parsed.header else {})

    def test_header_fields(self) -> None:
        header = parse_codex_rollout(RICH).header
        assert header is not None
        assert header.harness == "codex"
        assert header.session_id == "01a0a73c-73e6-7ed1-90c6-4bf6ccd47614"
        assert header.started_at == "2026-09-15T22:42:32Z"
        assert header.ended_at == "2026-09-15T23:10:05Z"
        assert header.duration_s == pytest.approx(1652.954, rel=0.01)

        meta = header.meta
        assert meta["cli_version"] == "0.154.0"
        assert meta["provider"] == "openai"
        assert meta["cwd"] == "/home/user/projects/agentzero"
        assert meta["models"] == ["gpt-5.6-sol"]
        assert meta["reasoning_efforts"] == ["medium", "high"]
        assert meta["resumed"] is True
        git = meta["git"]
        assert isinstance(git, dict)
        assert git["branch"] == "fix/planner-ward-autospawn"
        assert git["commit_hash"] == "abc999"  # latest meta wins after resume

    def test_thread_totals_are_latest_not_summed(self) -> None:
        header = parse_codex_rollout(RICH).header
        assert header is not None
        # Latest thread_token_usage (71759 in), never the sum of records.
        assert parsed_session_usage()["input_tokens"] == 71759
        assert parsed_session_usage()["total_tokens"] == 72103

    def test_per_turn_usage_attached_to_agent_turns(self) -> None:
        turns = parse_codex_rollout(RICH).turns
        agents = [turn for turn in turns if turn.role == "agent"]
        assert agents[0].token_usage == {
            "input_tokens": 21759,
            "cached_input_tokens": 6784,
            "output_tokens": 244,
            "total_tokens": 22003,
        }
        assert agents[1].token_usage == {
            "input_tokens": 50000,
            "output_tokens": 100,
            "total_tokens": 50100,
        }
        assert all(turn.token_usage is None for turn in turns if turn.role != "agent")

    def test_turn_only_rollout_still_parses(self) -> None:
        parsed = parse_codex_rollout(FIXTURES / "codex_rollout.jsonl")
        assert parsed.turns  # older fixture: turn-only path kept working
        header = parsed.header
        assert header is not None  # synthesized from filename
        assert header.session_id


class TestTranscriptWriter:
    def _ingest(self, data_dir: Path, path: Path = RICH) -> tuple[Memex, TranscriptLinkReport]:
        memex = Memex(Config(data_dir=data_dir))
        parsed = parse_transcript("codex", path)
        report = memex.ingest_transcript(
            IngestTranscriptInput(
                session_id=parsed.header.session_id if parsed.header else "s",
                turns=parsed.turns,
                header=parsed.header,
                token_usage=parsed.session_usage,
            )
        )
        return memex, report

    def test_usage_in_meta_not_transcript(self, data_dir: Path) -> None:
        _memex, report = self._ingest(data_dir)
        jsonl = Path(report.transcript_file)
        lines = jsonl.read_text().splitlines()
        header = json.loads(lines[0])
        assert header["type"] == "memex_session_header"
        assert header["session_id"] == report.session_id
        assert "token_usage" not in header["meta"]  # counts never in the JSONL
        for line in lines[1:]:
            assert "token_usage" not in json.loads(line)
        meta = json.loads(Path(report.meta_file).read_text())
        assert meta["token_usage"]["input_tokens"] == 71759
        per_turn = meta["turn_token_usage"]
        assert {entry["turn"] for entry in per_turn} == {2, 4}  # agent turns billed

    def test_recapture_refreshes_header_totals(self, data_dir: Path, tmp_path: Path) -> None:
        # Simulate a later notify: same rollout with one more usage record.
        memex, report = self._ingest(data_dir)
        grown = tmp_path / "grown.jsonl"
        grown.write_text(
            RICH.read_text()
            + json.dumps(
                {
                    "timestamp": "2026-09-15T23:11:00.000Z",
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "t3",
                        "turn_token_usage": {"input_tokens": 900, "total_tokens": 950},
                        "thread_token_usage": {"input_tokens": 90000, "total_tokens": 90500},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        memex.ingest_transcript(
            IngestTranscriptInput(session_id=report.session_id, header=None, turns=[]),
            overwrite=True,
        )
        parsed = parse_transcript("codex", grown)
        memex.ingest_transcript(
            IngestTranscriptInput(
                session_id=report.session_id,
                turns=parsed.turns,
                header=parsed.header,
                token_usage=parsed.session_usage,
            ),
            overwrite=True,
        )
        meta = json.loads(Path(report.meta_file).read_text())
        assert meta["token_usage"]["input_tokens"] == 90000
        episodes = [
            p for p in (data_dir / "docs/global/episodes").glob("*.md") if p.name != "index.md"
        ]
        assert len(episodes) == 1  # idempotent, no duplicate episode


class TestReaders:
    def test_reader_skips_header(self, data_dir: Path) -> None:
        _memex, report = TestTranscriptWriter()._ingest(data_dir)
        turns = read_transcript_turns(Path(report.transcript_file))
        assert turns and all(isinstance(turn, TurnStreamEntry) for turn in turns)

    def test_old_turn_only_transcripts_readable(self, tmp_path: Path) -> None:
        legacy = tmp_path / "legacy.jsonl"
        legacy.write_text(
            json.dumps({"role": "user", "content": "old", "turn": 1, "ts": ""}) + "\n",
            encoding="utf-8",
        )
        turns = read_transcript_turns(legacy)
        assert len(turns) == 1 and turns[0].content == "old"

    def test_cli_load_turns_skips_header(
        self, tmp_path: Path, data_dir: Path, capture: dict[str, str]
    ) -> None:
        from memex import cli

        _memex, report = TestTranscriptWriter()._ingest(data_dir)
        source = Path(report.transcript_file)
        code = cli.main(
            [
                "--data-dir",
                str(tmp_path / "other"),
                "ingest-transcript",
                "--session-id",
                "roundtrip",
                "--turns-file",
                str(source),
            ]
        )
        assert code == 0
        payload = json.loads(capture["out"])
        assert payload["turn_count"] > 0


def test_pi_session_usage_reaches_meta_sidecar(tmp_path: Path) -> None:
    """pi assistant usage lands in meta.json (total_tokens) and per-turn."""
    import json

    from memex.application.memory import Memex
    from memex.domain.models import IngestTranscriptInput
    from memex.infrastructure.config import MemexConfig
    from memex.infrastructure.harness.transcripts import parse_transcript

    parsed = parse_transcript("pi", Path("tests/fixtures/pi_session.jsonl"))
    assert parsed.session_usage is not None
    assert parsed.session_usage.get("total_tokens", 0) > 0
    agent_turns = [t for t in parsed.turns if t.role == "agent"]
    assert any(t.token_usage for t in agent_turns)

    store = tmp_path / "store"
    memex = Memex(MemexConfig(data_dir=store))
    memex.ingest_transcript(
        IngestTranscriptInput(
            session_id="sess-pi-usage",
            turns=parsed.turns,
            token_usage=parsed.session_usage,
        )
    )
    memex.close()
    metas = list((store / "transcripts").rglob("sess-pi-usage.meta.json"))
    assert metas, "meta sidecar missing"
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["token_usage"]["total_tokens"] == parsed.session_usage["total_tokens"]
    assert meta["turn_token_usage"], "per-turn usage not persisted"
