# Architecture decision records

Architecture decision records preserve choices whose trade-offs should not be
reconstructed from code. Accepted records are immutable apart from their status
when a later ADR supersedes or deprecates them.

| ADR | Status | Date | Decision |
| --- | --- | --- | --- |
| [0001](0001-use-markdown-pages-as-memory-source-of-truth.md) | Accepted | 2026-09-15 | Markdown pages are authoritative; SQLite is disposable derived state. |
| [0002](0002-use-layered-package-and-shared-adapter-contracts.md) | Accepted | 2026-09-15 | The package uses domain, application, and infrastructure ownership with shared adapter contracts. |
| [0003](0003-use-one-llm-port-with-api-and-harness-providers.md) | Accepted | 2026-09-15 | Consolidation uses one LLM port implemented by compatible APIs or installed harnesses. |
| [0004](0004-require-user-scope-enablement.md) | Accepted | 2026-09-16 | Capture, injection, and consolidation require explicit user-scope enablement. |
| [0005](0005-adopt-okf-v02-page-front-matter.md) | Accepted | 2026-09-23 | Page front matter is OKF v0.2 first, with Memex fields as conformant extensions. |
| [0006](0006-three-kinds-of-concept-type.md) | Accepted | 2026-09-24 | Built-in, catalogue, and custom concept types; the directory is the declaration, `log.md` is the record. |

## Adding a decision

Use the next four-digit number and the filename
`NNNN-kebab-case-title.md`. A record contains status, date, context, decision,
consequences, and alternatives considered. New decisions start as `Proposed`
and become `Accepted` or `Rejected` through review.

Do not edit an accepted record's body. A later decision creates a new record
and changes the old status to `Superseded by ADR-NNNN`. Regenerate this table
from the record headers whenever records change.
