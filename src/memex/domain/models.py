"""Typed models for every public input and output contract (spec §4, §5).

Timestamps are ISO8601 UTC strings ending in ``Z``. Constructors validate
untrusted input eagerly so invalid states stay unrepresentable past the
boundary.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from memex.domain.types import NODE_TYPES as NODE_TYPES
from memex.domain.types import validate_type_name as validate_type_name

PAGE_STATUSES: tuple[str, ...] = ("active", "pending", "superseded", "archived")

# OKF v0.2 relation vocabulary. ``DEFAULT_REL`` is what an untyped link means,
# ``BODY_REL`` is what a ``[[slug]]`` body reference means, and the four
# relation fields project into the graph under their own field name.
DEFAULT_REL = "relates-to"
BODY_REL = "mentions"
RELATION_FIELDS: tuple[str, ...] = ("parent", "supersedes", "implements", "depends_on")
NON_EPISODE_TYPES: tuple[str, ...] = tuple(t for t in NODE_TYPES if t != "episode")
TURN_ROLES: tuple[str, ...] = ("user", "agent", "tool")
FORGET_MODES: tuple[str, ...] = ("hard", "soft", "decay")

# Stored description budget: one line, at most 512 UTF-8 bytes (spec AC-0002).
# Enforced on the stored (post-scrub) value, so redaction growth cannot land
# an over-budget field on disk with a misleading boundary error.
DESCRIPTION_MAX_BYTES = 512

# Wire-level enums; pinned to the runtime tuples by test so they cannot drift.
TurnRole = Literal["user", "agent", "tool"]
ForgetMode = Literal["hard", "soft", "decay"]
ConsolidateMode = Literal["full", "dry-run"]

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PROJECT_ID = re.compile(r"^[a-f0-9]{24,64}$")


def utc_now_iso() -> str:
    """Current UTC time as an ISO8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """Generate a UUID4 node identifier."""
    return str(uuid.uuid4())


def _check_iso(value: str, field_name: str) -> None:
    if not _ISO.match(value):
        raise ValueError(f"{field_name} must be an ISO8601 UTC string like 2026-09-15T10:00:00Z")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid date: {value}") from exc


def _check_description(description: str) -> None:
    """One line, no control characters, at most 512 UTF-8 bytes (spec AC-0002)."""
    if len(description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        raise ValueError("description must stay within 512 UTF-8 bytes")
    if any(unicodedata.category(character) == "Cc" for character in description):
        raise ValueError("description must be a single line without control characters")


def _check_project_metadata(scope: str, project_id: str | None, project_label: str | None) -> None:
    """Reject identity inputs that could expose a repository remote or path."""
    if scope == "project" and (project_id is None or not _PROJECT_ID.fullmatch(project_id)):
        raise ValueError("project_id must be an opaque lowercase hexadecimal identifier")
    if project_label is not None and (
        not project_label.strip()
        or any(token in project_label for token in ("/", "\\", "://", "@"))
    ):
        raise ValueError("project_label must be a plain display name")


def _norm_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lower().replace(" ", "-")
        if not cleaned:
            raise ValueError("tags must be non-empty")
        normalized.append(cleaned)
    if len(set(normalized)) != len(normalized):
        raise ValueError("tags must be unique")
    return normalized


def _norm_slugs(slugs: list[str], field_name: str) -> list[str]:
    normalized = [slug.strip().lower() for slug in slugs]
    if any(not slug for slug in normalized):
        raise ValueError(f"{field_name} must contain non-empty slugs")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must be unique")
    return normalized


@dataclass(slots=True)
class SemanticLink:
    """One OKF v0.2 typed link: a target concept and the relation to it.

    Stored front matter carries these as inline mappings
    (``{target: "slug", rel: "relates-to"}``). ``label`` is optional display
    text and is never used for routing or lookup.
    """

    target: str
    rel: str = DEFAULT_REL
    label: str | None = None

    def __post_init__(self) -> None:
        self.target = self.target.strip().lower()
        if not self.target:
            raise ValueError("link target must be non-empty")
        self.rel = self.rel.strip().lower().replace(" ", "-")
        if not self.rel:
            raise ValueError("link rel must be non-empty")
        if self.label is not None:
            if not self.label.strip():
                raise ValueError("link label must be non-empty when provided")
            if any(unicodedata.category(character) == "Cc" for character in self.label):
                raise ValueError("link label must be a single line")

    @classmethod
    def parse(cls, value: object) -> SemanticLink:
        """Build a link from a mapping, a ``target:rel`` string, or a bare slug."""
        if isinstance(value, SemanticLink):
            return cls(target=value.target, rel=value.rel, label=value.label)
        if isinstance(value, str):
            target, separator, rel = value.partition(":")
            return cls(target=target, rel=rel if separator else DEFAULT_REL)
        if isinstance(value, Mapping):
            unknown = set(value) - {"target", "rel", "label"}
            if unknown:
                raise ValueError(f"link has unsupported fields: {sorted(unknown)}")
            if "target" not in value:
                raise ValueError("link is missing required field 'target'")
            members = {key: value.get(key) for key in ("target", "rel", "label")}
            for name, member in members.items():
                if member is not None and not isinstance(member, str):
                    raise ValueError(f"link field {name!r} must be a string")
            target_value = members["target"]
            rel_value = members["rel"]
            label_value = members["label"]
            if not isinstance(target_value, str):
                raise ValueError("link field 'target' must be a string")
            return cls(
                target=target_value,
                rel=rel_value if isinstance(rel_value, str) else DEFAULT_REL,
                label=label_value if isinstance(label_value, str) else None,
            )
        raise ValueError("link must be a string or a mapping with 'target' and 'rel'")

    def to_mapping(self) -> dict[str, str]:
        """Front-matter form: ``target`` and ``rel``, plus ``label`` when set."""
        mapping = {"target": self.target, "rel": self.rel}
        if self.label is not None:
            mapping["label"] = self.label
        return mapping


def _norm_links(links: list[object]) -> list[SemanticLink]:
    """Normalize every accepted link form, deduplicating on (target, rel)."""
    normalized: list[SemanticLink] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        parsed = SemanticLink.parse(link)
        identity = (parsed.target, parsed.rel)
        if identity in seen:
            raise ValueError("links must be unique on (target, rel)")
        seen.add(identity)
        normalized.append(parsed)
    return normalized


@dataclass(slots=True)
class TurnStreamEntry:
    """One conversation turn in a transcript (spec §4.1).

    An empty ``ts`` means the timestamp is unknown; it is omitted from the
    stored JSONL rather than fabricated. ``token_usage`` carries the
    harness-reported usage for the completed turn that produced this
    entry, when available (never summed or estimated).
    """

    role: str
    content: str
    turn: int
    ts: str = ""
    tool_name: str | None = None
    result: str | None = None
    query: str | None = None
    token_usage: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.role not in TURN_ROLES:
            raise ValueError(f"role must be one of {TURN_ROLES}, got {self.role!r}")
        if self.turn < 1:
            raise ValueError("turn must be a 1-based integer")
        if self.ts:
            _check_iso(self.ts, "ts")
        if self.role != "tool" and (self.tool_name is not None or self.result is not None):
            raise ValueError("tool_name/result are only allowed when role='tool'")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TurnStreamEntry:
        """Build a turn from untrusted JSON, validating required fields.

        A ``memex_session_header`` line (or any line without a role) is a
        ValueError; readers filter headers before calling this.
        """
        try:
            role = data["role"]
            content = data["content"]
            turn = data["turn"]
        except KeyError as exc:
            raise ValueError(f"turn entry missing required field: {exc.args[0]}") from exc
        if not isinstance(role, str) or not isinstance(content, str) or not isinstance(turn, int):
            raise ValueError("turn entry fields role/content must be str and turn must be int")
        optional: dict[str, str] = {}
        for key in ("ts", "tool_name", "result", "query"):
            value = data.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"turn field {key} must be a string")
                optional[key] = value
        usage = data.get("token_usage")
        token_usage = (
            {str(k): int(v) for k, v in usage.items()} if isinstance(usage, dict) else None
        )
        return cls(role=role, content=content, turn=turn, token_usage=token_usage, **optional)


@dataclass(slots=True)
class SessionHeader:
    """First line of a transcript JSONL: session identity and totals.

    Universal fields are typed; harness-specific detail (git, models,
    reasoning efforts, provider metadata) lives in ``meta`` verbatim.
    """

    type: str = "memex_session_header"
    harness: str = ""
    session_id: str = ""
    captured_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class WriteInput:
    """Input contract for the write operation (spec §4.2)."""

    type: str
    title: str
    body: str
    description: str = ""
    resource: str | None = None
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    links: list[object] = field(default_factory=list)
    parent: str | None = None
    supersedes: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    session_id: str | None = None
    transcript_ref: str | None = None
    stale_after: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    # v1-guardrails fields: lifecycle + provenance (all optional, default active)
    status: str = "active"
    occurred_at: str | None = None
    source: str | None = None
    harness: str | None = None
    confidence: str | None = None
    scope: str = "global"
    project_id: str | None = None
    project_label: str | None = None
    project_locator: str | None = None
    #: Consolidation-only: a candidate name for a concept type no listed type
    #: fits. Never validated here — the consolidator validates and clears it
    #: before any node reaches ``Memex.write``.
    proposed_type: str | None = None

    def __post_init__(self) -> None:
        validate_type_name(self.type)
        if not self.title.strip():
            raise ValueError("title must be non-empty")
        _check_description(self.description)
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError("importance must be within [0.0, 1.0]")
        self.tags = _norm_tags(self.tags)
        self.links = list(_norm_links(self.links))
        self.parent = self.parent.strip().lower() if self.parent else None
        self.supersedes = _norm_slugs(self.supersedes, "supersedes")
        self.implements = _norm_slugs(self.implements, "implements")
        self.depends_on = _norm_slugs(self.depends_on, "depends_on")
        if self.status not in PAGE_STATUSES:
            raise ValueError(f"status must be one of {PAGE_STATUSES}, got {self.status!r}")
        if self.occurred_at is not None:
            _check_iso(self.occurred_at, "occurred_at")
        if self.type == "episode" and not (self.session_id or "").strip():
            raise ValueError("session_id is required for episode nodes")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("session_id must be non-empty when provided")
        for name in ("stale_after", "valid_from", "valid_until"):
            value = getattr(self, name)
            if value is not None:
                _check_iso(value, name)
        if self.transcript_ref is not None and not self.transcript_ref.strip():
            raise ValueError("transcript_ref must be non-empty when provided")
        if self.scope not in {"global", "project"}:
            raise ValueError("scope must be 'global' or 'project'")
        if self.scope == "project" and not (self.project_id or "").strip():
            raise ValueError("project_id is required for project scope")
        if self.scope == "global" and self.project_id is not None:
            raise ValueError("project_id is only allowed for project scope")
        _check_project_metadata(self.scope, self.project_id, self.project_label)


@dataclass(slots=True)
class WikiNode:
    """Full representation of a wiki page, front matter plus body.

    The field set is OKF v0.2 first — ``type`` through ``links`` carry the
    names and meanings of the Open Knowledge Format — followed by the Memex
    extension fields, which OKF preserves verbatim as unknown keys.
    """

    type: str
    title: str
    body: str
    id: str
    slug: str = ""
    file_path: str | None = None
    description: str = ""
    resource: str | None = None
    tags: list[str] = field(default_factory=list)
    #: OKF ``timestamp``: refreshed on every write, like the OKF reference
    #: writer. Creation time lives in the Memex ``created`` field, which OKF
    #: has no equivalent for.
    timestamp: str = field(default_factory=utc_now_iso)
    valid_from: str | None = None
    valid_until: str | None = None
    stale_after: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)
    parent: str | None = None
    supersedes: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    links: list[SemanticLink] = field(default_factory=list)
    created: str = field(default_factory=utc_now_iso)
    importance: float = 0.5
    access_count: int = 0
    last_access: str | None = None
    transcript_ref: str | None = None
    session_id: str | None = None
    content_hash: str = ""
    status: str = "active"
    occurred_at: str | None = None
    source: str | None = None
    harness: str | None = None
    confidence: str | None = None
    scope: str = "global"
    project_id: str | None = None
    project_label: str | None = None
    project_locator: str | None = None

    def __post_init__(self) -> None:
        validate_type_name(self.type)
        if not self.title.strip():
            raise ValueError("title must be non-empty")
        _check_description(self.description)
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError("importance must be within [0.0, 1.0]")
        self.links = _norm_links(list(self.links))
        self.parent = self.parent.strip().lower() if self.parent else None
        self.supersedes = _norm_slugs(self.supersedes, "supersedes")
        self.implements = _norm_slugs(self.implements, "implements")
        self.depends_on = _norm_slugs(self.depends_on, "depends_on")
        if self.status not in PAGE_STATUSES:
            raise ValueError(f"status must be one of {PAGE_STATUSES}, got {self.status!r}")
        if self.occurred_at is not None:
            _check_iso(self.occurred_at, "occurred_at")
        if self.scope not in {"global", "project"}:
            raise ValueError("scope must be 'global' or 'project'")
        if self.scope == "project" and not (self.project_id or "").strip():
            raise ValueError("project_id is required for project scope")
        if self.scope == "global" and self.project_id is not None:
            raise ValueError("project_id is only allowed for project scope")
        _check_project_metadata(self.scope, self.project_id, self.project_label)

    def link_targets(self) -> list[str]:
        """Front-matter link targets, in declaration order."""
        return [link.target for link in self.links]

    def relations(self) -> list[tuple[str, str]]:
        """Every front-matter relation as ``(target, rel)``, links first.

        Covers typed links plus the four OKF relation fields; body ``[[slug]]``
        references are added by the link manager, which owns the body.
        """
        pairs = [(link.target, link.rel) for link in self.links]
        if self.parent:
            pairs.append((self.parent, "parent"))
        for name in ("supersedes", "implements", "depends_on"):
            pairs.extend((target, name) for target in getattr(self, name))
        return pairs


@dataclass(slots=True)
class RecallHit:
    """One ranked search result (spec §5.1)."""

    slug: str
    file_path: str
    title: str
    node_type: str
    importance: float
    score: float
    rank: int
    snippet: str
    snippet_source: str
    tags: list[str]
    created: str
    timestamp: str
    updated_at: str
    last_access: str | None
    transcript_ref: str | None
    links: list[str]
    description: str = ""
    status: str = "active"
    scope: str = "global"
    project_id: str | None = None
    project_label: str | None = None


@dataclass(slots=True)
class RecallResult:
    """Recall output: query, ranked hits, and search metadata."""

    query: str
    hits: list[RecallHit]
    total_indexed: int
    search_engine: str
    search_time_ms: float


@dataclass(slots=True)
class TaskRecallInput:
    """A bounded set of project questions for one task context."""

    goal: str
    questions: list[str]
    project_id: str
    max_hits: int = 36
    max_tokens: int = 4096
    node_type: str | None = None
    tags: list[str] | None = None

    def __post_init__(self) -> None:
        _check_project_metadata("project", self.project_id, None)
        if not self.goal.strip() or len(self.goal.encode("utf-8")) > 1024:
            raise ValueError("goal must contain text within 1,024 UTF-8 bytes")
        if (
            not isinstance(self.questions, list)
            or not 1 <= len(self.questions) <= 3
            or any(not isinstance(question, str) for question in self.questions)
        ):
            raise ValueError("questions must be a list of one to three strings")
        self.questions = self.questions.copy()
        if not 1 <= self.max_hits <= 36:
            raise ValueError("max_hits must be in [1, 36]")
        if not 1 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens must be in [1, 4096]")
        if self.node_type is not None:
            validate_type_name(self.node_type)


@dataclass(slots=True)
class TaskRecallResult:
    """Rendered evidence and explicit gaps for a task."""

    context: str
    sources: list[str]
    unanswered_questions: list[str]
    omitted_questions: list[str]
    rendered_tokens: int


@dataclass(slots=True)
class ForgetResult:
    """Output of the forget operation (spec §9.4)."""

    slug: str
    forgotten: bool
    mode: str
    file_path: str | None


@dataclass(slots=True)
class ConsolidateInput:
    """Trigger for LLM consolidation (spec §4.3)."""

    mode: str = "full"
    episode_ids: list[str] | None = None
    max_episodes: int = 10
    include_links: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("full", "dry-run"):
            raise ValueError("mode must be 'full' or 'dry-run'")
        if self.max_episodes < 1:
            raise ValueError("max_episodes must be >= 1")


@dataclass(slots=True)
class ConsolidationReport:
    """Output of consolidation (spec §5.2)."""

    mode: str
    episodes_processed: int
    nodes_created: list[WriteInput]
    nodes_updated: list[str]
    links_added: int
    llm_calls: int
    llm_prompt_tokens: int
    llm_completion_tokens: int
    dry_run: bool


@dataclass(slots=True)
class IngestTranscriptInput:
    """Transcript ingest contract (spec §4.4).

    ``header`` is optional: when present it is written as the first line
    of the transcript JSONL; older turn-only inputs keep working.
    """

    session_id: str
    turns: list[TurnStreamEntry]
    metadata: dict[str, str] = field(default_factory=dict)
    header: SessionHeader | None = None
    token_usage: dict[str, int] | None = None  # session totals -> meta.json

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.session_id):
            raise ValueError("session_id must be alphanumeric with '.', '_', '-' only")


@dataclass(slots=True)
class TranscriptLinkReport:
    """Transcript ingest output (spec §5.3)."""

    session_id: str
    transcript_file: str
    meta_file: str
    episode_node: str
    episode_file_path: str
    turn_count: int
    user_turns: int
    agent_turns: int
    tool_turns: int


@dataclass(slots=True)
class ProvenanceReport:
    """Trace from a wiki page back to originating transcripts (spec §7 Utility 7)."""

    slug: str
    direct_transcript_ref: str | None
    linked_episodes: list[str]
    transcript_files: list[str]
    meta_files: list[str]
    confidence: str


@dataclass(slots=True)
class SessionSummary:
    """One stored session as listed by ``list_sessions`` (spec §12.5).

    ``token_usage`` carries the harness-reported session totals when the
    capture recorded them (never summed or estimated here).
    """

    session_id: str
    started_at: str | None
    ended_at: str | None
    turn_count: int
    episode_slug: str | None
    file_path: str
    token_usage: dict[str, int] | None = None


@dataclass(slots=True)
class RebuildIndexReport:
    """Output of the index rebuild operation (spec §9.6).

    ``errors`` carries malformed-page messages only; navigation outcomes
    (collision, write failure) are bounded entries in ``navigation_defects``
    so per-node counts stay honest.
    """

    nodes_indexed: int
    nodes_skipped: int
    nodes_errored: int
    duration_ms: float
    errors: list[str]
    navigation_defects: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BackupReport:
    """Output of the backup operation (spec §9.7)."""

    archive_path: str
    size_bytes: int
    file_counts: dict[str, int]
    duration_ms: float


@dataclass(slots=True)
class RestoreReport:
    """Output of the restore operation (spec §9.8)."""

    restored: bool
    file_counts: dict[str, int]
    index_rebuilt: bool
    warnings: list[str]
    previous_backup_dir: str | None
