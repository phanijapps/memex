# ADR-0006: Three kinds of concept type

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** phanijapps
- **Spec:** [`../specs/project-concept-types/spec.md`](../specs/project-concept-types/spec.md)

## Context

Every memex page carried one of five built-in types (`entity`, `preference`,
`procedure`, `summary`, `episode`), each pinned to its own directory and
enforced by a closed set in the domain model. OKF v0.2 requires only a
non-empty `type` string and keeps no registry of its own, so the five-type
ceiling was memex's choice, not the format's. In practice a project's
decisions, policies, domain model, and access matrix all ended up filed as
`entity`, because there was nowhere else for them to go.

The owner's own store is the evidence: pages that are clearly durable
knowledge (a chosen architecture, a written policy, a decision record) read no
differently from a fleeting episode summary once both are typed `entity`.
Widening `type` to any string, alone, would trade a five-item ceiling for a
typo hazard — `decison` and `decision` would silently become two shelves.

## Decision

Three kinds of type, one vocabulary module (`src/memex/domain/types.py`):

- **Built-in** — the original five, memex-defined, with behaviour: capture
  writes `episode`, consolidation writes `summary`. Unchanged.
- **Catalogue** — `domain`, `architecture`, `rule`, `policy`, `decision`.
  memex defines the name and a contract (who may author it, whether it
  ages); a project turns one on with `memex types enable`.
- **Custom** — a shelf a project declares by name (`memex types add`), or a
  model nominates as a draft pending a person's decision.

The directory is the declaration: a type exists for a project exactly when
`projects/<project>/<type>/` exists, and that directory's `log.md` — OKF's
reserved, previously-unwritten scoped history — carries one line per
lifecycle event (`declare`, `propose`, `remove`), each shaped
`<ISO-UTC> <verb> <target> by <actor>[: <text>]`. Declaring a type writes
that directory and its first `log.md` line; nothing else marks a name as
known. The filesystem stays the source of truth and an OKF reader still sees
ordinary concept directories — no side-channel registry to fall out of sync
with the tree it describes.

Two rules follow from memory versus knowledge, and both live in the same
module so every layer applies them the same way:

- **Memory decays, knowledge does not.** `RecencyDecay` skips every catalogue
  type; the built-in five and custom types still age out. A policy nobody
  recalled for six months is not less true.
- **Memory is automatic, knowledge is governed.** `episode` and `summary` are
  `active` on write, always. Every other type is `active` when a person
  writes it and `pending` when a model does, governed by
  `[governance] knowledge_approval = "manual" | "auto"` (default `manual`).
  `memory-governance` owns the approval verbs over that pending state.

A page written by a model may nominate a **draft** type it did not declare
(`proposed_type`), or `memex types suggest` may surface a recurring tag,
deterministically and without a model call. A draft's directory exists and
its `log.md` records the nomination, but every page inside it is `pending`,
which every recall path already excludes — a draft is invisible until a
person accepts it.

## Alternatives considered

**Free strings only, no catalogue and no declaration.** Simplest change to
the model, but every project reinvents `decision` vs `descision`, and
consolidation output could plant an untraceable directory the owner never
agreed to. Rejected: no shape, no typo defense, no place to check a type
"exists" without scanning every page ever written under that name.

**Catalogue only, no custom types.** Keeps the long tail of what a real
project actually tracks — access matrices, story maps, runbooks — filed as
`entity` anyway, which is the exact problem the spec exists to fix. Rejected:
it caps expressiveness at what memex's authors anticipated instead of what
the project needs.

**A config-file registry of declared types.** `memex.toml` already carries
governance settings and would be the obvious place to list `["decision",
"access-matrix"]`. Rejected: a registry can drift from the directories on
disk (a type removed from the tree but left in config, or vice versa), and
it does not travel with the project the way a page does — clone the repo
without the config and the pages look undeclared for no reason a filesystem
inspection would reveal.

## Consequences

MCP and CLI `type` is no longer a closed five-value enum; it accepts any
built-in, project-enabled catalogue, or project-declared custom name, and an
undeclared name is rejected before any file is written on every surface
(CLI, MCP, Python, import) naming `memex types` as the remedy. Consolidation
may write `decision` when a project has enabled it, and may nominate a draft
type through `proposed_type`; it may not write any other catalogue type or
declare, rename, or merge a type — those stay governed acts.

`GovernanceConfig.knowledge_approval` replaces `approval`; a `memex.toml`
carrying the retired `approval` key fails loading with the replacement name.
The default changes from `auto` to `manual`, and that default change is the
behaviour change for existing stores: model-written knowledge (`entity`,
`preference`, `procedure`, any catalogue or custom type) now lands `pending`
by default, where the old `auto` default landed it `active`. Under the old
`approval = "manual"` setting itself nothing changes — that setting already
sent the same model-written knowledge to `pending`. `summary` and `episode`
are unaffected either way, since memory has always been automatic.

Existing five-type stores need no migration: they are exactly the built-in
case, since every built-in directory is still pre-created and no
declaration is required to use it.
