"""The Memex facade: the single public API surface (spec §7 Utility core)."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from memex.application.concept_types import ConceptTypes
from memex.application.consolidator import WikiConsolidator
from memex.application.decay import RecencyDecay
from memex.application.ports import LLMClient
from memex.domain.errors import LLMError, WikiStoreError
from memex.domain.models import (
    FORGET_MODES,
    BackupReport,
    ConsolidateInput,
    ConsolidationReport,
    ForgetResult,
    IngestTranscriptInput,
    ProvenanceReport,
    RebuildIndexReport,
    RecallHit,
    RecallResult,
    RestoreReport,
    SemanticLink,
    SessionSummary,
    TaskRecallInput,
    TaskRecallResult,
    TranscriptLinkReport,
    WikiNode,
    WriteInput,
    utc_now_iso,
)
from memex.domain.scrub import scrub
from memex.domain.types import DESCRIPTION_MAX_BYTES
from memex.infrastructure.config import ConfigLoader, MemexConfig
from memex.infrastructure.harness.transcript_hook import TranscriptHook
from memex.infrastructure.llm_clients import client_from_config
from memex.infrastructure.logging import setup_logging
from memex.infrastructure.run_log import append_run
from memex.infrastructure.search.bm25_retriever import BM25Retriever, _query_tokens
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.search.link_manager import LinkManager
from memex.infrastructure.store.backup import BackupRestore
from memex.infrastructure.store.import_export import ImportExport
from memex.infrastructure.store.navigation import NavigationGenerator
from memex.infrastructure.store.navigation_search import FALLBACK_SEARCH_ENGINE, NavigationSearch
from memex.infrastructure.store.wiki_store import WikiStore, hash_body

RecallEngine = Literal["fts5", "navigation"]
RECALL_ENGINES: tuple[RecallEngine, ...] = ("fts5", "navigation")


class Memex:
    """Composes storage, index, retrieval, transcripts, and maintenance.

    The wiki filesystem is the source of truth; every index mutation is
    paired with the file write that justifies it.
    """

    def __init__(self, config: MemexConfig | None = None) -> None:
        self.config = config if config is not None else ConfigLoader().load()
        self.data_dir = self.config.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logging(self.config.logging)
        self._llm: LLMClient | None = None
        self._consolidator: WikiConsolidator | None = None
        self._open_storage()

    def _open_storage(self) -> None:
        self.wiki_store = WikiStore(self.data_dir, slug_algo=self.config.wiki.slug_algo)
        self.index_manager = IndexManager(self.config.db_path)
        rebuild_on_open = self.index_manager.needs_rebuild()
        if rebuild_on_open:
            # mem.db is disposable: a stale schema rebuilds from the wiki.
            self.index_manager.drop_for_rebuild()
        self.link_manager = LinkManager(self.index_manager.connection, self.wiki_store.wiki_dir)
        self.retriever = BM25Retriever(
            self.config.db_path,
            k1=self.config.bm25.k1,
            b=self.config.bm25.b,
            default_top_k=self.config.bm25.default_top_k,
        )
        self.transcript_hook = TranscriptHook(
            self.data_dir,
            self.wiki_store,
            self.index_manager,
            self.link_manager,
            on_page_written=self._on_page_written,
        )
        self.backup_restore = BackupRestore(self.data_dir, self.config.db_path)
        self.import_export = ImportExport(
            self.wiki_store,
            self.index_manager,
            self.link_manager,
            on_page_written=self._on_page_written,
        )
        self.navigation = NavigationGenerator(self.wiki_store.wiki_dir)
        self.types = ConceptTypes(self.wiki_store, self.index_manager, self.navigation)
        if rebuild_on_open:
            # Navigation regeneration never happens on open: the transparent
            # index upgrade stays free of Markdown writes (spec AC-0015).
            self.rebuild_index(force=True, regenerate_navigation=False)

    def close(self) -> None:
        self.retriever.close()
        self.index_manager.close()

    def write(self, input: WriteInput) -> WikiNode:
        """Persist a memory node and update the index and link graph.

        A new node gets a derived, collision-suffixed slug (``ruff-linter``,
        ``ruff-linter-2``); writing an existing slug updates it, preserving
        ``id``, ``created``, ``access_count``, and ``last_access`` from the
        stored page. The Markdown file is written atomically (temp + rename).

        Side effects: writes ``wiki/{type}/{slug}.md``, upserts the row in
        ``mem.db``, and replaces the node's outgoing ``wiki_links`` entries.

        Args:
            input: Node fields; ``type`` must be one of the five node types
                and ``session_id`` is required for episodes.

        Returns:
            The stored node with ``id``, ``slug``, ``file_path``, timestamps,
            and ``content_hash`` assigned.

        Raises:
            ValueError: Body exceeds ``wiki.max_body_chars``, or ``input``
                fails field validation.
            WikiStoreError: The wiki directory is not writable.
        """
        self._guard_reserved(input)
        if len(input.body) > self.config.wiki.max_body_chars:
            raise ValueError(
                f"body exceeds wiki.max_body_chars ({self.config.wiki.max_body_chars})"
            )
        clean_body, body_kinds = scrub(input.body)
        clean_description, description_kinds = scrub(input.description)
        scrub_kinds = body_kinds + description_kinds
        if scrub_kinds:
            self.logger.warning("operation=write scrubbed=%s", ",".join(scrub_kinds))
        if len(clean_description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
            # AC-0002 budget applies to the stored value: redaction can grow
            # an input that passed the WriteInput boundary, so the budget is
            # re-checked on the scrubbed text with an honest message.
            raise ValueError(
                "description exceeds 512 UTF-8 bytes after secret redaction; "
                "shorten the description"
            )
        node = WikiNode(
            type=input.type,
            title=input.title,
            body=clean_body,
            description=clean_description,
            id="",
            tags=input.tags,
            importance=input.importance,
            resource=input.resource,
            links=[SemanticLink.parse(link) for link in input.links],
            parent=input.parent,
            supersedes=input.supersedes,
            implements=input.implements,
            depends_on=input.depends_on,
            session_id=input.session_id,
            transcript_ref=input.transcript_ref,
            stale_after=input.stale_after,
            valid_from=input.valid_from,
            valid_until=input.valid_until,
            scope=input.scope,
            project_id=input.project_id,
            project_label=input.project_label,
            project_locator=input.project_locator,
        )
        stored = self.wiki_store.write(node)
        self.index_manager.update_record(stored)
        self.link_manager.sync_node(stored)
        self._refresh_navigation(stored.file_path)
        self.logger.info("operation=write slug=%s type=%s", stored.slug, stored.type)
        return stored

    def recall(
        self,
        query: str,
        *,
        top_k: int | None = None,
        node_type: str | None = None,
        time_range: tuple[str, str] | None = None,
        tags: list[str] | None = None,
        include_expired: bool = False,
        include_inactive: bool = False,
        max_tokens: int | None = None,
        scope: str = "global",
        project_id: str | None = None,
        engine: RecallEngine = "fts5",
    ) -> RecallResult:
        """Ranked recall from the FTS5 index or the generated navigation.

        FTS5 hits record access statistics; navigation hits do not. The query
        is reduced to safe alphanumeric semantic tokens, searched
        with strict AND matching, and retried with OR only when strict matching
        returns no rows. Untrusted input never reaches the FTS5 MATCH parser.
        Hits are ordered by ascending BM25 score (lower is better, per SQLite
        FTS5). Nodes outside their ``valid_from``/``valid_until`` window are invisible unless
        the caller opts in; this is how soft-forgetting and decay hide memories.

        Side effects: every returned hit gets ``access_count += 1`` and a
        refreshed ``last_access`` in the index. Reads never touch the files.

        Args:
            query: Free-text terms; must contain one alphanumeric token.
            top_k: Maximum hits, in [1, 100]; defaults to the configured
                ``bm25.default_top_k``.
            node_type: Restrict hits to one type, e.g. ``"preference"``.
            time_range: ``(from, to)`` ISO8601 bounds on ``updated``.
            tags: All listed tags must be present (AND semantics).
            include_expired: Also return soft-forgotten and decayed nodes.
            engine: ``"fts5"`` (the index) or ``"navigation"`` (the generated
                ``index.md`` rows: titles and descriptions only, no access
                statistics, ``time_range`` rejected). With ``"fts5"`` and an
                index holding zero rows while navigation lists pages, the
                navigation engine answers and ``search_engine`` says so.

        Returns:
            RecallResult with 1-based ranks, best first. Empty ``hits`` is a
            normal result, not an error.

        Raises:
            ValueError: Query has no searchable terms, or ``top_k`` outside
                [1, 100].
        """
        if engine not in RECALL_ENGINES:
            raise ValueError(f"engine must be one of {RECALL_ENGINES}, got {engine!r}")
        if engine == "navigation":
            result = self._navigation_recall(
                query,
                top_k=top_k,
                node_type=node_type,
                time_range=time_range,
                tags=tags,
                include_expired=include_expired,
                include_inactive=include_inactive,
                max_tokens=max_tokens,
                scope=scope,
                project_id=project_id,
            )
            self.logger.info(
                "operation=recall engine=%s hits=%d navigation_rows=%d",
                result.search_engine,
                len(result.hits),
                result.total_indexed,
            )
            return result
        if self.index_manager.count() == 0:
            # A missing or emptied mem.db degrades recall to the generated
            # navigation instead of answering nothing. time_range needs the
            # index, so that request answers nothing; the notice keeps the
            # degraded state visible to operators.
            if time_range is not None:
                self.logger.warning(
                    "operation=recall engine=fts5 indexed_rows=0 reason=time_range hits=0"
                )
            else:
                fallback = self._navigation_recall(
                    query,
                    top_k=top_k,
                    node_type=node_type,
                    time_range=None,
                    tags=tags,
                    include_expired=include_expired,
                    include_inactive=include_inactive,
                    max_tokens=max_tokens,
                    scope=scope,
                    project_id=project_id,
                )
                if fallback.total_indexed:
                    result = replace(fallback, search_engine=FALLBACK_SEARCH_ENGINE)
                    self.logger.warning(
                        "operation=recall engine=%s indexed_rows=0 navigation_rows=%d hits=%d",
                        result.search_engine,
                        result.total_indexed,
                        len(result.hits),
                    )
                    return result
        if max_tokens is not None:
            from memex.application.context_injection import pack_to_budget

            result = self.retriever.retrieve_without_access(
                query,
                top_k=top_k,
                node_type=node_type,
                time_range=time_range,
                tags=tags,
                include_expired=include_expired,
                include_inactive=include_inactive,
                scope=scope,
                project_id=project_id,
            )
            result.hits = _renumber_hits(pack_to_budget(result.hits, max_tokens))
            self.retriever.record_access(result.hits)
        else:
            result = self.retriever.retrieve(
                query,
                top_k=top_k,
                node_type=node_type,
                time_range=time_range,
                tags=tags,
                include_expired=include_expired,
                include_inactive=include_inactive,
                scope=scope,
                project_id=project_id,
            )
        self.logger.info(
            "operation=recall hits=%d total_indexed=%d", len(result.hits), result.total_indexed
        )
        return result

    def _navigation_recall(
        self,
        query: str,
        *,
        top_k: int | None,
        node_type: str | None,
        time_range: tuple[str, str] | None,
        tags: list[str] | None,
        include_expired: bool,
        include_inactive: bool,
        max_tokens: int | None,
        scope: str,
        project_id: str | None,
    ) -> RecallResult:
        """Recall from the generated navigation; no index row is touched."""
        from memex.application.context_injection import pack_to_budget

        result = NavigationSearch(
            self.wiki_store.wiki_dir, default_top_k=self.config.bm25.default_top_k
        ).search(
            query,
            top_k=top_k,
            node_type=node_type,
            time_range=time_range,
            tags=tags,
            include_expired=include_expired,
            include_inactive=include_inactive,
            scope=scope,
            project_id=project_id,
        )
        if max_tokens is not None:
            result.hits = _renumber_hits(pack_to_budget(result.hits, max_tokens))
        return result

    def recall_task(self, input: TaskRecallInput) -> TaskRecallResult:
        """Gather project evidence for caller-written questions within one budget."""
        from memex.application.task_recall import (
            assemble_task_recall,
            validate_task_budget,
            with_linked_pages,
        )

        input = TaskRecallInput(
            goal=input.goal,
            questions=input.questions,
            project_id=input.project_id,
            max_hits=input.max_hits,
            max_tokens=input.max_tokens,
            node_type=input.node_type,
            tags=list(input.tags) if input.tags is not None else None,
        )
        for question in input.questions:
            try:
                _query_tokens(question)
            except ValueError as exc:
                raise ValueError(f"invalid question: {exc}") from exc
        validate_task_budget(input)
        ranked = [
            self.retriever.retrieve_without_access(
                question,
                top_k=12,  # Deep per-question pool for the budget-bounded pack.
                node_type=input.node_type,
                tags=input.tags,
                scope="project",
                project_id=input.project_id,
            ).hits
            for question in input.questions
        ]
        result, selected = assemble_task_recall(input, ranked)
        result = with_linked_pages(self, input, result, selected)
        self.retriever.record_access(selected)
        self.logger.info("operation=recall_task hits=%d", len(selected))
        return result

    def consolidate(self, input: ConsolidateInput) -> ConsolidationReport:
        """LLM-driven consolidation of episode nodes (spec §11 prompt).

        The only operation that calls an LLM, and only when explicitly
        invoked. Episodes are selected by ``episode_ids`` or the most recent
        ``max_episodes``. LLM output is parsed as a JSON array of nodes;
        entries that fail validation are skipped, not fatal.

        In ``dry-run`` mode nothing is written; the report carries the nodes
        that would be created. On LLM API failure the operation does not
        raise: a partial report with empty ``nodes_created`` is returned and
        the error is logged.

        Side effects (``full`` mode only): writes each produced node, syncs
        its ``wiki_links``, and upserts its index row.

        Raises:
            LLMError: No API key configured for a remote provider.
            ValueError: An ``episode_ids`` entry is not an episode node.
        """
        if self._consolidator is None:
            self._consolidator = WikiConsolidator(
                self.wiki_store,
                self.index_manager,
                self.link_manager,
                self._llm_client(),
                self.config,
                on_page_written=self._on_page_written,
            )
        report = self._consolidator.consolidate(input)
        append_run(
            self.data_dir,
            {
                "ts": utc_now_iso(),
                "kind": "consolidation",
                "mode": report.mode,
                "episodes_processed": report.episodes_processed,
                "nodes_created": len(report.nodes_created),
            },
        )
        self.logger.info(
            "operation=consolidate mode=%s episodes=%d nodes=%d",
            report.mode,
            report.episodes_processed,
            len(report.nodes_created),
        )
        return report

    def forget(
        self,
        slug: str,
        *,
        mode: str = "hard",
        valid_until: str | None = None,
    ) -> ForgetResult:
        """Remove a memory: delete the page, or retire it temporally.

        ``hard`` deletes the file and purges its index row and wiki_links
        entries in both directions — irreversible. ``soft`` and ``decay`` both
        set ``valid_until`` — ``soft`` to now, ``decay`` to one configured
        half-life from now; both keep the file and default the
        timestamp to now, and both hide the node from recall unless the
        caller passes ``include_expired=True``.

        Args:
            slug: Wiki page slug.
            mode: One of ``hard``, ``soft``, ``decay``.
            valid_until: ISO8601 UTC timestamp for soft/decay; defaults to now.

        Raises:
            FileNotFoundError: No page exists for ``slug``.
            ValueError: ``mode`` is invalid.
        """
        if mode not in (*FORGET_MODES, "archive"):
            raise ValueError(f"mode must be one of {FORGET_MODES}, got {mode!r}")
        node = self.wiki_store.read(slug)
        if node is None:
            raise FileNotFoundError(f"no wiki page for slug: {slug!r}")

        if mode == "archive":
            node.status = "archived"
            stored = self.wiki_store.write(node)
            self.index_manager.update_record(stored)
            self._refresh_navigation(stored.file_path)
            self.logger.info("operation=forget mode=archive slug=%s", slug)
            return ForgetResult(slug=slug, forgotten=True, mode=mode, file_path=stored.file_path)
        if mode == "hard":
            page_path = node.file_path
            self.wiki_store.delete(slug)
            self.index_manager.remove_record(slug)
            self.link_manager.remove_slug(slug)
            self._refresh_navigation(page_path)
            self.logger.info("operation=forget mode=hard slug=%s", slug)
            return ForgetResult(slug=slug, forgotten=True, mode=mode, file_path=None)

        # Both modes end the validity window; only the instant differs.
        # "soft" ends it now, "decay" ends it one half-life from now.
        node.valid_until = valid_until or self._validity_end(mode)
        stored = self.wiki_store.write(node)
        self.index_manager.update_record(stored)
        self._refresh_navigation(stored.file_path)
        self.logger.info("operation=forget mode=%s slug=%s", mode, slug)
        return ForgetResult(slug=slug, forgotten=True, mode=mode, file_path=stored.file_path)

    def _validity_end(self, mode: str) -> str:
        """When a forget mode ends the validity window.

        ``soft`` retires the page now. ``decay`` gives it one configured
        half-life first, so the two modes are distinguishable operations
        rather than two names for the same write.
        """
        if mode != "decay":
            return utc_now_iso()
        end = datetime.now(UTC) + timedelta(days=self.config.recency_decay.half_life_days)
        return end.strftime("%Y-%m-%dT%H:%M:%SZ")

    def ingest_transcript(
        self, input: IngestTranscriptInput, *, overwrite: bool = False
    ) -> TranscriptLinkReport:
        """Store a raw transcript and link it to an episode node.

        Turn contents are stored verbatim and never logged. Side effects:
        writes ``transcripts/{session_id}.jsonl`` and ``.meta.json``, creates
        ``wiki/episodes/{session_id}.md`` with ``transcript_ref`` front
        matter, and indexes the episode.

        Args:
            input: Session turns and optional metadata; ``session_id`` is
                restricted to ``[A-Za-z0-9._-]`` (it becomes a filename).
            overwrite: Replace an existing transcript for this session.

        Raises:
            FileExistsError: A transcript for ``session_id`` already exists
                and ``overwrite`` is False.
            ValueError: ``session_id`` or a turn entry is invalid.
        """
        report = self.transcript_hook.ingest(input, overwrite=overwrite)
        append_run(
            self.data_dir,
            {
                "ts": utc_now_iso(),
                "kind": "capture",
                "session_id": input.session_id,
                "harness": input.header.harness if input.header else None,
                "turn_count": report.turn_count,
                "cwd_recorded": bool(input.header and input.header.meta.get("cwd")),
            },
        )
        self.logger.info(
            "operation=ingest_transcript session=%s turns=%d",
            input.session_id,
            report.turn_count,
        )
        return report

    def clear_transcripts(self, *, confirm: bool = False) -> int:
        """Clear raw transcript files while retaining retired episode pages."""
        count = self.transcript_hook.clear_transcripts(confirm=confirm)
        self.logger.info("operation=clear_transcripts count=%d", count)
        return count

    def rebuild_index(
        self, *, force: bool = False, regenerate_navigation: bool = True
    ) -> RebuildIndexReport:
        """Rescan the wiki and refresh the secondary index and link graph.

        The wiki files are the truth: index rows for deleted pages are
        removed, every node's links are re-synced, and pages whose
        front-matter ``content_hash`` went stale through external editing
        are rewritten with a fresh hash. Without ``force``, nodes whose
        stored hash matches the index are skipped; with ``force`` every
        node is re-indexed. Malformed pages are skipped and reported in
        ``errors`` — one bad hand-edit never blocks a rebuild.

        Side effects: updates ``last_index_rebuild`` and ``wiki_file_count``
        in ``index_meta``.
        """
        started = time.perf_counter()
        errors: list[str] = []
        nodes = self.wiki_store.scan_all(errors)
        known: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        if not force:
            for row in self.index_manager.get_all_records():
                key = (
                    str(row["scope"]),
                    str(row["project_id"]),
                    str(row["node_type"]),
                    str(row["slug"]),
                )
                known[key] = (str(row["content_hash"]), str(row["description"] or ""))

        wiki_keys = {(node.scope, node.project_id or "", node.type, node.slug) for node in nodes}
        for row in self.index_manager.get_all_records():
            key = (
                str(row["scope"]),
                str(row["project_id"]),
                str(row["node_type"]),
                str(row["slug"]),
            )
            if key not in wiki_keys:
                self.index_manager.remove_record(
                    str(row["slug"]),
                    scope=str(row["scope"]),
                    project_id=str(row["project_id"]),
                    node_type=str(row["node_type"]),
                )

        skipped = 0
        for node in nodes:
            self.link_manager.sync_node(node)
            if node.content_hash != hash_body(node.body):
                # Externally edited page: refresh the stale front-matter hash.
                node = self.wiki_store.write(node)
            key = (node.scope, node.project_id or "", node.type, node.slug)
            if not force and known.get(key) == (node.content_hash, node.description):
                skipped += 1
                continue
            self.index_manager.update_record(node)

        now = utc_now_iso()
        self.index_manager.set_meta("last_index_rebuild", now)
        self.index_manager.set_meta("wiki_file_count", str(len(nodes)))
        navigation_defects: list[str] = []
        if regenerate_navigation:
            navigation_report = self.navigation.regenerate(nodes)
            for change in navigation_report.by_category("collision"):
                navigation_defects.append(f"navigation collision: {change.path}")
            for change in navigation_report.by_category("write_failed"):
                navigation_defects.append(f"navigation write_failed: {change.path}")
        duration_ms = (time.perf_counter() - started) * 1000
        self.logger.info(
            "operation=rebuild_index nodes=%d skipped=%d errors=%d navigation_defects=%d",
            len(nodes),
            skipped,
            len(errors),
            len(navigation_defects),
        )
        return RebuildIndexReport(
            nodes_indexed=len(nodes) - skipped,
            nodes_skipped=skipped,
            nodes_errored=len(errors),
            duration_ms=round(duration_ms, 3),
            errors=errors,
            navigation_defects=navigation_defects,
        )

    def backup(self, output_path: Path, *, include_mem_db: bool = True) -> BackupReport:
        return self.backup_restore.backup(output_path, include_mem_db=include_mem_db)

    def restore(self, input_path: Path) -> RestoreReport:
        """Restore from an archive, then rebuild the index from the wiki.

        Existing data is never deleted: it is moved to
        ``pre-restore-{timestamp}/`` first. Archive members are validated
        (no absolute paths, traversal, or links) before extraction. Storage
        connections are closed and reopened around the swap, and the index
        is force-rebuilt, so ``index_rebuilt`` is always True on success.

        Raises:
            BackupError: Archive missing, fails verification, or contains
                unsafe members.
        """
        self.retriever.close()
        self.index_manager.close()
        report = self.backup_restore.restore(input_path)
        self._open_storage()
        self.rebuild_index(force=True)
        report.index_rebuilt = True
        self.logger.info("operation=restore warnings=%d", len(report.warnings))
        return report

    @staticmethod
    def _guard_reserved(input: object) -> None:
        """User-supplied source/harness/confidence is a forgery attempt."""
        reserved = {
            key: value
            for key, value in (
                ("source", getattr(input, "source", None)),
                ("harness", getattr(input, "harness", None)),
                ("confidence", getattr(input, "confidence", None)),
            )
            if value is not None
        }
        if reserved:
            raise ValueError(
                "source/harness/confidence are reserved namespaces set by "
                f"capture and consolidation, not user input: {sorted(reserved)}"
            )

    def approve(self, slug: str) -> dict[str, object]:
        """Flip a pending page to active and re-index it."""
        node = self.wiki_store.read(slug)
        if node is None:
            raise FileNotFoundError(f"no wiki page for slug: {slug!r}")
        if node.status != "pending":
            raise ValueError(f"page {slug!r} is {node.status!r}, not pending")
        node.status = "active"
        stored = self.wiki_store.write(node)
        self.index_manager.update_record(stored)
        self._refresh_navigation(stored.file_path)
        self.logger.info("operation=approve slug=%s", slug)
        return {"slug": slug, "status": "active"}

    def merge(self, target: str, source: str) -> dict[str, object]:
        """Append source's body into target; source becomes superseded."""
        target_node = self.wiki_store.read(target)
        if target_node is None:
            raise FileNotFoundError(f"no wiki page for slug: {target!r}")
        source_node = self.wiki_store.read(source)
        if source_node is None:
            raise FileNotFoundError(f"no wiki page for slug: {source!r}")
        target_node.body = (
            target_node.body.rstrip() + f"\n\n## Merged from [[{source}]]\n\n{source_node.body}"
        )
        target_node.supersedes = sorted({*target_node.supersedes, source})
        target_node = self.wiki_store.write(target_node)
        self.index_manager.update_record(target_node)
        self.link_manager.sync_node(target_node)

        source_node.status = "superseded"
        source_node = self.wiki_store.write(source_node)
        self.index_manager.update_record(source_node)
        self._refresh_navigation(target_node.file_path)
        self._refresh_navigation(source_node.file_path)
        self.logger.info("operation=merge target=%s source=%s", target, source)
        return {"target": target, "source": source, "source_status": "superseded"}

    def _on_page_written(self, path: Path) -> None:
        """Shared post-write hook for component-driven page writes.

        Transcript capture, consolidation, and import write pages directly;
        this hook gives them the same navigation refresh the facade applies
        to its own mutations. Never raises: refresh is best effort.
        """
        self._refresh_navigation(str(path))

    def _refresh_navigation(self, file_path: str | None) -> None:
        """Best-effort single-page index refresh after a page mutation.

        Re-reads only the mutated page (absent means deleted) and splices
        its entry into the directory index, so the cost never grows with the
        page's siblings. A page the store cannot parse is invisible to every
        full render, so the chain refresh drops its row instead of leaving
        it stale. Navigation is disposable: a failure here never fails or
        rolls back the mutation. ``memex verify`` derives and reports the
        defect, and a full rebuild repairs it.
        """
        if not file_path:
            return
        page_path = Path(file_path)
        try:
            try:
                node = self.wiki_store.read_path(page_path) if page_path.exists() else None
            except WikiStoreError:
                report = self.navigation.refresh(page_path.parent, self.wiki_store.scan_dir)
            else:
                report = self.navigation.refresh_page(page_path, node, self.wiki_store.scan_dir)
        except Exception:
            self.logger.warning("operation=navigation_refresh status=failed")
            return
        defects = {
            category: count
            for category, count in report.category_counts().items()
            if count and category not in {"written", "removed"}
        }
        if defects:
            self.logger.warning(
                "operation=navigation_refresh status=partial %s",
                " ".join(f"{category}={n}" for category, n in sorted(defects.items())),
            )

    def get_provenance(self, slug: str) -> ProvenanceReport | None:
        return self.transcript_hook.get_provenance(slug)

    def list_sessions(self) -> list[SessionSummary]:
        return self.transcript_hook.list_sessions()

    def status(self) -> dict[str, object]:
        """One-command health: index freshness, captures, pending, zero-yield."""
        from memex.infrastructure.run_log import read_runs, zero_yield_streak

        stale = 0
        from memex.infrastructure.store.wiki_store import hash_body

        for node in self.wiki_store.scan_all():
            row = self.index_manager.get(
                node.slug,
                scope=node.scope,
                project_id=node.project_id or "",
                node_type=node.type,
            )
            if (
                row is None
                or str(row["content_hash"]) != hash_body(node.body)
                or str(row["description"] or "") != node.description
            ):
                stale += 1
        runs = read_runs(self.data_dir)
        last_capture: dict[str, str | None] = {}
        for run in runs:
            if run.get("kind") == "capture" and isinstance(run.get("harness"), str):
                last_capture.setdefault(run["harness"], str(run.get("ts")))
        counts = {"pending": 0, "archived": 0, "superseded": 0}
        for node in self.wiki_store.scan_all():
            if node.status in counts:
                counts[node.status] += 1
        return {
            "index_stale_rows": stale,
            "index_total": self.index_manager.count(),
            "last_capture": last_capture,
            "pending": counts["pending"],
            "archived": counts["archived"],
            "superseded": counts["superseded"],
            "zero_yield_streak": zero_yield_streak(runs),
        }

    def apply_decay(self, *, dry_run: bool = False) -> list[tuple[str, float, float]]:
        """Recompute importance for every node via half-life decay.

        Explicit maintenance call — decay is never applied on access.
        Returns ``(slug, old, new)`` for each node that would change; with
        ``dry_run`` the wiki files and index are left untouched.
        """
        decay = RecencyDecay(
            half_life_days=self.config.recency_decay.half_life_days,
            enabled=self.config.recency_decay.enabled,
        )
        return decay.apply_decay(self.wiki_store, self.index_manager, dry_run=dry_run)

    def _llm_client(self) -> LLMClient:
        """The consolidation client: [consolidation] overrides over [llm].

        Lets distillation run on a cheaper low-effort model than the main
        configuration without duplicating credentials.
        """
        if self._llm is None:
            llm = self.config.consolidation_llm()
            if not llm.api_key and llm.provider in ("openai", "openrouter"):
                raise LLMError("llm.api_key is required (set MEMEX_API_KEY or [llm].api_key)")
            self._llm = client_from_config(llm)
        return self._llm


def _renumber_hits(hits: list[RecallHit]) -> list[RecallHit]:
    return [replace(hit, rank=rank) for rank, hit in enumerate(hits, start=1)]


__all__ = ["Memex"]
