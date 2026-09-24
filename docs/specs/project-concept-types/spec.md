# Spec: Project-level concept types

- **Status:** Shipped
- **Owner:** phanijapps
- **Plan:** [`plan.md`](plan.md) (authored on approval)
- **Constrained by:** ADR-0001, ADR-0004, ADR-0005
- **Brief:** none
- **Discovery:** brainstorm 2026-09-23 (this spec, `memory-governance`, `link-graph-visualizer`, `git-backed-memory` were decomposed from one conversation)
- **Contract:** the page `type` field, the type directory layout (`docs/gitpages/spec.md` §6.1), and the new `memex types` command
- **Shape:** data

> **Spec contract:** this document defines what "done" means. The implementing
> PR must match this spec, or update it. Verification must be derivable from it.

## Objective

Memex grows from a memory of what happened into a store of what is known, and
the concept vocabulary grows with it. Today every page is one of five types
pinned to five directories, so a project's *decisions*, *policies*, *domain
model*, and *access matrix* all end up filed as "entity". OKF v0.2 already
lets `type` be any non-empty string. This spec gives that freedom a shape:
three kinds of type, a contract that says how each behaves, and a path for
concepts the model discovers to become real only when a person says so.

| Kind | Who defines it | What it carries | Examples |
| --- | --- | --- | --- |
| Built-in | memex | behaviour: capture writes `episode`, consolidation writes `summary` | the current five |
| Catalogue | memex; a project enables it | a contract: authorship, aging, supersession | `domain`, `architecture`, `rule`, `policy`, `decision` |
| Custom | a project declares it, or the model nominates it as a draft | a shelf and a heading; may later map onto a catalogue contract | `access-matrix`, `story-map` |

Two lines follow from memory versus knowledge:

- **Memory decays, knowledge does not.** A policy nobody recalled for six
  months is not less true. Catalogue types are exempt from recency decay and
  change only by supersession.
- **Memory is automatic, knowledge is governed.** `episode` and `summary`
  are active on write. Every other type is active when a person writes it and
  pending when a model does, unless the store's owner sets
  `knowledge_approval = "auto"` (`memory-governance`).

## Durable Outputs

| Semantic role | Applicability | Destination | Owner | Expected evidence | Closeout condition |
| --- | --- | --- | --- | --- | --- |
| Decision rationale | Applicable — a type vocabulary with contracts is durable representation | `docs/adr/0006-three-kinds-of-concept-type.md` | author | Alternatives (free strings only; catalogue only; config-file registry) and the accepted trade-off | Referenced from this header |
| Current product truth | Applicable | `docs/gitpages/guide.md` (`memex types`, catalogue table, draft types) | author | Guide shows declaring, enabling, nominating, and writing to a type | `mkdocs build --strict` passes |
| Current architecture | Applicable — type validation moves from the domain enum to the store; decay gains a rule | `docs/architecture/overview.md` | author | Names where a type is validated, the contract table, the decay exemption | Section names all three |
| Release history | Applicable — MCP `type` loses its closed enum | `docs/product/changelog.md` | author | Unreleased entry | Entry present |
| Interface compatibility | Applicable — MCP schema, CLI choices, export format | `docs/gitpages/guide.md`, MCP docstrings | author | `memex_write` accepts any enabled or declared type; export carries type declarations | Documented |

## Boundaries

### Always do

- Keep the five built-in types, their directories, and their behaviour
  unchanged.
- Make the directory the declaration: a type exists for a project exactly when
  `projects/<project>/<type>/` exists. The filesystem stays the source of
  truth and an OKF reader sees ordinary concept directories.
- Keep the type name and directory name identical for catalogue and custom
  types (`[a-z][a-z0-9-]{0,63}`); no plural mapping.
- Validate `type` at the write boundary against built-in ∪ enabled catalogue
  ∪ declared custom for that scope and project; reject anything else with an
  error naming the `memex types` remedy.
- Treat a draft type as invisible to agents: its pages are `status: pending`,
  which every recall path already excludes.
- Exempt catalogue-type pages from `RecencyDecay`.

### Ask first

- Catalogue or custom types at global scope. This spec is project-only.
- Consolidation writing directly to catalogue types other than `decision`.
- A body schema per catalogue type (level 3). This spec stops at the contract
  (level 2).

### Never do

- Accept an undeclared `type` on any surface (CLI, MCP, Python, import).
- Let a model create anything other than a draft; declaration, enabling, and
  approval are governed acts (see `memory-governance`).
- Store type declarations anywhere but the project's docs tree.
- Break reading of any page written today.

## The catalogue

| Type | Answers | Authorship | Model-written status (default) | Aging | Supersession |
| --- | --- | --- | --- | --- | --- |
| `domain` | What exists in the business and what its words mean | human, consolidation | pending | never decays | refined in place; `parent` for decomposition |
| `architecture` | How the system is put together and why | human | pending | never decays | `supersedes` on redesign; `implements` a decision; `depends_on` |
| `rule` | A constraint that can be checked | human | pending | never decays; `valid_until` when withdrawn | `supersedes`; `implements` a policy |
| `policy` | A principle that guides judgment | human | pending | never decays; `valid_until` when withdrawn | `supersedes`; `depends_on` |
| `decision` | What was chosen, when, and why | human, consolidation | pending | fixed at the moment made | only ever `supersedes`; `implements`, `depends_on` |

`rule` and `policy` are distinct on purpose: a rule is mechanically checkable,
a policy needs judgment. `decision` is the one type consolidation may write, because "we chose X over Y because Z" is what a transcript contains; being knowledge written by a model, it lands as `status: pending`.

## Draft types

A type the model discovers is a **draft**: the directory exists, every page in
it is `status: pending`, and the directory's `log.md` (OKF's reserved scoped
history, which memex today never writes) records who proposed it, when, from
which sessions, and how many pages. Nominations come from two places:

- consolidation, through one optional `proposed_type` per node, constrained to
  the name shape and rejected on collision with a built-in or catalogue name;
- `memex types suggest`, deterministic and model-free: tags carried by at
  least `min_pages` pages that are not already types.

Approval, rename, and merge of a draft are governance verbs and live in
`memory-governance`.

## Testing Strategy

- **Name validation, directory mapping, contract lookup:** TDD, pure.
- **Declare / enable / write / read / list / rename / remove round-trips:**
  TDD as acceptance tests on the real filesystem, including the generated
  index heading.
- **Undeclared-type rejection on every surface:** TDD, one test per surface.
- **Decay exemption:** TDD — a catalogue page's importance is unchanged after
  `apply_decay`; a built-in page's is not.
- **Draft lifecycle:** TDD — nomination creates a pending directory with a
  `log.md` entry; recall, injection, task recall, and navigation search all
  return nothing from it.
- **Export/import carries declarations and enabled catalogue types:** TDD.
- **CLI:** goal-based — `memex types --help` and `memex types list` on an
  isolated store, output recorded.

## Acceptance Criteria

- [x] **AC-0001.** `memex types enable <catalogue> --scope project` and
      `memex types add <custom> --scope project [--description <text>]` create
      `projects/<project>/<name>/` with a generated `index.md`, and
      `memex types list --scope project` prints built-in, enabled catalogue,
      declared custom, and draft types with page counts and kind.
- [x] **AC-0002.** A name outside `[a-z][a-z0-9-]{0,63}`, or colliding with a
      built-in name or directory, a catalogue name, `index`, or `log`, is
      rejected with an error naming the rule.
- [x] **AC-0003.** `memex write --type <name>` and the MCP and Python
      equivalents store the page at `projects/<project>/<name>/<slug>.md`
      with `type: "<name>"`, for enabled catalogue and declared custom types.
- [x] **AC-0004.** A `type` that is neither built-in, enabled, nor declared
      for that project is rejected before any file is written, on CLI, MCP,
      Python, and import, naming `memex types`.
- [x] **AC-0005.** The project's `index.md` lists each non-built-in type
      under a heading derived from its name, after the built-in headings, in
      deterministic order, and the navigation-consistent verify check treats
      those headings as it treats built-in ones.
- [x] **AC-0006.** `RecencyDecay.apply_decay` leaves every catalogue-type
      page's importance unchanged and reports zero changes for them.
- [x] **AC-0007.** `memex recall --type <name>`, the MCP `node_type` filter,
      and task recall's `node_type` accept enabled and declared types.
- [x] **AC-0008.** `memex verify` reports, as distinct named checks: a page
      whose `type` differs from its directory; a project directory that is
      neither built-in, enabled, declared, nor draft; and a draft directory
      holding a non-pending page.
- [x] **AC-0009.** A consolidation node carrying `proposed_type` lands as a
      pending page in a draft directory whose `log.md` gains one entry naming
      the proposer, date, session ids, and page count; every recall path
      returns nothing from that directory.
- [x] **AC-0010.** `memex types suggest --scope project` lists tags present on
      at least `min_pages` (default 3) pages that are not types, with counts,
      and makes no LLM call.
- [x] **AC-0011.** `memex types remove <name>` refuses while pages exist and
      names the count; `--force` archives them first. `memex types rename`
      and `merge` are governance verbs (`memory-governance`).
- [x] **AC-0012.** Export includes each project's enabled and declared types
      with descriptions; import recreates them before writing pages and fails
      on a page whose type is absent, naming the page.
- [x] **AC-0013.** Every page written before this change reads, indexes,
      recalls, and verifies unchanged.
- [x] **AC-0013a.** A `summary` or `episode` written by consolidation or
      capture is `active` on write; under the default `knowledge_approval =
      "manual"`, an `entity`, `procedure`, `preference`, catalogue, or custom
      page written by consolidation is `pending`, and the same page written
      through `memex write` by a person is `active`.
- [x] **AC-0014.** Spec, guide, architecture overview, and changelog describe
      the three kinds, the catalogue table, and the widened `type` contract;
      `mkdocs build --strict` passes.

## Follow-ons

- author: `docs/backlog/` — types at global scope.
- author: `docs/backlog/` — body schemas per catalogue type (level 3).
- author: `docs/backlog/` — consolidation targeting catalogue types beyond
  `decision`.

## Assumptions

- Technical: OKF v0.2 requires only a non-empty `type` string and keeps no
  registry (`~/Downloads/okf-kit/wiki/format/okf-format.md`;
  `~/code/okf-wiki/src/concepts/parser.ts`).
- Technical: the closed set is enforced at 11 sites across six modules
  (probe 2026-09-23); `wiki_index.node_type` is already `TEXT`.
- Technical: `RecencyDecay.apply_decay` iterates `scan_all()` with no type
  rule (`src/memex/application/decay.py:52`).
- Technical: `log.md` is reserved and structural but never written
  (`src/memex/infrastructure/store/navigation.py:6`); this spec and
  `memory-governance` make it an append-only history memex writes.
- Product: three kinds, catalogue plus custom, project scope only; the
  directory is the declaration; model output is always a draft (owner,
  brainstorm 2026-09-23).

## Resolved during implementation

- A type's description has no field of its own: it lives in the directory's
  `log.md`, on the `declare` (or `propose`) line that creates it
  (`<ISO-UTC> declare <name> by <actor>: <description>`). Reading it back is
  `WikiStore._log_state`; there is no second place a description could drift
  from the declaration.
- Whether a draft type's approval, rename, and merge verbs, and global-scope
  catalogue/custom types, belong here or in a later spec stayed decided as
  drafted: both are out of scope for this spec and are `memory-governance`'s
  and a follow-on's, respectively (see Follow-ons and "Ask first" above).
