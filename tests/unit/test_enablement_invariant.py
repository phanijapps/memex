"""T9: no repo-carried config can enable capture or injection (AC-0010)."""

from pathlib import Path

import pytest

from memex import Memex
from memex.infrastructure.config import ConfigLoader, MemexConfig


def _repo_with_config_files(tmp_path: Path) -> Path:
    repo = tmp_path / "cloned-repo"
    repo.mkdir()
    (repo / "memex.toml").write_text(
        '[governance]\nknowledge_approval = "auto"\n', encoding="utf-8"
    )
    (repo / ".memex.toml").write_text('[llm]\nmodel = "smuggled"\n', encoding="utf-8")
    (repo / "hooks.json").write_text('{"SessionStart": []}\n', encoding="utf-8")
    return repo


def test_repo_config_never_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    repo = _repo_with_config_files(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MEMEX_DATA_DIR", str(tmp_path / "memex-home"))
    config = ConfigLoader().load()
    # Repo file says "auto"; the loaded default is "manual", proving it was ignored.
    assert config.governance.knowledge_approval == "manual"
    assert config.llm.model != "smuggled"


def test_home_scope_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_DATA_DIR", str(tmp_path / "memex-home"))
    m = Memex(MemexConfig(data_dir=tmp_path / "memex-home"))
    assert m.data_dir == tmp_path / "memex-home"
    m.close()
