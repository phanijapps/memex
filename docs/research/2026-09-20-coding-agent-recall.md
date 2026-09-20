# Coding-agent task recall: six real-project workflows

- **Run date:** 2026-09-20
- **Source revision:** `928f7c4afcbfec8bf7c66eaed87eaf9f8ebe1297`
- **Reproduce:** `uv run python -m eval.agent_workflow`

## Question and plan

Can Memex return *all* the durable facts a coding agent needs for a task, rather than one page that happens to match a question? The test uses six goals drawn from work on this repository: Claude Code session handoff, project memory scope, index recovery, MCP integration, safe distillation, and prompt recall tuning. Each goal needs three separate memories to be actionable.

The committed [fixture](../../eval/data/coding-agent-workflow.json) contains 18 current memory cards paraphrased from `docs/architecture/overview.md` and `docs/gitpages/guide.md` at the pinned revision. Every real card names a source file and an exact excerpt; the fixture pins SHA-256 hashes of both source files, and the integration test checks those hashes and excerpts. Two archived cards simulate superseded behavior; two other simulated cards belong to another project. The six task prompts and required page labels were written before running the query. They are human judgments about the minimum useful evidence, not model-generated labels.

The [runner](../../eval/agent_workflow.py) writes those cards through the real `Memex.write` API into a fresh temporary store, then calls the production `Memex.recall` path with an explicit project ID. It compares one broad goal query with three focused subqueries per task. Both strategies keep at most eight distinct hits; focused recall uses at most three hits per subquery and three calls. The runner rejects any archived or other-project hit in the task results. No developer memory store, external service, or LLM is involved.

## Observed result

| Coding task | Required pages | Broad query found | Focused queries found | Broad query missed |
| --- | ---: | ---: | ---: | --- |
| Claude session handoff | 3 | 3 | 3 | — |
| Project memory missing | 3 | 2 | 3 | Derived project identity |
| Index recovery | 3 | 1 | 3 | Markdown source truth; verify checks |
| MCP integration review | 3 | 3 | 3 | — |
| Safe distillation | 3 | 2 | 3 | Pending approval |
| Prompt recall tuning | 3 | 2 | 3 | Token budget |
| **Total** | **18** | **13 (72.2%)** | **18 (100%)** | **5** |

A task counts as complete only if all three labeled pages appear in its first eight distinct hits. The broad goal query completed **2/6 tasks**; the focused queries completed **6/6 tasks**. Project-scoped recall returned **0 archived or other-project cards** among these results. This is evidence retrieval only: it does not score whether an agent writes correct code or chooses good subqueries unaided.

## Interpretation and limits

On this small corpus, asking the retrieval layer about the whole goal tends to favor words shared by many notes and miss a supporting fact. An agent that breaks the task into concrete unknowns can recover those facts with more calls. The comparison is deliberately operational rather than a controlled ranker contest: one query versus three queries has different search cost, and the focused queries were authored by someone who knew the documentation. A future evaluation should use independently written task prompts and query plans from actual harness sessions, a larger corpus, and a fixed token budget for the combined context.

The fixture is based on real Memex documentation but its memory cards are curated, not captured transcripts. Both other-project and archived distractors are simulated; the archived cards model earlier interface behavior but are not used as evidence for current facts. A 22-card store does not test scale, noise from long histories, or memory extraction quality. This test therefore supports a narrow conclusion about retrieving a known evidence set through the production index. The task-level design is consistent with the gap between isolated recall and action in [MemoryArena](https://arxiv.org/abs/2602.16313); this run makes no comparison to that benchmark.
