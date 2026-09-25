# Infrastructure guidance

Applies to `src/memex/infrastructure/`. Inherits the root `AGENTS.md`. Scope-specific deltas only.

- All I/O lives here: filesystem, SQLite, harness hooks, LLM clients, the
  dashboard. Read the docstrings and extend the owning module rather than
  adding a parallel one.
- Markdown under `~/.memex/docs/` is the source of truth; the SQLite index is
  disposable and must stay rebuildable from those files.
- This layer writes to the developer's real `~/.memex`. Exercise it only with an
  isolated `MEMEX_DATA_DIR=/tmp/...`.
- Never log memory contents, credentials, or tool inputs.

## Where a module belongs

| Package | Owns | Modules |
| --- | --- | --- |
| `store/` | The Markdown tree that is the source of truth | `wiki_store.py` (page CRUD; a project's type set is its directories — `declare_type`/`declared_types` read and write the declaration, and each declared directory's `log.md` is its append-only, body-only record), `navigation.py` (generated `index.md`), `navigation_search.py` (ranked recall over generated `index.md` rows; front matter only, never bodies), `watcher.py` (mtime polling), `backup.py` (archive), `import_export.py` (JSON transfer) |
| `search/` | The disposable SQLite index and everything read from it | `index_manager.py` (schema, upsert, rebuild), `bm25_retriever.py` (FTS5 ranking and visibility), `link_manager.py` (the `(source, target, rel)` graph) |
| `harness/` | Integration with coding agents | `installer.py` (hook and MCP registration), `transcripts.py` (harness-native transcript parsing), `transcript_hook.py` (capture, episode pages, provenance), `episode_enrichment.py` (runs a harness CLI for episode summaries) |
| `web/` | The read-only localhost dashboard | `server.py` (HTTP server and routes), `components.py` (shell, scope controls), `explorer.py` (selection, pagination), `sessions.py` (replay), `markdown.py` (body rendering), `assets/` (CSS and htmx) |
| root | Cross-cutting runtime concerns no package claims | `config.py`, `logging.py`, `run_log.py`, `workspace_context.py`, `llm_clients.py` |

A subpackage earns its place when its modules are used together and mostly by
each other. Do not create one for a single module — `llm_clients.py` stays at
the root for exactly that reason, being the only module that speaks to a model
SDK. Orchestration over these adapters belongs in `application/`, which is why
`WikiConsolidator` lives there and not here.

Assets live beside the code that reads them (`web/assets/`, and
`memex/marketplace/` for harness install files) and are loaded relative to the
package. Hatchling ships everything inside the package directory, so a new
asset needs no packaging entry — verified by building a wheel without one.
Resolve such paths by anchoring on the package root by name, never by counting
parents of `__file__`: a module that moves one level deeper would otherwise
break silently. `pyproject.toml` still pins per-file lint ignores by path, so
moving a module moves its ignore with it.
