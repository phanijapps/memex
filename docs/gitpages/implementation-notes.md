# Implementation notes — deviations from docs/spec.md

The spec (v1.0.0) was implemented as written except for the items below.
Each deviation is either a spec-internal defect fix or a decision ratified
during plan review (2026-09-15): "zero required dependencies" is not a hard
rule, and the OpenAI SDK is the single LLM client.

## Fixed spec defects

1. **FTS5 DDL could not index bodies (spec §6.2).** The external-content
   `wiki_fts` table mirrors `wiki_index`, which had no `body` column, and the
   sync triggers inserted `''` for body. `wiki_index` now carries a `body`
   mirror column and the ai/ad/au triggers sync real body text.
2. **Snippet column off-by-one (spec §7 Utility 3).** With FTS columns
   `(slug, title, body, tags)`, column 1 is the title. Body snippets use
   column 2; the title is the fallback. Because FTS5 `snippet()` returns text
   even for columns without a match, the body-vs-title decision detects the
   `<mark>` tags rather than emptiness.
3. **`bm25.k1`/`b` are not SQL-tunable.** SQLite FTS5 `bm25()` uses
   compile-time defaults. The config fields are parsed and stored but
   documented as reserved; ranking uses the built-in `bm25()`.
4. **Transcript filename contradiction.** §12.2 showed dated filenames
   (`2026-09-15-sess-x.jsonl`); the normative contracts (§7 Utility 7, §9.5)
   use `{session_id}.jsonl`. The contracts win.
5. **`read()` was specified to mutate files.** Incrementing `access_count`
   in front matter on every read would rewrite each page and churn git
   history. Reads are side-effect free; access counting lives in the index
   and is applied by recall.

## Ratified decisions

- **Dependencies.** `mcp>=2,<3` (already declared in pyproject) and
  `openai>=1.60` are required. Core memory operations (write/recall/forget/
  transcripts/backup) import neither at runtime.
- **One LLM client.** `OpenAICompatClient` (official `openai` SDK) serves
  OpenAI, Ollama, LM Studio, OpenRouter, and any compatible endpoint via
  `llm.provider` → default `api_base` mapping plus `llm.api_base` override.
  The spec's `OllamaClient`/`LMStudioClient` (hand-rolled `http.client`) and
  `AnthropicClient` (non-compatible API) were dropped.
- **MCP SDK v2.** `mcp` 2.x renamed `FastMCP` to `MCPServer`; the server uses
  `mcp.server.mcpserver.MCPServer` with `add_tool`.
- **Layered layout.** Modules follow AGENTS.md layering instead of the spec's
  flat build order: `memex/domain` (pure models, slugs, front matter, links),
  `memex/application` (facade, decay, ports), `memex/infrastructure`
  (filesystem, SQLite, LLM, config, backup), plus `cli.py` / `mcp_server.py`
  adapters. Spec module names are preserved.
- **YAML front matter.** Stdlib has no YAML parser. `memex.domain.frontmatter`
  implements a strict codec for the documented subset; PyYAML is not needed.
- **Extractor is single-match per turn.** The spec said "each matching cue
  phrase generates a WriteInput" (noisy: one turn could emit preference,
  rule, and fact nodes). The extractor emits the first matching category
  (preference > procedure > fact); complex extraction belongs to
  consolidation.
- **`WriteResult` (spec §14 File 1) is not defined by any schema and §9.1
  returns a `WikiNode`; it is not exported.**

## Behavioral notes

- **Descriptions and directory navigation (agentic front matter search).**
  Two deliberate scope readings from the 2026-09-21 spec
  (`docs/specs/agentic-frontmatter-search/`): (a) `memex rebuild-index`
  regenerates navigation by default — AC-0015's "explicit rebuild path" is
  the rebuild command, while the transparent on-open schema-v5 upgrade still
  never writes Markdown; (b) an empty store generates no root `index.md` by
  design, so `memex verify` stays green on fresh stores (AC-0015 read
  literally would create navigation merely by opening an empty store).
  Descriptions mirror into the disposable index only; existing pages keep an
  empty description until edited.
- **Downgrade behavior (same feature).** A pre-description binary opening a
  v5 store treats the schema as compatible, but its recall degrades silently:
  SQLite FTS5 accepts fewer `bm25()` weights than columns, so the old
  four-weight call runs against the five-column table with the old body
  weight landing on the description column and old snippet column indexes
  reading the wrong column — delete `mem.db` (it rebuilds on next open) to
  restore correct ranking. The old scanner also reports generated body-only
  `index.md` files and pages carrying a `description` key as malformed pages.
  Markdown is never touched by the upgrade; recovery from a downgrade is
  deleting `mem.db`, removing generated indexes, and stripping descriptions.
- **Incremental navigation refresh.**
  `NavigationGenerator.refresh_page(page_path, node, scan_dir)` refreshes
  navigation after one page mutation (`node` is `None` for a delete). It
  reads the directory's `index.md`, confirms it is structural with
  `is_structural`, parses it back into entry lines keyed by node type and
  slug plus child-directory names, replaces or removes the page's own row,
  and re-serializes through the same composer `render` uses, so the splice
  and a full render produce identical bytes. Anything the parser does not
  recognise (missing index, non-generator shape, the page listed under two
  headings, a delete of an unlisted page, an orphan index) falls back to the
  unchanged chain `refresh(directory, scan_dir)`. Ancestor indexes list child
  directories only and are rewritten only when the directory loses its last
  page. The facade (`Memex._refresh_navigation`) receives only the page path
  from its callers, so it re-reads that one page through the store (an absent
  file means deleted); this is the one place the application layer calls
  `WikiStore._read_path`, pending a public `read_path`. The watcher keeps the
  directory-level `refresh`, and `rebuild-index` keeps `regenerate`.
- **Navigation engine reads front matter only.** `NavigationSearch` fills
  `RecallHit` fields from a bounded front-matter read (stops at the closing
  `---`, 16 KB ceiling) and never opens a body, so hits have no body snippet
  (`snippet_source` is `title` or `description`) and no access statistics
  are recorded. Score is higher-is-better for this engine, unlike FTS5's
  ascending `bm25()`; read `search_engine` before comparing scores. A project
  filter learns each project directory's id from one page's front matter,
  since locators are opaque. No stop-word or document-frequency pruning
  applies: every alphanumeric query token scores.
- **Link expansion visibility reuse.** `BM25Retriever.neighbours` runs the
  one SQL join from `wiki_links` to `wiki_index` (target resolved in the
  source page's scope and project) under the same visibility clauses recall
  applies (active status, validity window, scope/project). The application
  layer's `graph_expansion` only walks the graph it returns, so the
  visibility rule lives in exactly one place and no SQL leaves
  infrastructure.
- **Cross-namespace links are not expanded.** A project page naming a global
  slug (or vice versa) yields no expansion entry in this slice; the join
  requires the same scope and project as the source.
- **Expansion budget rule.** `expand_links` reserves the omitted marker
  before packing and stops at the first entry that does not fit.
  `build_injection` and `with_linked_pages` then drop the whole linked
  section if even the marker would push the rendered text over the bound;
  direct hits are never trimmed for expansion.
- **Benchmark cannot show the expansion gain.** The committed fixture's four
  `[[...]]` references target slugs no card carries, so expansion adds zero
  pages/tokens there; a fixture with linked cards is needed to measure
  coverage lift.
- Change detection hashes body text on read: a hand-edited page keeps a stale
  front-matter `content_hash`, so the watcher and `rebuild_index` compare a
  freshly computed hash against the index row and refresh the front matter
  when it differs.
- `IndexManager.rebuild_from_wiki` (spec §7 Utility 2) lives on the facade as
  `Memex.rebuild_index()`, which composes store scan, index upsert, link
  sync, stale-row removal, and metadata bookkeeping.
- Consolidation failures return a partial report (§9.3) with the LLM error
  logged; tool errors over MCP are sanitized to generic messages.
- Restore validates archive members (no absolute paths, `..`, links, or
  unexpected entries), moves current data to `pre-restore-{timestamp}/`
  instead of deleting, and snapshots `mem.db` via the SQLite backup API so
  WAL-mode databases archive consistently.

## Architecture update (post-build)

- **Shared operation contracts.** Operation descriptions and wire
  datatypes live in `memex.domain.operations` and are consumed by every
  adapter — the CLI (subcommand help), the MCP server (tool
  descriptions and schemas), and any future API. AGENTS.md's
  dependency-free-domain rule was replaced by this reuse mandate; parity
  tests pin the wire datatypes to their domain-model twins.

## Harness hooks (post-build)

- The `memex hook` command family is the stable adapter contract:
  `session-start` and `prompt` emit the spec §5.4 context block on
  stdout (empty output when nothing is stored), `transcript` ingests
  harness-native session files (pi sessions, Claude transcripts, Codex
  rollouts) with idempotent, filename-derived session ids. The
  src/memex/marketplace/ directory ships per-harness adapters over this contract;
  pi is the reference implementation.

- `memex verify` (L3) always checks health (parseable wiki, index
  freshness against content hashes, link integrity) and optionally
  enforces recall/write activity evidence since an ISO cutoff — the
  exit code is CI-able. `memex harness install` ships the marketplace
  adapters idempotently (pi copy; claude/codex config merge with
  backups; copilot instructions + verify workflow).

- **First-class hook-driven consolidation (spec §2.2 deviation, opt-in).**
  The spec excluded auto-summarization because tool-calling LLMs are not
  universally available. Consolidation remains off by default and
  LLM-free until explicitly enabled — via `memex hook transcript
  --consolidate` or `MEMEX_AUTO_CONSOLIDATE=1` — and a dedicated
  low-effort model can be configured under `[consolidation]`, inheriting
  `[llm]` credentials. Failures degrade to partial reports and never
  block the hook.

- **Harness-as-LLM-provider + seamless install.** `claude`, `codex`,
  and `pi` are valid `[consolidation]` providers: consolidation runs
  through the harness CLI's print mode (its model, credentials,
  billing) instead of a configured HTTP endpoint. `memex install`
  replaces `harness install` as the primary command, resolves the
  marketplace from the bundled package copy, provisions `[consolidation]`
  for harness installs, and `custom` initializes `~/.memex` only.
  `--data-dir` now locates `memex.toml` too, making redirected runs
  self-contained.

- **Transcript session headers.** The first line of a transcript JSONL
  is now a `memex_session_header`. The Codex parser is built against
  real rollout data (`session_meta`, `turn_context`,
  `token_usage_record` with `turn_token_usage`/`thread_token_usage`):
  totals take the latest records — cumulative values are never summed —
  per-turn usage attaches to the agent turn it billed, resumed sessions
  (re-emitted `session_meta`) refresh cwd/git and set `resumed`, and
  model changes collect into ordered `models`/`reasoning_efforts` lists.
  Readers skip headers; turn-only transcripts stay readable.

- **Codex capture hardening + docs layout (0.2.0).** The notify wrapper
  handles agent-turn-complete, PostCompact, and SessionEnd (fast
  detached handoff — Codex's 1-3s teardown bound), addresses sessions
  by the payload's session_id/transcript_path instead of the newest
  rollout, logs diagnostics (event, session, category; never content),
  and stays nonblocking. The parser captures tool calls/outputs
  (function/custom/web/tool-search, paired by call_id), agent_message
  entries, and skips compacted replacement history. Repeated captures
  merge idempotently, preserving pre-compaction turns. Storage moved
  from ~/.memex/wiki/ to ~/.memex/docs/ (pure memory layer, not a
  wiki): existing installs migrate in place on first open, old backups
  with wiki/ remain restorable.

- **v1 guardrails shipped (wave 1 + C1).** Token-budget recall with
  skip-and-continue packing and a top-1-whole guarantee (A3); injection
  floor — weak matches inject silence (A4); `occurred_at` dual timestamp
  (B4); page `status` lifecycle with archive/merge and recall filtering
  (B5); HITL approval via `[governance] knowledge_approval = "manual"`
  (default; renamed from `approval` in ADR-0006 — model-written knowledge
  lands `pending`, memory types and person writes stay `active`) + `memex
  approve` (C1); reserved provenance namespaces source/harness/confidence
  (C2); 10-pattern secret scrubber at every write boundary (D1, adapted
  from Hindsight's 45-pattern catalog to anchored stdlib regexes);
  enablement invariant pinned — no repo-carried config enables capture
  (D2); `memex status` + verify zero-yield warning over the new
  `logs/runs.jsonl` run log (F2/F4); three-line memory constitution on
  every injected block (G2). Schema v2: stale `mem.db` auto-rebuilds from
  the wiki on open — no DDL migration path exists.

- **Quality-gated retrieval winner.** Production recall now reports
  `search_engine="semantic-and-fallback-fts5"`. The ranker keeps SQLite FTS5 as
  the only runtime search dependency, reduces queries to safe tokens, removes
  known semantic scaffolding phrases, runs strict weighted `AND` matching first
  (`slug=1, title=1, body=2, tags=1`), and falls back to weighted `OR` only
  when strict matching returns zero rows. Snippets are capped at 12 tokens;
  filters apply before limiting; returned slugs are unique; links and access
  rows are batched once after ranking; ascending slug is the final tie-break.
  Definitive clean production-path promotion evidence at commit `226bad7` selected the
  ranker with overall Recall@10/MRR `0.9819588`, nDCG@10 `0.9323851`, hard
  Recall@10 `0.9809524`, and hard MRR `0.9809524` versus the `0.9738095`
  truthful legacy baseline. Hard tokens per correct result were `966.49`
  versus the `1251.81` threshold and `1564.77` baseline, a `38.23%` reduction.
  10K p99 improved from `48.05 ms` to `10.12 ms`, and 100K p99 improved from
  `400.72 ms` to `74.33 ms`. All repaired realistic, Gutenberg, and Salesforce
  workload gates passed in the promotion report, which recorded no failures.
  The evaluator exercised the production `BM25Retriever` no-access path, and
  the retained candidate metadata records `max_query_bytes=1024` and
  `max_query_tokens=64`.
  The report also retained negative-control diagnostics for one Salesforce hard
  negative-control query with zero non-empty results while omitting raw query
  text. The retained report has SHA-256
  `8a810445ff9587c0a8c5e346461dc9ee0f7982f83ae4619cf2e5cd52ae46e705`.

- **Retrieval dependency disposition.** `rgapi==0.1.22` remains an optional
  evaluation candidate only. It is not imported by `src/memex`, is not required
  for base installs, and is not a user-facing recall selector. No embeddings,
  cross-encoder, hosted search service, graph database, network call, or `rg`
  executable was added to the production recall path.

- **OKF v0.2 page front matter (ADR-0005).** Pages are Open Knowledge Format
  v0.2 concepts. Front matter declares `okf_version: "0.2"` and emits the OKF
  fields first in the reference implementation's order — `type`, `title`,
  `description`, `resource`, `tags`, `timestamp`, `valid_from`, `valid_until`,
  `stale_after`, `updated_at`, `parent`, `supersedes`, `implements`,
  `depends_on`, `links` — followed by the Memex fields OKF has no equivalent
  for. Four spec field names are retired with no alias: `created` keeps its
  name but becomes a Memex extension, `updated` becomes OKF `timestamp`
  (refreshed on every write) with `updated_at` moving only when the body
  changes, `valid_to` becomes `valid_until`, and `expires_at` becomes
  `stale_after` — advisory only, never a recall filter. Recall visibility is
  the `valid_from`/`valid_until` window alone, so `forget --soft` and
  `forget --decay` both end that window, the first at once and the second one
  configured half-life out. Front-matter `links` are OKF typed relations
  (`[{target: "slug", rel: "relates-to"}]`, written inline so the codec stays
  line-oriented); a body `[[slug]]` reference is a `mentions` edge in
  `wiki_links` and is no longer copied into front matter. `wiki_links` gained
  a `rel` column inside its primary key and the schema moved to version 6,
  which rebuilds from Markdown. `memex verify` gained five OKF conformance
  checks, and a generated store passes the OKF reference linter with zero
  errors and zero warnings. There is no migration: a pre-OKF store must be
  deleted.

- **Infrastructure packages and the retired extractor.** `infrastructure/` is
  grouped by concern: `store/` (the authoritative Markdown tree — `wiki_store`,
  `navigation`, `watcher`, `backup`, `import_export`), `search/` (the
  disposable index — `index_manager`, `bm25_retriever`, `link_manager`),
  `harness/` (`installer`, `transcripts`, `transcript_hook`,
  `episode_enrichment`), and `web/` (the dashboard and its assets). Only
  cross-cutting runtime adapters stay at the root: `config`, `logging`,
  `run_log`, `workspace_context`, and `llm_clients`. `WikiConsolidator` moved
  to `application/consolidator.py`, where the layer rule puts it: it
  orchestrates over the `LLMClient` port and holds no SDK. `NodeExtractor`
  (spec §7 Utility 6) is removed — it was built, never wired into any shipped
  path, and its own docstring sent anything beyond simple cues to the
  consolidator. Two path couplings caught by tests during the move, recorded
  here because they will catch the next one: `packaged_marketplace()` resolved
  the marketplace directory by counting parents of `__file__` (it now anchors
  on the package root by name), and `pyproject.toml` pins both packaged assets
  and per-file lint ignores by full path.

- **Harness assets are package data.** `marketplace/` lives at
  `src/memex/marketplace/`, inside the package that reads it, because
  `memex install <harness>` needs it at runtime. That collapses what used to
  be three mechanisms into one path: the `force-include` build bridge, the
  wheel-versus-editable fork in `packaged_marketplace()`, and the
  current-directory fallback in `default_marketplace()` are all gone.
  `packaged_marketplace()` is removed; `default_marketplace(explicit)` is the
  only resolver, honouring `--from` and otherwise reading the copy inside the
  installed package. A checkout's `./marketplace` no longer shadows the
  installed assets, which is the correct behavior: installer files must match
  the installed version, and `--from` remains the explicit override.
  `pyproject.toml` now carries no `force-include` section at all — a wheel
  built without one was confirmed to contain every asset, so the two entries
  for `dashboard.css` and `htmx.min.js` were redundant too. Moving the
  assets under `src/` also brought the shipped harness scripts into `ruff`
  and `mypy`, which found and fixed two defects in the Codex wrapper.
