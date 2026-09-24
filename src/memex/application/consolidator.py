"""LLM-driven wiki consolidation (spec §7 Utility 5, §11 prompt verbatim)."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from memex.application.ports import LLMClient
from memex.domain.errors import LLMError
from memex.domain.models import (
    ConsolidateInput,
    ConsolidationReport,
    SemanticLink,
    WikiNode,
    WriteInput,
)
from memex.domain.scrub import scrub
from memex.domain.types import initial_status, validate_type_name
from memex.infrastructure.config import MemexConfig
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.search.link_manager import LinkManager
from memex.infrastructure.store.wiki_store import WikiStore

logger = logging.getLogger("memex")

# The types consolidation may always create; ``decision`` joins this set for a
# run's group only when that project has declared it (spec: catalogue types
# are governed, so an undeclared ``decision`` is out of reach for a model).
BASE_ALLOWED_TYPES: frozenset[str] = frozenset({"entity", "preference", "procedure", "summary"})

SYSTEM_PROMPT = """You are a memory consolidation engine. Your task is to read session episode
records and produce new memory nodes that capture what the agent and user
decided, agreed on, or discovered during the session.

You must output valid JSON matching the schema below. No markdown code fences,
no explanations, no preamble. Just the JSON array."""

USER_PROMPT_TEMPLATE = """## TASK
Analyze the episode nodes below and create new nodes, of the allowed types
listed below, that should be permanently stored in the wiki memory.

## RULES
1. Create a node ONLY if the episode contains a non-obvious, durable fact,
   preference, procedure, or insight worth remembering across sessions.
2. Do NOT create a node for ephemeral, one-off, or obvious facts.
3. Each node title must be descriptive and kebab-case-friendly (e.g.,
   "user-prefers-ruff-over-flake8", "project-tooling-stack").
4. Use importance 0.8-1.0 for critical facts (preferences, hard rules).
   Use 0.5-0.7 for useful context.
5. Include [[wiki-link]] references to existing nodes where relevant.
   Only link to nodes listed in "Existing nodes in the knowledge base" below.
6. Node bodies should be 2-5 sentences. Be specific.
7. tags should be lowercase, kebab-case: ["preference", "python", "tooling"]
8. Give each node a "description": one short sentence on a single line
   (at most 512 UTF-8 bytes) stating WHEN the node is useful — the situation
   or question it answers, phrased in a searcher's words rather than copied
   from the body. It is optional but recommended.

## ALLOWED TYPES
{allowed_types}

## OUTPUT FORMAT
Return a JSON array of node objects. Each object:
{{
  "type": one of the allowed types listed above,
  "title": "kebab-case-title",
  "description": "optional one-sentence summary, single line",
  "body": "2-5 sentence description. May include [[wiki-link]] references.",
  "tags": ["tag1", "tag2"],
  "importance": 0.0-1.0,
  "links": ["existing-node-slug", "other-node-slug:depends-on"],
  "proposed_type": "optional kebab-case name for a concept no listed type fits"
}}

## EXISTING NODES IN THE KNOWLEDGE BASE
{existing_nodes}

## EPISODE NODES TO PROCESS
{episode_nodes}

## OUTPUT"""

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_BODY_EXCERPT = 200


def _format_existing(nodes: list[WikiNode]) -> str:
    lines = []
    for node in nodes:
        excerpt = node.body[:_BODY_EXCERPT].replace("\n", " ")
        lines.append(f"- [{node.type}] {node.slug}: {excerpt}")
    return "\n".join(lines) if lines else "(none)"


def _format_episodes(episodes: list[WikiNode]) -> str:
    blocks = []
    for episode in episodes:
        header = f"### episode {episode.slug} (session_id: {episode.session_id})"
        blocks.append(f"{header}\n\n{episode.body}")
    return "\n\n".join(blocks) if blocks else "(none)"


class WikiConsolidator:
    """Reads episodes, calls the LLM with the §11 prompt, writes nodes."""

    def __init__(
        self,
        wiki_store: WikiStore,
        index_mgr: IndexManager,
        link_mgr: LinkManager,
        llm_client: LLMClient,
        config: MemexConfig,
        *,
        on_page_written: Callable[[Path], None] | None = None,
    ) -> None:
        self._store = wiki_store
        self._index = index_mgr
        self._links = link_mgr
        self._llm = llm_client
        self._config = config
        self._knowledge_approval = config.governance.knowledge_approval
        self._on_page_written = on_page_written

    def consolidate(self, input: ConsolidateInput) -> ConsolidationReport:
        episodes = self._select_episodes(input)

        report = ConsolidationReport(
            mode=input.mode,
            episodes_processed=len(episodes),
            nodes_created=[],
            nodes_updated=[],
            links_added=0,
            llm_calls=0,
            llm_prompt_tokens=0,
            llm_completion_tokens=0,
            dry_run=input.mode == "dry-run",
        )
        if not episodes:
            return report

        groups: dict[tuple[str, str | None], list[WikiNode]] = {}
        for episode in episodes:
            groups.setdefault((episode.scope, episode.project_id), []).append(episode)

        all_nodes = self._store.list()
        for (scope, project_id), group_episodes in groups.items():
            self._consolidate_group(scope, project_id, group_episodes, all_nodes, report)
        return report

    def _consolidate_group(
        self,
        scope: str,
        project_id: str | None,
        episodes: list[WikiNode],
        all_nodes: list[WikiNode],
        report: ConsolidationReport,
    ) -> None:
        """Run the prompt -> parse -> store loop for one ``(scope, project_id)``
        namespace, so consolidated knowledge lands where its episodes came
        from instead of always in global (``docs/v1_enhance.md`` B7).

        ``all_nodes`` is the whole store, listed once in ``consolidate`` and
        shared across every group's call instead of re-scanning per group."""
        existing = [
            node
            # Summaries are consolidated memory but still useful prompt
            # context, so only episodes (the raw material) are excluded.
            for node in all_nodes
            if node.type != "episode" and node.scope == scope and node.project_id == project_id
        ]
        allowed = set(BASE_ALLOWED_TYPES)
        if scope == "project":
            declared = self._store.declared_types(scope=scope, project_id=project_id)
            if "decision" in declared:
                allowed.add("decision")

        prompt = self._build_prompt(existing, episodes, allowed)
        try:
            response = self._llm.complete(
                SYSTEM_PROMPT, prompt, max_tokens=self._config.llm.max_tokens
            )
        except LLMError:
            logger.warning("operation=consolidate status=llm-error episodes=%d", len(episodes))
            return
        report.llm_calls += 1
        report.llm_prompt_tokens += response.prompt_tokens
        report.llm_completion_tokens += response.completion_tokens

        session_ids = sorted({e.session_id for e in episodes if e.session_id})
        # Every episode in a group shares scope and project_id (that is the
        # grouping key); the label is carried per-page rather than per-node,
        # so take the first one an episode actually set.
        project_label = next((e.project_label for e in episodes if e.project_label), None)
        candidates = self._parse_nodes(response.text, allowed)
        proposed_counts = Counter(
            candidate.proposed_type for candidate in candidates if candidate.proposed_type
        )
        for candidate in candidates:
            report.nodes_created.append(candidate)
            if report.dry_run:
                continue
            self._store_node(
                candidate,
                report,
                scope=scope,
                project_id=project_id,
                project_label=project_label,
                session_ids=session_ids,
                proposed_counts=proposed_counts,
            )

    def _select_episodes(self, input: ConsolidateInput) -> list[WikiNode]:
        if input.episode_ids:
            episodes = []
            for slug in input.episode_ids:
                node = self._store.read(slug)
                if node is None or node.type != "episode":
                    raise ValueError(f"not an episode node: {slug!r}")
                episodes.append(node)
            return episodes
        episodes = self._store.list("episode")
        episodes.sort(key=lambda node: node.created, reverse=True)
        return episodes[: input.max_episodes]

    def _build_prompt(
        self, existing: list[WikiNode], episodes: list[WikiNode], allowed: set[str]
    ) -> str:
        return USER_PROMPT_TEMPLATE.format(
            allowed_types=", ".join(sorted(allowed)),
            existing_nodes=_format_existing(existing),
            episode_nodes=_format_episodes(episodes),
        )

    def _parse_nodes(self, text: str, allowed: set[str]) -> list[WriteInput]:
        cleaned = _FENCE.sub("", text.strip())
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("operation=consolidate status=unparseable-llm-output")
            return []
        if not isinstance(data, list):
            return []
        candidates: list[WriteInput] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            node_type = str(item.get("type", ""))
            if node_type not in allowed:
                logger.warning("operation=consolidate status=invalid-node-skipped")
                continue
            try:
                candidates.append(
                    WriteInput(
                        type=node_type,
                        title=str(item.get("title", "")),
                        description=self._model_description(item),
                        body=str(item.get("body", "")),
                        tags=[str(tag) for tag in item.get("tags", [])],
                        importance=float(item.get("importance", 0.5)),
                        links=[str(link) for link in item.get("links", [])],
                        proposed_type=self._accepted_proposed_type(item),
                    )
                )
            except (ValueError, TypeError):
                logger.warning("operation=consolidate status=invalid-node-skipped")
        return candidates

    @staticmethod
    def _accepted_proposed_type(item: dict[str, object]) -> str | None:
        """The model's ``proposed_type``, validated as a custom type name.

        An invalid proposal is dropped (never logged by name); the node
        keeps its stated ``type`` and is not otherwise affected.
        """
        value = item.get("proposed_type")
        if not isinstance(value, str) or not value:
            return None
        try:
            return validate_type_name(value, custom=True)
        except ValueError:
            logger.warning("operation=consolidate proposed_type=rejected")
            return None

    @staticmethod
    def _model_description(item: dict[str, object]) -> str:
        """Scrub the model's optional description; the WriteInput validator
        remains the enforcement boundary for shape and size."""
        value = item.get("description")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("description must be a string")
        return scrub(value)[0]

    def _store_node(
        self,
        candidate: WriteInput,
        report: ConsolidationReport,
        *,
        scope: str,
        project_id: str | None,
        project_label: str | None,
        session_ids: list[str],
        proposed_counts: Counter[str],
    ) -> None:
        node_type = candidate.type
        status = initial_status(candidate.type, "consolidation", self._knowledge_approval)
        if scope == "project" and candidate.proposed_type:
            self._declare_draft_if_needed(
                candidate.proposed_type,
                project_id,
                session_ids,
                proposed_counts[candidate.proposed_type],
            )
            node_type = candidate.proposed_type
            status = "pending"  # drafts are pending by construction, regardless of policy
        node = WikiNode(
            type=node_type,
            title=candidate.title,
            description=candidate.description,
            body=candidate.body,
            id="",
            tags=candidate.tags,
            importance=candidate.importance,
            links=[SemanticLink.parse(link) for link in candidate.links],
            status=status,
            source="consolidation",
            harness=self._config.llm.provider,
            scope=scope,
            project_id=project_id,
            project_label=project_label,
        )
        updating = bool(node.slug) and self._store.exists(node.slug)
        stored = self._store.write(node)
        self._index.update_record(stored)
        links = self._links.sync_node(stored)
        report.links_added += len(links)
        if self._on_page_written is not None and stored.file_path:
            self._on_page_written(Path(stored.file_path))
        if updating:
            report.nodes_updated.append(stored.slug)

    def _declare_draft_if_needed(
        self,
        name: str,
        project_id: str | None,
        session_ids: list[str],
        pages: int,
    ) -> None:
        """Found ``name`` as a project draft type the first time this run
        nominates it; a second candidate proposing the same name in the same
        run reuses the declaration already made. ``pages`` is nomination
        evidence recorded before the pages exist: the number of candidate
        pages nominated for ``name`` in this run, not a live count of pages
        on disk."""
        declared = self._store.declared_types(scope="project", project_id=project_id)
        if name in declared:
            return
        self._store.declare_type(
            name,
            scope="project",
            project_id=project_id,
            actor="consolidation",
            draft=True,
            description=f"sessions={','.join(session_ids)} pages={pages}",
        )
