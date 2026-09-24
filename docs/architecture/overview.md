# Architecture overview

Memex is an installable Python package and CLI. It has no required service
process: commands and coding-agent hooks open a local store, perform one
operation, and exit. The optional `memex viz` server exists only for the life of
that command.

## System boundaries

```text
coding-agent harnesses ─┐
CLI (`memex`) ──────────┼─> application facade ─> Markdown pages
MCP stdio server ───────┘          │               SQLite FTS5 index
                                   │               transcript JSONL
                                   └─> optional LLM provider (consolidate only)
```

The filesystem under `MEMEX_DATA_DIR` or `~/.memex/` is the durable boundary.
Markdown pages are the source of truth. SQLite, run logs, transcript metadata,
and rendered dashboard responses are derived or supporting state.

## Code ownership

| Area | Responsibility | Start here |
| --- | --- | --- |
| `src/memex/domain/` | Validated models, front matter, slugs, links, scrubbing, the concept-type vocabulary, and adapter-neutral operation datatypes. No filesystem, database, network, or SDK calls. | `models.py`, `operations.py`, `types.py` |
| `src/memex/application/` | The public `Memex` facade, orchestration, consolidation, context packing, link-graph expansion, verification, decay, and the LLM port. | `memory.py`, `ports.py`, `graph_expansion.py` |
| `src/memex/infrastructure/` | Cross-cutting runtime adapters no package claims: configuration, logging, the run log, workspace identity, and LLM clients. | `config.py`, `llm_clients.py` |
| `src/memex/infrastructure/store/` | The Markdown tree that is the source of truth: page CRUD, generated navigation, navigation search, watching, archive, and transfer. | `wiki_store.py`, `navigation.py`, `navigation_search.py` |
| `src/memex/infrastructure/search/` | The disposable SQLite index: FTS5 retrieval, index upkeep, and the link graph. | `bm25_retriever.py`, `index_manager.py` |
| `src/memex/infrastructure/harness/` | Coding-agent integration: installers, harness-native transcript parsing, capture, and harness-assisted episode summaries. | `installer.py`, `transcript_hook.py` |
| `src/memex/infrastructure/web/` | The read-only localhost dashboard: HTTP server, HTML components, and its assets. | `server.py`, `components.py` |
| `src/memex/cli.py` | Argument parsing and JSON/text presentation for the shared services. | `_build_parser()`, `_run()` |
| `src/memex/mcp_server.py` | Typed stdio MCP tools over the same facade and domain datatypes. | `build_server()` |
| `src/memex/marketplace/` | Thin harness-specific installation assets over the stable `memex hook` contract. Package data: `memex install` reads it at runtime. | Per-harness `README.md` |
| `eval/` | Offline corpus generation, retrieval metrics, and scale experiments. It is development tooling, not package runtime. | `run.py`, `runner.py` |

The `store/` and `search/` packages are the two halves of ADR-0001 made
visible: `store/` owns the authoritative Markdown, `search/` owns the
disposable index rebuilt from it.

`Memex` is both the public Python API and the composition root: it constructs
the filesystem store, index, retriever, link manager, transcript hook, archive
service, and import/export service. Infrastructure may implement application
ports and use domain types. Adapters should call the facade or shared
application services instead of reimplementing operations.

The CLI has two deliberate infrastructure-facing seams: harness installation
and harness-native transcript parsing. They adapt external files and processes
before handing normalized data to the shared application contract.

The dashboard keeps `web/server.py` as the public local-server seam and route
adapter. `web/components.py` owns the shell and scope controls;
`web/assets/dashboard.css` owns the local visual system; `web/explorer.py` owns
selection and pagination; and `web/sessions.py` owns grouped session and replay
rendering. Routes compose
server-rendered fragments from those parts. Dashboard search uses the
retriever's non-mutating read path, so GET requests do not update index access
counters. The dashboard is a read-only localhost projection. Its durable
trust-boundary rules live in [dashboard security](security.md).

## Runtime flows

### Write

1. An adapter creates a validated `WriteInput`.
2. The facade rejects reserved provenance fields, scrubs secret-shaped text,
   and asks `WikiStore` to atomically write the Markdown page.
3. `IndexManager` mirrors the page into SQLite and `LinkManager` refreshes its
   outgoing links.

The page write justifies every index mutation. A failed or deleted index can be
rebuilt from the pages. After the page commits, the affected directory
indexes refresh as a best-effort follow-up that never fails the write.

### Recall and injection

1. `BM25Retriever` reduces untrusted query text to safe alphanumeric tokens,
   removes only known semantic scaffolding phrases, and searches active,
   non-expired pages by default.
2. Returned hits update access statistics in SQLite; reads do not rewrite
   Markdown pages.
3. Hook injection applies a relevance floor, packs hits to a token budget, and
   emits a bounded context block. Explicit recall can still return weak hits.
4. Link-graph expansion (`application/graph_expansion.py`, OKF
   `read_concept`) appends pages linked from the packed hits. Given seed
   `RecallHit`s it walks `wiki_links` breadth-first (level by level,
   alphabetical slug within a level, each page once, seeds excluded),
   resolving each target inside its source page's scope and project and
   through `BM25Retriever.neighbours`, which applies recall's own visibility
   rule so only pages recall would return are reachable. It returns `Expansion(entries, omitted)`;
   `render_expansion` yields the short blocks plus the omitted-count marker.
   Consumers are `context_injection.build_injection` (session-start hook) and
   `task_recall.with_linked_pages` (called by `Memex.recall_task`). Direct hits
   are packed first and never displaced; expansion only spends the remaining
   token budget.

Production recall uses the `semantic-and-fallback-fts5` ranker. It caps the
safe-token query at 64 tokens and 1,024 UTF-8 bytes, then first runs a strict
`AND` FTS5 query over the safe tokens with column weights
`slug=1, title=1, description=2, body=2, tags=1` — a purpose-written
description match ranks like a body match, because a query hitting the
signpost is as strong a relevance signal as a body hit; stores without
descriptions are unaffected — then broadens to an `OR` query only when the
strict query returns zero rows. Snippets are capped at 12 tokens and are
selected from the matching column: body first, then description, then title,
with `snippet_source` reporting the match origin. Filters are
applied before each limit, returned slugs are unique, links and access
statistics are loaded once after ranking, and ascending slug is the final
tie-break. The ranker is local SQLite FTS5 only: no embeddings, network service,
runtime `rgapi`, subprocess search, or new required dependency is on the recall
path.

**Navigation search** (`infrastructure/store/navigation_search.py`) is a
second recall engine that reads the Markdown tree's own navigation instead of
`mem.db`: it parses the page rows of every structural `index.md` (legacy pages
at that name are skipped through `classify_reserved`), ranks them by weighted
title/description term overlap using the FTS5 retriever's query boundary, and
opens only the front-matter block of the candidates it returns to apply the
shared visibility rules. `Memex.recall` selects the engine
(`engine="fts5" | "navigation"`) and falls back to it when the index reports
zero rows while navigation lists pages, so a missing or emptied `mem.db`
degrades recall rather than silencing it. Trust boundary: index rows and front
matter are untrusted data; links are validated lexically to sibling `slug.md`
pages and bodies are never read.

The task-evidence candidate lives in `eval/task_evidence_model.py`, outside the
production recall path. It compares goal-based questions from a coding harness
with the held-out baseline under wall-time and catalog-equivalent spend limits.
One pi run completed 7 of 24 retrieval tasks, below the linked-summary
baseline's 11 of 24. The promotion gate kept the candidate experimental; no
question planner or model call runs in ordinary recall or hook injection.
Optional task mode on the existing recall adapters accepts up to three
caller-written questions, searches one project for each through the same
ranker, interleaves unique hits, then packs a complete context to 4,096
estimated tokens. It marks unanswered and budget-omitted questions and records
access only for included pages. The mode adds no model call or new index.
See `docs/research/2026-09-20-task-evidence-focused-candidate.md` for the
earlier measured result and its limits.

### Retrieval evaluation security controls

Retrieval evaluation uses independent offline workloads and treats every source
as untrusted. Salesforce coverage is represented by concise, independently
authored offline Salesforce facts with official URLs as provenance; tests and
evaluations never scrape or fetch Salesforce pages. Gutenberg coverage comes
from a maintainer-supplied local Project Gutenberg catalog import, not from
HTML crawling or book text.

Fixture output is confined to the resolved repository fixture directory before
replacement. The Gutenberg importer accepts only a regular `.csv.gz` catalog,
enforces compressed and expanded parser limits, validates the allowlisted schema
and UTF-8 JSONL output, and stops before replacement on gzip, CSV, schema,
size, row, field, path, or serialization errors. Ordinary tests and evaluation
run with no network access; socket and HTTP sentinels prove that path.
Integrity failures fail closed rather than producing promotion evidence.
Retained reports are redacted: they keep categories, metrics, workload
manifests, ranker metadata, and non-identifying environment fields, but exclude
memory contents, credentials, raw non-generated queries, absolute or
user-specific paths, hostnames, usernames, device names, profile paths, stack
traces, and exception strings.

### Transcript capture

Harness adapters call `memex hook transcript` with the harness-native session
file. The parser normalizes turns and a session header, `TranscriptHook` writes
JSONL plus metadata, and an episode page links back to the transcript. Repeated
Codex captures merge by stable session identity so compaction and shutdown
events remain idempotent.

### Consolidation

Consolidation is the only operation that needs an LLM. `WikiConsolidator`
selects episode pages, calls the configured application `LLMClient` port, and
validates each returned node before using the normal write/index/link path.
Remote OpenAI-compatible providers and local harness CLI providers implement
the same port. Failures return a partial report and do not block the harness
hook.

### Generated directory navigation

Pages themselves are OKF v0.2 concepts (ADR-0005): front matter declares
`okf_version: "0.2"`, carries the OKF field set first in the reference
implementation's order, and then the Memex fields OKF has no equivalent for,
which an OKF reader preserves verbatim. Relations are one graph — typed
front-matter `links`, the `parent`/`supersedes`/`implements`/`depends_on`
fields, and body `[[slug]]` references all become `(source, target, rel)` rows
in `wiki_links`.

`NavigationGenerator` derives one deterministic `index.md` per directory that
contains pages: the memory-root index declares `okf_version: "0.2"` and
descendants are body-only listings of titles, descriptions, and child links.
These files are disposable views of the pages, exactly like `mem.db`: the
explicit `rebuild-index` path regenerates them, and a failed refresh never
fails an authoritative page write. After a single page write, update, or
delete, `NavigationGenerator.refresh_page` parses the directory's existing
`index.md` back into the renderer's middle form, splices that page's row in
sorted position, and re-serializes through the same composer, so the result
is byte-identical to a full render at a fraction of the cost; a missing,
hand-written, or ambiguous index falls back to the full directory-chain
render, and ancestor indexes are rewritten only when a child directory gains
or loses its last page. The filenames `index.md` and `log.md` are
reserved at every level — structural files never enter the store scan, so
they cannot become `WikiNode`s, FTS rows, links, export entries,
consolidation input, or watcher lifecycle events. A valid legacy page at a
reserved name is preserved byte-for-byte; generation skips the colliding
path and verification reports it. Opening a store never writes navigation.

A project's type set is its directories: built-in types keep their
pre-created plural directories, and a catalogue or custom type (ADR-0006,
`domain/types.py`) exists for a project exactly when
`projects/<project>/<type>/` exists on disk — there is no separate registry
to fall out of sync with the tree. `log.md` in each declared directory
records the declaration: an append-only, body-only history of
`declare`/`propose`/`remove` lines memex writes and never rewrites, kept
structural by the same reserved-filename rule as `index.md` so it never
enters the store scan, FTS index, or export. `WikiStore.write`, `get_path`,
`move`, and `list` validate `type` against built-in ∪ enabled catalogue ∪
declared custom for the caller's scope and project before any file is
written, and `NavigationGenerator` orders headings built-in-first then
declared types alphabetically, falling back to a full render when a parsed
index's section order is not canonical. `RecencyDecay.apply_decay` skips
every catalogue page (`domain/types.py:decays`): the five built-ins and
custom pages age on recency, catalogue knowledge does not.

### Verification and maintenance

`memex verify` checks page parsing, index freshness (including the mirrored
description), links, OKF graph and temporal conformance (parent resolution and
acyclicity, relation targets, validity ordering, advisory staleness inside the
window), optional recall/write activity evidence, that generated navigation
matches the page tree, and three type checks: a page whose `type` differs
from its directory, a project directory that is neither built-in, enabled,
declared, nor draft, and a draft directory holding a non-pending page.
`rebuild-index` reconstructs
derived SQLite state and regenerates directory indexes. Backup and restore
validate archive paths and links; restore preserves the previous store in a
timestamped directory before replacement.

## Durable state

```text
~/.memex/                         # overridden by MEMEX_DATA_DIR
├── memex.toml                    # user-owned configuration
├── docs/index.md                 # generated disposable navigation (root)
├── docs/global/<type>/<slug>.md  # global authoritative memory pages
├── docs/projects/git-<repo>/<type>/<slug>.md    # project pages from a Git origin
├── docs/projects/<folder>/<type>/<slug>.md      # project pages without a usable origin
├── mem.db                        # disposable SQLite/FTS5 index
├── transcripts/<yyyy-mm-dd>/<session>.jsonl     # captured turns
├── transcripts/<yyyy-mm-dd>/<session>.meta.json
└── logs/                         # diagnostics and operation evidence
```

Repository content cannot enable capture or injection. Installation modifies
user-owned harness configuration only after an explicit command, and tests use
an isolated `MEMEX_DATA_DIR` rather than the developer's real store.

## Invariants

- Markdown pages remain sufficient to rebuild the searchable memory store.
- Generated `index.md` navigation files are disposable views rebuilt from the
  pages; `index.md` and `log.md` are reserved filenames excluded from every
  memory surface, and navigation refresh failures never fail an authoritative
  page write.
- Page front matter carries scope. Project identifiers are opaque hashes and
  remain the namespace authority; readable project directories are locators
  only.
- Git origins use `git-<repo>` from the repository basename for any usable
  provider, including enterprise GitLab. Local Git without a usable origin and
  non-Git workspaces fall back to the workspace folder name.
- Raw repository remotes, credentials, internal hostnames, and absolute local
  paths are never stored in project metadata or the folder locator.
- Legacy ID-named project folders stay readable and rebuildable until a
  separately confirmed external migration moves them. Runtime code does not
  move or merge existing project folders.
- A duplicate `(project_id, node_type, slug)` across folders is an ambiguous
  source-of-truth state; lookup and force rebuild fail closed.
- Domain modules perform no I/O.
- CLI and MCP behavior share services and wire datatypes.
- Stored memories, transcript data, archive members, and tool inputs are
  untrusted at every boundary.
- Logs contain identifiers and categories, never memory contents, credentials,
  or raw tool inputs.
- LLM use is explicit or opt-in; ordinary write, recall, capture, backup, and
  verification remain LLM-free.
- Hook failures degrade safely and do not block the coding-agent turn.

## Change guidance

- Change a page field in the domain model, strict front-matter codec,
  persistence mapping, index schema, import/export path, and adapter schemas
  together.
- Change a shared operation in the facade first, then keep CLI and MCP adapters
  thin and parity-tested.
- Change harness behavior behind the `memex hook` contract unless the shared
  contract itself must change.
- Treat a persistent schema change as rebuildable when all information exists
  in Markdown; otherwise provide an explicit migration and backward-compatibility
  plan.

The normal gates are `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy src tests`, `uv run pytest`, and `uv run mkdocs build --strict`.
