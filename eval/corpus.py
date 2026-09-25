"""Synthetic corpus generator for memory-layer evaluation.

Produces deterministic, realistic coding-agent memories with ground-truth
query mappings. Realistic corpus shape (tool preferences, project facts,
procedures, people, decisions) — not lorem ipsum.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from memex.application.memory import Memex
from memex.domain.models import WikiNode

# ---------------------------------------------------------------------------
# Content templates — each produces a realistic coding-agent memory
# ---------------------------------------------------------------------------

TOOLS = [
    ("ruff", "linter", "Python"),
    ("black", "formatter", "Python"),
    ("mypy", "type checker", "Python"),
    ("pytest", "test framework", "Python"),
    ("uv", "package manager", "Python"),
    ("pip", "package manager", "Python"),
    ("docker", "container runtime", "deployment"),
    ("kubernetes", "orchestrator", "deployment"),
    ("terraform", "infrastructure", "cloud"),
    ("ansible", "configuration", "infrastructure"),
    ("git", "version control", "VCS"),
    ("github", "code hosting", "VCS"),
    ("postgres", "database", "storage"),
    ("redis", "cache", "storage"),
    ("kafka", "message queue", "streaming"),
    ("graphql", "API style", "API"),
    ("react", "frontend framework", "web"),
    ("vue", "frontend framework", "web"),
    ("fastapi", "web framework", "Python"),
    ("django", "web framework", "Python"),
    ("celery", "task queue", "Python"),
    ("sqlalchemy", "ORM", "Python"),
    ("prometheus", "monitoring", "observability"),
    ("grafana", "dashboards", "observability"),
    ("nginx", "reverse proxy", "web"),
    ("caddy", "web server", "web"),
    ("openai", "LLM provider", "AI"),
    ("anthropic", "LLM provider", "AI"),
]

PEOPLE = [
    "alice",
    "bob",
    "carol",
    "dave",
    "eve",
    "frank",
    "grace",
    "henry",
    "iris",
    "jack",
    "karen",
    "liam",
    "mia",
    "noah",
    "olivia",
    "peter",
]

PROJECTS = [
    "auth-service",
    "user-api",
    "payment-gateway",
    "notification-service",
    "search-indexer",
    "data-pipeline",
    "ml-inference",
    "web-frontend",
    "mobile-app",
    "admin-dashboard",
    "billing-engine",
    "analytics-worker",
]

DECISIONS = [
    "use {choice} over {alternative}",
    "migrate from {alternative} to {choice}",
    "standardize on {choice}",
    "deprecate {alternative} in favour of {choice}",
    "adopt {choice} for {purpose}",
    "split {alternative} into {choice} modules",
]

LANGUAGES = ["Python", "TypeScript", "Go", "Rust", "Java", "Kotlin", "Swift"]

ERRORS = [
    "authentication timeout",
    "connection pool exhaustion",
    "memory leak",
    "race condition in cache invalidation",
    "certificate rotation failure",
    "database migration deadlock",
    "API rate limit exceeded",
    "circuit breaker false positive",
    "kafka consumer lag",
    "kubernetes pod eviction",
    "git merge conflict",
    "CI flaky test",
    "docker image bloat",
    "nginx misconfigured upstream",
]


@dataclass(slots=True)
class QuerySpec:
    """One ground-truth query with its expected results."""

    query: str
    expected_slugs: list[str]  # slugs that SHOULD appear in top-K
    difficulty: str  # easy | medium | hard | discriminator
    family: str = ""
    corpus: str = "synthetic"
    negative: bool = False

    @property
    def relevant_slugs(self) -> list[str]:
        """Return the complete positive relevance set for this query."""
        return list(self.expected_slugs)


@dataclass(slots=True)
class CorpusResult:
    """What the generator produced."""

    memories_written: int
    queries: list[QuerySpec] = field(default_factory=list)
    elapsed_ms: float = 0.0
    domain_counts: dict[str, int] = field(default_factory=dict)


class CorpusGenerator:
    """Builds a deterministic synthetic corpus with ground truth."""

    def __init__(self, memex: Memex, seed: int = 42) -> None:
        self._memex = memex
        self._rng = random.Random(seed)  # noqa: S311 - deterministic, not crypto
        self._queries: list[QuerySpec] = []
        self._slugs: list[str] = []
        self._used_slugs: set[str] = set()

    def generate(
        self,
        size: int,
        *,
        overlap_pct: float = 0.15,
        stale_pct: float = 0.30,
    ) -> CorpusResult:
        """Generate `size` memories with the given characteristics."""
        import time

        started = time.perf_counter()
        self._queries.clear()
        self._slugs.clear()

        # Plan the composition
        n_entities = size // 5 * 2  # 40%
        n_preferences = size // 5  # 20%
        n_procedures = size // 5  # 20%
        n_summaries = size - n_entities - n_preferences - n_procedures

        for i in range(n_entities):
            self._gen_entity(i)
        for i in range(n_preferences):
            self._gen_preference(i)
        for i in range(n_procedures):
            self._gen_procedure(i)
        for i in range(n_summaries):
            self._gen_summary(i)

        # Add overlap memories (near-duplicates that test discrimination)
        n_overlap = int(size * overlap_pct)
        for i in range(n_overlap):
            self._gen_distractor(i)

        # Bulk index: one scan + batch insert (not per-node roundtrips)
        self._memex.rebuild_index(force=True)

        elapsed = (time.perf_counter() - started) * 1000
        return CorpusResult(
            memories_written=len(self._slugs),
            queries=list(self._queries),
            elapsed_ms=round(elapsed, 1),
        )

    # ------------------------------------------------------------- generators

    def _gen_entity(self, i: int) -> None:
        tool, category, lang = self._rng.choice(TOOLS)
        purpose = f"used in {self._rng.choice(PROJECTS)}"
        body = (
            f"{tool.title()} is the {category} for this project.\n\n"
            f"It handles {purpose}. Configuration lives in "
            f"`{tool}.config.yaml`. Related: [[{lang.lower()}-stack]]."
        )
        title = f"{tool.title()} {category}"
        tags = [lang.lower(), category.replace(" ", "-"), "tool"]
        node = self._write("entity", title, body, tags)
        self._add_query(f"what {category} do we use", [node.slug], "medium")
        self._add_query(tool, [node.slug], "easy")

    def _gen_preference(self, i: int) -> None:
        tool_a, cat_a, _ = self._rng.choice(TOOLS)
        tool_b, _, _ = self._rng.choice(
            [t for t in TOOLS if t[0] != tool_a and t[1] == cat_a]
            or [t for t in TOOLS if t[0] != tool_a]
        )
        task = self._rng.choice(["linting", "formatting", "testing", "deployment", "CI/CD"])
        body = (
            f"The user prefers {tool_a} over {tool_b} for {task}.\n\n"
            f"This was decided because {tool_a} is faster and has better "
            f"integration with the existing workflow. Always default to "
            f"{tool_a} when starting new work. See [[{tool_a}]] for details."
        )
        title = f"User prefers {tool_a} over {tool_b}"
        node = self._write("preference", title, body, ["preference", task])
        self._add_query(f"{task} preference", [node.slug], "medium")
        self._add_query(f"prefer {tool_a} or {tool_b}", [node.slug], "discriminator")

    def _gen_procedure(self, i: int) -> None:
        action = self._rng.choice(
            [
                "deploy to staging",
                "deploy to production",
                "run integration tests",
                "rotate API keys",
                "reset the database",
                "update dependencies",
                "hotfix a bug",
                "rollback a release",
                "set up local development",
            ]
        )
        n_steps = self._rng.randint(3, 5)
        actions = ["Run", "Check", "Wait for", "Verify", "Execute"]
        objects = ["the build", "CI pipeline", "tests", "the migration", "health check"]
        steps = [
            f"{j}. {self._rng.choice(actions)} {self._rng.choice(objects)}"
            for j in range(1, n_steps + 1)
        ]
        body = f"Procedure to {action}:\n\n" + "\n".join(steps)
        title = f"How to {action}"
        node = self._write("procedure", title, body, ["procedure", action.split()[0]])
        self._add_query(f"how to {action}", [node.slug], "easy")

    def _gen_summary(self, i: int) -> None:
        project = self._rng.choice(PROJECTS)
        decision = self._rng.choice(DECISIONS)
        tool_a, _, _ = self._rng.choice(TOOLS)
        tool_b, _, _ = self._rng.choice([t for t in TOOLS if t[0] != tool_a])
        reason = self._rng.choice(
            [
                "it's more mature",
                "team familiarity",
                "better performance",
                "lower cost",
                "better ecosystem",
                "easier to debug",
            ]
        )
        decision_text = decision.format(choice=tool_a, alternative=tool_b, purpose=project)
        body = (
            f"Architecture decision: {decision_text}.\n\n"
            f"Reasoning: {reason}. This was decided in the {project} "
            f"review. See [[{tool_a}]] and [[{tool_b}]] for context."
        )
        title = f"Decision: {decision_text}"
        node = self._write("summary", title, body, ["decision", project])
        self._add_query(f"{project} architecture", [node.slug], "medium")

    def _gen_distractor(self, i: int) -> None:
        """Near-duplicate of an existing memory with one detail changed."""
        if not self._slugs:
            return
        target_slug = self._rng.choice(self._slugs)
        target = self._memex.wiki_store.read(target_slug)
        if target is None:
            return
        # Change one word
        alt_tool = self._rng.choice(TOOLS)[0]
        modified_body = (
            target.body.replace("ruff", alt_tool) if "ruff" in target.body else target.body
        )
        if modified_body == target.body:
            modified_body = target.body + f"\n\nNote: also supports {alt_tool}."
        title = target.title + f" (variant {i})"
        self._write(target.type, title, modified_body, [*target.tags, "variant"])
        # The discriminator query should find the ORIGINAL, not the variant
        self._add_query(
            target.title.split("(")[0].strip(),
            [target_slug],
            "discriminator",
        )

    # ------------------------------------------------------------- helpers

    def _write(self, node_type: str, title: str, body: str, tags: list[str]) -> WikiNode:
        """Direct file write — bypasses WikiStore to avoid O(n²) slug scanning."""
        from memex.domain.frontmatter import serialize_front_matter
        from memex.domain.models import utc_now_iso
        from memex.domain.types import TYPE_DIRS
        from memex.infrastructure.store.wiki_store import hash_body, node_front_matter

        slug = _slugify(title)
        base = slug
        suffix = 1
        while slug in self._used_slugs:
            suffix += 1
            slug = f"{base}-{suffix}"
        self._used_slugs.add(slug)

        now = utc_now_iso()
        node = WikiNode(
            type=node_type,
            title=title,
            body=body,
            id=str(__import__("uuid").uuid4()),
            slug=slug,
            tags=tags,
            created=now,
            timestamp=now,
            updated_at=now,
            content_hash=hash_body(body),
        )
        path = self._memex.data_dir / "docs" / TYPE_DIRS[node_type] / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize_front_matter(node_front_matter(node), body), encoding="utf-8")
        self._slugs.append(slug)
        return node

    def _add_query(self, query: str, expected: list[str], difficulty: str) -> None:
        self._queries.append(QuerySpec(query=query, expected_slugs=expected, difficulty=difficulty))


def _slugify(text: str) -> str:
    import re as _re

    slug = text.lower().replace(" ", "-")
    slug = _re.sub(r"[^a-z0-9-]", "", slug)[:64]
    return slug or "node"
