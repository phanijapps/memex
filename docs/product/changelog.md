# Changelog

Notable user-visible changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Memex versions
through 0.2.4 are reconstructed from the version-bump commits; later work remains
unreleased until a release tag is created.

## [Unreleased]

### Added

- `memex types list|add|enable|remove|suggest` (ADR-0006): a project
  declares its own concept types alongside the built-in five. Catalogue
  types (`domain`, `architecture`, `rule`, `policy`, `decision`) are
  memex-defined with a contract and turned on with `memex types enable`;
  custom types are project-named shelves declared with `memex types add
  --description`. The directory is the declaration — a type exists for a
  project exactly when `projects/<project>/<type>/` exists — and its
  `log.md` records `declare`/`propose`/`remove` lifecycle lines. A model may
  nominate an undeclared type as a `pending`-only draft (`proposed_type` in
  consolidation output, or deterministic, LLM-free `memex types suggest`
  over recurring page tags); every recall path excludes it until a person
  accepts it. `memex verify` gains three checks: `types-match-directory`,
  `types-declared`, `draft-types-pending`. Export carries each project's
  enabled and declared types with descriptions; import declares them before
  writing pages and warns `types_skipped=N` when a project directory's id
  cannot be read.
- Link-graph expansion (OKF `read_concept`) in recall packing: `memex hook
  session-start` and task recall now append pages linked within one hop of
  the direct hits, in deterministic breadth-first order (alphabetical within
  a level), each as a short block naming the page, its depth, and the
  relation route (`+ Title (type) | depth: 1 | via: seed -mentions-> slug`),
  with title, file path, and description or short snippet only. Direct hits
  pack first exactly as before; linked pages fill only the budget they leave
  under the same 4,096-token bound, and the block ends with
  `[memex] linked pages omitted (token budget): N` when anything was cut.
  Depth is a keyword parameter (`build_injection(depth=...)`,
  `with_linked_pages(depth=...)`, default 1); no configuration
  key. Expansion reuses the retriever's visibility rule, so archived,
  expired, pending, and other-project pages are never expanded.
- `memex recall --engine {fts5,navigation}` and `Memex.recall(engine=...)`:
  the `navigation` engine ranks the generated `index.md` rows (titles and
  descriptions only, no body text, no SQLite) by weighted term overlap and
  reads only the front matter of the pages it returns; `search_engine`
  reports `navigation-index-md`. `time_range` is not supported by this
  engine.

### Removed

- `packaged_marketplace()` from the installer module. `default_marketplace()`
  is the single resolver: it honours an explicit `--from` path and otherwise
  reads the harness assets shipped inside the package. A checkout's
  `./marketplace` directory no longer overrides the installed copy.
- `NodeExtractor` (`memex.infrastructure.extractor`), the rule-based
  cue-phrase extractor from specification §7 Utility 6. It was never wired
  into capture, consolidation, or any command, so no shipped behavior changes.
  Extraction is `memex consolidate`, as the specification always intended for
  anything beyond simple cues.

### Changed

- **`type` is no longer a closed five-value enum.** CLI `--type`, MCP
  `type`/`node_type`, and Python accept any built-in, project-enabled
  catalogue, or project-declared custom type; an undeclared type is
  rejected before any file is written, on every surface, naming the
  `memex types` remedy. `[governance] knowledge_approval = "manual" |
  "auto"` (default `manual`) replaces `approval`: `episode` and `summary`
  stay `active` on write always, and every other type — built-in or
  not — lands `pending` when consolidation writes it and `active` when a
  person writes it, unless `knowledge_approval = "auto"`. A `memex.toml`
  carrying the retired `approval` key fails loading, naming the
  replacement. Catalogue pages are exempt from
  `RecencyDecay` — knowledge is refined or superseded, never aged out;
  built-in and custom pages still age.
  Consolidation writes knowledge into the project the source episode came
  from, and may write `decision` only where a project has enabled it.
- Harness install assets moved from the repository root to
  `src/memex/marketplace/`, inside the package that reads them at runtime.
  Wheels are unchanged — the files still land at `memex/marketplace` — and
  `pyproject.toml` no longer needs a `force-include` section.
- Infrastructure modules are grouped by concern: `infrastructure/store/`
  (Markdown persistence), `infrastructure/search/` (the disposable SQLite
  index), `infrastructure/harness/` (coding-agent integration), and
  `infrastructure/web/` (the local dashboard). `WikiConsolidator` moved to
  `memex.application.consolidator`. Import paths change for anyone importing
  these modules directly; the `Memex` facade, CLI, and MCP tools are
  unaffected.
- **Breaking — pages are OKF v0.2 concepts.** Front matter declares
  `okf_version: "0.2"` and carries the Open Knowledge Format field set first,
  in the order the OKF reference implementation writes it, followed by the
  Memex fields OKF has no equivalent for. Four names are retired with no
  alias: `updated` is now `timestamp` (refreshed on every write, with the new
  `updated_at` moving only when the body changes), `valid_to` is now
  `valid_until`, and `expires_at` is now `stale_after` — advisory only, never
  hiding a page from recall. `created` survives as a Memex field and is still
  written once. Recall visibility is the `valid_from`/`valid_until` window
  alone, so `forget --soft` ends that window now and `forget --decay` ends it
  one configured half-life out. Front-matter `links` are typed OKF relations
  (`--link target[:rel]`, MCP `"target:rel"`), joined in one link graph by the
  new `parent`, `supersedes`, `implements`, and `depends_on` fields; a body
  `[[slug]]` reference is a `mentions` edge and is no longer copied into front
  matter. CLI `--links` and `--expires-at` are gone, `--valid-to` is now
  `--valid-until`, and recall hits carry `timestamp`/`updated_at` in place of
  `updated`. Export JSON follows the page keys. `memex verify` gained five OKF
  conformance checks, and a generated store passes the OKF reference linter
  with zero errors and zero warnings. **There is no migration: delete an
  existing `~/.memex/docs` and `mem.db` before upgrading.** The SQLite schema
  is version 6 and rebuilds itself from Markdown.

- Task recall evidence pack widened from 8 to 36 distinct pages (12 hits per
  question) with the 4,096-token context budget unchanged; multi-part coding
  tasks now surface far more of their required evidence (goal-shaped
  benchmark 12/24 → 22/24 tasks). Descriptions are weighted like body text in
  BM25, and every write surface teaches purpose-style ("when is this page
  useful") description authoring.
- A single page write, update, or delete now splices that page's row into
  its directory `index.md` in sorted position instead of re-rendering the
  directory from a scan of every sibling page. Bytes stay identical to a full
  render. Per-write cost at 600 pages in one directory fell from 354 ms to
  14 ms (16 ms at 1000 pages) on the reference laptop. Ancestor indexes are
  rewritten only when a child directory gains or loses its last page. A
  missing, hand-written, or ambiguous index falls back to the full chain
  render; a legacy page at `index.md` stays untouched and reports a
  collision; `memex rebuild-index` still regenerates every index from a full
  scan.
- Recall with the default `fts5` engine now degrades instead of returning
  nothing when the SQLite index holds zero rows but the generated navigation
  lists pages: the answer comes from the navigation engine and
  `search_engine` reports `navigation-index-md-fallback`; one bounded log
  notice records the engine name and counts.
- Task-evidence benchmark re-pinned to measured values (rendered tokens
  16,240 / 81,005 / 61,386 / 4,749; completion and fact recall unchanged)
  with a 2026-09-23 amendment table in
  `docs/research/2026-09-20-task-evidence-baseline.md`.

## [memex][0.5.0] — 2026-09-21

### Added

- Searchable page descriptions: an optional single-line `description` (≤512
  UTF-8 bytes) on every write path (`--description` / MCP `description` /
  Python), searched at a neutral FTS weight, returned on recall hits, scrubbed
  for credentials, and preserved through edit, backup/restore, and
  export/import. Generated OKF-style `index.md` directory navigation lists
  titles and descriptions one directory at a time (root index declares
  `okf_version: "0.2"`); `index` and `log` are reserved slugs for new writes,
  pre-existing pages at those names are preserved, and stale SQLite indexes
  rebuild transparently at schema v5 without touching Markdown. A
  pre-description binary on a v5 store silently mis-ranks recall (old
  weights land on shifted FTS columns) and reports generated `index.md`
  files and `description` keys as malformed pages — delete `mem.db` and
  remove generated indexes before downgrading.
- Token-budgeted recall and hook injection, a weak-match injection floor, page
  status lifecycle, manual approval, provenance fields, write-boundary secret
  scrubbing, `memex status`, and zero-yield verification warnings.
- `memex viz`, an on-demand read-only localhost dashboard for pages, sessions,
  token use, and store health.
- Offline synthetic and realistic retrieval evaluation with Recall@K, MRR,
  precision, distractor, difficulty, and scale reporting.
- Deterministic episode summaries and optional harness-assisted episode
  enrichment.
- Optional project task recall through `memex recall --question` and the
  existing `memex_recall` MCP tool. It combines up to three caller-written
  questions into a bounded, source-linked context and names retrieval gaps.

### Changed

- MCP exposes five agent-facing tools. Transcript capture remains in harness
  hooks; manual transcript ingestion, transcript cleanup, import, and export
  remain on the CLI.
- MCP `memex_write` requires an explicit `global` or `project` scope. Omitted
  and invalid scopes are rejected before writing. Project IDs are still derived
  from the server workspace when omitted.
- Stale SQLite schema versions rebuild automatically from Markdown pages.
- Episode and capture metadata retain richer session, model, reasoning-effort,
  Git, and token-usage context when the harness provides it.
- Recall now uses the `semantic-and-fallback-fts5` SQLite FTS5 ranker, which
  passed repaired realistic, Gutenberg, and Salesforce quality gates while
  reducing hard-query context tokens and latency and keeping recall offline.

### Fixed

- Codex capture no longer re-ingests compacted replacement history or records
  enrichment subprocesses as new sessions.
- Claude Code capture now extracts session headers consistently.
- Repeated transcript capture merges later turns without duplicating earlier
  content.

## [memex][0.2.4] — 2026-09-15

### Changed

- Renamed user-facing "wiki" storage terminology to memory pages under
  `~/.memex/docs/`; `[pages]` is the primary config section and legacy `[wiki]`
  remains accepted.
- Corrected source-install and wheel-cache guidance.

## [memex][0.2.3] — 2026-09-15

### Fixed

- The Codex wrapper resolves rollout locations through `CODEX_HOME` and the
  thread store instead of assuming one filesystem layout.

## [memex][0.2.2] — 2026-09-15

### Changed

- Moved session and per-turn token counts from transcript JSONL into the
  transcript metadata sidecar.

## [memex][0.2.1] — 2026-09-15

### Fixed

- Updated the Codex notification wrapper to consume the actual hook event
  schema and log unsupported event categories without recording contents.

## [memex][0.2.0] — 2026-09-15

### Added

- Hardened Codex transcript capture for turn completion, compaction, shutdown,
  tool turns, idempotent merging, and bounded diagnostics.

### Changed

- Moved authoritative memory pages from `~/.memex/wiki/` to
  `~/.memex/docs/`, with in-place migration and legacy-backup restore support.

## [memex][0.1.4] — 2026-09-15

### Fixed

- Kept the Codex `notify` setting in the root config table during installation
  and repaired previously nested placement.

## [memex][0.1.3] — 2026-09-15

### Added

- Transcript session headers with Codex identity, Git, model,
  reasoning-effort, and token-usage metadata.

## [memex][0.1.2] — 2026-09-15

### Added

- Optional MCP registration during harness installation.

## [memex][0.1.1] — 2026-09-15

### Fixed

- Bumped the package version so source reinstalls do not receive a stale cached
  wheel.

## [memex][0.1.0] — 2026-09-15

### Added

- Initial local-first Markdown memory store, SQLite FTS5 retrieval, Python API,
  CLI, MCP tools, transcript hooks, consolidation, backup/restore, harness
  installation, and CI verification.
