"""Realistic synthetic corpus generator for memory-layer evaluation.

Produces diverse, real-world-shaped memories from 8 knowledge domains
that mirror what coding agents actually store.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from pathlib import Path

from eval.corpus import CorpusResult, QuerySpec
from memex.domain.frontmatter import serialize_front_matter
from memex.domain.models import WikiNode, utc_now_iso
from memex.domain.slugs import slugify, unique_slug
from memex.domain.types import TYPE_DIRS
from memex.infrastructure.store.wiki_store import hash_body, node_front_matter

# ---------------------------------------------------------------------------
# Domain vocabularies
# ---------------------------------------------------------------------------

SERVICES = [
    "ingest-api",
    "query-engine",
    "auth-gateway",
    "stream-processor",
    "index-builder",
    "notification-relay",
    "scheduler",
    "config-service",
    "payment-gateway",
    "user-profile",
    "search-indexer",
    "rate-limiter",
    "event-bus",
    "audit-log",
    "file-processor",
    "email-dispatcher",
]

PATTERNS = [
    "event sourcing",
    "CQRS",
    "circuit breaker",
    "saga orchestration",
    "retry queue",
    "bulkhead isolation",
    "sidecar proxy",
    "service mesh",
    "API gateway",
    "pub/sub",
    "streaming-first",
    "bulk processing",
]

CONCERNS = [
    "fault tolerance",
    "data consistency",
    "backpressure management",
    "service discovery",
    "rate limiting",
    "graceful degradation",
    "load balancing",
    "circuit isolation",
    "eventual consistency",
]

ALTERNATIVES = [
    "CRUD with transactions",
    "direct synchronous calls",
    "shared database",
    "polling",
    "two-phase commit",
    "blocking I/O",
    "monolithic deployment",
]

REASONS = [
    "it handles partial failures without blocking callers",
    "the team had prior operational experience with it",
    "it reduces blast radius during failures",
    "the performance profile fits our latency budget",
    "it enables independent scaling of read and write paths",
    "it eliminates the distributed transaction problem",
    "it simplified our deployment topology",
    "the operational tooling is more mature",
]

SYMPTOMS = [
    "intermittent 500 errors",
    "memory leak after 48 hours",
    "race condition during deploy",
    "connection pool exhaustion",
    "duplicate event delivery",
    "clock skew breaking TTL checks",
    "slow query under load",
    "circuit breaker false positives",
    "kafka consumer lag",
    "kubernetes pod eviction",
    "git merge conflicts in generated files",
    "CI flaky test failures",
    "docker image bloat",
    "certificate rotation failures",
    "database migration deadlock",
    "API rate limit exceeded",
]

ROOT_CAUSES = [
    "the poll interval was configured in seconds instead of milliseconds",
    "the goroutine wasn't releasing the database connection on error paths",
    "the retry logic didn't account for clock drift between nodes",
    "the index wasn't rebuilt after a schema migration",
    "the health check timeout was shorter than the startup probe interval",
    "the garbage collector was running too aggressively under memory pressure",
    "the connection pool was shared across goroutines without a semaphore",
    "the event consumer was reading from the wrong partition offset",
    "the load balancer was caching DNS entries past their TTL",
    "the API gateway was forwarding stale auth tokens",
]

FIXES = [
    "wrap the database call in a defer/finally to guarantee release",
    "use exponential backoff with jitter instead of fixed intervals",
    "add a startup readiness probe that waits for the index to load",
    "switch to idempotency keys so retries are safe",
    "implement a proper connection pool with max-lifetime eviction",
    "partition the consumer group by entity ID for ordering",
    "add a circuit breaker around the downstream service call",
    "migrate to a monotonic clock source for TTL calculations",
    "rebuild the index after every schema migration as a post-deploy step",
    "instrument the hot path with distributed tracing",
]

PRINCIPLES = [
    "resource cleanup must happen on all code paths, not just the happy path",
    "distributed systems need retries to be idempotent",
    "health checks must distinguish startup from readiness",
    "connection pools need lifecycle management, not just size limits",
    "time-based logic must use a consistent clock source",
    "schema changes require index rebuilds, not just data migrations",
    "partial failures should trigger fallback, not cascade",
    "monitoring should alert on symptoms the user would notice",
]

CONTEXTS = [
    "a production incident at 3am",
    "a routine code review",
    "a load test that exceeded the SLO",
    "a post-deploy smoke test",
    "a cross-team debugging session",
    "a capacity planning review",
    "an on-call shift handover",
    "a performance profiling session",
]

ENDPOINTS = [
    ("POST", "/v1/users", "creates a user account"),
    ("GET", "/v1/sessions/{id}", "retrieves session metadata"),
    ("PATCH", "/v1/deployments/{id}", "triggers a canary deploy"),
    ("DELETE", "/v1/keys/{key_id}", "revokes an API key"),
    ("GET", "/v1/search", "executes a full-text search"),
    ("POST", "/v1/events", "publishes an event to the bus"),
    ("GET", "/v1/metrics/summary", "returns aggregated metrics"),
    ("PUT", "/v1/config/{key}", "updates a runtime configuration"),
]

ERROR_CONDITIONS = [
    "the request body is malformed",
    "the auth token is expired",
    "the caller lacks required permissions",
    "the resource does not exist",
    "a unique constraint is violated",
    "the request fails schema validation",
    "the caller exceeded the rate limit",
    "an internal error occurred",
    "the upstream service is unavailable",
]

CONVENTIONS = [
    ("use dataclasses over dicts at boundaries", "passing raw dicts between layers"),
    ("prefer keyword-only for optional arguments", "positional booleans that read like flags"),
    ("validate at the boundary, trust internally", "re-validating on every function call"),
    ("prefer composition over inheritance", "deep class hierarchies with fragile base classes"),
    ("never catch bare Exception", "swallowing errors without context"),
    ("always use type ignore with a reason comment", "silencing the type checker"),
    ("prefer standard library for ordinary I/O", "adding a dependency for one function"),
    ("separate business logic from transport", "mixing HTTP handling with domain rules"),
]

CODE_EXAMPLES = [
    "def process(user: User, *, dry_run: bool = False) -> Result:",
    'data = load_config(path=Path.cwd() / "config.yaml")',
    "with transaction.atomic():\n    order.process()\n    event.publish()",
    "def validate(payload: dict) -> Validated:\n    return Validated.from_raw(payload)",
]

CONCEPTS = [
    "idempotency",
    "eventual consistency",
    "CAP theorem trade-offs",
    "LSM-tree compaction",
    "WAL replay",
    "vector clocks",
    "consistent hashing",
    "backpressure",
    "circuit breaking",
    "distributed tracing",
    "exponential backoff",
    "dead letter queues",
    "optimistic locking",
    "pessimistic locking",
    "MVCC",
]

MISCONCEPTIONS = [
    (
        "eventual consistency means data is lost",
        "it means replicas converge, not that writes disappear",
    ),
    (
        "circuit breakers prevent failures",
        "they prevent cascading failures, not the initial failure",
    ),
    ("idempotency means caching", "it means the same operation produces the same result"),
    ("WAL replay is slow", "it's sequential I/O which is the fastest disk operation"),
    ("consistent hashing eliminates hot spots", "it reduces hot spots but doesn't eliminate them"),
]

CHANGES = [
    ("MongoDB", "PostgreSQL", "operational complexity of replica set management"),
    ("REST", "GraphQL", "over-fetching and multiple round trips"),
    ("Celery", "Kafka Streams", "task queue limitations under high throughput"),
    ("monolith", "microservices", "independent deployment and scaling needs"),
    ("Jenkins", "GitHub Actions", "CI maintenance burden"),
    ("JSON", "protobuf", "payload size and schema enforcement"),
    ("server-rendered", "SPA", "interactivity requirements"),
    ("Redis", "Memcached", "persistence and data structure support"),
]

PROJECT_NAMES = [
    "atlas",
    "vertex",
    "cobalt",
    "meridian",
    "quasar",
    "beacon",
    "harbor",
    "sentinel",
    "prism",
    "forge",
    "lattice",
    "apex",
]

SESSION_TASKS = [
    "debugged the auth timeout in production",
    "implemented the notification service",
    "migrated the CI pipeline from Jenkins to GitHub Actions",
    "set up distributed tracing across all services",
    "resolved the kafka consumer lag incident",
    "implemented feature flags for gradual rollout",
]


class RealisticCorpusGenerator:
    """Builds diverse real-world-shaped memories from 8 domains."""

    def __init__(self, data_dir: Path, seed: int = 42) -> None:
        self._data_dir = Path(data_dir)
        # Test-data generator, not a security context.
        self._rng = random.Random(seed)  # noqa: S311
        self._queries: list[QuerySpec] = []
        self._slugs: list[str] = []
        self._used_slugs: set[str] = set()
        self._domain_counts: dict[str, int] = {}
        # topic key -> all slugs sharing that topic; ground truth expects
        # any same-topic page since duplicates are equally relevant
        self._topic_slugs: dict[str, list[str]] = {}
        self._pending: list[tuple[str, str, str]] = []

        for type_dir in TYPE_DIRS.values():
            (self._data_dir / "docs" / type_dir).mkdir(parents=True, exist_ok=True)

    def generate(self, size: int) -> CorpusResult:
        """Generate `size` memories across all 8 domains."""
        import time

        started = time.perf_counter()
        self._queries.clear()
        self._pending.clear()
        self._slugs.clear()
        self._used_slugs.clear()
        self._domain_counts.clear()
        self._topic_slugs.clear()

        n_arch = int(size * 0.25)
        n_debug = int(size * 0.20)
        n_api = int(size * 0.15)
        n_conv = int(size * 0.12)
        n_infra = int(size * 0.10)
        n_domain = int(size * 0.08)
        n_temporal = int(size * 0.05)
        n_session = size - sum([n_arch, n_debug, n_api, n_conv, n_infra, n_domain, n_temporal])

        for _ in range(n_arch):
            self._gen_architecture()
        for _ in range(n_debug):
            self._gen_debugging()
        for _ in range(n_api):
            self._gen_api_contract()
        for _ in range(n_conv):
            self._gen_convention()
        for _ in range(n_infra):
            self._gen_infrastructure()
        for _ in range(n_domain):
            self._gen_domain_knowledge()
        for _ in range(n_temporal):
            self._gen_temporal()
        for _ in range(n_session):
            self._gen_session()

        elapsed = (time.perf_counter() - started) * 1000
        return CorpusResult(
            memories_written=len(self._slugs),
            queries=self._resolve_queries(),
            elapsed_ms=round(elapsed, 1),
            domain_counts=dict(self._domain_counts),
        )

    # ------------------------------------------------------------- domains

    def _gen_architecture(self) -> None:
        rng = self._rng
        service = rng.choice(SERVICES)
        pattern = rng.choice(PATTERNS)
        concern = rng.choice(CONCERNS)
        alternative = rng.choice(ALTERNATIVES)
        reason = rng.choice(REASONS)
        related = rng.choice([s for s in SERVICES if s != service])
        infra = rng.choice(["kubernetes", "docker", "serverless", "vm-cluster"])
        failure_mode = rng.choice(["circuit breaker", "bulkhead", "retry with backoff"])
        quarter = rng.randint(1, 4)
        topic = f"arch:{service}:{concern}"
        year = rng.choice([2023, 2024, 2025])

        body = (
            f"The {service} uses {pattern} for {concern}.\n\n"
            f"This component is responsible for {concern} within the {service}. "
            f"It receives events from upstream and produces state changes downstream.\n\n"
            f"Key invariants:\n"
            f"- All messages are delivered at-least-once\n"
            f"- State transitions are idempotent\n"
            f"- Failures trigger {failure_mode}\n\n"
            f"Trade-offs: we chose {pattern} over {alternative} because {reason}. "
            f"This was decided in the Q{quarter} {year} review.\n\n"
            f"Related: [[{related}]], [[{infra}-topology]]"
        )
        title = f"{service} {concern} design"
        tags = ["architecture", service.split("-")[0], concern.split()[0]]
        self._write("entity", title, body, tags, topic=topic)
        self._add_query(f"how does {service} handle {concern}", topic, "medium")
        self._add_query(f"{service} {concern} architecture", topic, "easy")
        self._add_query(
            f"why {pattern} instead of {alternative} for {service} {concern}",
            topic,
            "hard",
        )

    def _gen_debugging(self) -> None:
        rng = self._rng
        symptom = rng.choice(SYMPTOMS)
        cause = rng.choice(ROOT_CAUSES)
        fix = rng.choice(FIXES)
        principle = rng.choice(PRINCIPLES)
        context = rng.choice(CONTEXTS)
        system = rng.choice(
            [
                "the kafka consumer",
                "the postgres pool",
                "the kubernetes deployment",
                "the redis cache",
                "the grpc interceptor",
                "the nginx upstream",
            ]
        )
        trigger = rng.choice(["deploying", "under load", "after failover", "during scale-up"])
        symptom_topic = f"debug-symptom:{symptom}"
        system_topic = f"debug-system:{symptom}:{system}"

        body = (
            f"When {trigger}, {system} exhibits {symptom}.\n\n"
            f"Root cause: {cause}. The issue manifests in the connection "
            f"lifecycle management code.\n\n"
            f"Fix: {fix}.\n\n"
            f"Underlying principle: {principle}. "
            f"Watch for this pattern whenever you see {symptom} in similar contexts.\n\n"
            f"Discovered during {context}."
        )
        title = f"{symptom} in {system}"
        tags = ["debugging", system.split()[-1] if " " in system else system]
        self._write("entity", title, body, tags, topic=[symptom_topic, system_topic])
        self._add_query(symptom, symptom_topic, "medium")
        self._add_query(f"why is {system} showing {symptom}", system_topic, "hard")

    def _gen_api_contract(self) -> None:
        rng = self._rng
        method, path, purpose = rng.choice(ENDPOINTS)
        cond1 = rng.choice(ERROR_CONDITIONS[:4])
        cond2 = rng.choice(ERROR_CONDITIONS[4:])
        rate = f"{rng.randint(10, 1000)}/min"
        idempotent = "yes, via Idempotency-Key header" if rng.random() > 0.5 else "not idempotent"
        versioned = "versioned" if rng.random() > 0.3 else "unversioned but stable"
        weeks = rng.randint(2, 4)
        err1 = rng.choice(["400", "401", "403", "404"])
        err2 = rng.choice(["409", "422", "429", "500", "503"])
        topic = f"api:{method}:{path}"

        body = (
            f"The `{method} {path}` endpoint {purpose}.\n\n"
            f'Request: `{{"id": "uuid", "data": "object"}}`\n'
            f'Response: `{{"status": "string", "result": "object", '
            f'"created_at": "ISO8601"}}`\n\n'
            f"Errors:\n"
            f"- `{err1}`: {cond1}\n"
            f"- `{err2}`: {cond2}\n\n"
            f"Rate limit: {rate}. Idempotency: {idempotent}.\n\n"
            f"This contract is {versioned}. "
            f"Breaking changes require {weeks} weeks notice."
        )
        title = f"{method} {path} contract"
        tags = ["api", path.split("/")[1] if "/" in path else "api"]
        self._write("entity", title, body, tags, topic=topic)
        self._add_query(f"{method} {path}", topic, "easy")
        self._add_query(f"rate limit {path}", topic, "medium")

    def _gen_convention(self) -> None:
        rng = self._rng
        convention, anti = rng.choice(CONVENTIONS)
        topic = f"conv:{convention}"
        example = rng.choice(CODE_EXAMPLES)
        reason = rng.choice(REASONS)
        origin = rng.choice(
            [
                "a production incident",
                "a difficult code review",
                "a refactoring session that took 3 days",
            ]
        )
        exception = rng.choice(
            [
                "performance-critical hot paths",
                "legacy code during migration",
                "test files",
                "prototype/POC code",
                "third-party SDK wrappers",
            ]
        )
        default_action = rng.choice(
            [
                "ask in #engineering",
                "follow the existing code",
                "prefer explicit over implicit",
            ]
        )

        body = (
            f"In this project, always {convention}. Never {anti}.\n\n"
            f"Reason: {reason}. This was established after {origin}.\n\n"
            f"Example:\n```python\n{example}\n```\n\n"
            f"Exception: acceptable in {exception}. "
            f"When in doubt, {default_action}."
        )
        title = f"Convention: {convention[:50]}"
        tags = ["convention", "coding-standards"]
        self._write("procedure", title, body, tags, topic=topic)
        self._add_query(f"why {convention}", topic, "medium")

    def _gen_infrastructure(self) -> None:
        rng = self._rng
        env = rng.choice(["staging", "production", "development", "edge"])
        resource = rng.choice(
            [
                "cluster autoscaling",
                "database replication",
                "CDN cache rules",
                "DNS failover",
                "load balancer health checks",
                "pod disruption budgets",
            ]
        )
        nodes = rng.randint(2, 20)
        region = rng.choice(["us-east-1", "eu-west-1", "ap-south-1", "us-west-2"])
        instance = rng.choice(["m5.large", "c5.xlarge", "r5.2xlarge", "t3.medium"])
        rolling = rng.choice(["maxSurge 1, maxUnavailable 0", "maxSurge 25%, maxUnavailable 25%"])
        constraint = rng.choice(
            [
                "zero-downtime required",
                "can tolerate 30s of degraded service",
                "must drain connections before termination",
            ]
        )
        max_deploys = rng.randint(2, 5)
        last_change = rng.choice(["2024-Q1", "2024-Q3", "2025-Q1", "2025-Q2"])
        topic = f"infra:{env}:{resource}"

        body = (
            f"The {env} {resource} configuration:\n\n"
            f"- Nodes: min {max(1, nodes // 2)}, max {nodes}\n"
            f"- Region: {region}\n"
            f"- Instance type: {instance}\n"
            f"- Rolling update: {rolling}\n\n"
            f"Constraints: {constraint}. "
            f"Max {max_deploys} concurrent deploys.\n\n"
            f"Last changed: {last_change}. "
            f"Runbook: [[{resource.replace(' ', '-')}-runbook]]"
        )
        title = f"{env} {resource}"
        tags = ["infrastructure", env]
        self._write("entity", title, body, tags, topic=topic)
        self._add_query(f"{env} {resource}", topic, "easy")
        self._add_query(f"how many nodes in {env} {resource}", topic, "medium")

    def _gen_domain_knowledge(self) -> None:
        rng = self._rng
        concept = rng.choice(CONCEPTS).strip()
        misconception, correction = rng.choice(MISCONCEPTIONS)
        concept2 = rng.choice([c for c in CONCEPTS if c.strip() != concept]).strip()
        topic = f"concept:{concept}"

        body = (
            f"{concept.title()} is a fundamental concept in distributed systems.\n\n"
            f"In practice, this means:\n"
            f"1. Operations must be safe to retry without side effects\n"
            f"2. State changes propagate asynchronously across replicas\n"
            f"3. Temporary inconsistency is acceptable if convergence is guaranteed\n\n"
            f"Common misconception: {misconception}. "
            f"The correct understanding is {correction}.\n\n"
            f"This matters for service reliability and data integrity. "
            f"Related concepts: [[{concept2}]]"
        )
        title = f"{concept.title()} explained"
        tags = ["concept", "distributed-systems"]
        self._write("summary", title, body, tags, topic=topic)
        self._add_query(f"what is {concept}", topic, "easy")
        self._add_query(f"{concept} misconception", topic, "hard")

    def _gen_temporal(self) -> None:
        rng = self._rng
        from_tech, to_tech, motivation = rng.choice(CHANGES)
        project = rng.choice(PROJECT_NAMES)
        quarter = rng.choice(["Q1", "Q2", "Q3", "Q4"])
        year = rng.choice([2023, 2024, 2025])
        weeks = rng.randint(2, 6)
        topic = f"temporal:{project}:{from_tech}"

        body = (
            f"As of {quarter} {year}, the {project} project has migrated "
            f"from {from_tech} to {to_tech}.\n\n"
            f"Previously: {from_tech} was used for all data storage needs. "
            f"The migration was driven by {motivation}.\n\n"
            f"Migration notes:\n"
            f"- Data was migrated using a dual-write strategy\n"
            f"- Read traffic was switched gradually over {weeks} weeks\n"
            f"- Rollback plan: keep the {from_tech} cluster running for 30 days\n\n"
            f"The old {from_tech} setup is kept in read-only mode. "
            f"All new development targets {to_tech}."
        )
        title = f"{project}: migrated from {from_tech} to {to_tech}"
        tags = ["migration", project]
        self._write("summary", title, body, tags, topic=topic)
        self._add_query(f"why did {project} switch from {from_tech}", topic, "medium")
        self._add_query(f"{project} {to_tech}", topic, "easy")

    def _gen_session(self) -> None:
        rng = self._rng
        task = rng.choice(SESSION_TASKS)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        date = f"2024-{month:02d}-{day:02d}"
        task_verb = task.split()[0]
        topic = f"session-verb:{task_verb}"
        pattern = rng.choice(PATTERNS)
        service = rng.choice(SERVICES)
        topics = rng.choice(
            [
                "authentication, rate limiting, monitoring",
                "deployment, observability, alerting",
                "data modeling, caching, consistency",
            ]
        )

        body = (
            f"In the session on {date}, the agent {task}.\n\n"
            f"Key decisions:\n"
            f"- Used {pattern} for the approach\n"
            f"- Chose {service} as the implementation vehicle\n\n"
            f"Knowledge stored: architecture docs, debugging notes, "
            f"conventions discovered during the session.\n\n"
            f"The conversation covered {topics}."
        )
        title = f"Session {date}: {task[:40]}"
        tags = ["session", "episodic"]
        self._write(
            "episode",
            title,
            body,
            tags,
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            topic=topic,
        )
        self._add_query(task_verb, topic, "hard")

    # ------------------------------------------------------------- helpers

    def _write(
        self,
        node_type: str,
        title: str,
        body: str,
        tags: list[str],
        session_id: str | None = None,
        topic: str | Sequence[str] | None = None,
    ) -> WikiNode:
        base = slugify(title)[:64] or "node"
        slug = unique_slug(base, self._used_slugs)
        self._used_slugs.add(slug)
        if topic is not None:
            topics = [topic] if isinstance(topic, str) else topic
            for item in topics:
                self._topic_slugs.setdefault(item, []).append(slug)

        now = utc_now_iso()
        node = WikiNode(
            type=node_type,
            title=title,
            body=body,
            id=str(uuid.uuid4()),
            slug=slug,
            tags=tags,
            created=now,
            timestamp=now,
            updated_at=now,
            session_id=session_id,
            content_hash=hash_body(body),
        )
        path = self._data_dir / "docs" / TYPE_DIRS[node_type] / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize_front_matter(node_front_matter(node), body), encoding="utf-8")

        self._slugs.append(slug)
        self._domain_counts[node_type] = self._domain_counts.get(node_type, 0) + 1
        return node

    def _add_query(self, query: str, topic: str, difficulty: str) -> None:
        """Defer resolution: expected slugs = every page of that topic."""
        self._pending.append((query, topic, difficulty))

    def _resolve_queries(self) -> list[QuerySpec]:
        resolved = []
        for query, topic, difficulty in self._pending:
            expected = list(self._topic_slugs.get(topic, []))
            if not expected:
                continue
            resolved.append(
                QuerySpec(
                    query=query,
                    expected_slugs=expected,
                    difficulty=difficulty,
                    family=topic.split(":", 1)[0],
                    corpus="realistic",
                )
            )
        return resolved
