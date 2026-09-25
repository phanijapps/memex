# Spec: Git-backed memory

- **Status:** Draft
- **Owner:** phanijapps
- **Plan:** [`plan.md`](plan.md) (authored on approval)
- **Constrained by:** ADR-0001, ADR-0004
- **Brief:** `docs/v1_enhance.md` items C1 ("the diff is the approval surface") and E3 (git seeding)
- **Discovery:** brainstorm 2026-09-23
- **Contract:** `[git]` in `memex.toml`; `memex git init|status`; branch-per-approval and merge-request creation; `memex seed --from-git`
- **Shape:** integration

> **Spec contract:** this document defines what "done" means. The implementing
> PR must match this spec, or update it. Verification must be derivable from it.

## Objective

The memory store is Markdown, so it can be a git repository — and once it is,
approval has a natural second stage. A person or agent approves a draft type
or pending page (`memory-governance`); memex commits that change to a branch
and opens a merge request; the merge is the moment the knowledge becomes the
team's. Reviewers see the full diff — directory, pages, `log.md` — in the
tool they already review code in. The same connection runs the other way:
`memex seed --from-git` distils a project's commit history into a summary
page on cold start, so a fresh checkout is not a blank memory.

## Durable Outputs

| Semantic role | Applicability | Destination | Owner | Expected evidence | Closeout condition |
| --- | --- | --- | --- | --- | --- |
| Decision rationale | Applicable — memory leaving the machine is a privacy boundary | `docs/adr/0008-git-backed-memory-store.md` | author | What is committed, what never is, why push is never automatic | Referenced from this header |
| Security review | Applicable — network egress and secret exposure | plan carries `security-checklists` secrets and outbound modules | author | Threat notes; scrubber coverage confirmed | Reviewed before approval |
| Current product truth | Applicable | `docs/gitpages/guide.md` (git section) | author | Init, branch naming, MR flow, seeding | `mkdocs build --strict` passes |
| Release history | Applicable | `docs/product/changelog.md` | author | Unreleased entry | Entry present |

## Boundaries

### Always do

- Version `docs/` only. `transcripts/`, `mem.db*`, and `logs/` are written to
  the store's `.gitignore` by `memex git init` and never committed: raw
  conversations are private capture, not team knowledge.
- Require explicit enablement: `[git] enabled = true` and a remote set by the
  user in `memex.toml` (ADR-0004). Nothing in a cloned repository can enable
  pushing.
- Run the secret scrubber over every page in a commit exactly as the write
  boundary does; refuse the commit if any page fails.
- Use the repository's own `gh` or `glab` when present for merge requests;
  never embed tokens; never call a forge API directly.
- Name branches deterministically: `memex/approve/<slug>`,
  `memex/type/<name>`, `memex/seed/<short-sha>`.

### Ask first

- Committing anything under `global/` (cross-project memory) to a
  project-shared remote.
- Any automatic merge, even of the store's own branches.
- Rebasing or rewriting store history.

### Never do

- Push without `[git].enabled` and a configured remote.
- Commit transcripts, the index, logs, or any file outside `docs/`.
- Let an agent approval reach a push unless `[governance].agent_git = true`.
- Seed from git history that contains a secret the scrubber flags; report and
  skip that commit.

## Flows

**Init.** `memex git init` turns the store into a repository (or adopts an
existing one), writes `.gitignore`, makes an initial commit of `docs/`, and
records `enabled = true` only when the user passes `--remote <url>`.

**Approval → branch → merge request.** When a governance verb runs on an
enabled store, memex creates the branch, commits the verb's changes with the
`log.md` line as the commit message body, pushes, and opens a merge request
whose description lists the affected slugs and the actor. Without `gh`/`glab`
it pushes and prints the compare URL. Without a remote it commits locally.
Agent approvals follow the same path only when `agent_git` is true.

**Seeding.** `memex seed --from-git [--limit 300] [--depth message|full]`
reads the *project's* repository (not the store's), distils commits into one
`summary` page per project keyed on `HEAD`, upserts idempotently, and skips
commits the scrubber flags. Re-running at the same `HEAD` changes nothing.

## Testing Strategy

- **Init and ignore rules:** TDD on a temporary repository — `.gitignore`
  content, initial commit contents, no transcript ever tracked.
- **Branch and commit shapes:** TDD — deterministic names, commit body equals
  the `log.md` line, scrubber refusal blocks the commit.
- **Push and MR:** goal-based with a fake `gh`/`glab` on `PATH` recording its
  arguments; no network in tests.
- **Enablement:** TDD — every push path refuses without `enabled` and a remote.
- **Seeding idempotence:** TDD — two runs at one `HEAD` produce identical
  bytes; a new `HEAD` changes the page.
- **Real invocation:** manual QA against a scratch remote, recorded.

## Acceptance Criteria

- [ ] **AC-0001.** `memex git init` creates or adopts a repository at the
      store root with a `.gitignore` excluding `transcripts/`, `mem.db*`, and
      `logs/`, and an initial commit containing only `docs/`.
- [ ] **AC-0002.** `git ls-files` in an initialised store never lists a file
      outside `docs/`, after any sequence of writes, approvals, and seeds.
- [ ] **AC-0003.** With `enabled = false` or no remote, every governance verb
      commits locally (or, when the store is not a repository, writes only)
      and no `git push` is attempted.
- [ ] **AC-0004.** With `enabled = true` and a remote, a human approval
      creates `memex/approve/<slug>` or `memex/type/<name>`, commits with the
      `log.md` line as body, pushes, and — when `gh` or `glab` is on `PATH` —
      opens a merge request whose description names the slugs and actor.
- [ ] **AC-0005.** A commit containing a page the secret scrubber flags is
      refused, the branch is not created, and the refusal names the slug and
      the redaction category only.
- [ ] **AC-0006.** An agent approval on an enabled store with a remote is
      refused unless `[governance].agent_git = true`; with it, the merge
      request description marks the actor as `agent:<harness>`.
- [ ] **AC-0007.** `memex seed --from-git` writes one `summary` page per
      project whose front matter records the seeded `HEAD`; a second run at
      the same `HEAD` changes no bytes; a run at a new `HEAD` updates the page.
- [ ] **AC-0008.** Seeding skips a commit whose message or diff the scrubber
      flags and reports the count skipped, never the content.
- [ ] **AC-0009.** No git or forge command receives a token, password, or page
      content as an argument; credentials come only from the user's existing
      `gh`/`glab`/git configuration.
- [ ] **AC-0010.** Guide, changelog, and ADR-0008 state what is versioned,
      what never is, and that pushing requires explicit user-scope enablement;
      `mkdocs build --strict` passes.

## Follow-ons

- author: `docs/backlog/` — pulling a teammate's approved knowledge: `memex
  git sync` fetching and rebuilding the index.
- author: `docs/backlog/` — conflict handling when two branches touch one
  page.

## Assumptions

- Technical: the store is plain Markdown under `~/.memex/docs/` and the index
  rebuilds from it (ADR-0001), so versioning `docs/` alone loses nothing.
- Technical: the write boundary already scrubs secrets (v1-guardrails, D1);
  commits reuse that scrubber.
- Technical: `memex verify` and `rebuild-index` regenerate everything a
  fresh clone needs; a teammate's clone plus `rebuild-index` is a working
  store.
- Product: git is the ratification layer for approvals and the source for
  cold-start seeding; pushing is never automatic (owner, brainstorm
  2026-09-23; `docs/v1_enhance.md` C1, E3).
