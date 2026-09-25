import json
from pathlib import Path

import pytest

from memex.application.concept_types import ConceptTypes
from memex.application.consolidator import WikiConsolidator
from memex.application.ports import LLMResponse
from memex.domain.errors import LLMError
from memex.domain.models import ConsolidateInput, WikiNode
from memex.infrastructure.config import ConfigLoader
from memex.infrastructure.llm_clients import OpenAICompatClient, client_from_config
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.search.link_manager import LinkManager
from memex.infrastructure.store.navigation import NavigationGenerator
from memex.infrastructure.store.wiki_store import WikiStore

VALID_LLM_OUTPUT = json.dumps(
    [
        {
            "type": "preference",
            "title": "user-prefers-ruff",
            "body": "The user prefers ruff over flake8 for linting. See [[ruff-linter]].",
            "tags": ["preference", "tooling"],
            "importance": 0.9,
            "links": [],
        },
        {
            "type": "summary",
            "title": "tooling-decisions",
            "body": "Ruff was chosen as the linter for this project.",
            "tags": ["tooling"],
            "importance": 0.6,
        },
    ]
)


class FakeLLM:
    def __init__(self, text: str = VALID_LLM_OUTPUT, error: bool = False) -> None:
        self.text = text
        self.error = error
        self.last_prompt: str | None = None

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResponse:
        self.last_prompt = user
        if self.error:
            raise LLMError("boom")
        return LLMResponse(text=self.text, prompt_tokens=111, completion_tokens=222)


@pytest.fixture
def harness(data_dir: Path) -> tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM]:
    store = WikiStore(data_dir)
    index = IndexManager(data_dir / "mem.db")
    links = LinkManager(index.connection, store.wiki_dir)
    config = ConfigLoader().load()
    fake = FakeLLM()
    consolidator = WikiConsolidator(store, index, links, fake, config)
    return consolidator, store, index, fake


def _episode(store: WikiStore, index: IndexManager, session_id: str) -> WikiNode:
    node = store.write(
        WikiNode(
            type="episode",
            title=f"Session {session_id}",
            body="The user said: I prefer ruff over flake8.",
            id="",
            session_id=session_id,
            transcript_ref=f"transcripts/{session_id}.jsonl",
        )
    )
    index.update_record(node)
    return node


def test_full_mode_writes_nodes(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, _ = harness
    _episode(store, index, "sess-1")

    report = consolidator.consolidate(ConsolidateInput(mode="full"))

    assert report.episodes_processed == 1
    assert len(report.nodes_created) == 2
    assert report.llm_calls == 1
    assert report.llm_prompt_tokens == 111
    assert report.llm_completion_tokens == 222
    written = store.read("user-prefers-ruff")
    assert written is not None
    row = index.get("user-prefers-ruff")
    assert row is not None


def test_dry_run_writes_nothing(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, _ = harness
    _episode(store, index, "sess-2")

    report = consolidator.consolidate(ConsolidateInput(mode="dry-run"))

    assert report.dry_run is True
    assert len(report.nodes_created) == 2
    assert store.read("user-prefers-ruff") is None


def test_llm_error_returns_partial_report(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    fake.error = True
    _episode(store, index, "sess-3")

    report = consolidator.consolidate(ConsolidateInput())

    assert report.nodes_created == []
    assert report.llm_calls == 0
    assert report.episodes_processed == 1


def test_invalid_nodes_skipped(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    _episode(store, index, "sess-4")
    fake.text = json.dumps(
        [
            {"type": "entity", "title": "good node", "body": "fine", "importance": 0.5},
            {"type": "bogus", "title": "bad type", "body": "x", "importance": 0.5},
            {"type": "entity", "title": "", "body": "empty title", "importance": 0.5},
            "not a dict",
        ]
    )

    report = consolidator.consolidate(ConsolidateInput())
    assert len(report.nodes_created) == 1


def test_code_fenced_output_tolerated(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    _episode(store, index, "sess-5")
    fake.text = f"```json\n{VALID_LLM_OUTPUT}\n```"
    report = consolidator.consolidate(ConsolidateInput())
    assert len(report.nodes_created) == 2


def test_specific_episode_ids(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    episode = _episode(store, index, "sess-6")
    fake.text = "[]"

    report = consolidator.consolidate(ConsolidateInput(episode_ids=[episode.slug]))
    assert report.episodes_processed == 1

    with pytest.raises(ValueError, match="not an episode"):
        consolidator.consolidate(ConsolidateInput(episode_ids=["nonexistent"]))


def test_max_episodes_limits_selection(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    fake.text = "[]"
    for i in range(4):
        _episode(store, index, f"sess-m{i}")

    report = consolidator.consolidate(ConsolidateInput(max_episodes=2))
    assert report.episodes_processed == 2


def test_prompt_contains_spec_sections(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, fake = harness
    existing = store.write(WikiNode(type="entity", title="Ruff linter", body="fast", id=""))
    index.update_record(existing)
    _episode(store, index, "sess-p")

    consolidator.consolidate(ConsolidateInput())
    assert fake.last_prompt is not None
    assert "## TASK" in fake.last_prompt
    assert "## RULES" in fake.last_prompt
    assert "## OUTPUT FORMAT" in fake.last_prompt
    assert existing.slug in fake.last_prompt
    assert "sess-p" in fake.last_prompt


def test_prompt_includes_existing_summary_as_context(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    """Summaries are consolidated memory but still useful prompt context;
    _consolidate_group's existing-nodes filter must not drop them."""
    consolidator, store, index, fake = harness
    summary = store.write(WikiNode(type="summary", title="Weekly Recap", body="b", id=""))
    index.update_record(summary)
    _episode(store, index, "sess-q")

    consolidator.consolidate(ConsolidateInput())
    assert fake.last_prompt is not None
    assert summary.slug in fake.last_prompt


def test_consolidated_node_carries_episode_project_label(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    consolidator, store, index, _fake = harness
    project_id = "a" * 24
    episode = store.write(
        WikiNode(
            type="episode",
            title="Session labeled",
            body="The user said: I prefer ruff over flake8.",
            id="",
            session_id="sess-label",
            scope="project",
            project_id=project_id,
            project_label="Acme Web",
        )
    )
    index.update_record(episode)

    consolidator.consolidate(ConsolidateInput())
    node = store.read("user-prefers-ruff")
    assert node is not None and node.project_label == "Acme Web"


def test_withdrawn_proposed_type_is_dropped_not_resurrected(
    harness: tuple[WikiConsolidator, WikiStore, IndexManager, FakeLLM],
) -> None:
    """A model never resurrects a withdrawn type: the candidate lands under
    its own stated type instead of aborting the run with an uncaught
    "withdrawn" WikiStoreError from WikiStore.write."""
    consolidator, store, index, fake = harness
    project_id = "a" * 24

    declared = store.declare_type("story-map", scope="project", project_id=project_id)
    page = store.write(
        WikiNode(
            type="story-map",
            title="Old Map",
            body="b",
            id="",
            scope="project",
            project_id=project_id,
        )
    )
    index.update_record(page)

    types = ConceptTypes(store, index, NavigationGenerator(store.wiki_dir))
    types.remove("story-map", project_id=project_id, force=True)
    log_after_withdrawal = (declared.directory / "log.md").read_text(encoding="utf-8")
    assert store.declared_types(scope="project", project_id=project_id)["story-map"].kind == (
        "withdrawn"
    )

    episode = store.write(
        WikiNode(
            type="episode",
            title="Session withdrawn",
            body="The user said: I prefer ruff over flake8.",
            id="",
            session_id="sess-w",
            scope="project",
            project_id=project_id,
        )
    )
    index.update_record(episode)
    fake.text = json.dumps(
        [
            {
                "type": "entity",
                "title": "Who edits",
                "body": "b",
                "tags": [],
                "importance": 0.5,
                "links": [],
                "proposed_type": "story-map",
            }
        ]
    )

    report = consolidator.consolidate(ConsolidateInput())

    assert report.nodes_created  # the run completed instead of aborting
    node = store.read("who-edits")
    assert node is not None and node.type == "entity"
    assert store.declared_types(scope="project", project_id=project_id)["story-map"].kind == (
        "withdrawn"
    )
    assert (declared.directory / "log.md").read_text(encoding="utf-8") == log_after_withdrawal


def test_client_factory_builds_openai_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    monkeypatch.delenv("MEMEX_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMEX_DATA_DIR", raising=False)
    config = ConfigLoader().load()
    client = client_from_config(config.consolidation_llm())
    assert isinstance(client, OpenAICompatClient)
    assert captured["base_url"] == "https://api.openai.com/v1"


def test_complete_wraps_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingCompletions:
        def create(self, **kwargs: object) -> object:
            raise RuntimeError("api down")

    class ExplodingChat:
        completions = ExplodingCompletions()

    class ExplodingOpenAI:
        def __init__(self, **kwargs: object) -> None:
            pass

        chat = ExplodingChat()

    monkeypatch.setattr("openai.OpenAI", ExplodingOpenAI)
    monkeypatch.delenv("MEMEX_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMEX_DATA_DIR", raising=False)
    client = client_from_config(ConfigLoader().load().consolidation_llm())
    with pytest.raises(LLMError, match="LLM API call failed"):
        client.complete("sys", "user", max_tokens=10)
