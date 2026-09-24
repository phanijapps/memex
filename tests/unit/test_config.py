from pathlib import Path

import pytest

from memex.domain.errors import ConfigError
from memex.infrastructure.config import ConfigLoader, LLMConfig


class TestDefaults:
    def test_no_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("MEMEX_DATA_DIR", "MEMEX_API_KEY", "MEMEX_LLM_PROVIDER", "MEMEX_LLM_MODEL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEMEX_DATA_DIR", str(tmp_path / "home"))
        config = ConfigLoader().load()
        assert config.data_dir == tmp_path / "home"
        assert config.llm.provider == "openai"
        assert config.llm.base_url == "https://api.openai.com/v1"
        assert config.bm25.default_top_k == 10
        assert config.recency_decay.enabled is True
        assert config.wiki.slug_algo == "kebab"
        assert config.logging.level == "INFO"


class TestTomlLoading:
    def write_config(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_full_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("MEMEX_DATA_DIR", "MEMEX_API_KEY", "MEMEX_LLM_PROVIDER", "MEMEX_LLM_MODEL"):
            monkeypatch.delenv(var, raising=False)
        config_path = tmp_path / "memex.toml"
        self.write_config(
            config_path,
            "\n".join(
                [
                    "[llm]",
                    'provider = "ollama"',
                    'model = "llama3.1"',
                    'api_base = "http://localhost:11434/v1"',
                    "[bm25]",
                    "default_top_k = 5",
                    "[recency_decay]",
                    "enabled = false",
                    "[wiki]",
                    "max_body_chars = 1000",
                ]
            ),
        )
        config = ConfigLoader().load(config_path)
        assert config.llm.provider == "ollama"
        assert config.llm.base_url == "http://localhost:11434/v1"
        assert config.bm25.default_top_k == 5
        assert config.recency_decay.enabled is False
        assert config.wiki.max_body_chars == 1000

    def test_explicit_zero_not_overridden(self, tmp_path: Path) -> None:
        config_path = tmp_path / "memex.toml"
        self.write_config(config_path, "[wiki]\ndefault_importance = 0.0\n")
        config = ConfigLoader().load(config_path)
        assert config.wiki.default_importance == 0.0

    def test_invalid_toml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "memex.toml"
        self.write_config(config_path, "[llm\n")
        with pytest.raises(ConfigError, match="invalid TOML"):
            ConfigLoader().load(config_path)

    def test_unknown_provider(self, tmp_path: Path) -> None:
        config_path = tmp_path / "memex.toml"
        self.write_config(config_path, '[llm]\nprovider = "weird"\n')
        with pytest.raises(ConfigError, match="provider"):
            ConfigLoader().load(config_path)


class TestEnvOverrides:
    def test_env_beats_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = tmp_path / "memex.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '[llm]\nprovider = "openai"\nmodel = "gpt-4o"\n[logging]\nlevel = "INFO"\n'
        )
        monkeypatch.setenv("MEMEX_API_KEY", "sk-test")
        monkeypatch.setenv("MEMEX_LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("MEMEX_LLM_MODEL", "m-model")
        monkeypatch.setenv("MEMEX_LOG_LEVEL", "debug")
        config = ConfigLoader().load(config_path)
        assert config.llm.api_key == "sk-test"
        assert config.llm.provider == "openrouter"
        assert config.llm.base_url == "https://openrouter.ai/api/v1"
        assert config.llm.model == "m-model"
        assert config.logging.level == "DEBUG"


class TestLLMConfig:
    def test_custom_requires_base(self) -> None:
        config = LLMConfig(provider="custom")
        with pytest.raises(ConfigError, match="api_base"):
            _ = config.base_url

    def test_provider_base_urls(self) -> None:
        assert LLMConfig(provider="ollama").base_url.endswith(":11434/v1")
        assert LLMConfig(provider="lmstudio").base_url.endswith(":1234/v1")
        assert LLMConfig(provider="openai", api_base="http://x/v1").base_url == "http://x/v1"


def test_knowledge_approval_replaces_approval(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text('[governance]\nknowledge_approval = "auto"\n')
    assert ConfigLoader().load(path).governance.knowledge_approval == "auto"


def test_knowledge_approval_defaults_to_manual(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text("")
    assert ConfigLoader().load(path).governance.knowledge_approval == "manual"


def test_old_approval_key_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text('[governance]\napproval = "manual"\n')
    with pytest.raises(ConfigError, match=r"knowledge_approval.*manual.*auto"):
        ConfigLoader().load(path)


def test_pages_section_with_wiki_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "memex.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[pages]\ndefault_importance = 0.9\n", encoding="utf-8")
    assert ConfigLoader().load(config_path).wiki.default_importance == 0.9

    legacy = tmp_path / "legacy.toml"
    legacy.write_text("[wiki]\ndefault_importance = 0.7\n", encoding="utf-8")
    assert ConfigLoader().load(legacy).wiki.default_importance == 0.7
