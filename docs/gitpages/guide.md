# Memex user guide

Everything you need to run memex for yourself, your agent, or your team.

- [Concepts](#concepts)
- [Getting started](#getting-started)
- [Operations](#operations)
- [Transcripts and provenance](#transcripts-and-provenance)
- [Harness integration](#harness-integration)
- [Deterministic checks (CI)](#deterministic-checks-ci)
- [Data portability](#data-portability)
- [Configuration reference](#configuration-reference)
- [Data safety](#data-safety)

## Concepts

**The memory store is the filesystem.** Every memory is a Markdown page under
`~/.memex/docs/`, with YAML front matter and a Markdown body. Pages are
human-readable, git-able, and editable by hand in any editor. If you edit a
page externally, `memex rebuild-index` (or `memex watch`) picks it up.

```
~/.memex/
├── docs/
│   ├── global/          # shared memories, grouped by type
│   │   ├── entities/
│   │   ├── preferences/
│   │   ├── procedures/
│   │   ├── summaries/
│   │   └── episodes/
│   └── projects/        # project memories, grouped by readable project folder
│       └── git-memex/
│           ├── preferences/
│           └── episodes/
├── transcripts/         # raw session JSONL + metadata
├── mem.db               # disposable BM25 index (rebuildable)
├── memex.toml           # configuration
└── logs/                # operation audit trail (no memory contents)
```

**Node types.**

| Type | Holds | Example |
|---|---|---|
| `entity` | People, tools, concepts | "Ruff linter" |
| `preference` | Durable user preferences | "Prefer dark mode" |
| `procedure` | Rules, how-tos, constraints | "Never force-push main" |
| `summary` | Synthesized overviews | "Tooling decisions, Sept 2026" |
| `episode` | One captured session | "Session sess-abc123" |

**Links.** Reference other pages in any body with `[[slug]]` links
(`[[Ruff Linter]]` normalizes to `[[ruff-linter]]`). Links are indexed both
directions — backlinks answer "what mentions this?".

**The index is disposable.** `mem.db` mirrors the pages for fast SQLite FTS5
search and freshness tracking. It is never the source of truth:

```bash
rm ~/.memex/mem.db
memex rebuild-index        # fully rebuilt from the memory files
```

**Temporal validity.** Every node optionally carries `expires_at`,
`valid_from`, and `valid_to`. Expired or retired nodes are hidden from
recall by default (`--include-expired` / `include_expired=True` opts back in).

## Getting started

Install the CLI directly from GitHub, then connect it to a coding agent from
your project directory:

```bash
uv tool install git+https://github.com/phanijapps/memex.git
memex install claude          # or codex, pi, copilot

memex write --type preference --title "Deploy on Fridays" \
    --body "The team deploys to production on Fridays only." --tags deploy
memex recall "deploy"
cat ~/.memex/docs/global/preferences/deploy-on-fridays.md
```

Run `memex install` without a name to choose a harness interactively. For
source development, clone the repository and run `uv sync --all-groups`;
`uv tool install . --force` installs that checkout as the CLI. If you rebuild
at the same version after changing source files, add `--no-cache` because uv
may reuse the earlier wheel.

Version-control your memory if you like:

```bash
cd ~/.memex/docs && git init && git add -A && git commit -m "memory: initial"
```

## Operations

### write

```bash
memex write --type entity --title "Ruff linter" \
    --body "Fast Python linter written in Rust. See also [[python-3-12]]." \
    --tags tool,lint --importance 0.8 --links python-3-12
```

- Slugs derive from titles (`Ruff linter` → `ruff-linter`), collisions get
  `-2`, `-3` suffixes.
- Writing an existing slug **updates** it, preserving `id`, `created`, and
  access counters.
- `importance` ∈ [0, 1]; `[[slug]]` links in the body are indexed as edges.

### recall

```bash
memex recall "deploy" --top-k 5
memex recall "linting" --type preference --tag tooling
```

- Local SQLite FTS5 over slug, title, body, and tags. Recall reduces the query
  to safe alphanumeric tokens, removes known scaffolding phrases, then runs the
  production `semantic-and-fallback-fts5` ranker: strict `AND` matching first,
  body weighted 2x, and broad `OR` fallback only when strict matching has no
  hits.
- Results are deterministic and ranked best-first with short
  `<mark>`-highlighted snippets. Filters apply before limiting, returned slugs
  are unique, and ascending slug is the final tie-break.
- Filters: `--type`, `--tag` (AND semantics), `--top-k` (1–100),
  `--include-expired`.
- Every hit bumps its access counter — recall telemetry feeds
  [recency decay](#configuration-reference) and `verify` evidence.
- Recall stays offline and dependency-light: no embeddings, hosted search,
  runtime `rgapi`, or `rg` executable is required.

### forget

```bash
memex forget deploy-on-fridays               # hard: file deleted, irreversible
memex forget deploy-on-fridays --mode soft   # valid_to=now; hidden from recall
memex forget deploy-on-fridays --mode decay --valid-to 2027-01-01T00:00:00Z
```

| Mode | Effect |
|---|---|
| `hard` | Deletes the page, its index row, and its links — irreversible |
| `soft` | Sets `valid_to`; page stays, hidden from recall by default |
| `decay` | Sets `expires_at`; naturally excluded once past |

### consolidate

The only LLM-calling operation, and only on explicit request. It reads recent
episode nodes and proposes durable entity/preference/procedure/summary nodes
(the prompt and rules live in the specification, §11).

```bash
memex consolidate --mode dry-run     # propose, write nothing
memex consolidate --max-episodes 10  # distill and write
```

Requires LLM credentials (`MEMEX_API_KEY` or `[llm]` in `memex.toml`). Works
with any OpenAI-compatible endpoint — OpenAI, Ollama, LM Studio, OpenRouter
— via one `openai`-SDK client pointed at the configured base URL.

### Token budgets and injection floor

Recall and hook injection pack to a token budget (`--max-tokens`, default
4096): page text counts against the budget, metadata is free, a hit that
does not fit is skipped in favor of smaller ones, and the top hit is always
returned whole. Hook injection also stays silent when the best match ranks
below the floor — weak matches inject nothing rather than noise. Every
injected block opens with the three-line memory constitution.

### Page status and approval

Pages carry `status: active | pending | superseded | archived`. Recall and
injection see `active` pages only (pass `--include-inactive` /
`include_inactive` to see the rest). `memex forget <slug> --mode archive`
retires in place instead of deleting. `memex merge <target> <source>`
appends the source body into the target and marks the source superseded
with a backlink. With `[governance] approval = "manual"` in `memex.toml`,
consolidation-created pages land `pending`; `memex approve <slug>` makes
them recallable. `memex status` reports index freshness, last capture per
harness, pending/archived counts, and consecutive zero-yield
consolidations (memex verify warns on a streak of three).

### Secret scrubbing

Every write boundary — CLI, MCP, transcript ingest, consolidation —
redacts a catalog of credential patterns (API keys, tokens, private keys,
database URLs, JWTs) before anything is stored, replacing matches with
typed `[REDACTED:<kind>]` markers. Redaction categories are logged; matched
text never is.

### Project memory and dashboard

The durable layout separates global pages at `docs/global/<type>/` from project
pages at `docs/projects/<project-folder>/<type>/`. The project folder is a
readable locator, not the project identity. When memex can read a usable Git
origin, including an enterprise or self-hosted GitLab remote, it uses the
repository basename with a `git-` prefix, such as
`docs/projects/git-memex/preferences/example.md`. A local Git repository
without a usable origin and a non-Git workspace use the workspace folder name,
such as `docs/projects/memex/preferences/example.md`.

Project front matter remains the authority for the opaque `project_id` and safe
display label. Use `--scope project --project-id <id> --project-label <name>`
when writing, then use the same scope and id to recall only that project.
For agent-initiated writes, choose project scope for workspace architecture,
conventions, and decisions; choose global scope for facts intended across
projects. If unclear, choose project. MCP writes require an explicit `scope`;
the server rejects omitted or invalid values before saving a page. The CLI and
MCP tool can derive a project identity when project scope is selected without
an explicit ID, using the working directory of the CLI or MCP server process
respectively.
Omitting project selectors recalls across all memory. Explicit `project_id`
values stay supported: if that project already has pages, writes continue in
its existing directory; otherwise memex uses an ID-named directory unless the
caller supplies an explicit validated folder locator.

Legacy ID-named project folders remain readable and rebuildable. Normal runtime
operations do not rename, move, or merge them; an existing store move is a
separate maintenance procedure with preview, backup, collision review, and an
operation-specific confirmation. If two folders contain the same
`project_id`, node type, and slug, lookup and force rebuild fail closed instead
of choosing one page.

`memex viz` starts a localhost-only, read-only dashboard. It provides direct
links and HTMX-enhanced navigation for global and project memory, health,
sessions, and token use. The dashboard does not write pages or transcript data.

![Memex Memories view with global and project pages](assets/dashboard-memories.png)

The screenshot uses sample data. Start with the
[dashboard overview](assets/dashboard-overview.png) for counts, search, and
recent memories.

The Memories view shows 20 newest-first cards per page and preserves type
and scope in direct Previous/Next URLs. Selecting a new type or scope starts on
page one. “All memory” browses every namespace; “Global only” browses global
pages; a project label browses that project. Dashboard search uses BM25 without
updating access counters: “Best match” searches the chosen project first and
falls back to all memory, while “Search everywhere” searches all namespaces.
The Sessions view groups captured sessions by date, project label when known,
and harness. A session replay shows separate User, AI assistant, and Tool cards;
tool input and output have bounded previews with expandable detail and a
truncation notice for longer values.

### Maintenance

```bash
memex rebuild-index --force    # full re-index from the memory files
memex watch                    # poll for hand-edited pages and re-index
memex info                     # counts, index state, last rebuild
```

## Transcripts and provenance

Store a session transcript and memex links it to an episode node — the
foundation for tracing any memory back to the conversation that produced it.

Transcript JSONL and metadata use dated directories under
`~/.memex/transcripts/YYYY-MM-DD/`. `memex clear-transcripts --confirm` removes
raw transcript files and retires their episode transcript links; it is an
explicit lifecycle operation, not part of dashboard browsing.

```bash
memex ingest-transcript --session-id sess-abc --turns-file turns.jsonl
```

`turns.jsonl` — one JSON object per turn:

```json
{"role": "user", "content": "I prefer ruff over flake8", "ts": "2026-09-15T10:00:00Z", "turn": 1}
{"role": "agent", "content": "Got it.", "ts": "2026-09-15T10:00:01Z", "turn": 2}
{"role": "tool", "tool_name": "bash", "result": "ruff installed", "ts": "2026-09-15T10:00:02Z", "turn": 3}
```

The first transcript line is an optional session header
(`type: memex_session_header`) carrying identity — session id, CLI
version, provider, cwd, git branch/commit, models and reasoning
efforts used, timestamps, duration. Token counts are metadata, not
transcript content: the `{session_id}.meta.json` sidecar carries
session totals (`token_usage`) and per-turn usage
(`turn_token_usage`), always the latest reported values and never
summed across cumulative records. Older turn-only transcripts remain
readable; repeated captures rewrite the sidecar with current totals.

Ingestion writes `transcripts/YYYY-MM-DD/sess-abc.jsonl` + `.meta.json`, creates
`docs/global/episodes/sess-abc.md` with a `transcript_ref`, and indexes it. From
Python or MCP, `get_provenance(slug)` / `memex_provenance` reports how a
node traces back: **direct** (it has a transcript), **inferred** (an episode
links to it), or **none**.

Harness adapters capture transcripts automatically — see below.

## Harness integration

MCP tools alone depend on the model choosing to call them. Memex adds a
deterministic push layer and a verifiable proof layer:

```
L3  PROOF    memex verify (CI / pre-commit)   exit code fails the build
L2  PUSH     memex hook <event>               context injection + capture
L1  PULL     memex serve-mcp                  five typed tools
```

### The hook contract

```
memex hook session-start [--query Q] [--top-k N]
    stdout: a memory context block (spec §5.4) or nothing; exit 0 either way
memex hook prompt [--prompt TEXT | stdin] [--top-k N]
    stdin: raw text, or a hook JSON payload with a "prompt" key
memex hook transcript --harness H [--path FILE]
    ingests a harness-native session file; idempotent; --path may instead
    arrive as transcript_path in stdin JSON
```

### Adapters

Install any of them with `memex install <name>` — the marketplace ships
inside the package, so no source checkout is needed (`memex install`
with no argument opens an interactive picker; installs are idempotent
and back up existing configs). `memex install custom` initializes
`~/.memex` only: directory tree plus a starter `memex.toml` with plain
LLM config, for harnesses memex doesn't know yet.

Installs register the MCP server where the harness supports it: Claude
Code via `claude mcp add --scope user` (when the CLI is available — the
note tells you the exact command otherwise), Codex via `[mcp_servers]`
in `config.toml`, and Copilot via `.vscode/mcp.json` for VS Code agent
mode. pi intentionally has no built-in MCP; its extension is the
integration. `--no-mcp` skips registration everywhere.

From the project where it was installed, remove one adapter with
`memex uninstall <name>` (or `memex harness uninstall <name>`). This removes
Memex's hooks, MCP registration, copied adapter files,
and exact guidance snippets for that harness. Unrelated settings and modified
adapter files are left in place and reported. The command keeps `~/.memex`
memories, transcripts, and configuration; run it once per installed harness.
After removing adapters, `uv tool uninstall memex` removes the CLI. Your
`~/.memex/` data remains until you remove it separately.

Installing `claude`, `codex`, or `pi` also provisions `memex.toml`
(absent one) with `[consolidation] provider = "<harness>"` — so
distillation rides the coding harness's own model, credentials, and
billing via its CLI print mode, with no separate API key.

**pi** — the reference adapter. A TypeScript extension injects repo-level
memories on the first turn and prompt-relevant memories on every turn, and
includes scoped-write guidance on the first turn even if recall is empty. It
captures the session file on shutdown. Knobs: `MEMEX_BIN`, `MEMEX_TOP_K`,
`MEMEX_DISABLE`.

**Claude Code** — hooks in `settings.json`: SessionStart and
UserPromptSubmit inject context (hook stdout becomes context); SessionEnd
ingests the session transcript. The installer adds scoped-write guidance to the
project's `CLAUDE.md`. MCP: `claude mcp add memex -- memex serve-mcp`.

**Codex** — no native injection point, so: an AGENTS.md memory contract
(recall at task start, write durable facts), MCP via `config.toml`, and a
`notify` wrapper that ingests each rollout on `agent-turn-complete`.

**GitHub Copilot (hosted)** — no hooks, no local stdio: the deterministic
layer carries it. The adapter installs a memory contract into
`.github/copilot-instructions.md` and a `memex verify` workflow on every PR.

### Automatic consolidation at session end

Capture and distillation can be one step. Any harness hook that ingests a
transcript can distill the fresh episode immediately — enabled per call with
`--consolidate`, or globally with `MEMEX_AUTO_CONSOLIDATE=1` (works for the
pi, Claude Code, and Codex adapters unchanged, since they all invoke the
same hook). Off by default: it spends tokens and needs credentials. Point
`[consolidation]` at a low-effort model — a local Ollama model, a
mini-tier endpoint, or a coding harness itself (`provider = "codex"`)
so distillation rides the same model your agent already uses. Failure never blocks the hook — a missing key
reports the reason, an unreachable model returns an empty consolidation
result.

```bash
memex hook transcript --harness pi --path <session.jsonl> --consolidate
```

### MCP tools

`memex serve-mcp` exposes five tools with typed schemas (enums and bounds
in `inputSchema`, documented `{"error": ...}` result convention):

`memex_write`, `memex_recall`, `memex_consolidate`, `memex_forget`,
`memex_provenance`. Transcript capture uses harness hooks; manual ingestion,
transcript cleanup, import, and export remain CLI commands.

Schema violations are rejected by the server with a field-precise error;
domain rejections return sanitized error data. Tool descriptions are
call-time contracts authored in `memex.domain.operations` — the same
registry the CLI help uses.

## Deterministic checks (CI)

```bash
memex verify                                # health only
memex verify --since 2026-09-15T00:00:00Z --require-recall --require-write
```

Always checked: every page parses; the index matches content hashes;
every link resolves. With `--since`, memex additionally reports recall
activity (access telemetry) and write activity (updated timestamps) since
the cutoff; `--require-*` turns missing evidence into exit code 1. The
Copilot adapter ships a ready-made workflow (`marketplace/copilot/
memex-verify.yml`).

## Data portability

```bash
memex backup --output memex-backup.tar.gz   # pages + transcripts + mem.db snapshot
memex verify memex-backup.tar.gz 2>/dev/null || true   # (verification is built into restore)
memex restore --input memex-backup.tar.gz   # validates members, moves old data aside, rebuilds index
memex export --output nodes.json            # JSON node document
memex import --input nodes.json             # invalid entries skipped and reported
```

Archives are validated against path traversal and symlinks before
extraction; restores never delete your current data (moved to
`pre-restore-<timestamp>/`).

## Configuration reference

`~/.memex/memex.toml` — every section optional, defaults shown:

```toml
[app]
name = "memex"

[llm]
provider = "openai"        # openai | ollama | lmstudio | openrouter | custom
model = "gpt-4o"
# api_base = "http://localhost:11434/v1"   # per-provider default otherwise
# api_key — prefer the MEMEX_API_KEY env var
timeout = 60
max_tokens = 4096

[consolidation]
# Optional: distill episodes on a cheaper low-effort model.
# Every field falls back to [llm] when unset.
provider = "openai"        # openai | ollama | lmstudio | openrouter | custom
                           # ...or a coding harness: "claude" | "codex" | "pi"
model = "gpt-4o-mini"      # harness providers: passed as the CLI's --model

[bm25]
default_top_k = 10         # k1/b are reserved: SQLite FTS5 bm25() is not SQL-tunable

[recency_decay]
enabled = true
half_life_days = 30        # importance halves per N idle days (explicit apply only)

[index]
watch_poll_interval = 60   # 0 disables
auto_rebuild_on_startup = false

[pages]
default_importance = 0.5
max_body_chars = 50000
slug_algo = "kebab"        # kebab | sha1

[logging]
level = "INFO"             # DEBUG | INFO | WARNING | ERROR
# file = "~/.memex/logs/memex.log"
```

Environment overrides (highest priority): `MEMEX_DATA_DIR`, `MEMEX_API_KEY`,
`MEMEX_LLM_PROVIDER`, `MEMEX_LLM_MODEL`, `MEMEX_LOG_LEVEL`, and for the
distillation model `MEMEX_CONSOLIDATE_PROVIDER`, `MEMEX_CONSOLIDATE_MODEL`,
`MEMEX_CONSOLIDATE_API_KEY`, `MEMEX_AUTO_CONSOLIDATE`.

## Data safety

- **Logs never contain memory contents.** The audit trail records
  operations, slugs, and counts — nothing else.
- **Stored memories and tool inputs are untrusted by design.** Front matter,
  transcripts, archives, and LLM output are validated at every boundary;
  consolidation output is schema-checked before any write.
- **MCP tool errors are sanitized** — no paths, memory text, or provider
  details cross the stdio boundary.
- **API keys** belong in `MEMEX_API_KEY`, not in `memex.toml`.
- **Nothing leaves the machine.** The only network call memex ever makes is
  the explicit `consolidate` operation, against the endpoint you configure.
