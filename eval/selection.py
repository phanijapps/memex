"""Paired baseline-versus-candidate retrieval selection for PR evidence."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from eval.comparison import (
    ABSOLUTE_P99_LIMITS_MS,
    COMPARISON_SCHEMA_VERSION,
    FLOAT_TOLERANCE,
    CandidateMetrics,
    ComparisonMetadata,
    QueryContextObservation,
    VolatileRunMetadata,
    build_comparison_report,
    evaluate_candidate,
    nearest_rank_p99_ms,
    p99_latency_from_hits,
    tokens_per_correct_hard_query,
)
from eval.corpus import CorpusResult, QuerySpec
from eval.realistic import RealisticCorpusGenerator
from eval.rgapi_candidate import rank_rgapi_candidate
from eval.runner import HitResult
from eval.weighted_retriever import WeightedLexicalRetriever
from eval.workloads import (
    GUTENBERG_FIXTURE,
    SALESFORCE_FIXTURE,
    first_hit_rank,
    load_gutenberg_workload,
    load_salesforce_workload,
    ndcg_at_k,
    validate_fixture_slug,
    validate_fixture_slug_value,
    validate_queries,
)
from memex import __version__ as MEMEX_VERSION
from memex.application import context_injection
from memex.application.memory import Memex
from memex.domain.frontmatter import serialize_front_matter
from memex.domain.models import RecallHit, WikiNode, utc_now_iso
from memex.domain.slugs import unique_slug
from memex.domain.types import TYPE_DIRS
from memex.infrastructure.config import MemexConfig
from memex.infrastructure.search.bm25_retriever import BM25Retriever, production_ranker_metadata
from memex.infrastructure.store.wiki_store import hash_body, node_front_matter

type CandidateName = Literal["field-channel-rrf-k60", "semantic-and-fallback-fts5", "rgapi-0.1.22"]
type WorkloadName = Literal["realistic", "gutenberg", "salesforce"]
type FailureCategory = Literal[
    "dependency_unavailable",
    "invalid_query",
    "path_rejected",
    "incomplete_search",
    "measurement_failed",
    "report_invalid",
    "source_unreproducible",
]
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

CANDIDATE_NAMES: tuple[CandidateName, ...] = (
    "field-channel-rrf-k60",
    "semantic-and-fallback-fts5",
    "rgapi-0.1.22",
)
WORKLOAD_NAMES: tuple[WorkloadName, ...] = ("realistic", "gutenberg", "salesforce")
PROMOTION_WORKLOADS: tuple[WorkloadName, ...] = ("realistic", "gutenberg", "salesforce")
CLOSED_FAILURE_CATEGORIES: set[FailureCategory] = {
    "dependency_unavailable",
    "invalid_query",
    "path_rejected",
    "incomplete_search",
    "measurement_failed",
    "report_invalid",
    "source_unreproducible",
}
PROMOTION_SIZES = (10_000, 100_000)
PROMOTION_SEED = 42
PROMOTION_TOP_K = 10
RGAPI_CANDIDATE_BUDGET_SECONDS = 15.0
SELECTION_SCHEMA_VERSION = 1
SELECTION_MAX_QUERIES_PER_GROUP = 25
SELECTION_QUERY_BOUND_THRESHOLD = 2_000
TOKEN_BUDGET = context_injection.DEFAULT_MAX_TOKENS
SQLITE_IN_BATCH_SIZE = 500
WORKLOAD_RECALL_FLOOR = 0.90
WORKLOAD_MRR_FLOOR = 0.50
WORKLOAD_NDCG_FLOOR = 0.75
WORKLOAD_HARD_RECALL_FLOOR = 0.90
WORKLOAD_HARD_MRR_FLOOR = 0.80
WORKLOAD_FAMILY_RECALL_FLOOR = 0.70
_MAX_FAILURE_REASON = 120
_QUERY_TOKENS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class GitState:
    source_revision: str
    git_dirty: bool | None

    @property
    def promotion_eligible(self) -> bool:
        return self.source_revision != "unknown" and self.git_dirty is False


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    size: int = 100
    seed: int = PROMOTION_SEED
    top_k: int = PROMOTION_TOP_K
    candidates: Sequence[CandidateName] = CANDIDATE_NAMES
    workloads: Sequence[WorkloadName] = ("realistic",)
    evidence_dir: Path | None = None
    data_root: Path | None = None
    promotion_mode: bool = False
    git_state: GitState | None = None


@dataclass(frozen=True, slots=True)
class SelectionFailure:
    category: FailureCategory
    reason: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"category": self.category, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class WorkloadMetrics:
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    hard_recall_at_10: float
    hard_mrr: float
    by_family: Mapping[str, float]
    hard_query_count: int = 1

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "recall_at_10": self.recall_at_10,
            "mrr": self.mrr,
            "ndcg_at_10": self.ndcg_at_10,
            "hard_recall_at_10": self.hard_recall_at_10,
            "hard_mrr": self.hard_mrr,
            "hard_query_count": self.hard_query_count,
            "by_family": dict(self.by_family),
        }


@dataclass(frozen=True, slots=True)
class WorkloadVerdict:
    passed: bool
    gates: Mapping[str, bool]

    def to_dict(self) -> dict[str, JsonValue]:
        return {"passed": self.passed, "gates": dict(self.gates)}


@dataclass(frozen=True, slots=True)
class RunSummary:
    query_count: int
    ordered_query_ids: tuple[int, ...]
    ordered_slugs: tuple[tuple[str, ...], ...]
    p99_ms: float
    access_mutations: int
    pre_run_access_count: int
    complete: bool
    stop_reason: FailureCategory | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "query_count": self.query_count,
            "ordered_query_ids": list(self.ordered_query_ids),
            "ordered_slugs": [list(slugs) for slugs in self.ordered_slugs],
            "p99_ms": self.p99_ms,
            "access_mutations": self.access_mutations,
            "pre_run_access_count": self.pre_run_access_count,
            "complete": self.complete,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    name: CandidateName
    baseline: RunSummary
    candidate: RunSummary
    scales: Mapping[int, dict[str, JsonValue]]
    comparison: dict[str, JsonValue]
    workload_manifest: tuple[dict[str, JsonValue], ...]
    workload_metrics: dict[str, JsonValue]
    passed: bool
    promotion_eligible: bool
    failures: tuple[SelectionFailure, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "scales": {str(size): dict(status) for size, status in self.scales.items()},
            "comparison": self.comparison,
            "workload_manifest": [dict(item) for item in self.workload_manifest],
            "workload_metrics": self.workload_metrics,
            "passed": self.passed,
            "promotion_eligible": self.promotion_eligible,
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True, slots=True)
class SelectionReport:
    metadata: dict[str, JsonValue]
    query_manifest: tuple[dict[str, JsonValue], ...]
    candidates: Mapping[str, CandidateSelection]
    selected_candidate: str | None
    failures: tuple[SelectionFailure, ...]
    volatile: dict[str, JsonValue] = field(default_factory=dict)
    report_path: Path | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "metadata": self.metadata,
            "query_manifest": [dict(item) for item in self.query_manifest],
            "candidates": {name: item.to_dict() for name, item in self.candidates.items()},
            "selected_candidate": self.selected_candidate,
            "failures": [failure.to_dict() for failure in self.failures],
            "volatile": dict(self.volatile),
        }


@dataclass(slots=True)
class _MeasuredRun:
    results: list[HitResult]
    observations: list[QueryContextObservation]
    summary: RunSummary
    ranker_metadata: dict[str, JsonValue]


def run_selection(config: EvaluationConfig) -> SelectionReport:
    """Run a diagnostic or the canonical two-scale promotion evaluation."""
    _validate_config(config)
    _preflight_output_dirs(config)
    started = time.perf_counter()
    git_state = config.git_state or _git_state()
    sizes = PROMOTION_SIZES if config.promotion_mode else (config.size,)
    root_context: tempfile.TemporaryDirectory[str] | nullcontext[str]
    if config.data_root is None:
        root_context = tempfile.TemporaryDirectory(prefix="memex-selection-")
    else:
        config.data_root.mkdir(parents=True, exist_ok=True)
        root_context = nullcontext(str(config.data_root))
    with root_context as root:
        report = _run_in_root(config, Path(root), sizes, git_state, started)
    return _write_report(report, config.evidence_dir)


def _run_in_root(
    config: EvaluationConfig,
    root: Path,
    sizes: Sequence[int],
    git_state: GitState,
    started: float,
) -> SelectionReport:
    quality_size = sizes[0]
    corpora: dict[int, CorpusResult] = {}
    baselines: dict[int, _MeasuredRun] = {}
    candidate_runs: dict[CandidateName, dict[int, _MeasuredRun]] = {
        name: {} for name in config.candidates
    }
    corpus, baseline = _run_scale_setup(root, quality_size, config, retain_quality=True)
    corpora[quality_size] = corpus
    baselines[quality_size] = baseline
    for name in config.candidates:
        candidate_runs[name][quality_size] = _run_candidate_pair(
            name=name,
            snapshot_dir=root / str(quality_size) / "snapshot",
            data_dir=root / str(quality_size) / f"candidate-{name}",
            corpus=corpus,
            top_k=config.top_k,
            retain_quality=True,
        )
    eligible = {
        name
        for name, runs in candidate_runs.items()
        if config.promotion_mode and _eligible_for_large_scale(baseline, runs[quality_size])
    }
    if config.promotion_mode and eligible:
        large_size = sizes[1]
        large_corpus, large_baseline = _run_scale_setup(
            root, large_size, config, retain_quality=False
        )
        corpora[large_size] = large_corpus
        baselines[large_size] = large_baseline
        for name in config.candidates:
            if name in eligible:
                candidate_runs[name][large_size] = _run_candidate_pair(
                    name=name,
                    snapshot_dir=root / str(large_size) / "snapshot",
                    data_dir=root / str(large_size) / f"candidate-{name}",
                    corpus=large_corpus,
                    top_k=config.top_k,
                    retain_quality=False,
                )
    reports: dict[str, CandidateSelection] = {
        name: _candidate_selection(
            name=name,
            baselines=baselines,
            candidates=runs,
            corpora=corpora,
            config=config,
            git_state=git_state,
        )
        for name, runs in candidate_runs.items()
    }
    failures = [failure for item in reports.values() for failure in item.failures]
    selected = _select_candidate(reports) if config.promotion_mode else None
    if not git_state.promotion_eligible:
        failures.append(_failure("source_unreproducible", "source revision is dirty or unknown"))
    return SelectionReport(
        metadata=_metadata(config, corpora, git_state, selected is not None),
        query_manifest=_query_manifest(corpora[quality_size].queries),
        candidates=reports,
        selected_candidate=selected,
        failures=tuple(_dedupe_failures(failures)),
        volatile={
            "created_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    )


def stable_selection_payload(report: SelectionReport) -> dict[str, JsonValue]:
    payload = report.to_dict()
    payload.pop("volatile", None)
    for candidate in _candidate_payloads(payload):
        _drop_timing(candidate)
    return payload


def _validate_config(config: EvaluationConfig) -> None:
    if config.size < 1:
        raise ValueError("size must be positive")
    if not config.workloads:
        raise ValueError("at least one workload is required")
    unknown = [name for name in config.workloads if name not in WORKLOAD_NAMES]
    if unknown:
        raise ValueError(f"unknown workload: {unknown[0]}")
    if config.promotion_mode and (config.seed, config.top_k) != (PROMOTION_SEED, PROMOTION_TOP_K):
        raise ValueError("promotion mode requires seed=42 and top_k=10")
    if config.promotion_mode:
        missing = [name for name in PROMOTION_WORKLOADS if name not in config.workloads]
        if missing:
            required = ", ".join(PROMOTION_WORKLOADS)
            absent = ", ".join(missing)
            raise ValueError(f"promotion mode requires workloads: {required}; missing: {absent}")


def _preflight_output_dirs(config: EvaluationConfig) -> None:
    paths = [path for path in (config.evidence_dir, config.data_root) if path is not None]
    if len(paths) == 2 and paths[0].resolve() == paths[1].resolve():
        raise ValueError("evidence and paired data directories must differ")
    if config.data_root is not None:
        requested_root = config.data_root.expanduser().resolve()
        for protected_root in _protected_memex_roots():
            if requested_root == protected_root or requested_root.is_relative_to(protected_root):
                raise ValueError("selection data directory must be outside protected Memex stores")
    for path in paths:
        _assert_fresh_dir(path, "selection output directory")


def _protected_memex_roots() -> set[Path]:
    default_root = Path.home() / ".memex"
    base = Path(os.environ.get("MEMEX_DATA_DIR", str(default_root))).expanduser()
    roots = {default_root.resolve(), base.resolve()}
    config_path = base / "memex.toml"
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return roots
    data_dir = raw.get("data_dir")
    if isinstance(data_dir, dict):
        override = data_dir.get("path")
        if isinstance(override, str) and override:
            roots.add(Path(override).expanduser().resolve())
    return roots


def _run_scale_setup(
    root: Path,
    size: int,
    config: EvaluationConfig,
    *,
    retain_quality: bool,
) -> tuple[CorpusResult, _MeasuredRun]:
    scale_root = root / str(size)
    snapshot_dir = scale_root / "snapshot"
    corpus = _generate_snapshot(
        snapshot_dir, size=size, seed=config.seed, workloads=config.workloads
    )
    baseline = _run_baseline_pair(
        snapshot_dir=snapshot_dir,
        data_dir=scale_root / "baseline",
        corpus=corpus,
        top_k=config.top_k,
        retain_quality=retain_quality,
    )
    return corpus, baseline


def _generate_snapshot(
    snapshot_dir: Path,
    *,
    size: int,
    seed: int,
    workloads: Sequence[WorkloadName] = ("realistic",),
) -> CorpusResult:
    snapshot_dir.mkdir(parents=True)
    combined = CorpusResult(memories_written=0, queries=[], domain_counts={})
    if "realistic" in workloads:
        realistic = RealisticCorpusGenerator(snapshot_dir, seed=seed).generate(size)
        combined.memories_written += realistic.memories_written
        combined.queries.extend(realistic.queries)
        combined.domain_counts.update(realistic.domain_counts)
    if "gutenberg" in workloads:
        gutenberg = load_gutenberg_workload()
        _append_fixture_workload(snapshot_dir, GUTENBERG_FIXTURE, corpus="gutenberg")
        _merge_workload(combined, gutenberg, "gutenberg")
    if "salesforce" in workloads:
        salesforce = load_salesforce_workload()
        _append_fixture_workload(snapshot_dir, SALESFORCE_FIXTURE, corpus="salesforce")
        _merge_workload(combined, salesforce, "salesforce")
    validate_queries(combined.queries)
    combined.queries = _bound_query_manifest(combined.queries)
    return combined


def _merge_workload(combined: CorpusResult, workload: CorpusResult, name: str) -> None:
    combined.memories_written += workload.memories_written
    combined.queries.extend(workload.queries)
    combined.domain_counts[name] = workload.memories_written


def _bound_query_manifest(queries: Sequence[QuerySpec]) -> list[QuerySpec]:
    if len(queries) <= SELECTION_QUERY_BOUND_THRESHOLD:
        return list(queries)
    retained: list[QuerySpec] = []
    counts: dict[tuple[str, str, str, bool], int] = {}
    for query in queries:
        key = (query.corpus, query.family, query.difficulty, query.negative)
        count = counts.get(key, 0)
        if count >= SELECTION_MAX_QUERIES_PER_GROUP:
            continue
        counts[key] = count + 1
        retained.append(query)
    return retained


def _append_fixture_workload(snapshot_dir: Path, fixture: Path, *, corpus: str) -> None:
    rows = _fixture_rows(fixture)
    used_slugs = {path.stem for path in (snapshot_dir / "docs").rglob("*.md")}
    for row in rows:
        slug = validate_fixture_slug_value(row.get("slug"), field=f"{corpus} fixture slug")
        if slug in used_slugs:
            slug = unique_slug(slug, used_slugs)
            slug = validate_fixture_slug(slug, field=f"{corpus} fixture slug")
        used_slugs.add(slug)
        body = _fixture_body(row, corpus=corpus)
        now = utc_now_iso()
        node = WikiNode(
            type="entity",
            title=str(row["title"] if corpus == "gutenberg" else row["slug"]),
            body=body,
            id=str(uuid.uuid4()),
            slug=slug,
            tags=[corpus, "evaluation"],
            created=now,
            timestamp=now,
            updated_at=now,
            content_hash=hash_body(body),
        )
        path = _fixture_page_path(snapshot_dir, slug)
        path.write_text(serialize_front_matter(node_front_matter(node), body), encoding="utf-8")


def _fixture_rows(fixture: Path) -> list[dict[str, object]]:
    with fixture.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    for row in rows:
        validate_fixture_slug_value(row.get("slug"), field="fixture slug")
    return rows


def _fixture_page_path(snapshot_dir: Path, slug: str) -> Path:
    safe_slug = validate_fixture_slug(slug, field="fixture slug")
    docs_root = snapshot_dir / "docs"
    entity_root = docs_root / TYPE_DIRS["entity"]
    if docs_root.is_symlink():
        raise ValueError("fixture page destination escapes snapshot")
    docs_root.mkdir(parents=True, exist_ok=True)
    _reject_existing_symlink(docs_root, root=snapshot_dir)
    if entity_root.is_symlink():
        raise ValueError("fixture page destination escapes snapshot")
    entity_root.mkdir(parents=True, exist_ok=True)
    _reject_existing_symlink(entity_root, root=snapshot_dir)
    path = entity_root / f"{safe_slug}.md"
    if path.is_symlink():
        raise ValueError("fixture page destination escapes snapshot")
    resolved_root = entity_root.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("fixture page destination escapes snapshot")
    return path


def _reject_existing_symlink(path: Path, *, root: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("fixture page destination escapes snapshot")


def _fixture_body(row: Mapping[str, object], *, corpus: str) -> str:
    if corpus == "gutenberg":
        authors = ", ".join(_object_sequence(row.get("authors")))
        return "\n".join(
            [
                f"Title: {row['title']}",
                f"Authors: {authors}",
                f"Subjects: {row.get('subjects', '')}",
                f"Language: {row.get('language', '')}",
                f"Project Gutenberg book id: {row.get('book_id', '')}",
            ]
        )
    aliases = ", ".join(_object_sequence(row.get("aliases")))
    objects = ", ".join(_object_sequence(row.get("api_or_object_names")))
    return "\n".join(
        [
            f"Salesforce fact: {row['fact']}",
            f"Aliases: {aliases}",
            f"API or object names: {objects}",
            f"Product context: {row.get('product_context', '')}",
            f"Official source URL: {row.get('source_url', '')}",
        ]
    )


def _object_sequence(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(item) for item in value]
    return []


def _run_baseline_pair(
    *,
    snapshot_dir: Path,
    data_dir: Path,
    corpus: CorpusResult,
    top_k: int,
    retain_quality: bool = True,
) -> _MeasuredRun:
    _copy_snapshot(snapshot_dir, data_dir)
    memex = Memex(MemexConfig(data_dir=data_dir))
    try:
        memex.rebuild_index(force=True)
        pre_access = _total_access_count(data_dir)
        results: list[HitResult] = []
        observations: list[QueryContextObservation] = []
        latencies: list[float] = []
        for query in corpus.queries:
            started = time.perf_counter()
            recalled = memex.retriever.retrieve_legacy_or(query.query, top_k=top_k)
            latency_ms = (time.perf_counter() - started) * 1000
            latencies.append(latency_ms)
            if retain_quality:
                results.append(_hit_result(query, [hit.slug for hit in recalled.hits], latency_ms))
                observations.append(
                    _observation(query, recalled.hits, recalled.total_indexed, "bm25", latency_ms)
                )
        access_mutations = _total_access_count(data_dir) - pre_access
    finally:
        memex.close()
    metadata: dict[str, JsonValue] = {
        "name": "sqlite-fts5-bm25",
        "bm25_parameters": "sqlite-fts5-defaults",
        "query_strategy": "safe-token-or",
        "snippet_tokens": 32,
        "tie_break": "score-then-slug",
        "top_k": top_k,
    }
    if not retain_quality:
        return _latency_only_run(
            latencies, len(corpus.queries), access_mutations, pre_access, metadata
        )
    return _measured_run(results, observations, access_mutations, pre_access, metadata)


def _run_candidate_pair(
    *,
    name: CandidateName,
    snapshot_dir: Path,
    data_dir: Path,
    corpus: CorpusResult,
    top_k: int,
    retain_quality: bool = True,
) -> _MeasuredRun:
    _copy_snapshot(snapshot_dir, data_dir)
    memex = Memex(MemexConfig(data_dir=data_dir))
    try:
        memex.rebuild_index(force=True)
    finally:
        memex.close()
    pre_access = _total_access_count(data_dir)
    results: list[HitResult] = []
    observations: list[QueryContextObservation] = []
    latencies: list[float] = []
    complete = True
    stop_reason: FailureCategory | None = None
    metadata: dict[str, JsonValue] = {"name": name}
    candidate_started = time.perf_counter()
    weighted = _weighted_retriever_for_candidate(name, data_dir / "mem.db")
    production = _production_retriever_for_candidate(name, data_dir / "mem.db")
    rgapi_conn = sqlite3.connect(data_dir / "mem.db") if name == "rgapi-0.1.22" else None
    if rgapi_conn is not None:
        rgapi_conn.row_factory = sqlite3.Row
    try:
        for query in corpus.queries:
            if weighted is not None:
                outcome = _run_weighted_query(weighted, query, top_k)
            elif production is not None:
                outcome = _run_production_query(production, query, top_k)
            else:
                outcome = _run_rgapi_query(data_dir, rgapi_conn, query, top_k)
            metadata = outcome.ranker_metadata
            latencies.append(outcome.latency_ms)
            if retain_quality:
                results.append(
                    _hit_result(query, [hit.slug for hit in outcome.hits], outcome.latency_ms)
                )
                observations.append(
                    _observation(
                        query, outcome.hits, corpus.memories_written, name, outcome.latency_ms
                    )
                )
            if not outcome.complete:
                complete = False
                stop_reason = outcome.stop_reason or "incomplete_search"
                break
            if (
                name == "rgapi-0.1.22"
                and time.perf_counter() - candidate_started >= RGAPI_CANDIDATE_BUDGET_SECONDS
                and len(latencies) < len(corpus.queries)
            ):
                complete = False
                stop_reason = "incomplete_search"
                metadata = {**metadata, "evaluation_budget_seconds": RGAPI_CANDIDATE_BUDGET_SECONDS}
                break
    finally:
        if weighted is not None:
            weighted.close()
        if production is not None:
            production.close()
        if rgapi_conn is not None:
            rgapi_conn.close()
    access_mutations = _total_access_count(data_dir) - pre_access
    measured = (
        _measured_run(results, observations, access_mutations, pre_access, metadata)
        if retain_quality
        else _latency_only_run(
            latencies, len(results) or len(latencies), access_mutations, pre_access, metadata
        )
    )
    return _replace_completion(measured, complete, stop_reason)


@dataclass(frozen=True, slots=True)
class _CandidateOutcome:
    hits: list[RecallHit]
    latency_ms: float
    complete: bool
    stop_reason: FailureCategory | None
    ranker_metadata: dict[str, JsonValue]


def _run_weighted_query(
    retriever: WeightedLexicalRetriever | None, query: QuerySpec, top_k: int
) -> _CandidateOutcome:
    if retriever is None:
        raise RuntimeError("weighted retriever is unavailable")
    started = time.perf_counter()
    recalled = retriever.retrieve(query.query, top_k=top_k)
    latency_ms = (time.perf_counter() - started) * 1000
    return _CandidateOutcome(
        recalled.hits, latency_ms, True, None, _json_mapping(retriever.metadata())
    )


def _weighted_retriever_for_candidate(
    name: CandidateName,
    db_path: Path,
) -> WeightedLexicalRetriever | None:
    if name == "field-channel-rrf-k60":
        return WeightedLexicalRetriever(db_path)
    return None


def _production_retriever_for_candidate(
    name: CandidateName,
    db_path: Path,
) -> BM25Retriever | None:
    if name == "semantic-and-fallback-fts5":
        return BM25Retriever(db_path)
    return None


def _run_production_query(
    retriever: BM25Retriever | None, query: QuerySpec, top_k: int
) -> _CandidateOutcome:
    if retriever is None:
        raise RuntimeError("production retriever is unavailable")
    started = time.perf_counter()
    recalled = retriever.retrieve_without_access(query.query, top_k=top_k)
    latency_ms = (time.perf_counter() - started) * 1000
    retriever.record_access(recalled.hits)
    return _CandidateOutcome(
        recalled.hits,
        latency_ms,
        True,
        None,
        _json_mapping(production_ranker_metadata()),
    )


def _run_rgapi_query(
    data_dir: Path, conn: sqlite3.Connection | None, query: QuerySpec, top_k: int
) -> _CandidateOutcome:
    if conn is None:
        raise RuntimeError("rgapi database is unavailable")
    started = time.perf_counter()
    try:
        ranking = rank_rgapi_candidate(query.query, data_dir / "docs")
    except ValueError:
        return _CandidateOutcome(
            [],
            (time.perf_counter() - started) * 1000,
            False,
            "invalid_query",
            {"name": "rgapi-0.1.22", "complete": False},
        )
    stop_reason = _failure_category(ranking.stop_reason)
    if not ranking.complete:
        return _CandidateOutcome(
            [],
            (time.perf_counter() - started) * 1000,
            False,
            stop_reason,
            _json_mapping(ranking.ranker_metadata()),
        )
    hits = _hydrate_rgapi_hits(conn, query.query, ranking.actual_slugs, top_k)
    _record_access(conn, hits)
    return _CandidateOutcome(
        hits,
        (time.perf_counter() - started) * 1000,
        True,
        None,
        _json_mapping(ranking.ranker_metadata()),
    )


def _hydrate_rgapi_hits(
    conn: sqlite3.Connection, query: str, slugs: Sequence[str], top_k: int
) -> list[RecallHit]:
    if not slugs:
        return []
    now = utc_now_iso()
    by_slug: dict[str, sqlite3.Row] = {}
    retained_slugs: list[str] = []
    for batch in _batches(slugs, SQLITE_IN_BATCH_SIZE):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT * FROM wiki_index WHERE slug IN (" + placeholders + ") "  # noqa: S608
            "AND (valid_from IS NULL OR valid_from <= ?) "
            "AND (valid_until IS NULL OR valid_until >= ?) "
            "AND (status IS NULL OR status = 'active')",
            (*batch, now, now),
        ).fetchall()
        batch_by_slug = {str(row["slug"]): row for row in rows}
        for slug in batch:
            row = batch_by_slug.get(slug)
            if row is None:
                continue
            by_slug[slug] = row
            retained_slugs.append(slug)
            if len(retained_slugs) == top_k:
                break
        if len(retained_slugs) == top_k:
            break
    if not retained_slugs:
        return []
    links = _links_for_slugs(conn, retained_slugs)
    return [
        _rgapi_hit(by_slug[slug], rank, query, links.get(slug, []))
        for rank, slug in enumerate(retained_slugs, start=1)
    ]


def _batches(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _links_for_slugs(conn: sqlite3.Connection, slugs: Sequence[str]) -> dict[str, list[str]]:
    placeholders = ",".join("?" for _ in slugs)
    rows = conn.execute(
        "SELECT source_slug, target_slug FROM wiki_links WHERE source_slug IN ("  # noqa: S608
        + placeholders
        + ") ORDER BY source_slug, target_slug",
        tuple(slugs),
    ).fetchall()
    links: dict[str, list[str]] = {}
    for row in rows:
        links.setdefault(str(row["source_slug"]), []).append(str(row["target_slug"]))
    return links


def _rgapi_hit(row: sqlite3.Row, rank: int, query: str, links: list[str]) -> RecallHit:
    return RecallHit(
        slug=str(row["slug"]),
        file_path=str(row["file_path"]),
        title=str(row["title"]),
        node_type=str(row["node_type"]),
        importance=float(row["importance"]),
        score=0.0,
        rank=rank,
        snippet=_snippet(str(row["body"]), query),
        snippet_source="body",
        tags=json.loads(str(row["tags"])),
        created=str(row["created"]),
        timestamp=str(row["timestamp"]),
        updated_at=str(row["updated_at"]),
        last_access=row["last_access"],
        transcript_ref=row["transcript_ref"],
        links=links,
        status=str(row["status"]),
    )


def _record_access(conn: sqlite3.Connection, hits: Sequence[RecallHit]) -> None:
    now = utc_now_iso()
    with conn:
        conn.executemany(
            "UPDATE wiki_index SET access_count = access_count + 1, last_access = ? WHERE slug = ?",
            [(now, hit.slug) for hit in hits],
        )


def _observation(
    query: QuerySpec,
    hits: Sequence[RecallHit],
    total_indexed: int,
    engine: str,
    latency_ms: float,
) -> QueryContextObservation:
    return QueryContextObservation(
        query=query.query,
        difficulty=query.difficulty,
        expected_slugs=query.expected_slugs,
        hits=hits,
        negative=query.negative,
        total_indexed=total_indexed,
        search_engine=engine,
        search_time_ms=latency_ms,
    )


def _measured_run(
    results: list[HitResult],
    observations: list[QueryContextObservation],
    access_mutations: int,
    pre_access: int,
    ranker_metadata: dict[str, JsonValue],
) -> _MeasuredRun:
    summary = RunSummary(
        query_count=len(results),
        ordered_query_ids=tuple(range(len(results))),
        ordered_slugs=tuple(tuple(result.actual_slugs) for result in results),
        p99_ms=p99_latency_from_hits(results) if results else 0.0,
        access_mutations=access_mutations,
        pre_run_access_count=pre_access,
        complete=True,
    )
    return _MeasuredRun(results, observations, summary, ranker_metadata)


def _latency_only_run(
    latencies: Sequence[float],
    query_count: int,
    access_mutations: int,
    pre_access: int,
    ranker_metadata: dict[str, JsonValue],
) -> _MeasuredRun:
    summary = RunSummary(
        query_count=query_count,
        ordered_query_ids=(),
        ordered_slugs=(),
        p99_ms=nearest_rank_p99_ms(latencies) if latencies else 0.0,
        access_mutations=access_mutations,
        pre_run_access_count=pre_access,
        complete=True,
    )
    return _MeasuredRun([], [], summary, ranker_metadata)


def _replace_completion(
    measured: _MeasuredRun, complete: bool, stop_reason: FailureCategory | None
) -> _MeasuredRun:
    old = measured.summary
    summary = RunSummary(
        old.query_count,
        old.ordered_query_ids,
        old.ordered_slugs,
        old.p99_ms,
        old.access_mutations,
        old.pre_run_access_count,
        complete,
        stop_reason,
    )
    return _MeasuredRun(measured.results, measured.observations, summary, measured.ranker_metadata)


def _eligible_for_large_scale(baseline: _MeasuredRun, candidate: _MeasuredRun) -> bool:
    if not candidate.summary.complete:
        return False
    try:
        base = _quality_metrics(baseline, {})
        cand = _quality_metrics(candidate, {})
    except ValueError:
        return False
    verdict = evaluate_candidate(
        baseline=_with_p99(base, baseline.summary.p99_ms),
        candidate=_with_p99(cand, candidate.summary.p99_ms),
    )
    required_gates = (
        "hard_recall_at_10",
        "hard_mrr",
        "recall_regression",
        "tokens_per_correct_hard_query",
        "paired_p99",
    )
    return (
        all(verdict.gates[name].passed for name in required_gates)
        and candidate.summary.p99_ms < ABSOLUTE_P99_LIMITS_MS[10_000]
    )


def _with_p99(metrics: CandidateMetrics, p99_ms: float) -> CandidateMetrics:
    return CandidateMetrics(
        hard_recall_at_10=metrics.hard_recall_at_10,
        hard_mrr=metrics.hard_mrr,
        recall_at_10=metrics.recall_at_10,
        tokens_per_correct_hard_query=metrics.tokens_per_correct_hard_query,
        p99_ms={10_000: p99_ms},
        complete=metrics.complete,
    )


def _candidate_selection(
    *,
    name: CandidateName,
    baselines: Mapping[int, _MeasuredRun],
    candidates: Mapping[int, _MeasuredRun],
    corpora: Mapping[int, CorpusResult],
    config: EvaluationConfig,
    git_state: GitState,
) -> CandidateSelection:
    quality_size = min(corpora)
    baseline = baselines[quality_size]
    candidate = candidates[quality_size]
    failures: list[SelectionFailure] = []
    for size, run in candidates.items():
        if run.summary.stop_reason is not None:
            failures.append(
                _failure(
                    run.summary.stop_reason,
                    f"candidate search did not complete at {size} memories",
                )
            )
    try:
        comparison_report = build_comparison_report(
            baseline=_quality_metrics(
                baseline,
                {size: run.summary.p99_ms for size, run in baselines.items()},
                complete=all(run.summary.complete for run in baselines.values()),
            ),
            candidate=_quality_metrics(
                candidate,
                {size: run.summary.p99_ms for size, run in candidates.items()},
                complete=all(run.summary.complete for run in candidates.values()),
            ),
            metadata=_comparison_metadata(
                name,
                config,
                corpora,
                git_state,
                baseline.ranker_metadata,
                candidate.ranker_metadata,
            ),
            volatile=VolatileRunMetadata(datetime.now(UTC).isoformat(), 0.0),
        )
        comparison = comparison_report.stable_payload()
    except ValueError:
        failures.append(_failure("measurement_failed", "token metric could not be computed"))
        comparison = {"schema_version": COMPARISON_SCHEMA_VERSION, "verdict": {"passed": False}}
    verdict = comparison.get("verdict", {})
    workload_manifest = _workload_manifest(config.workloads)
    workload_metrics = _workload_report(corpora[quality_size].queries, candidate.results)
    floor_verdict = validate_workload_metrics(_overall_workload_metrics(workload_metrics))
    if not floor_verdict.passed:
        failures.append(_failure("measurement_failed", "workload quality floor failed"))
    for workload_name, metrics_payload in _workload_metric_items(workload_metrics):
        verdict_payload = metrics_payload.get("verdict")
        if isinstance(verdict_payload, dict) and verdict_payload.get("passed") is not True:
            failures.append(
                _failure("measurement_failed", f"{workload_name} workload quality floor failed")
            )
    passed = (
        bool(isinstance(verdict, dict) and verdict.get("passed") is True)
        and floor_verdict.passed
        and not any(failure.category == "measurement_failed" for failure in failures)
    )
    promotion_eligible = config.promotion_mode and passed and git_state.promotion_eligible
    if config.promotion_mode and passed and not git_state.promotion_eligible:
        failures.append(_failure("source_unreproducible", "source revision is dirty or unknown"))
    return CandidateSelection(
        name,
        baseline.summary,
        candidate.summary,
        _scale_statuses(baselines, candidates),
        comparison,
        workload_manifest,
        workload_metrics,
        passed,
        promotion_eligible,
        tuple(failures),
    )


def validate_workload_metrics(metrics: WorkloadMetrics) -> WorkloadVerdict:
    gates = {
        "recall_at_10": metrics.recall_at_10 + FLOAT_TOLERANCE >= WORKLOAD_RECALL_FLOOR,
        "mrr": metrics.mrr + FLOAT_TOLERANCE >= WORKLOAD_MRR_FLOOR,
        "ndcg_at_10": metrics.ndcg_at_10 + FLOAT_TOLERANCE >= WORKLOAD_NDCG_FLOOR,
        "hard_recall_at_10": metrics.hard_query_count > 0
        and metrics.hard_recall_at_10 + FLOAT_TOLERANCE >= WORKLOAD_HARD_RECALL_FLOOR,
        "hard_mrr": metrics.hard_query_count > 0
        and metrics.hard_mrr + FLOAT_TOLERANCE >= WORKLOAD_HARD_MRR_FLOOR,
        "family_recall_at_10": bool(metrics.by_family)
        and all(
            value + FLOAT_TOLERANCE >= WORKLOAD_FAMILY_RECALL_FLOOR
            for value in metrics.by_family.values()
        ),
    }
    return WorkloadVerdict(passed=all(gates.values()), gates=gates)


def _workload_report(
    queries: Sequence[QuerySpec],
    results: Sequence[HitResult],
) -> dict[str, JsonValue]:
    by_workload: dict[str, JsonValue] = {}
    for name in sorted({query.corpus for query in queries}):
        workload_pairs = [
            (query, result)
            for query, result in zip(queries, results, strict=False)
            if query.corpus == name
        ]
        workload_queries = [query for query in queries if query.corpus == name]
        metrics = _metrics_for_queries(
            workload_queries,
            [result for _, result in workload_pairs],
        )
        by_workload[name] = {
            **metrics.to_dict(),
            "by_difficulty": cast(
                dict[str, JsonValue],
                _metrics_by_difficulty(
                    workload_queries,
                    [result for _, result in workload_pairs],
                ),
            ),
            "negative_controls": _negative_control_report(workload_pairs),
            "verdict": validate_workload_metrics(metrics).to_dict(),
        }
    overall = _metrics_for_queries(queries, results)
    pairs = _query_result_pairs(queries, results)
    return {
        "overall": overall.to_dict(),
        "by_difficulty": cast(dict[str, JsonValue], _metrics_by_difficulty(queries, results)),
        "overall_verdict": validate_workload_metrics(overall).to_dict(),
        "by_workload": by_workload,
        "negative_controls": _negative_control_report(pairs),
    }


def _metrics_for_queries(
    queries: Sequence[QuerySpec],
    results: Sequence[HitResult],
) -> WorkloadMetrics:
    paired = _positive_query_result_pairs(queries, results)
    if not paired:
        return WorkloadMetrics(0.0, 0.0, 0.0, 0.0, 0.0, {})
    hard = [(query, result) for query, result in paired if query.difficulty == "hard"]
    by_family = {
        family: _paired_recall_at_10(
            [(query, result) for query, result in paired if query.family == family]
        )
        for family in sorted({query.family for query, _ in paired if query.family})
    }
    return WorkloadMetrics(
        recall_at_10=_paired_recall_at_10(paired),
        mrr=sum(_reciprocal_rank(query, result) for query, result in paired) / len(paired),
        ndcg_at_10=_paired_ndcg_at_10(paired),
        hard_recall_at_10=_paired_recall_at_10(hard) if hard else 0.0,
        hard_mrr=_paired_mrr(hard) if hard else 0.0,
        by_family=by_family,
        hard_query_count=len(hard),
    )


def _metrics_by_difficulty(
    queries: Sequence[QuerySpec],
    results: Sequence[HitResult],
) -> dict[str, dict[str, JsonValue]]:
    paired = _positive_query_result_pairs(queries, results)
    return {
        difficulty: _difficulty_metrics(
            [(query, result) for query, result in paired if query.difficulty == difficulty]
        )
        for difficulty in ("easy", "medium", "hard")
    }


def _difficulty_metrics(paired: Sequence[tuple[QuerySpec, HitResult]]) -> dict[str, JsonValue]:
    return {
        "query_count": len(paired),
        "recall_at_10": _paired_recall_at_10(paired),
        "mrr": _paired_mrr(paired),
        "ndcg_at_10": _paired_ndcg_at_10(paired),
    }


def _paired_recall_at_10(paired: Sequence[tuple[QuerySpec, HitResult]]) -> float:
    if not paired:
        return 0.0
    return sum(
        1
        for query, result in paired
        if first_hit_rank(result.actual_slugs, query.expected_slugs) is not None
    ) / len(paired)


def _reciprocal_rank(query: QuerySpec, result: HitResult) -> float:
    rank = first_hit_rank(result.actual_slugs, query.expected_slugs)
    return 0.0 if rank is None else 1.0 / rank


def _paired_mrr(paired: Sequence[tuple[QuerySpec, HitResult]]) -> float:
    if not paired:
        return 0.0
    return sum(_reciprocal_rank(query, result) for query, result in paired) / len(paired)


def _paired_ndcg_at_10(paired: Sequence[tuple[QuerySpec, HitResult]]) -> float:
    if not paired:
        return 0.0
    return sum(
        ndcg_at_k(result.actual_slugs, query.expected_slugs, 10) for query, result in paired
    ) / len(paired)


def _positive_query_result_pairs(
    queries: Sequence[QuerySpec],
    results: Sequence[HitResult],
) -> list[tuple[QuerySpec, HitResult]]:
    return [
        (query, result)
        for query, result in _query_result_pairs(queries, results)
        if not query.negative
    ]


def _query_result_pairs(
    queries: Sequence[QuerySpec],
    results: Sequence[HitResult],
) -> list[tuple[QuerySpec, HitResult]]:
    return list(zip(queries, results, strict=False))


def _negative_control_report(
    paired: Sequence[tuple[QuerySpec, HitResult]],
) -> dict[str, JsonValue]:
    negative_pairs = [(query, result) for query, result in paired if query.negative]
    count = len(negative_pairs)
    non_empty_count = sum(1 for _, result in negative_pairs if result.actual_slugs)
    return {
        "query_count": count,
        "non_empty_result_count": non_empty_count,
        "non_empty_result_rate": non_empty_count / count if count else 0.0,
        "by_family": _negative_counts_by(negative_pairs, "family"),
        "by_difficulty": _negative_counts_by(negative_pairs, "difficulty"),
    }


def _negative_counts_by(
    paired: Sequence[tuple[QuerySpec, HitResult]], field: Literal["family", "difficulty"]
) -> dict[str, JsonValue]:
    counts: dict[str, dict[str, int]] = {}
    for query, result in paired:
        name = getattr(query, field)
        bucket = counts.setdefault(name, {"query_count": 0, "non_empty_result_count": 0})
        bucket["query_count"] += 1
        if result.actual_slugs:
            bucket["non_empty_result_count"] += 1
    return {
        name: {
            **bucket,
            "non_empty_result_rate": bucket["non_empty_result_count"] / bucket["query_count"],
        }
        for name, bucket in sorted(counts.items())
    }


def _overall_workload_metrics(report: Mapping[str, JsonValue]) -> WorkloadMetrics:
    raw = report["overall"]
    if not isinstance(raw, dict):
        return WorkloadMetrics(0.0, 0.0, 0.0, 0.0, 0.0, {})
    by_family = raw.get("by_family")
    return WorkloadMetrics(
        recall_at_10=_json_float(raw.get("recall_at_10")),
        mrr=_json_float(raw.get("mrr")),
        ndcg_at_10=_json_float(raw.get("ndcg_at_10")),
        hard_recall_at_10=_json_float(raw.get("hard_recall_at_10")),
        hard_mrr=_json_float(raw.get("hard_mrr")),
        by_family=_json_float_mapping(by_family),
        hard_query_count=int(_json_float(raw.get("hard_query_count"))),
    )


def _json_float(value: JsonValue) -> float:
    return float(value) if isinstance(value, str | int | float | bool) else 0.0


def _json_float_mapping(value: JsonValue) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {key: _json_float(item) for key, item in value.items()}


def _workload_metric_items(
    report: Mapping[str, JsonValue],
) -> list[tuple[str, dict[str, JsonValue]]]:
    by_workload = report.get("by_workload")
    if not isinstance(by_workload, dict):
        return []
    return [
        (name, metrics)
        for name, metrics in by_workload.items()
        if isinstance(name, str) and isinstance(metrics, dict)
    ]


def _workload_manifest(workloads: Sequence[WorkloadName]) -> tuple[dict[str, JsonValue], ...]:
    manifests: dict[WorkloadName, dict[str, JsonValue]] = {
        "realistic": {
            "name": "realistic",
            "fixture_version": "generated",
            "source_manifest": "seed-42",
        },
        "gutenberg": {
            "name": "gutenberg",
            "fixture_version": "eval/data/gutenberg-books.jsonl",
            "source_manifest": "gutenberg-books.jsonl provenance",
        },
        "salesforce": {
            "name": "salesforce",
            "fixture_version": "eval/data/salesforce-facts.jsonl",
            "source_manifest": "salesforce-facts.jsonl citations",
        },
    }
    return tuple(manifests[name] for name in workloads)


def _scale_statuses(
    baselines: Mapping[int, _MeasuredRun], candidates: Mapping[int, _MeasuredRun]
) -> dict[int, dict[str, JsonValue]]:
    statuses: dict[int, dict[str, JsonValue]] = {}
    for size in sorted(set(baselines) | set(candidates)):
        statuses[size] = {
            "baseline": _run_status(baselines.get(size)),
            "candidate": _run_status(candidates.get(size)),
        }
    return statuses


def _run_status(run: _MeasuredRun | None) -> JsonValue:
    if run is None:
        return None
    return {
        "query_count": run.summary.query_count,
        "p99_ms": run.summary.p99_ms,
        "complete": run.summary.complete,
        "stop_reason": run.summary.stop_reason,
    }


def _quality_metrics(
    measured: _MeasuredRun,
    p99_ms: Mapping[int, float],
    *,
    complete: bool | None = None,
) -> CandidateMetrics:
    hard = [
        result for result in measured.results if result.difficulty == "hard" and not result.negative
    ]
    if not hard:
        raise ValueError("evaluation corpus must include hard queries")
    positive_results = _positive_results(measured.results)
    by_difficulty = _recall_by_difficulty(positive_results)
    return CandidateMetrics(
        hard_recall_at_10=_recall_at_k(hard, 10),
        hard_mrr=_mrr(hard),
        recall_at_10={
            "overall": _recall_at_k(positive_results, 10),
            "easy": by_difficulty.get("easy", 0.0),
            "medium": by_difficulty.get("medium", 0.0),
        },
        tokens_per_correct_hard_query=tokens_per_correct_hard_query(
            measured.observations, token_budget=TOKEN_BUDGET
        ),
        p99_ms=p99_ms,
        complete=measured.summary.complete if complete is None else complete,
    )


def _positive_results(results: Sequence[HitResult]) -> list[HitResult]:
    return [result for result in results if not result.negative]


def _recall_by_difficulty(results: Sequence[HitResult]) -> dict[str, float]:
    return {
        difficulty: _recall_at_k(
            [result for result in results if result.difficulty == difficulty], 10
        )
        for difficulty in ("easy", "medium")
    }


def _recall_at_k(results: Sequence[HitResult], k: int) -> float:
    if not results:
        return 0.0
    return sum(
        1 for result in results if result.found_rank is not None and result.found_rank <= k
    ) / len(results)


def _mrr(results: Sequence[HitResult]) -> float:
    if not results:
        return 0.0
    return sum(
        1.0 / result.found_rank for result in results if result.found_rank is not None
    ) / len(results)


def _hit_result(query: QuerySpec, actual_slugs: Sequence[str], latency_ms: float) -> HitResult:
    found_rank = next(
        (rank for rank, slug in enumerate(actual_slugs, start=1) if slug in query.expected_slugs),
        None,
    )
    return HitResult(
        query.query,
        query.difficulty,
        list(query.expected_slugs),
        list(actual_slugs),
        found_rank,
        latency_ms,
        query.negative,
    )


def _snippet(body: str, query: str) -> str:
    tokens = _QUERY_TOKENS.findall(query.lower())
    index = body.lower().find(tokens[0]) if tokens else -1
    if index < 0:
        return body[:256]
    return body[max(index - 96, 0) : min(index + 160, len(body))]


def _copy_snapshot(snapshot_dir: Path, data_dir: Path) -> None:
    _assert_fresh_dir(data_dir, "paired data directory")
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot_dir / "docs", data_dir / "docs")


def _total_access_count(data_dir: Path) -> int:
    conn = sqlite3.connect(data_dir / "mem.db")
    try:
        row = conn.execute("SELECT COALESCE(SUM(access_count), 0) FROM wiki_index").fetchone()
    finally:
        conn.close()
    return int(row[0])


def _metadata(
    config: EvaluationConfig,
    corpora: Mapping[int, CorpusResult],
    git_state: GitState,
    has_selected_candidate: bool,
) -> dict[str, JsonValue]:
    sizes = list(corpora)
    quality_size = min(sizes)
    return {
        "source_revision": git_state.source_revision,
        "git_dirty": git_state.git_dirty,
        "promotion_mode": config.promotion_mode,
        "promotion_eligible": config.promotion_mode
        and git_state.promotion_eligible
        and has_selected_candidate,
        "python_version": platform.python_version(),
        "memex_version": MEMEX_VERSION,
        "os_family": platform.system(),
        "cpu_architecture": platform.machine(),
        "corpus_generator": "realistic",
        "seed": config.seed,
        "requested_corpus_size": quality_size,
        "requested_corpus_sizes": [int(size) for size in sizes],
        "generated_corpus_size": corpora[quality_size].memories_written,
        "generated_corpus_sizes": {str(size): corpora[size].memories_written for size in sizes},
        "query_count": len(corpora[quality_size].queries),
        "query_sampling": {
            "strategy": "stable corpus-family-difficulty cap",
            "max_per_group": SELECTION_MAX_QUERIES_PER_GROUP,
            "bound_threshold": SELECTION_QUERY_BOUND_THRESHOLD,
        },
        "top_k": config.top_k,
        "token_budget": TOKEN_BUDGET,
        "renderer_identity": "memex.application.context_injection.format_context_block",
        "token_estimator_identity": "memex.application.context_injection.estimate_tokens",
        "percentile_method": "nearest-rank",
    }


def _comparison_metadata(
    name: CandidateName,
    config: EvaluationConfig,
    corpora: Mapping[int, CorpusResult],
    git_state: GitState,
    baseline_ranker: Mapping[str, JsonValue],
    candidate_ranker: Mapping[str, JsonValue],
) -> ComparisonMetadata:
    sizes = list(corpora)
    return ComparisonMetadata(
        source_revision=git_state.source_revision,
        git_dirty=git_state.git_dirty,
        python_version=platform.python_version(),
        memex_version=MEMEX_VERSION,
        os_family=platform.system(),
        cpu_architecture=platform.machine(),
        corpus_generator="realistic",
        seed=config.seed,
        requested_corpus_sizes=sizes,
        generated_corpus_sizes={size: corpora[size].memories_written for size in sizes},
        query_count=len(corpora[min(sizes)].queries),
        top_k=config.top_k,
        token_budget=TOKEN_BUDGET,
        renderer_identity="memex.application.context_injection.format_context_block",
        token_estimator_identity="memex.application.context_injection.estimate_tokens",  # noqa: S106
        percentile_method="nearest-rank",
        baseline_ranker=baseline_ranker,
        candidate_ranker={"candidate": name, **candidate_ranker},
    )


def _query_manifest(queries: Sequence[QuerySpec]) -> tuple[dict[str, JsonValue], ...]:
    return tuple(
        {
            "id": index,
            "corpus": query.corpus,
            "family": query.family,
            "difficulty": query.difficulty,
            "negative": query.negative,
            "expected_slugs": list(query.expected_slugs),
        }
        for index, query in enumerate(queries)
    )


def _select_candidate(candidates: Mapping[str, CandidateSelection]) -> str | None:
    eligible = [name for name, item in candidates.items() if item.promotion_eligible]
    return eligible[0] if len(eligible) == 1 else None


def _write_report(report: SelectionReport, evidence_dir: Path | None) -> SelectionReport:
    if evidence_dir is None:
        return report
    evidence_dir.mkdir(parents=True, exist_ok=True)
    suffix = (
        "promotion"
        if report.metadata["promotion_mode"]
        else str(report.metadata["requested_corpus_size"])
    )
    path = evidence_dir / f"selection-{suffix}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SelectionReport(
        report.metadata,
        report.query_manifest,
        report.candidates,
        report.selected_candidate,
        report.failures,
        report.volatile,
        path,
    )


def _git_state() -> GitState:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return GitState("unknown", None)
    return GitState(revision or "unknown", bool(status))


def _assert_fresh_dir(path: Path, label: str) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"{label} must be empty")


def _failure(category: FailureCategory, reason: str) -> SelectionFailure:
    if category not in CLOSED_FAILURE_CATEGORIES:
        category = "measurement_failed"
    return SelectionFailure(
        category, " ".join(reason.split())[:_MAX_FAILURE_REASON] or "evaluation failed"
    )


def _failure_category(value: object) -> FailureCategory | None:
    if value is None:
        return None
    if isinstance(value, str) and value in CLOSED_FAILURE_CATEGORIES:
        return value
    return "incomplete_search"


def _dedupe_failures(failures: Sequence[SelectionFailure]) -> list[SelectionFailure]:
    return list(dict.fromkeys(failures))


def _candidate_payloads(payload: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, dict):
        return []
    return [candidate for candidate in candidates.values() if isinstance(candidate, dict)]


def _drop_timing(candidate: dict[str, JsonValue]) -> None:
    for side in ("baseline", "candidate"):
        summary = candidate.get(side)
        if isinstance(summary, dict):
            summary.pop("p99_ms", None)
    scales = candidate.get("scales")
    if isinstance(scales, dict):
        for status in scales.values():
            if not isinstance(status, dict):
                continue
            for side in ("baseline", "candidate"):
                run = status.get(side)
                if isinstance(run, dict):
                    run.pop("p99_ms", None)
    comparison = candidate.get("comparison")
    if not isinstance(comparison, dict):
        return
    for side in ("baseline", "candidate"):
        metrics = comparison.get(side)
        if isinstance(metrics, dict):
            metrics.pop("p99_ms", None)
    verdict = comparison.get("verdict")
    if isinstance(verdict, dict):
        gates = verdict.get("gates")
        if isinstance(gates, dict):
            gates.pop("absolute_p99", None)
            gates.pop("paired_p99", None)


def _json_mapping(values: Mapping[str, object]) -> dict[str, JsonValue]:
    return {key: _json_value(value) for key, value in values.items()}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    return str(value)


def main(argv: Sequence[str] | None = None) -> int:
    from eval.run import main as run_main

    return run_main(list(argv) if argv is not None else sys.argv[1:])
