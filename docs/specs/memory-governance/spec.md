# Spec: Memory governance

- **Status:** Draft
- **Owner:** phanijapps
- **Plan:** [`plan.md`](plan.md) (authored on approval)
- **Constrained by:** ADR-0004, ADR-0005
- **Brief:** none
- **Discovery:** brainstorm 2026-09-23
- **Contract:** the approval lifecycle for pending pages and draft types: `memex approve|reject`, `memex types approve|rename|merge`, the MCP `memex_approve` tool, `[governance]` in `memex.toml`, and `log.md` history entries
- **Shape:** service

> **Spec contract:** this document defines what "done" means. The implementing
> PR must match this spec, or update it. Verification must be derivable from it.

## Objective

Memory is automatic; knowledge is governed. A page of a memory type
(`episode`, `summary`) is active the moment it is written, whoever wrote it.
A page of a knowledge type (`entity`, `procedure`, `preference`, and every
catalogue and custom type) is active when a person writes it and **pending**
when a model does — a person is their own approver; a model's claim about the
world waits. That default is a user's choice: `knowledge_approval = "auto"`
in `~/.memex/memex.toml` turns knowledge curation automatic for a store whose
owner trusts their models. Memory is never governed. Draft types are pending
by construction under either setting. Today approval is
one CLI command for one page. This spec makes approval a small, complete
lifecycle with two kinds of approver: **people**, through the CLI and the
dashboard, and **agents**, through one MCP tool bounded by a policy the user
sets. Every approval, by anyone, leaves a line in the directory's `log.md`, so
the history of how knowledge was ratified sits beside the knowledge itself,
in an OKF-reserved file any tool can read.

## Durable Outputs

| Semantic role | Applicability | Destination | Owner | Expected evidence | Closeout condition |
| --- | --- | --- | --- | --- | --- |
| Decision rationale | Applicable — agents approving knowledge is a trust boundary | `docs/adr/0007-bounded-agent-approval.md` | author | Why a policy-gated agent path is accepted, what it can never do | Referenced from this header |
| Security review | Applicable — a new write path reachable by an agent | this spec's plan carries the `security-checklists` modules for untrusted input and privilege boundaries | author | Threat notes per verb | Reviewed before approval |
| Current product truth | Applicable | `docs/gitpages/guide.md` (governance section) | author | Verbs, policy keys, what agents may approve | `mkdocs build --strict` passes |
| Release history | Applicable | `docs/product/changelog.md` | author | Unreleased entry | Entry present |

## Boundaries

### Always do

- Apply the memory/knowledge rule at every write boundary from the page's
  type kind, `source`, and `knowledge_approval`; no surface may bypass it,
  and no setting makes memory pending.
- Default the agent path to **off**. Enabling it is a user-scope act in
  `memex.toml` (ADR-0004); nothing in a repository can turn it on.
- Record every approval, rejection, rename, and merge as one append-only line
  in the affected directory's `log.md`: date, verb, target, actor
  (`user`, or `agent:<harness>`), and policy name when an agent acted.
- Make every verb the same facade function on every surface: CLI, MCP, and
  dashboard call `Memex.approve`, `Memex.reject`, `Memex.approve_type`,
  `Memex.rename_type`, `Memex.merge_type`; no surface has its own logic.
- Keep agent approvals reviewable and reversible: `memex governance log`
  lists them; `memex reject <slug>` and `memex types remove` undo them.

### Ask first

- Any agent verb beyond `approve` (agents never rename or merge types).
- Raising an agent's reach to catalogue types or to pages of catalogue type.
- Auto-approval by threshold without an agent or person calling the verb.

### Never do

- Let an agent approve a page or type of catalogue kind (`domain`,
  `architecture`, `rule`, `policy`, `decision`): those are always human.
- Let an agent approve a page created inside the policy's cooling period, so
  write-then-approve in one session is impossible.
- Let an agent approve while the store has a configured git remote and
  `[governance].agent_git = false` (the default) — approval would otherwise
  become a push (see `git-backed-memory`).
- Log or return page bodies, descriptions, or credential-shaped strings from
  any governance path; `log.md` entries carry slugs and actors only.

## Who wrote it

The rule needs every page to say who wrote it. `source` in front matter is
set by consolidation only today; this spec makes every write boundary set it:

| Surface | `source` | `harness` |
| --- | --- | --- |
| `memex write` (CLI) | `user` | unset |
| `memex_write` (MCP) | `agent` | the harness name |
| capture (`memex hook`) | `transcript` | the harness name |
| consolidation, episode enrichment | `consolidation` | the harness or provider |
| import | preserved from the export; `import` when absent | preserved |

A page's initial status follows from its type's kind, its `source`, and
`knowledge_approval`: memory kinds are always `active`; knowledge kinds are
`active` for `user`, and for `agent`, `transcript`, and `consolidation` they
are `pending` under `manual` (the default) and `active` under `auto`.

## Verbs

| Verb | Target | Effect | Surfaces |
| --- | --- | --- | --- |
| `approve` | pending page | `status: active`, re-index, `log.md` line | CLI, MCP (policy), viz |
| `reject` | pending page | `status: archived`, `log.md` line | CLI, viz |
| `types approve` | draft type | every page `pending → active`, `log.md` line; the directory is now declared | CLI, MCP (policy, custom only), viz |
| `types rename` | draft or declared type | move directory, rewrite `type` on each page, refresh navigation, `log.md` line in both | CLI, viz |
| `types merge <a> --into <b>` | draft or declared type | move pages into `b`, rewrite `type`, remove `a`, `log.md` lines | CLI, viz |

## Agent approval policy

`[governance]` in `memex.toml` (user scope) gains:

```toml
[governance]
# Memory is always active on write and is not configurable.
knowledge_approval = "manual"   # manual (model-written knowledge is pending) | auto
agent_approval = "off"          # off | pages | pages-and-custom-types
agent_cooling_period_hours = 1  # a page younger than this is never agent-approvable
agent_min_pages = 3             # a draft type needs this many pages...
agent_min_sessions = 2          # ...from this many distinct sessions
agent_git = false               # agents never approve into a git-backed store unless true
```

An agent calling `memex_approve(slug)` or `memex_approve(type=name)` succeeds
only when every condition holds, each checked deterministically from the page
or directory itself:

1. `knowledge_approval` is `manual` (under `auto` nothing is pending to approve) and the `agent_approval` level permits the target kind;
2. the target is not of catalogue kind;
3. for a page: `source` is `transcript` or `consolidation`, `harness` is set,
   and `created` is older than the cooling period;
4. for a draft type: page count ≥ `agent_min_pages` from ≥
   `agent_min_sessions` distinct `session_id` values, and the name collides
   with nothing;
5. `agent_git` permits it when a remote is configured.

A refusal returns the failing condition by name and nothing else. `memex
status` reports the count of agent approvals since the last human ran
`memex governance log`.

## Testing Strategy

- **Policy predicate:** TDD, pure — one test per condition, plus the
  all-conditions-hold case.
- **Verbs through the facade:** TDD acceptance on the real filesystem,
  asserting page status, navigation, index, and the exact `log.md` line.
- **Surface parity:** TDD — CLI, MCP, and the dashboard route each call the
  facade and produce identical store state for the same verb.
- **Agent refusal paths:** TDD, one per *Never do* line.
- **`log.md` stays structural:** TDD — after appends, `classify_reserved`
  still returns `structural`, navigation never rewrites it, and no recall
  path indexes it.
- **CLI:** goal-based — `memex governance log` on an isolated store, recorded.

## Acceptance Criteria

- [ ] **AC-0000a.** Every write surface sets `source` as the table above
      states, and under `knowledge_approval = "manual"` a page's initial
      status is `active` for memory kinds, and for knowledge kinds `active`
      when `source` is `user` and `pending` otherwise — on CLI, MCP, capture,
      consolidation, and import alike.
- [ ] **AC-0000b.** Under `knowledge_approval = "auto"`, every page is
      `active` on write regardless of kind or `source`; draft types stay
      pending; `memex status` names the setting so a reader knows curation is
      automatic.
- [ ] **AC-0000c.** `[governance].approval` is replaced by
      `knowledge_approval`; a `memex.toml` still carrying `approval` fails
      config loading with an error naming the new key and both values, and
      `memex.toml` is read only from the user's data directory (ADR-0004),
      never from a repository.
- [ ] **AC-0001.** `memex approve <slug>` and `memex reject <slug>` change a
      pending page to `active` or `archived`, re-index it, and append one
      line to its directory's `log.md` of the form
      `<date> <verb> <slug> by user`.
- [ ] **AC-0002.** `memex types approve <name>` flips every page in a draft
      type to `active`, appends one `log.md` line, and the type appears as
      declared in `memex types list`.
- [ ] **AC-0003.** `memex types rename <a> <b>` moves the directory, rewrites
      `type` on every page, refreshes navigation, and appends a line to both
      directories' `log.md`; `memex verify` is clean afterwards.
- [ ] **AC-0004.** `memex types merge <a> --into <b>` moves every page,
      rewrites `type`, removes `a`, appends lines to both logs, and `verify`
      is clean.
- [ ] **AC-0005.** With `agent_approval = "off"` (the default), the MCP
      `memex_approve` tool refuses every call with the reason
      `agent_approval_off`; a model-written knowledge page therefore stays
      pending until a person approves it.
- [ ] **AC-0006.** With `agent_approval = "pages"`, an agent approves a
      pending page whose `source` is `transcript` or `consolidation`,
      `harness` is set, kind is not catalogue, and `created` is older than
      the cooling period; the `log.md` line reads `by agent:<harness>
      policy=pages`.
- [ ] **AC-0007.** An agent call for a page younger than the cooling period,
      a catalogue-kind page, a page without `source`, or a page without
      `harness` is refused, each with its own named reason.
- [ ] **AC-0008.** With `agent_approval = "pages-and-custom-types"`, an agent
      approves a draft custom type with at least `agent_min_pages` pages from
      at least `agent_min_sessions` sessions; a draft below either floor is
      refused naming the floor.
- [ ] **AC-0009.** An agent call to `rename`, `merge`, `reject`, or to
      approve a catalogue type does not exist as an MCP capability and the
      `memex_approve` tool exposes no such argument.
- [ ] **AC-0010.** When a git remote is configured and `agent_git` is false,
      every agent approval is refused with `agent_git_disabled`.
- [ ] **AC-0011.** `memex governance log [--since <date>] [--actor agent]`
      lists `log.md` entries across the store with date, verb, target, actor,
      and directory; `memex status` reports the number of agent approvals
      newer than the last `governance log` run.
- [ ] **AC-0012.** No governance output — CLI, MCP, dashboard, `log.md`, or
      logger — contains a page body, description, credential-shaped string,
      or absolute home path.
- [ ] **AC-0013.** After any number of appends, `log.md` classifies as
      `structural`, is never indexed, never returned by recall, and never
      rewritten by navigation.
- [ ] **AC-0014.** Guide, changelog, and ADR-0007 describe the verbs, the
      policy keys and defaults, and what agents can never do;
      `mkdocs build --strict` passes.

## Follow-ons

- author: `docs/backlog/` — a reviewer role: agent approvals require a second
  agent's independent approval before taking effect.
- author: `docs/backlog/` — per-project policy overrides, if projects diverge
  in trust.

## Assumptions

- Technical: `Memex.approve` exists for pages (`src/memex/application/memory.py:682`)
  and is CLI-only; MCP exposes five tools without it. `GovernanceConfig` exists in `src/memex/infrastructure/config.py:69` with one
  key, `approval = "auto" | "manual"`, a global switch. The owner's rule
  (2026-09-23) replaces it: memory auto, knowledge manual for model writers,
  decided per page from type kind and `source`, with `knowledge_approval`
  as the one knob (owner: "keep it configurable where I can turn it to auto
  knowledge curation"). The old key is replaced, not aliased, per the
  no-legacy preference.
- Technical: `source` is set only by consolidation today
  (`src/memex/application/consolidator.py:225`); CLI and MCP writes leave it
  null, which is why the rule needs the write-boundary table above.
- Technical: `status: pending` is already excluded by every recall path
  (v1-guardrails), so a draft type's invisibility costs no new filter.
- Technical: `log.md` is reserved and structural, never written today
  (`src/memex/domain/reserved.py:17`, `store/navigation.py:6`); an
  append-only body keeps it structural because it carries no front matter.
- Product: agents may approve, under a user-set policy, custom knowledge and
  pages only; catalogue knowledge is always human (owner, brainstorm
  2026-09-23).
