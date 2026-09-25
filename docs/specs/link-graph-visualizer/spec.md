# Spec: Link graph visualizer

- **Status:** Draft
- **Owner:** phanijapps
- **Plan:** [`plan.md`](plan.md) (authored on approval)
- **Constrained by:** ADR-0001, ADR-0004, `memory-governance` (the write verbs)
- **Brief:** none
- **Discovery:** none
- **Contract:** dashboard routes `/graph`, `/graph.json`, and the governance write routes `/govern/*` (new)
- **Shape:** ui

> **Spec contract:** this document defines what "done" means. The implementing
> PR must match this spec, or update it. Verification must be derivable from it.

## Objective

The local dashboard shows the memory store as a graph: pages are nodes, typed
relations are edges, and a person can see at a glance which pages are hubs,
which are orphans, what depends on what, and where a project's knowledge
clusters. The graph already exists — every `[[mention]]`, typed link,
`parent`, `supersedes`, `implements`, and `depends_on` is a row in
`wiki_links` with its `rel` — but today it is visible only as a flat
`Links:` line per page. A D3 force-directed view on `memex viz` renders it,
filtered by scope, project, type, and relation, and clicking a node opens the
existing page view. Draft types (`memory-governance`) appear as dashed
clusters, and the graph is where a person approves, renames, or merges them —
the dashboard's first and only write capability, limited to those governance
verbs and guarded by a per-launch token. Everything else stays read-only and
on localhost.

## Durable Outputs

| Semantic role | Applicability | Destination | Owner | Expected evidence | Closeout condition |
| --- | --- | --- | --- | --- | --- |
| Dependency admission | Applicable — D3 is a vendored third-party asset, and the repository has no third-party notice at all today (the htmx admission in `docs/specs/memex-viz/spec.md` recorded size and origin but not its license) | New root `THIRD_PARTY_NOTICES.md` listing every vendored asset: D3 (ISC) and, backfilled, htmx | author | Per asset: name, version, license, upstream URL, SHA-256, size, purpose | Notice present; `AGENTS.md`'s "every dependency needs a clear purpose and license check" rule is satisfied for both assets |
| Current product truth | Applicable — new dashboard view | `docs/gitpages/guide.md` (viz section) | author | What the view shows, the filters, and the node limit | `mkdocs build --strict` passes |
| Current architecture | Applicable — new route pair and asset | `docs/architecture/overview.md` (dashboard paragraph) | author | Names the JSON endpoint as the only data seam | Paragraph updated |
| Release history | Applicable | `docs/product/changelog.md` | author | Unreleased "Added" entry | Entry present |

## Boundaries

### Always do

- Serve D3 from a vendored file under `web/assets/`, like `htmx.min.js`;
  never from a CDN. The dashboard makes no network requests.
- Keep the JSON payload minimal: slug, title, type, scope, project label,
  status per node; source, target, rel per edge. No bodies, no descriptions,
  no file paths, no absolute paths.
- Escape every title before it reaches HTML or SVG text; treat stored memory
  as untrusted.
- Read through the retriever's non-mutating path and the link manager; a
  graph render never changes access counts or any page.
- Bound the payload: a node limit with an explicit notice when it is hit.

### Ask first

- Any write action beyond the governance verbs (creating a link by drag,
  editing a rel, editing a page).
- Rendering descriptions or snippets inside the graph itself rather than in
  the existing page view.
- Any layout that depends on wall-clock randomness in a way tests cannot pin.

### Never do

- Load scripts, fonts, or styles from outside the package.
- Expose the graph outside localhost or without the existing dashboard's
  guards.
- Include archived, expired, or other-project pages when the corresponding
  filter is off; the graph obeys the same visibility rules recall does.
- Accept a governance write without the per-launch token, from a non-loopback
  address, or through any route other than `/govern/*`; and never implement
  a verb in the dashboard — every route calls the same facade function the
  CLI does.

## Testing Strategy

- **`/graph.json` contract** (shape, filters, escaping, node limit, visibility
  rules, no bodies or paths): TDD against the route handler with a seeded
  store.
- **Asset serving and shell:** goal-based — `/graph` returns the shell with the
  vendored script reference and no inline memory content; `/static/d3.min.js`
  returns the bytes with the right content type and the recorded SHA-256.
- **Deterministic layout:** TDD on the seed — the simulation is initialised
  with a fixed pseudo-random source so two renders of one store produce the
  same node positions (checked through the exported positions endpoint or a
  headless run, whichever the plan selects).
- **Visual QA:** a recorded screenshot of a 50-page store with three
  relation types, plus the click-through to a page view.

## Acceptance Criteria

- [ ] **AC-0001.** `GET /graph.json` returns `{"nodes": [...], "edges": [...],
      "truncated": false}` where each node carries exactly `slug`, `title`,
      `type`, `scope`, `project_label`, `status`, and each edge exactly
      `source`, `target`, `rel`; no other keys appear.
- [ ] **AC-0002.** `GET /graph.json` never includes a page body, description,
      file path, or absolute path in any field, for any store.
- [ ] **AC-0003.** Query parameters `scope`, `project`, `type`, and `rel`
      filter nodes and edges; an edge appears only when both endpoints are in
      the node set.
- [ ] **AC-0004.** With no `include_inactive` parameter, archived, pending,
      and superseded pages and pages outside their validity window are
      absent, matching recall's rule; `include_inactive=1` includes them with
      their `status` set.
- [ ] **AC-0005.** The response holds at most 2,000 nodes; when the store has
      more, `truncated` is `true` and the nodes kept are the 2,000 with the
      highest edge degree, ties by slug.
- [ ] **AC-0006.** `GET /graph` returns the dashboard shell with a
      `<script src="/static/d3.min.js">` reference and no memory content;
      `GET /static/d3.min.js` returns the vendored bytes as
      `application/javascript`, and their SHA-256 equals the value recorded
      in the dependency-admission note.
- [ ] **AC-0007.** Node colour encodes `type` and edge colour encodes `rel`,
      each with a legend; hovering a node highlights its edges and
      neighbours; clicking a node navigates to the existing `/page/<slug>`
      view for that scope and project.
- [ ] **AC-0008.** A `focus=<slug>` parameter centres the graph on that page
      and limits it to nodes within `depth` hops (default 2), reusing the
      breadth-first order of `graph_expansion`.
- [ ] **AC-0009.** Every title rendered into SVG or HTML passes through the
      dashboard's existing escaping; a title containing `<script>` renders as
      text.
- [ ] **AC-0010.** Rendering the graph changes no `access_count`,
      `last_access`, page, or index row.
- [ ] **AC-0011.** Two renders of the same store with the same filters yield
      identical node positions (fixed pseudo-random source).
- [ ] **AC-0012.** Draft types render as dashed clusters labelled `draft`,
      with their pending pages inside; no `include_inactive` flag is needed to
      see them, because a draft is governance state, not memory state.
- [ ] **AC-0013.** Selecting a draft cluster offers exactly three actions —
      approve, rename, merge into — each posting to `/govern/*` with the
      per-launch token; the route calls `Memex.approve_type`,
      `Memex.rename_type`, or `Memex.merge_type` and returns the refreshed
      graph.
- [ ] **AC-0014.** A `/govern/*` request without the token, or from a
      non-loopback address, is refused with no state change; the token is
      generated per `memex viz` launch, printed once to the terminal, and
      never written to disk.
- [ ] **AC-0015.** The guide, architecture overview, changelog, and the
      third-party notice describe the view, the governance actions, and the
      vendored asset, and `mkdocs build --strict` passes.

## Follow-ons

- author: `docs/backlog/` — time slider over `timestamp` to watch the graph
  grow, once the static view has been used.
- author: `docs/backlog/` — export the current view as SVG.

## Assumptions

- Technical: the dashboard already vendors a script asset (`web/assets/htmx.min.js`,
  50,918 bytes) and serves it from `do_GET` (`src/memex/infrastructure/web/server.py`
  lines 38–94, probe 2026-09-23); D3 follows the same path.
- Technical: `wiki_links` rows carry `(source_scope, source_project_id,
  source_slug, target_slug, rel)` and `LinkManager.get_link_graph` exists
  (`src/memex/infrastructure/search/link_manager.py`); the JSON endpoint is a
  projection of those rows joined to `wiki_index` for title, type, and status.
- Technical: D3 v7 is ISC-licensed; the minified bundle is roughly 280 KB,
  about five times `htmx.min.js`. Acceptable for a localhost tool; recorded
  in the admission note.
- Technical: `graph_expansion.expand_links` already provides deterministic
  BFS from a seed, which `focus=` reuses rather than re-implementing.
- Product: the dashboard's read-only charter (`docs/specs/modern-htmx-dashboard/spec.md`)
  is amended by this spec to admit governance verbs only — agreed in the
  2026-09-23 brainstorm: governance is what a human-facing surface is for,
  while agents live on the CLI and hooks.

### Open decisions to settle before approval

1. Resolved during drafting: no vendored-asset license is recorded anywhere
   in the repository, so this spec creates root `THIRD_PARTY_NOTICES.md` and
   backfills `htmx.min.js` alongside D3. The htmx license name is read from
   the vendored file's header during the plan, not assumed.
2. Whether the graph page is a new top-level dashboard tab or a mode of the
   existing memory explorer.
