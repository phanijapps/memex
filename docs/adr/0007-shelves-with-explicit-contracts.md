# ADR-0007: Shelves with explicit contracts replace type-name behavior

- **Status:** Proposed
- **Date:** 2026-09-25
- **Decides:** how page `type` governs behavior after concept types made the
  vocabulary dynamic (`docs/adr/0006-three-kinds-of-concept-type.md`)
- **Supersedes:** the behavioral halves of ADR-0006 (its kind taxonomy), not
  its filesystem-declaration mechanics

## Context

ADR-0006 made `type` dynamic: built-in, catalogue, and custom kinds, with the
directory as the declaration. Live use since then (a 78,182-book project
library, governance presets, decay runs, the dashboard type filter) exposed
three structural problems:

1. **One field carries three meanings.** `type` encodes *shape* (preference vs
   procedure), *behavior* (episode is capture-written; catalogue types do not
   decay; model-written knowledge lands pending), and *shelf* (`book`,
   `architecture`). They are orthogonal; a policy is knowledge-shaped, files
   under governance, and never decays — the current design can say only one.
2. **Behavior is hardwired to names.** `if type == "episode"` and
   `if name in CATALOGUE` are scattered across decay, consolidation,
   governance, and the web KPIs. A declared custom type is a directory with
   no behavior: pig's books decay like gossip and nothing but a hardcoded
   catalogue entry could stop it.
3. **The original five were a seed mistaken for a law.** "entity" is a
   catch-all; real projects declare their own nouns; and the five still leak
   into UI (the dashboard's type filter rejected every dynamic type until
   this ADR's first fix landed), KPIs, and validation defaults.

## Decision

**Behavior moves from type names to an explicit contract recorded on the
declaration; the catalogue becomes a preset library of contracts; the five
built-ins demote to seeded declarations.**

1. **`type` = shelf = directory.** Purely organizational and project-owned.
   OKF conformance is unchanged (a non-empty string; OKF keeps no registry).
2. **The declaration line carries a contract.** The `log.md` lifecycle line
   already names the shelf's purpose; it gains bracketed keys:
   `declare <name> by <actor>: <description> [decay=yes|no]
   [model=deny|propose|allow] [capture=target]`. Parsing stays strict,
   single-line, append-only (ADR-0006 mechanics unchanged).
3. **Contract keys are the only behavior surface:**
   - `decay` — whether `RecencyDecay` may touch the shelf's pages;
   - `model` — whether consolidation may write (`allow`), must propose a
     draft (`propose`, the knowledge-governance default), or cannot touch
     the shelf (`deny`, e.g. episode);
   - `capture=target` — the single shelf the transcript pipeline writes to
     (episode). Exactly two names remain special in code: the capture shelf
     and the consolidation output shelf (`summary`), both as seeded
     declarations with fixed contracts, referenced by the pipeline — not as
     a taxonomy.
4. **The catalogue is a preset library**, not a kind. Enabling
   `memex types enable policy` declares a shelf with the shipped policy
   contract (`decay=no model=propose`). New presets ship without code
   changes; projects copy and edit them.
5. **Defaults replace the `kind` switch.** An undeclared custom shelf gets
   `decay=yes model=propose`; `knowledge_approval` config becomes the
   default `model` contract for undeclared shelves rather than a parallel
   knob. `builtin/catalogue/custom/draft/withdrawn` collapses to *declared,
   with contract* (plus the existing `draft`/`withdrawn` lifecycle states,
   which are verbs in the log, not kinds).
6. **Shape vocabulary is advisory.** Preference-vs-procedure-vs-entity is
   expressible as seeded shelves for compatibility, but new behavior never
   keys off those names; recall already searches tags and signpost
   descriptions, which carry vocabulary better than the type filter.

## Consequences

- Declaring a shelf grants behavior: pig's `declare book … [decay=no]
  [model=allow]` stops decay and lets consolidation write book pages — no
  code change, no catalogue entry.
- Two names remain load-bearing (`episode`, `summary`) as pipeline anchors;
  renaming them is out of scope. Everything else — including the seeded
  five — is data.
- Read compatibility: existing `log.md` lines parse as declarations with
  default contracts; current implicit behavior (catalogue ⇒ no decay;
  built-in memory types ⇒ auto-active) is written out explicitly as the
  seeded/preset contracts at migration time. No page rewrites; the
  declaration is the single source.
- The web layer already follows this ADR's spirit: the Memories filter and
  KPI tiles enumerate shelves from the index, not from a hardcoded list
  (landed 2026-09-25 with dynamic-type filtering).

## Alternatives considered

- **Status quo + more catalogue entries.** Keeps behavior hardwired; every
  new need (runbooks, books) becomes a core-code change.
- **Kill the type taxonomy; tags only.** Loses OKF navigation semantics and
  the directory shelf, which pig and the dashboard both use productively.
- **A sidecar registry (types.toml).** A second source of truth beside the
  filesystem; ADR-0006's directory-as-declaration already works and the
  contract fits its line format.
