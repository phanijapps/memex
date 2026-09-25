# Domain guidance

Applies to `src/memex/domain/`. Inherits the root `AGENTS.md`. Scope-specific deltas only.

- Pure core: every import here is stdlib or intra-package, and it stays that way.
  No filesystem writes, no config reads, no SDKs, no logging of page content.
- Each module owns one vocabulary — see its docstring before adding a ninth module.
  Extend the owner instead: slugs, front matter, links, reserved names, scrubbing,
  concept types (`types.py`: built-in, catalogue, and custom type shape, kind,
  headings, and the initial-status rule; ADR-0006).
- `scrub.py` runs at the write boundary and is a security control, never cut or
  bypassed. It records categories only, never matched text.
