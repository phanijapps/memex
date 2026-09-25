# Spec: Shelf contracts

- **Status:** Draft
- **Owner:** phanijapps
- **Plan:** none yet (authored on approval)
- **Constrained by:** ADR-0001, ADR-0006, ADR-0007
- **Contract:** the type-declaration `log.md` line, the contract keys, and the
  behavior sites that read them
- **Shape:** data

> **Spec contract:** this document defines what "done" means. The
> implementing PR must match it, or update it. Verification must be
> derivable from it.

## Objective

Behavior stops keying off type names. A shelf's declaration line carries an
explicit contract — `decay`, `model` write access, and (for exactly one
pipeline shelf) `capture` — and every behavior site reads the contract. The
catalogue becomes a preset library; the five built-in types demote to seeded
declarations; the dashboard treats all shelves equally (already shipped for
the type filter and KPI tiles).

## Durable Outputs

| Semantic role | Destination | Expected evidence | Closeout |
| --- | --- | --- | --- |
| Decision rationale | `docs/adr/0007-shelves-with-explicit-contracts.md` | Alternatives and trade-off recorded | Referenced from here |
| Contract grammar | `docs/gitpages/guide.md` (types section) | Declare examples with keys; preset table | Guide matches the parser |
| Architecture truth | `docs/architecture/overview.md` | Behavior sites named with the keys they read | Site list complete |

## Contract grammar

```
<ISO-UTC> declare <name> by <actor>: <one-line description> [k=v]…
```

Keys (strict, single line, ≤512 bytes total as today):

| Key | Values | Default (absent) | Read by |
| --- | --- | --- | --- |
| `decay` | `yes` / `no` | `yes` | `RecencyDecay.apply_decay`, `memex status` |
| `model` | `deny` / `propose` / `allow` | `propose` | consolidation write path, governance |
| `capture` | `target` | — (never default) | transcript pipeline (exactly one shelf) |

Propose-drafts (`memex types propose`, consolidation `proposed_type`) keep
ADR-0006's draft lifecycle; the draft carries the same keys for the moment it
is approved into a declaration.

## Boundaries

### Always do

- Keep the declaration in `log.md`, append-only, strict single-line parsing
  (`_log_state` grows a contract parser; unknown keys fail closed with the
  rule named, as AC-0002 of the concept-types spec did for names).
- Seed the five built-ins as declarations with their current behavior written
  out explicitly (episode: `capture=target model=deny decay=yes`; summary:
  `model=allow decay=yes`; the other three: `model=propose decay=yes`) —
  seeded lazily per store, so existing stores are unchanged until a
  declaration is read.
- Convert the catalogue table into preset definitions (name, description,
  contract keys) consumed by `memex types enable`.
- Make `knowledge_approval` the default `model` value for undeclared shelves
  instead of a separate switch; keep the config key readable (warn-once
  deprecation) for one release.
- Update every behavior site to read contracts: decay exemption,
  consolidation write/propose routing, capture target, verify's type checks,
  export/import of declarations.

### Ask first

- More than the three contract keys above (e.g. per-shelf retention,
  per-shelf recall boosting) — each new key needs its own evidence.
- Renaming `episode` or `summary` (pipeline anchors).

### Never do

- Reintroduce a hardcoded type-name branch in any behavior site.
- Store contracts anywhere but the declaration line.
- Break reading of any page or declaration written before this change.

## Acceptance Criteria

- [ ] **AC-0001.** A declared shelf `[decay=no]` is untouched by
      `apply_decay` (importance unchanged, zero changes reported) while a
      `[decay=yes]` shelf in the same store decays; pig's `book` declaration
      upgraded with `[decay=no]` satisfies this without code changes.
- [ ] **AC-0002.** Consolidation writes directly to a `[model=allow]` shelf,
      proposes a draft for `[model=propose]`, and never touches
      `[model=deny]`; governance `pending` behavior follows the same key.
- [ ] **AC-0003.** Exactly one shelf in a store may carry `capture=target`;
      the transcript pipeline writes there; declaring a second fails with a
      named error.
- [ ] **AC-0004.** `memex types list` shows each shelf's contract keys;
      `types enable <preset>` declares with the preset's contract; the five
      built-ins appear as ordinary seeded declarations with their contracts.
- [ ] **AC-0005.** Unknown or malformed contract keys fail closed at
      declaration time and at read time (verify check), naming the rule.
- [ ] **AC-0006.** Every prior behavior-site special case (`type == "episode"`,
      `name in CATALOGUE`, plural-dir exceptions) is gone from behavior code;
      grep-clean, pinned by a test that reads behavior through contracts only.
- [ ] **AC-0007.** Stores, declarations, and pages written before this change
      read, recall, verify, export, and restore unchanged; the seeded
      contracts reproduce yesterday's behavior byte-for-byte on a
      pre-change store fixture.
- [ ] **AC-0008.** The dashboard type filter, KPI tiles, and type badges
      enumerate shelves from the index (already landed); new contract keys
      surface nowhere in the UI except `types list` output.

## Testing Strategy

- **Contract parser:** TDD — grammar accept/reject table, defaults, fail-closed
  unknown keys, round-trip through `_log_state`.
- **Behavior sites:** TDD per AC — decay, consolidation routing, capture
  uniqueness, verify check.
- **Compatibility:** goal-based — a frozen pre-change store fixture (with
  catalogue and custom declarations) passes verify and recalls identically
  before/after.
- **Presets:** table-driven — enabling each preset produces the documented
  contract.

## Follow-ons

- Per-shelf recall weighting (would need its own eval, per the 2026-09-21
  description-weight work).
- A `memex types edit` verb for contract changes with lifecycle history.
