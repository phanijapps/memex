"""Deterministic verify gate: health checks + activity evidence."""

from pathlib import Path

import pytest

from memex.application.memory import Memex
from memex.application.verify import verify
from memex.domain.models import WriteInput
from memex.infrastructure.config import MemexConfig


@pytest.fixture
def memex(data_dir: Path) -> Memex:
    return Memex(MemexConfig(data_dir=data_dir))


def test_healthy_store_passes(memex: Memex) -> None:
    memex.write(WriteInput(type="entity", title="Healthy node", body="fine"))
    report = verify(memex)

    assert report.ok is True
    assert all(check["ok"] for check in report.checks)
    # Health checks plus the five OKF v0.2 conformance checks plus three type checks.
    assert len(report.checks) == 12


def test_malformed_page_fails_health(memex: Memex, data_dir: Path) -> None:
    bad = data_dir / "docs/global/entities/broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("garbage\n", encoding="utf-8")

    report = verify(memex)
    assert report.ok is False
    parse_check = next(check for check in report.checks if check["check"] == "wiki-parseable")
    assert parse_check["ok"] is False


def test_broken_link_fails_health(memex: Memex) -> None:
    memex.write(WriteInput(type="entity", title="Dangling", body="see [[ghost-node]]"))
    report = verify(memex)

    assert report.ok is False
    links_check = next(check for check in report.checks if check["check"] == "links-resolve")
    assert links_check["ok"] is False


def test_index_fresh_fails_on_description_only_mismatch(memex: Memex) -> None:
    memex.write(
        WriteInput(type="entity", title="Fresh desc", body="b", description="indexed signpost")
    )
    page = memex.data_dir / "docs/global/entities/fresh-desc.md"
    page.write_text(
        page.read_text().replace(
            'description: "indexed signpost"', 'description: "edited signpost"'
        ),
        encoding="utf-8",
    )

    report = verify(memex)
    fresh_check = next(check for check in report.checks if check["check"] == "index-fresh")
    assert fresh_check["ok"] is False
    assert "1 stale" in str(fresh_check["detail"])

    memex.rebuild_index()
    assert verify(memex).ok is True


def test_require_recall_fails_without_activity(memex: Memex) -> None:
    memex.write(WriteInput(type="entity", title="Old", body="written long ago"))
    report = verify(memex, since="2100-01-01T00:00:00Z", require_recall=True)

    assert report.ok is False
    assert report.recall_evidence is False
    assert any("no recall activity" in warning for warning in report.warnings)


def test_recall_and_write_evidence(memex: Memex) -> None:
    memex.write(WriteInput(type="entity", title="Fresh", body="just written"))
    memex.recall("fresh")

    report = verify(memex, since="2000-01-01T00:00:00Z", require_recall=True, require_write=True)
    assert report.ok is True
    assert report.recall_evidence is True
    assert report.write_evidence is True


def test_cli_verify_exit_codes(data_dir: Path, capture: dict[str, str]) -> None:
    from memex import cli

    code_ok = cli.main(["--data-dir", str(data_dir), "verify"])
    assert code_ok == 0

    failing = cli.main(
        [
            "--data-dir",
            str(data_dir),
            "verify",
            "--since",
            "2100-01-01T00:00:00Z",
            "--require-write",
        ]
    )
    assert failing == 1
    payload = __import__("json").loads(capture["out"])
    assert payload["ok"] is False
