<p align="center"><img src="assets/logo-wordmark.svg" width="240" alt="memex"/></p>

# Memex

A durable, local memory layer for AI coding agents. Memories are readable
Markdown files under `~/.memex/docs/`; a rebuildable SQLite index makes them
searchable. Memex brings relevant pages into coding sessions and records where
they came from.

## Get started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

1. **Install the CLI from Git:**

    ```bash
    uv tool install git+https://github.com/phanijapps/memex.git
    ```

2. **Connect your coding agent** from your project directory:

    ```bash
    memex install claude   # or codex, pi, copilot
    ```

    Run `memex install` without a name to choose interactively.

3. **Store and find a memory:**

    ```bash
    memex write --type preference --title "Deploy on Fridays" \
        --body "The team deploys to production on Fridays only."
    memex recall "deploy"
    ```

    Use `--scope project` for workspace knowledge; Memex derives the project
    identity from the current directory. The [guide](guide.md#project-memory-and-dashboard)
    explains global and project scope.

4. **Open the dashboard** with `memex viz` to browse memories, sessions,
   projects, and health on your own machine.

![Memex dashboard overview showing memory counts and recent pages](assets/dashboard-overview.png)

The [Memories view](assets/dashboard-memories.png) shows scope and type filters.
Both screenshots use sample data.

To disconnect a harness, run `memex uninstall <name>` from the project where
you installed it. To remove the CLI, run `uv tool uninstall memex`. Your
memories and transcripts remain under `~/.memex/`.

## How Memex works

- **Pull:** MCP tools let the model search and write memory when it chooses.
- **Push:** Harness hooks inject relevant memories and capture transcripts.
- **Proof:** `memex verify` checks store health and can require memory activity
  in CI.

The filesystem is the source of truth. The index can be rebuilt from Markdown
pages, and captured sessions link memories to their source conversations.

## Where to go next

- [User guide](guide.md) — operations, project scope, harness setup, and configuration
- [Specification](spec.md) — architecture diagrams, memory model, and schemas
- [Implementation notes](implementation-notes.md) — shipped differences from the specification
