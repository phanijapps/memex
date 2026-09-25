# Measure link expansion on a linked fixture

## Scenario and outcome

A coding agent's session-start block finds one page by words and now also
carries the pages that page links to. The claim is that following links
recovers evidence the query's words alone cannot reach, which is the failure
mode behind injection's 5/24 score. Whether that is true, and at what token
cost, is unmeasured: the committed benchmark fixture has links on 2 of its
123 cards and none of them resolve, so the shipped expansion measured zero
pages and zero tokens of difference. Success means a number: injection and
task recall scored with and without expansion on a fixture whose pages link
the way real memories do.

## Current behavior and evidence

[`graph_expansion.expand_links`](../../src/memex/application/graph_expansion.py)
walks `wiki_links` outward from each seed hit in deterministic breadth-first
order under the caller's token budget;
[`context_injection.build_injection`](../../src/memex/application/context_injection.py)
and [`task_recall.with_linked_pages`](../../src/memex/application/task_recall.py)
append the result after the direct hits. The benchmark in
[`eval/agent_workflow.py`](../../eval/agent_workflow.py) and its pins in
`tests/integration/test_agent_workflow_eval.py` measured identical tuples at
depth 0 and depth 1 (spec `docs/specs/link-expansion/spec.md`, AC-0008).

Two limits of the shipped slice matter for the measurement:

1. Expansion follows edges out of the found page only. A page's children
   (`parent` pointing at it) and pages that cite it (`mentions` pointing at
   it) do not come along. On a store shaped like real memories these inbound
   pages are often the evidence a task needs.
2. Cross-namespace links (a project page naming a global slug) are not
   expanded.

## Rubric

Reuse the existing six-metric benchmark unchanged; do not add a metric that
only the new feature can score well on. Per strategy, per run:

| Metric | Meaning | Rule |
| --- | --- | --- |
| `task_complete` | Tasks (of 24) whose every required page was found | Must rise; the grade line stays A ≥ 90% (22/24) |
| `fact_recall` | Share of all required pages found across tasks | Must rise or hold |
| `rendered_tokens` | Context spent | ≤ 4,096 per task; tokens per required page found must not worsen (the `quality-gated-retrieval` gate) |
| `inactive_hits` | Archived, expired, or pending pages returned | Hard fail if not 0 |
| `other_project_hits` | Pages from another project returned | Hard fail if not 0 |
| `calls` | Recall calls made | Unchanged; expansion may not add agent-paid queries |

Three additions specific to this evaluation:

- **A linked fixture.** Cards carry 3–6 typed links each, including `parent`
  edges and body `[[mentions]]`, mirroring the link density of the owner's
  real project memories on 2026-09-22. Task labels stay hand-written from the
  card content before any run, exactly as the current fixture's were.
- **Attribution.** Run every strategy at `depth=0` and `depth=1` on the same
  fixture. Only the difference between those two runs counts; a change that
  also appears at depth 0 came from elsewhere.
- **A third arm: inbound expansion.** Add `parent` children and `mentions`
  citers to the walk and score it as its own arm, so the direction question
  from limit 1 is answered by the same run.

Decision rule: promote a change (or keep expansion on by default) only when
`task_complete` or `fact_recall` rises, both leakage counts stay 0, per-task
tokens stay within budget, and tokens per required page found does not
worsen. Any single gate failing keeps the current default and records the
numbers, as `docs/specs/idf-query-terms/` did.

## Potential fix

Build the linked fixture beside the existing one under `eval/data/`, add the
inbound-expansion arm as a keyword option on `expand_links` (off by default
until measured), run the three arms, pin the winner, and amend the baseline
report table with a before→after row per strategy. Record the result in a
dated note under `docs/research/`.
