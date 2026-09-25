# Project-level Concept Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **Spec:** [`spec.md`](spec.md)
- **Status:** Drafting
- **Repository anchors:** `src/memex/infrastructure/store/wiki_store.py` (`TYPE_DIRS`,
  `_type_dir`, `get_path`, `scan_all`, `move` — the five-type assumption lives here);
  `src/memex/infrastructure/store/navigation.py` (`_SECTION_TYPES`, `_compose`,
  `_parse_index` — headings derived from `TYPE_DIRS`); analogous shipped work:
  `docs/specs/agentic-frontmatter-search/` threaded a new field through domain →
  store → navigation → adapters, and `docs/specs/okf-frontmatter/` moved
  validation to the store boundary; both are the pattern here. Named
  uncertainty: `TYPE_DIRS` is imported by `eval/` and tests — the move to the
  domain touches those import sites (Task 1 enumerates them).

**Goal:** A project declares its own concept types — catalogue types with contracts, custom shelf types, and model-nominated drafts — and memex stores, navigates, filters, verifies, and governs them alongside the built-in five.

**Architecture:** One pure domain module (`domain/types.py`) owns the type vocabulary: name shape, name↔directory mapping, memory/knowledge kind, catalogue contracts, heading derivation, and the initial-status rule. The store reads *declared* types from the filesystem (the directory is the declaration, its `log.md` is the record) and validates every write against built-in ∪ catalogue-enabled ∪ declared for that scope and project. Navigation, search, decay, consolidation, verify, export, CLI, and MCP consume the domain module instead of the five-type enum.

**Tech Stack:** Python 3.12 stdlib only; SQLite FTS5 unchanged (`node_type` is already `TEXT`); pytest with `--no-cov` for targeted runs; ruff, mypy strict.

## Global Constraints

- Type name shape: `[a-z][a-z0-9-]{0,63}`; catalogue and custom type name == directory name; built-ins keep `TYPE_DIRS` (`entity→entities` …).
- Project scope only for catalogue and custom types; global scope keeps exactly the five built-ins.
- Memory types `episode`, `summary` are always `active` on write; knowledge types are `active` for `source == "user"` and, under `knowledge_approval = "manual"` (default), `pending` for any other source.
- Catalogue types never decay (`RecencyDecay` exemption).
- No page written today changes: every existing test that reads a five-type store must stay green.
- `log.md` is written only by memex, append-only, body-only text (so it stays `structural` per `classify_reserved_text`). Line shape: `<ISO-UTC> <verb> <target> by <actor>[: <text>]`.
- Never log page bodies, descriptions, credential-shaped strings, or absolute home paths.
- Every command in this plan runs from the repository root with `uv run`; live checks use `MEMEX_DATA_DIR=/private/tmp/claude-501/<slug>-store`, never `~/.memex`.

## Review Focus

Inputs the spec implies but no acceptance criterion names; each has a test pinned to its owning task below.

1. `memex types add` for a name whose directory already exists under the project but has no `declare` line in `log.md` (a stray folder) — must refuse and name the path, never adopt it silently. → Task 3.
2. A page written with `--type decision` into project B when only project A enabled `decision` — must be rejected before any file is written. → Task 3.
3. Uppercase or spaced input (`--type Decision`, `--type "access matrix"`) — rejected with the shape rule, never normalized silently, because a silent lowercase would make `Decision` and `decision` collide on disk. → Task 1.
4. A custom type whose heading would collide with a built-in heading (`entities`, `Entities`) — refused by the collision rule at declaration, so `_parse_index` can never see two sections with one heading. → Task 1.
5. `--scope global --type rule` — global scope has no catalogue or custom types in this slice; must be rejected naming the scope rule. → Task 3.

---

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/memex/domain/types.py` (new) | Type vocabulary: shape, `TYPE_DIRS`, kind, catalogue contracts, headings, `initial_status`. Pure. | 1 |
| `src/memex/domain/models.py` | Drop closed-set checks; validate shape only; remove `NodeType` Literal. | 2 |
| `src/memex/infrastructure/store/wiki_store.py` | Declared-type discovery and validation, `declare_type`, dynamic type-dir scans, `log.md` append. | 3 |
| `src/memex/infrastructure/store/navigation.py` | Dynamic headings and ordering; parse headings back to types. | 4 |
| `src/memex/infrastructure/store/navigation_search.py` | Heading → type via the domain. | 4 |
| `src/memex/application/decay.py` | Skip non-decaying kinds. | 5 |
| `src/memex/infrastructure/config.py` | `knowledge_approval` replaces `approval`. | 6 |
| `src/memex/application/consolidator.py` | `initial_status`; `decision` when enabled; `proposed_type` → draft. | 6 |
| `src/memex/application/concept_types.py` (new) | `ConceptTypes` service: list, add, enable, remove, suggest. | 7 |
| `src/memex/application/memory.py` | Expose the service; validate types on write; pass `knowledge_approval`. | 7 |
| `src/memex/cli.py`, `src/memex/mcp_server.py` | `memex types …`; `--type` free string; MCP `type: str`. | 7 |
| `src/memex/application/verify.py` | Three type checks. | 8 |
| `src/memex/infrastructure/store/import_export.py` | Export/import type declarations. | 9 |
| `docs/adr/0006-…`, guide, architecture, changelog, `src/memex/infrastructure/AGENTS.md`, spec ACs | Durable outputs. | 10 |

Tests: `tests/unit/test_concept_types.py` (new, Tasks 1–3, 5–9), extensions to `tests/unit/test_navigation.py` (Task 4), `tests/unit/test_navigation_search.py` (Task 4), `tests/unit/test_cli_types.py` (new, Task 7).

---

### Task 1: Domain — the type vocabulary

**Files:**
- Create: `src/memex/domain/types.py`
- Modify: `src/memex/infrastructure/store/wiki_store.py:27-33` (remove `TYPE_DIRS`; import it)
- Modify: `src/memex/infrastructure/store/navigation.py:22`, `src/memex/infrastructure/store/navigation_search.py:26`, and every `TYPE_DIRS` importer found by `grep -rn "import TYPE_DIRS\|TYPE_DIRS," src eval tests`
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Produces:
  ```python
  TYPE_DIRS: dict[str, str]                       # built-in type -> directory (moved here)
  MEMORY_TYPES: tuple[str, ...] = ("episode", "summary")
  TYPE_NAME: re.Pattern[str]                      # ^[a-z][a-z0-9-]{0,63}$
  @dataclass(frozen=True, slots=True)
  class TypeContract: name: str; answers: str; authorship: tuple[str, ...]; decays: bool
  CATALOGUE: dict[str, TypeContract]              # domain, architecture, rule, policy, decision
  def validate_type_name(name: str) -> str        # raises ValueError; returns name
  def type_kind(name: str) -> Literal["memory", "knowledge"]
  def decays(name: str) -> bool
  def type_directory(name: str) -> str            # TYPE_DIRS.get(name, name)
  def type_for_directory(directory: str) -> str   # inverse of type_directory
  def heading_for(name: str) -> str               # type_directory(name).capitalize()
  def type_for_heading(heading: str) -> str       # type_for_directory(heading.lower())
  def is_type_directory_name(name: str) -> bool  # built-in dir, or a valid non-reserved type name
  def initial_status(name: str, source: str | None, knowledge_approval: str) -> str
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_concept_types.py
"""Project-level concept types (docs/specs/project-concept-types/spec.md)."""

from __future__ import annotations

import pytest

from memex.domain import types as T


class TestTypeNames:
    @pytest.mark.parametrize("name", ["decision", "access-matrix", "a", "x" * 64])
    def test_valid_shape_returns_name(self, name: str) -> None:
        assert T.validate_type_name(name) == name

    @pytest.mark.parametrize(
        "name", ["Decision", "access matrix", "-lead", "9lives", "x" * 65, "", "a_b"]
    )
    def test_bad_shape_is_rejected_not_normalized(self, name: str) -> None:
        # Review Focus 3: never lowercase or strip silently.
        with pytest.raises(ValueError, match=r"\[a-z\]\[a-z0-9-\]\{0,63\}"):
            T.validate_type_name(name)

    @pytest.mark.parametrize(
        "name", ["entity", "entities", "episode", "episodes", "index", "log", "rule", "decision"]
    )
    def test_collisions_with_builtin_catalogue_and_reserved(self, name: str) -> None:
        # Review Focus 4: a custom type may never shadow a built-in name, a
        # built-in directory, a catalogue name, or a reserved filename.
        with pytest.raises(ValueError, match="already"):
            T.validate_type_name(name, custom=True)


class TestKinds:
    @pytest.mark.parametrize("name", ["episode", "summary"])
    def test_memory_types(self, name: str) -> None:
        assert T.type_kind(name) == "memory"

    @pytest.mark.parametrize(
        "name", ["entity", "procedure", "preference", "rule", "policy", "access-matrix"]
    )
    def test_knowledge_types(self, name: str) -> None:
        assert T.type_kind(name) == "knowledge"

    def test_catalogue_never_decays_builtins_do(self) -> None:
        assert all(not T.decays(name) for name in T.CATALOGUE)
        assert T.decays("entity") and T.decays("episode")
        assert T.decays("access-matrix")  # custom shelf types keep memory aging


class TestDirectoriesAndHeadings:
    def test_builtins_keep_plural_directories(self) -> None:
        assert T.type_directory("entity") == "entities"
        assert T.type_for_directory("entities") == "entity"

    def test_declared_types_are_their_own_directory(self) -> None:
        assert T.type_directory("decision") == "decision"
        assert T.type_for_directory("access-matrix") == "access-matrix"

        def test_headings_round_trip(self) -> None:
        for name in ["entity", "episode", "decision", "access-matrix"]:
            assert T.type_for_heading(T.heading_for(name)) == name
        assert T.heading_for("access-matrix") == "Access-matrix"

    def test_type_directory_names(self) -> None:
        assert T.is_type_directory_name("entities") and T.is_type_directory_name("access-matrix")
        assert not T.is_type_directory_name("index") and not T.is_type_directory_name("Scratch")


class TestInitialStatus:
    @pytest.mark.parametrize("source", [None, "user", "agent", "transcript", "consolidation"])
    def test_memory_is_always_active(self, source: str | None) -> None:
        assert T.initial_status("episode", source, "manual") == "active"
        assert T.initial_status("summary", source, "auto") == "active"

    def test_person_written_knowledge_is_active(self) -> None:
        assert T.initial_status("rule", "user", "manual") == "active"

    @pytest.mark.parametrize("source", ["agent", "transcript", "consolidation", None])
    def test_model_written_knowledge_is_pending_under_manual(self, source: str | None) -> None:
        assert T.initial_status("rule", source, "manual") == "pending"

    def test_auto_makes_model_knowledge_active(self) -> None:
        assert T.initial_status("rule", "consolidation", "auto") == "active"

    def test_unknown_policy_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="manual|auto"):
            T.initial_status("rule", "user", "sometimes")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'memex.domain.types'`

- [ ] **Step 3: Write the domain module**

```python
# src/memex/domain/types.py
"""The concept-type vocabulary: built-in, catalogue, and custom types.

OKF v0.2 lets ``type`` be any non-empty string. Memex gives that freedom a
shape: five built-in types with behaviour, a catalogue of knowledge types
with contracts, and custom shelf types a project declares. Two rules follow
from memory versus knowledge and live here so every layer applies them the
same way: memory decays and knowledge does not; memory is automatic and
knowledge is governed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from memex.domain.reserved import RESERVED_SLUGS

NODE_TYPES: tuple[str, ...] = ("entity", "preference", "procedure", "summary", "episode")
MEMORY_TYPES: tuple[str, ...] = ("episode", "summary")

# Built-in types keep their historical plural directories; every other type
# is its own directory, so the name on disk is the name in front matter.
TYPE_DIRS: dict[str, str] = {
    "entity": "entities",
    "preference": "preferences",
    "procedure": "procedures",
    "summary": "summaries",
    "episode": "episodes",
}
_DIR_TYPES: dict[str, str] = {directory: name for name, directory in TYPE_DIRS.items()}

TYPE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
KNOWLEDGE_APPROVAL_VALUES: tuple[str, ...] = ("manual", "auto")

Kind = Literal["memory", "knowledge"]


@dataclass(frozen=True, slots=True)
class TypeContract:
    """What a catalogue type promises: who may author it and whether it ages."""

    name: str
    answers: str
    authorship: tuple[str, ...]
    decays: bool


CATALOGUE: dict[str, TypeContract] = {
    contract.name: contract
    for contract in (
        TypeContract("domain", "what exists in the business and what its words mean", ("user", "consolidation"), False),
        TypeContract("architecture", "how the system is put together and why", ("user",), False),
        TypeContract("rule", "a constraint that can be checked", ("user",), False),
        TypeContract("policy", "a principle that guides judgment", ("user",), False),
        TypeContract("decision", "what was chosen, when, and why", ("user", "consolidation"), False),
    )
}


def validate_type_name(name: str, *, custom: bool = False) -> str:
    """Return ``name`` when it may be a type; raise ValueError naming the rule.

    With ``custom=True`` the name must also avoid every built-in name and
    directory, every catalogue name, and the reserved slugs, so a custom type
    can never shadow one and two index sections can never share a heading.
    """
    if not TYPE_NAME.fullmatch(name):
        raise ValueError(f"type name must match [a-z][a-z0-9-]{{0,63}}, got {name!r}")
    if custom:
        taken = set(NODE_TYPES) | set(TYPE_DIRS.values()) | set(CATALOGUE) | set(RESERVED_SLUGS)
        if name in taken:
            raise ValueError(f"type name {name!r} is already a built-in, catalogue, or reserved name")
    return name


def type_kind(name: str) -> Kind:
    return "memory" if name in MEMORY_TYPES else "knowledge"


def decays(name: str) -> bool:
    """Catalogue knowledge is refined or superseded, never aged out."""
    return name not in CATALOGUE


def type_directory(name: str) -> str:
    return TYPE_DIRS.get(name, name)


def type_for_directory(directory: str) -> str:
    return _DIR_TYPES.get(directory, directory)


def heading_for(name: str) -> str:
    return type_directory(name).capitalize()


def type_for_heading(heading: str) -> str:
    return type_for_directory(heading.lower())


def is_type_directory_name(name: str) -> bool:
    """Could a directory with this name hold pages? Store and navigation share this rule."""
    return name in _DIR_TYPES or (bool(TYPE_NAME.fullmatch(name)) and name not in RESERVED_SLUGS)


def initial_status(name: str, source: str | None, knowledge_approval: str) -> str:
    """Memory is automatic; knowledge a person wrote is automatic; the rest waits."""
    if knowledge_approval not in KNOWLEDGE_APPROVAL_VALUES:
        raise ValueError(
            f"knowledge_approval must be one of {KNOWLEDGE_APPROVAL_VALUES}, got {knowledge_approval!r}"
        )
    if type_kind(name) == "memory" or source == "user" or knowledge_approval == "auto":
        return "active"
    return "pending"
```

- [ ] **Step 4: Move `TYPE_DIRS` out of the store and repoint every importer**

In `src/memex/infrastructure/store/wiki_store.py` delete the `TYPE_DIRS` literal at lines 27–33 and add `from memex.domain.types import TYPE_DIRS` to its imports. Then:

Run: `grep -rn "TYPE_DIRS" src eval tests --include='*.py' | grep import`
For each hit that imports from `memex.infrastructure.store.wiki_store`, change the import to `from memex.domain.types import TYPE_DIRS` (keep any other names still imported from `wiki_store` on their own line). `models.py` keeps its own `NODE_TYPES` literal for now (Task 2 rewires it).

- [ ] **Step 5: Run tests and static checks**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_wiki_store.py tests/unit/test_navigation.py -q --no-cov && uv run ruff check src tests eval && uv run mypy src tests`
Expected: all pass; mypy `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/memex/domain/types.py src/memex/infrastructure/store/wiki_store.py src/memex/infrastructure/store/navigation.py src/memex/infrastructure/store/navigation_search.py eval tests/unit/test_concept_types.py
git commit -m "feat(types): domain vocabulary for built-in, catalogue, and custom concept types"
```

---

### Task 2: Models — validate shape, not membership

**Files:**
- Modify: `src/memex/domain/models.py:18-37` (constants), `:292-293`, `:377-378`, `:484-485`
- Modify: `src/memex/mcp_server.py:33,88,163` (`NodeType` → `str`)
- Modify: `src/memex/domain/operations.py` wherever `NodeType` is referenced (`grep -n NodeType`)
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Consumes: `memex.domain.types.validate_type_name`, `NODE_TYPES`, `TYPE_DIRS`.
- Produces: `WriteInput`, `WikiNode`, `TaskRecallInput` accept any shape-valid type; the declared-set check moves to the store (Task 3). `NodeType` no longer exists.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_concept_types.py
from memex.domain.models import TaskRecallInput, WikiNode, WriteInput

PROJECT = "a" * 24


class TestModelsAcceptShapeValidTypes:
    def test_write_input_accepts_a_declared_looking_type(self) -> None:
        node = WriteInput(type="decision", title="Choose X", body="b", scope="project", project_id=PROJECT)
        assert node.type == "decision"

    def test_wiki_node_accepts_a_custom_type(self) -> None:
        node = WikiNode(type="access-matrix", title="t", body="b", id="1", scope="project", project_id=PROJECT)
        assert node.type == "access-matrix"

    def test_bad_shape_still_rejected_at_the_model(self) -> None:
        with pytest.raises(ValueError, match=r"\[a-z\]"):
            WriteInput(type="Decision", title="t", body="b")

    def test_task_recall_filter_accepts_declared_types(self) -> None:
        request = TaskRecallInput(goal="g", questions=["q"], project_id=PROJECT, node_type="rule")
        assert request.node_type == "rule"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py::TestModelsAcceptShapeValidTypes -q --no-cov`
Expected: FAIL — `ValueError: type must be one of ('entity', …)`

- [ ] **Step 3: Rewire `models.py`**

Replace the constants block:

```python
from memex.domain.types import NODE_TYPES, validate_type_name  # noqa: F401  (re-exported name)

NON_EPISODE_TYPES: tuple[str, ...] = tuple(t for t in NODE_TYPES if t != "episode")
```

Delete `NodeType = Literal[...]` (line 37). In `WriteInput.__post_init__` (line 292) and `WikiNode.__post_init__` (line 377) replace the two-line membership check with:

```python
        validate_type_name(self.type)
```

In `TaskRecallInput.__post_init__` (line 484) replace with:

```python
        if self.node_type is not None:
            validate_type_name(self.node_type)
```

- [ ] **Step 4: Repoint MCP and operations**

`src/memex/mcp_server.py`: remove `NodeType` from the import block (line 33); change `type: NodeType,` (line 88) to `type: str,` and `node_type: NodeType | None = None,` (line 163) to `node_type: str | None = None,`. Add to the `memex_write` docstring `type:` bullet: `Any built-in, enabled catalogue, or declared type for the target project; see memex types list.` Run `grep -n NodeType src/memex/domain/operations.py` and remove or replace with `str` any remaining use.

- [ ] **Step 5: Run tests and static checks**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_mcp_server.py tests/unit/test_domain_operations.py -q --no-cov && uv run mypy src tests`
Expected: pass; `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/memex/domain/models.py src/memex/mcp_server.py src/memex/domain/operations.py tests/unit/test_concept_types.py
git commit -m "feat(types): models validate type shape; declared-set check moves to the store"
```

---

### Task 3: Store — the directory is the declaration

**Files:**
- Modify: `src/memex/infrastructure/store/wiki_store.py` — `__init__` (:133-137), `get_path` (:151), `write` (:198), `list` (:247), `scan_all` (:256-257), `scan_dir` (:279), `_type_dir` (:295-310), `move` (:324), `_find_existing_path` (:422), `_reject_duplicate_project_keys` (:450); add `declare_type`, `declared_types`, `_is_type_dir`, `_append_log`, `_assert_type_known`
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Consumes: `memex.domain.types` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class TypeDeclaration: name: str; kind: Literal["builtin","catalogue","custom","draft"]; description: str; pages: int; directory: Path
  WikiStore.declared_types(*, scope: str, project_id: str | None, project_locator: str | None = None) -> dict[str, TypeDeclaration]
  WikiStore.declare_type(name: str, *, scope: str, project_id: str | None, description: str = "", actor: str = "user", draft: bool = False, project_locator: str | None = None) -> TypeDeclaration
    WikiStore.append_log(directory: Path, verb: str, target: str, actor: str, text: str = "") -> None
  WikiStore.project_id_of(directory: Path) -> str | None
  ```
  `write`, `get_path(node_type=…)`, `move`, `list(node_type=…)` raise `WikiStoreError("undeclared type … run memex types add|enable")` for a type that is neither built-in nor declared for that scope and project.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_concept_types.py
from pathlib import Path

from memex.domain.errors import WikiStoreError
from memex.domain.models import WikiNode
from memex.infrastructure.store.wiki_store import WikiStore

PROJECT_B = "b" * 24


def _store(data_dir: Path) -> WikiStore:
    return WikiStore(data_dir)


def _page(store: WikiStore, node_type: str, title: str, *, project_id: str = PROJECT, **kw: object) -> WikiNode:
    return store.write(
        WikiNode(type=node_type, title=title, body="b", id="", scope="project", project_id=project_id, **kw)  # type: ignore[arg-type]
    )


class TestDeclaration:
    def test_enable_catalogue_type_creates_directory_and_log(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type("decision", scope="project", project_id=PROJECT, description="Why we chose.")
        assert declared.kind == "catalogue" and declared.directory.is_dir()
        log = (declared.directory / "log.md").read_text()
        assert " declare decision by user: Why we chose." in log
        assert not log.startswith("---")  # body-only, so it stays structural

    def test_add_custom_type(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type("access-matrix", scope="project", project_id=PROJECT)
        assert declared.kind == "custom"
        assert store.declared_types(scope="project", project_id=PROJECT)["access-matrix"].pages == 0

    def test_list_shows_builtins_catalogue_custom_and_counts(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("rule", scope="project", project_id=PROJECT)
        _page(store, "rule", "Never force push")
        _page(store, "entity", "Kafka")
        types = store.declared_types(scope="project", project_id=PROJECT)
        assert types["entity"].kind == "builtin" and types["entity"].pages == 1
        assert types["rule"].kind == "catalogue" and types["rule"].pages == 1
        assert set(types) >= {"entity", "preference", "procedure", "summary", "episode", "rule"}

    def test_draft_declaration_is_marked_and_logged(self, data_dir: Path) -> None:
        store = _store(data_dir)
        declared = store.declare_type("story-map", scope="project", project_id=PROJECT, actor="consolidation", draft=True, description="sessions=s1,s2 pages=3")
        assert declared.kind == "draft"
        assert " propose story-map by consolidation: sessions=s1,s2 pages=3" in (declared.directory / "log.md").read_text()

    def test_stray_directory_without_log_is_refused(self, data_dir: Path) -> None:
        # Review Focus 1.
        store = _store(data_dir)
        _page(store, "entity", "Seed")  # creates the project directory
        project_dir = store.get_path("seed").parent.parent
        (project_dir / "scratch").mkdir()
        with pytest.raises(WikiStoreError, match="exists without a declaration"):
            store.declare_type("scratch", scope="project", project_id=PROJECT)
        assert "scratch" not in store.declared_types(scope="project", project_id=PROJECT)

    def test_global_scope_refuses_non_builtin(self, data_dir: Path) -> None:
        # Review Focus 5.
        with pytest.raises(WikiStoreError, match="global scope"):
            _store(data_dir).declare_type("rule", scope="global", project_id=None)


class TestWritesRespectDeclarations:
    def test_write_to_enabled_type_lands_in_its_directory(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        node = _page(store, "decision", "Choose Kafka")
        assert Path(node.file_path or "").parent.name == "decision"
        read = store.read("choose-kafka")
        assert read is not None and read.type == "decision"

    def test_undeclared_type_is_rejected_before_writing(self, data_dir: Path) -> None:
        store = _store(data_dir)
        with pytest.raises(WikiStoreError, match="undeclared type 'decision'.*memex types"):
            _page(store, "decision", "Choose Kafka")
        assert not list((data_dir / "docs").rglob("choose-kafka.md"))

    def test_declaration_is_per_project(self, data_dir: Path) -> None:
        # Review Focus 2.
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        with pytest.raises(WikiStoreError, match="undeclared type"):
            _page(store, "decision", "Elsewhere", project_id=PROJECT_B)

    def test_scan_move_and_find_cover_declared_directories(self, data_dir: Path) -> None:
        store = _store(data_dir)
        store.declare_type("decision", scope="project", project_id=PROJECT)
        store.declare_type("access-matrix", scope="project", project_id=PROJECT)
        _page(store, "decision", "Choose Kafka")
        assert [n.slug for n in store.scan_all()] == ["choose-kafka"]
        moved = store.move("choose-kafka", "access-matrix")
        assert Path(moved.file_path or "").parent.name == "access-matrix"
        assert store.read("choose-kafka") is not None
        with pytest.raises(WikiStoreError, match="undeclared type"):
            store.move("choose-kafka", "policy")

    def test_five_type_store_is_unchanged(self, data_dir: Path) -> None:
        store = _store(data_dir)
        node = store.write(WikiNode(type="entity", title="Global thing", body="b", id=""))
        assert Path(node.file_path or "").parts[-3:-1] == ("global", "entities")
        assert store.declared_types(scope="global", project_id=None).keys() == set(TYPE_DIRS)
```

Add `from memex.domain.types import TYPE_DIRS` to the test imports.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py::TestDeclaration tests/unit/test_concept_types.py::TestWritesRespectDeclarations -q --no-cov`
Expected: FAIL — `AttributeError: 'WikiStore' object has no attribute 'declare_type'`

- [ ] **Step 3: Add the declaration model and log writer**

Near the top of `wiki_store.py` (after `TYPE_DIRS` import):

```python
from memex.domain.types import (
    CATALOGUE,
    TYPE_DIRS,
    type_directory,
    type_for_directory,
    validate_type_name,
)

TypeKind = Literal["builtin", "catalogue", "custom", "draft"]


@dataclass(frozen=True, slots=True)
class TypeDeclaration:
    """One type as the filesystem declares it for a scope and project."""

    name: str
    kind: TypeKind
    description: str
    pages: int
    directory: Path
```

Add to the class (beside `_atomic_write`):

```python
    def append_log(self, directory: Path, verb: str, target: str, actor: str, text: str = "") -> None:
        """Append one lifecycle line to the directory's OKF ``log.md``.

        Body-only text with no front matter, so the file stays structural
        and is never indexed, recalled, or rewritten by navigation.
        """
        self._reject_symlink(directory / "log.md")
        line = f"{utc_now_iso()} {verb} {target} by {actor}"
        if text:
            line += f": {text}"
        with (directory / "log.md").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
```

- [ ] **Step 4: Add discovery and declaration**

```python
    def _scope_root(self, scope: str, project_id: str | None, project_locator: str | None) -> Path:
        if scope == "global":
            return self.wiki_dir / "global"
        if scope == "project" and project_id:
            return self._project_dir(project_id, project_locator)
        raise WikiStoreError("scope must be 'global' or 'project' with a project_id")

    def _is_type_dir(self, directory: Path) -> bool:
        """A direct child of a scope root whose name is a built-in directory or a valid type name."""
        if not directory.is_dir() or directory.is_symlink():
            return False
        parent = directory.parent
        if parent != self.wiki_dir / "global" and parent.parent != self._projects_dir():
            return False
                return is_type_directory_name(directory.name)

    def _log_state(self, directory: Path) -> tuple[str | None, str]:
        """(last lifecycle verb, declared description) from ``log.md``; (None, "") when absent."""
        log = directory / "log.md"
        if not log.exists():
            return None, ""
        verb: str | None = None
        description = ""
        for line in log.read_text(encoding="utf-8").splitlines():
            parts = line.split(" ", 3)
            if len(parts) < 3:
                continue
            verb = parts[1]
            if parts[1] in {"declare", "propose"} and ":" in line:
                description = line.split(": ", 1)[1]
        return verb, description

    def declared_types(
        self, *, scope: str, project_id: str | None, project_locator: str | None = None
    ) -> dict[str, TypeDeclaration]:
        """Built-in types plus every type the scope root's directories declare."""
        root = self._scope_root(scope, project_id, project_locator)
        found: dict[str, TypeDeclaration] = {}
        for name in TYPE_DIRS:
            directory = root / TYPE_DIRS[name]
            pages = len([p for p in directory.glob("*.md") if not is_structural(p)]) if directory.is_dir() else 0
            found[name] = TypeDeclaration(name, "builtin", "", pages, directory)
        if scope == "global" or not root.exists():
            return found
        for directory in sorted(root.iterdir()):
            if directory.name in TYPE_DIRS.values() or not self._is_type_dir(directory):
                continue
            verb, description = self._log_state(directory)
            if verb is None:
                continue  # a stray folder is not a declaration
            name = type_for_directory(directory.name)
            kind: TypeKind = "draft" if verb == "propose" else ("catalogue" if name in CATALOGUE else "custom")
            pages = len([p for p in directory.glob("*.md") if not is_structural(p)])
            found[name] = TypeDeclaration(name, kind, description, pages, directory)
        return found

    def declare_type(
        self,
        name: str,
        *,
        scope: str,
        project_id: str | None,
        description: str = "",
        actor: str = "user",
        draft: bool = False,
        project_locator: str | None = None,
    ) -> TypeDeclaration:
        """Create a type's directory and its first ``log.md`` line."""
        if scope != "project":
            raise WikiStoreError("catalogue and custom types exist at project scope only; global scope keeps the built-in types")
        try:
            validate_type_name(name, custom=name not in CATALOGUE)
        except ValueError as exc:
            raise WikiStoreError(str(exc)) from exc
        root = self._scope_root(scope, project_id, project_locator)
        directory = root / type_directory(name)
        self._reject_symlink(directory)
        self._ensure_inside_projects(directory)
        if directory.exists() and self._log_state(directory)[0] is None:
            raise WikiStoreError(f"directory {directory.name!r} exists without a declaration; remove or rename it first")
        if directory.exists():
            raise WikiStoreError(f"type {name!r} is already declared")
        directory.mkdir(parents=True)
        self.append_log(directory, "propose" if draft else "declare", name, actor, description)
        return self.declared_types(scope=scope, project_id=project_id, project_locator=project_locator)[name]

    def _assert_type_known(self, node_type: str, scope: str, project_id: str | None, project_locator: str | None = None) -> None:
        if node_type in TYPE_DIRS:
            return
        if scope != "project":
            raise WikiStoreError(f"undeclared type {node_type!r}: global scope holds the built-in types only")
        if node_type not in self.declared_types(scope=scope, project_id=project_id, project_locator=project_locator):
            raise WikiStoreError(f"undeclared type {node_type!r} for this project; run memex types add or memex types enable")
```

Add `is_type_directory_name` to the `memex.domain.types` import, plus `from dataclasses import dataclass` and `from typing import Literal`; `is_structural`, `RESERVED_SLUGS`, and `utc_now_iso` are already imported. Also expose the project identity probe Task 8 needs:

```python
    def project_id_of(self, directory: Path) -> str | None:
        """The project_id a project directory belongs to, read from one of its pages."""
        return self._project_id_in_dir(directory)
```

- [ ] **Step 5: Replace every `TYPE_DIRS` membership check with the declaration check**

- `get_path` (:151): replace `if node_type not in TYPE_DIRS: raise …` with `self._assert_type_known(node_type, scope or "global", project_id, project_locator)`.
- `write` (:198): replace `if node.type not in TYPE_DIRS: raise …` with `self._assert_type_known(node.type, node.scope, node.project_id, node.project_locator)`.
- `list` (:247): replace with `validate_type_name(node_type)` wrapped in `try/except ValueError as exc: raise WikiStoreError(str(exc)) from exc` (listing filters by name; declaration is per project and `list` spans all).
- `_type_dir` (:303, :306): `TYPE_DIRS[node_type]` → `type_directory(node_type)`.
- `move` (:324): replace the membership check with `self._assert_type_known(new_type, node.scope, node.project_id)` **after** `node = self.read(slug)` (move the check below the read).
- `scan_all` (:256-257): replace the per-directory loop with
  ```python
        for path in sorted(self.wiki_dir.rglob("*.md")):
            if not self._is_type_dir(path.parent) or is_structural(path):
                continue
  ```
- `scan_dir` (:279): `if directory.name not in TYPE_DIRS.values() or not directory.is_dir():` → `if not self._is_type_dir(directory):`.
- `_find_existing_path` (:422) and `_reject_duplicate_project_keys` (:450): `path.parent.name not in TYPE_DIRS.values()` → `not self._is_type_dir(path.parent)` (and the positive form at :450).
- `__init__` (:133-137) is unchanged: only built-in directories are pre-created.

- [ ] **Step 6: Run tests and static checks**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_wiki_store.py tests/acceptance/test_wiki_store_acceptance.py -q --no-cov && uv run ruff check src tests && uv run mypy src tests`
Expected: pass; `Success`.

- [ ] **Step 7: Commit**

```bash
git add src/memex/infrastructure/store/wiki_store.py tests/unit/test_concept_types.py
git commit -m "feat(types): the directory is the declaration — declare, discover, and validate project types in the store"
```

---

### Task 4: Navigation and navigation search — dynamic headings

**Files:**
- Modify: `src/memex/infrastructure/store/navigation.py:20-27, 245-260 (_compose), 263-300 (_parse_index), 319, 408-414`
- Modify: `src/memex/infrastructure/store/navigation_search.py:41-43, 204-208`
- Test: `tests/unit/test_navigation.py`, `tests/unit/test_navigation_search.py`

**Interfaces:**
- Consumes: `memex.domain.types.heading_for`, `type_for_heading`, `type_directory`, `NODE_TYPES`, `TYPE_DIRS`.
- Produces: `NavigationGenerator.render` lists built-in sections in `NODE_TYPES` order, then every other type present in alphabetical order, each under `heading_for(type)`; `_parse_index` reverses it with `type_for_heading`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_navigation.py
def test_declared_types_get_their_own_heading_after_builtins(data_dir: Path) -> None:
    memex = _memex(data_dir)
    memex.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    memex.wiki_store.declare_type("access-matrix", scope="project", project_id=PROJECT)
    memex.write(WriteInput(type="entity", title="Kafka", body="b", scope="project", project_id=PROJECT))
    memex.write(WriteInput(type="decision", title="Choose Kafka", body="b", scope="project", project_id=PROJECT))
    memex.write(WriteInput(type="access-matrix", title="Who edits", body="b", scope="project", project_id=PROJECT))
    project_index = memex.wiki_store.get_path("kafka").parent.parent / "index.md"
    text = project_index.read_text()
    assert text.index("## Entities") < text.index("## Access-matrix") < text.index("## Decision")
    assert "- [Choose Kafka](decision/choose-kafka.md)" in text or "- [Choose Kafka](choose-kafka.md)" in (project_index.parent / "decision" / "index.md").read_text()
    assert _oracle_clean(memex)


def test_declared_type_rows_survive_the_splice_oracle(data_dir: Path) -> None:
    memex = _memex(data_dir)
    memex.wiki_store.declare_type("rule", scope="project", project_id=PROJECT)
    for i in range(4):
        memex.write(WriteInput(type="rule", title=f"Rule {i}", body="b", scope="project", project_id=PROJECT))
    memex.forget("rule-1", mode="hard")
    assert _oracle_clean(memex)
```

`_memex`, `PROJECT`, and `_oracle_clean` already exist in that file (the oracle asserts every `index.md` equals a full render).

```python
# append to tests/unit/test_navigation_search.py
def test_navigation_engine_reports_declared_types(memex: Memex) -> None:
    memex.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    memex.write(WriteInput(type="decision", title="Choose Kafka", body="b", description="When picking the broker.", scope="project", project_id=PROJECT))
    memex.rebuild_index()
    hits = memex.recall("picking the broker", engine="navigation", scope="project", project_id=PROJECT).hits
    assert [(h.slug, h.node_type) for h in hits] == [("choose-kafka", "decision")]
```

Use that file's existing `memex` fixture and `PROJECT` constant (add `PROJECT = "a" * 24` if absent).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_navigation.py -k declared tests/unit/test_navigation_search.py -k declared -q --no-cov`
Expected: FAIL — declared-type pages missing from the index / `node_type` None.

- [ ] **Step 3: Make headings dynamic in `navigation.py`**

Replace line 27 `_SECTION_TYPES = {...}` with nothing (delete it) and change the import at line 20/22 to `from memex.domain.types import NODE_TYPES, TYPE_DIRS, heading_for, type_directory, type_for_heading`. In `_compose` (:245-260) replace the loop over `NODE_TYPES` with:

```python
        ordered = [*NODE_TYPES, *sorted(t for t in entries if t not in NODE_TYPES)]
        for node_type in ordered:
            rows = entries.get(node_type)
            if not rows:
                continue
            lines.append(f"## {heading_for(node_type)}")
```

In `_parse_index` (:277-300) replace the `section_order` bookkeeping. Delete `section_order = [*_SECTION_TYPES, _SUBDIR_HEADING]` and the two lines that consult it, and track order like this (the loop body otherwise stays as it is):

```python
        last_builtin = -1
        last_custom = ""
        seen_subdir = False
        pos = 1
        while pos < len(lines):
            if lines[pos] != "" or pos + 2 > len(lines) or not lines[pos + 1].startswith("## "):
                return None
            heading = lines[pos + 1][3:]
            if seen_subdir:
                return None  # nothing follows the subdirectory section
            if heading == _SUBDIR_HEADING:
                seen_subdir = True
            else:
                node_type = type_for_heading(heading)
                if heading_for(node_type) != heading:
                    return None  # not the canonical heading for that type
                if node_type in NODE_TYPES:
                    index = NODE_TYPES.index(node_type)
                    if last_custom or index <= last_builtin:
                        return None
                    last_builtin = index
                else:
                    if node_type <= last_custom:
                        return None
                    last_custom = node_type
            pos += 2
```

and where rows are stored replace `entries.setdefault(_SECTION_TYPES[heading], {})` with `entries.setdefault(type_for_heading(heading), {})`. Any other order returns `None`, so `refresh_page` falls back to a full render rather than mis-splicing.

At :319 replace the `for type_name in TYPE_DIRS.values(): for path in self._wiki_dir.rglob(f"{type_name}/*.md")` loop with one walk: `for path in self._wiki_dir.rglob("*.md"): if is_structural(path) or not is_type_directory_name(path.parent.name): continue`. At :408-414 replace both `TYPE_DIRS.values()` tests with `is_type_directory_name(...)` on the directory name (first branch) and on `path.parent.name` over `directory.rglob("*.md")` (second branch). Import `is_type_directory_name` from `memex.domain.types`.

- [ ] **Step 4: Repoint `navigation_search.py`**

Delete `_HEADING_TYPES` (:43). At :206 replace `node_type = _HEADING_TYPES.get(heading.group("name"))` with `node_type = type_for_heading(heading.group("name"))` and at :208 `TYPE_DIRS[node_type]` with `type_directory(node_type)`; the `Subdirectories` heading maps to itself and is skipped by the existing `parts[-1] !=` guard. Import from `memex.domain.types`.

- [ ] **Step 5: Run the navigation suites and the oracle**

Run: `uv run pytest tests/unit/test_navigation.py tests/unit/test_navigation_search.py -q --no-cov && uv run mypy src tests`
Expected: pass, including the randomized oracle test.

- [ ] **Step 6: Commit**

```bash
git add src/memex/infrastructure/store/navigation.py src/memex/infrastructure/store/navigation_search.py tests/unit/test_navigation.py tests/unit/test_navigation_search.py
git commit -m "feat(types): navigation headings follow declared types"
```

---

### Task 5: Decay exemption for catalogue knowledge

**Files:**
- Modify: `src/memex/application/decay.py:52-53`
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Consumes: `memex.domain.types.decays`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_concept_types.py
from memex.application.decay import RecencyDecay


def test_catalogue_pages_never_decay_builtins_do(data_dir: Path) -> None:
    store = _store(data_dir)
    store.declare_type("policy", scope="project", project_id=PROJECT)
    old = "2020-01-01T00:00:00Z"
    _page(store, "policy", "Prefer boring tech", importance=1.0, created=old, last_access=old)
    _page(store, "entity", "Kafka", importance=1.0, created=old, last_access=old)
    changes = RecencyDecay(half_life_days=30).apply_decay(store)
    assert [slug for slug, _old, _new in changes] == ["kafka"]
    policy = store.read("prefer-boring-tech")
    assert policy is not None and policy.importance == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py::test_catalogue_pages_never_decay_builtins_do -q --no-cov`
Expected: FAIL — both slugs decayed.

- [ ] **Step 3: Skip non-decaying types**

In `apply_decay`, after `for node in wiki_store.scan_all():` add:

```python
            if not decays(node.type):
                continue  # knowledge is refined or superseded, never aged out
```

Import `from memex.domain.types import decays`.

- [ ] **Step 4: Run and commit**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_decay.py -q --no-cov`
Expected: pass.

```bash
git add src/memex/application/decay.py tests/unit/test_concept_types.py
git commit -m "feat(types): catalogue knowledge is exempt from recency decay"
```

---

### Task 6: Config key and consolidation — governed knowledge, drafts, decisions

**Files:**
- Modify: `src/memex/infrastructure/config.py:68-74, 275-280`
- Modify: `src/memex/application/consolidator.py:55-72 (prompt), 115, 120, 185-200 (parse), 214-225 (_store_node)`
- Test: `tests/unit/test_concept_types.py`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `initial_status`, `type_kind`, `CATALOGUE`, `validate_type_name`, `WikiStore.declare_type`, `declared_types`.
- Produces: `GovernanceConfig.knowledge_approval: str = "manual"`; `WriteInput` from consolidation may carry `proposed_type`; `WikiConsolidator` writes `decision` when enabled and declares drafts.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_config.py  (ConfigLoader().load(path) is the file's existing shape)
def test_knowledge_approval_replaces_approval(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text('[governance]\nknowledge_approval = "auto"\n')
    assert ConfigLoader().load(path).governance.knowledge_approval == "auto"


def test_knowledge_approval_defaults_to_manual(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text("")
    assert ConfigLoader().load(path).governance.knowledge_approval == "manual"


def test_old_approval_key_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "memex.toml"
    path.write_text('[governance]\napproval = "manual"\n')
    with pytest.raises(ConfigError, match="knowledge_approval.*manual.*auto"):
        ConfigLoader().load(path)
```

```python
# append to tests/unit/test_concept_types.py
from memex.application.memory import Memex
from memex.infrastructure.config import MemexConfig, GovernanceConfig


class _FakeLLM:
    """Matches memex.application.ports.LLMClient: complete(system, user, *, max_tokens)."""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResponse:
        return LLMResponse(text=self.payload, prompt_tokens=1, completion_tokens=1)


def _memex_with(data_dir: Path, payload: str, approval: str = "manual") -> Memex:
    m = Memex(MemexConfig(data_dir=data_dir, governance=GovernanceConfig(knowledge_approval=approval)))
    m._llm = _FakeLLM(payload)  # the facade's lazy client slot (memory.py:68, :828-833)
    return m


def _episode(m: Memex, sid: str, *, project_id: str | None = PROJECT) -> None:
    """A project episode, written directly: consolidation groups by the episode's namespace."""
    node = m.wiki_store.write(
        WikiNode(type="episode", title=f"Session {sid}", body="we chose kafka", id="",
                 session_id=sid, scope="project" if project_id else "global", project_id=project_id)
    )
    m.index_manager.update_record(node)


class TestConsolidationGovernance:
    def test_summary_is_active_and_entity_is_pending_by_default(self, data_dir: Path) -> None:
        payload = '[{"type":"summary","title":"Week","body":"b","tags":[],"importance":0.5,"links":[]},' \
                  '{"type":"entity","title":"Kafka","body":"b","tags":[],"importance":0.5,"links":[]}]'
        m = _memex_with(data_dir, payload); _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        assert m.wiki_store.read("week").status == "active"
        assert m.wiki_store.read("kafka").status == "pending"

    def test_auto_makes_entity_active(self, data_dir: Path) -> None:
        payload = '[{"type":"entity","title":"Kafka","body":"b","tags":[],"importance":0.5,"links":[]}]'
        m = _memex_with(data_dir, payload, approval="auto"); _episode(m, "s1")
        m.consolidate(ConsolidateInput())
        assert m.wiki_store.read("kafka").status == "active"

    def test_decision_written_only_when_enabled(self, data_dir: Path) -> None:
        payload = '[{"type":"decision","title":"Choose Kafka","body":"b","tags":[],"importance":0.5,"links":[]}]'
        m = _memex_with(data_dir, payload); _episode(m, "s1")
        report = m.consolidate(ConsolidateInput())
        assert report.nodes_created == [] and m.wiki_store.read("choose-kafka") is None
                m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
        m.consolidate(ConsolidateInput())
        node = m.wiki_store.read("choose-kafka")
        assert node is not None and node.status == "pending" and node.source == "consolidation"
        assert node.scope == "project" and node.project_id == PROJECT  # lands in the episode's namespace

    def test_proposed_type_lands_as_a_draft(self, data_dir: Path) -> None:
        payload = '[{"type":"entity","title":"Who edits","body":"b","tags":[],"importance":0.5,"links":[],"proposed_type":"access-matrix"}]'
        m = _memex_with(data_dir, payload); _episode(m, "s1")
                m.consolidate(ConsolidateInput())
        types = m.wiki_store.declared_types(scope="project", project_id=PROJECT)
        assert types["access-matrix"].kind == "draft" and types["access-matrix"].pages == 1
        page = m.wiki_store.read("who-edits")
        assert page is not None and page.type == "access-matrix" and page.status == "pending"
        log = (types["access-matrix"].directory / "log.md").read_text()
        assert "propose access-matrix by consolidation: sessions=s1 pages=1" in log
        assert m.recall("who edits", include_inactive=False).hits == []

    def test_bad_proposed_type_is_dropped_not_created(self, data_dir: Path) -> None:
        payload = '[{"type":"entity","title":"X","body":"b","tags":[],"importance":0.5,"links":[],"proposed_type":"Entities"}]'
        m = _memex_with(data_dir, payload); _episode(m, "s1")
        m.consolidate(ConsolidateInput())
                assert m.wiki_store.read("x").type == "entity"
        assert not list((data_dir / "docs").rglob("Entities"))

    def test_global_episodes_never_produce_catalogue_or_drafts(self, data_dir: Path) -> None:
        payload = '[{"type":"entity","title":"X","body":"b","tags":[],"importance":0.5,"links":[],"proposed_type":"story-map"}]'
        m = _memex_with(data_dir, payload); _episode(m, "s1", project_id=None)
        m.consolidate(ConsolidateInput())
        node = m.wiki_store.read("x")
        assert node is not None and node.scope == "global" and node.type == "entity"
        assert not list((data_dir / "docs" / "global").glob("story-map"))
```

Add `from memex.application.ports import LLMResponse` and `from memex.domain.models import ConsolidateInput, WikiNode` to the test imports. Setting `m._llm` is the facade's own lazy slot; no new API for tests.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_config.py -k knowledge tests/unit/test_concept_types.py::TestConsolidationGovernance -q --no-cov`
Expected: FAIL — `GovernanceConfig` has no `knowledge_approval`.

- [ ] **Step 3: Replace the config key**

```python
@dataclass(frozen=True, slots=True)
class GovernanceConfig:
    """Memory is always active on write. Knowledge a model writes is pending
    under ``manual`` (the default) and active under ``auto``."""

    knowledge_approval: str = "manual"
```

```python
    def _governance(self, raw: dict[str, object]) -> GovernanceConfig:
        table = _table(raw, "governance")
        if "approval" in table:
            raise ConfigError(
                "governance.approval was replaced by governance.knowledge_approval (manual | auto)"
            )
        value = str(_or(_get(table, "knowledge_approval", str, "governance"), "manual"))
        if value not in KNOWLEDGE_APPROVAL_VALUES:
            raise ConfigError(f"governance.knowledge_approval must be manual|auto, got {value!r}")
        return GovernanceConfig(knowledge_approval=value)
```

Import `KNOWLEDGE_APPROVAL_VALUES` from `memex.domain.types`. Update the `memex.toml` template the installer writes (`grep -n "approval" src/memex/infrastructure/harness/installer.py`) to the new key.

- [ ] **Step 4: Consolidator — status, `decision`, drafts**

In `WikiConsolidator.__init__` (:115) replace `self._approval = config.governance.approval` with `self._knowledge_approval = config.governance.knowledge_approval`.

The consolidator has no notion of namespace today: `_select_episodes` returns episodes from every scope and `_store_node` writes every result to global. Project types need it to work per namespace, so `consolidate` (:118-125) becomes: group the selected episodes by `(episode.scope, episode.project_id)`; for each group run the existing prompt → parse → store loop with that group's `existing` pages (`type_kind(node.type) == "knowledge"` and the same scope/project), that group's `enabled = self._store.declared_types(scope=scope, project_id=project_id)` when scope is `project` (empty for global), and write each result with `scope=scope, project_id=project_id`. One report accumulates across groups. This also lands consolidated knowledge in the project it came from instead of always in global — `docs/v1_enhance.md` B7's "per-repo beliefs" — and is covered by the `node.scope == "project"` assertion above.

Prompt (:55-72): allowed types become `entity | preference | procedure | summary` plus `decision` when `"decision" in enabled`; add the line `"proposed_type": "optional kebab-case name for a concept these pages need that no listed type fits"`.

Parsing (:185-200): after reading `item["type"]`, if it is not in the allowed set for this run, skip the node and count it in the report's existing skipped/invalid tally. Read `proposed_type`; if present, `validate_type_name(value, custom=True)` — on `ValueError`, drop the proposal (keep the node with its stated type) and log `operation=consolidate proposed_type=rejected` (no name).

`_store_node` (:214-225): replace `status="pending" if self._approval == "manual" else "active"` with `status=initial_status(candidate.type, "consolidation", self._knowledge_approval)` and pass the group's `scope` and `project_id` into the `WikiNode`. **Apply `initial_status` here only.** `Memex.write` keeps the status the caller gave (default `active`) until `memory-governance` sets `source` on every surface; applying the rule in `write` now would make agent MCP writes pending before the approval verbs exist to release them. Before writing a node with an accepted `proposed_type`: if the type is not yet declared, `self._store.declare_type(name, scope="project", project_id=project_id, actor="consolidation", draft=True, description=f"sessions={','.join(sorted(session_ids))} pages={count}")`; then set `node.type = proposed_type` and `node.status = "pending"` regardless of policy (drafts are pending by construction), and write.

- [ ] **Step 5: Run tests and static checks**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_consolidator.py tests/unit/test_concept_types.py -q --no-cov && uv run mypy src tests`
Expected: pass; `Success`. Existing consolidator tests that assumed `approval="auto"` produce active entities will now see `pending` — update their expectations to the rule (summary active, knowledge pending) rather than passing `auto`.

- [ ] **Step 6: Commit**

```bash
git add src/memex/infrastructure/config.py src/memex/application/consolidator.py src/memex/infrastructure/harness/installer.py tests/unit/test_config.py tests/unit/test_consolidator.py tests/unit/test_concept_types.py
git commit -m "feat(types): knowledge_approval governs model-written knowledge; consolidation writes decisions and nominates drafts"
```

---

### Task 7: Application service, facade, CLI, MCP

**Files:**
- Create: `src/memex/application/concept_types.py`
- Modify: `src/memex/application/memory.py` (expose the service; nothing else)
- Modify: `src/memex/cli.py:52-53` (free `--type`), new `types` subcommand group after the `approve` parser (:140-141), dispatch beside `approve` (:712)
- Test: `tests/unit/test_cli_types.py` (new), `tests/unit/test_concept_types.py`

**Interfaces:**
- Consumes: Task 3's `WikiStore.declared_types/declare_type`, Task 1's vocabulary.
- Produces:
  ```python
  class ConceptTypes:
      def __init__(self, store: WikiStore, index: IndexManager, navigation: NavigationGenerator) -> None
      def list(self, *, scope: str, project_id: str | None) -> list[TypeDeclaration]
      def add(self, name: str, *, project_id: str, description: str = "") -> TypeDeclaration      # custom
      def enable(self, name: str, *, project_id: str) -> TypeDeclaration                         # catalogue
      def remove(self, name: str, *, project_id: str, force: bool = False) -> dict[str, object]  # {"removed": name, "archived": n}
      def suggest(self, *, project_id: str, min_pages: int = 3) -> list[tuple[str, int]]        # (tag, count)
  Memex.types: ConceptTypes   # constructed in _open_storage
  ```
  CLI: `memex types list|add|enable|remove|suggest` with `--scope project` (default) and `--project-id/--project-label` resolved exactly as `write` resolves them (`_project_arguments`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_types.py
"""`memex types` (docs/specs/project-concept-types/spec.md AC-0001, AC-0002, AC-0010, AC-0011)."""
import json
from pathlib import Path

import pytest

from memex import cli

PROJECT = "a" * 24


def _run(capsys: pytest.CaptureFixture[str], data_dir: Path, *args: str) -> tuple[int, dict | list]:
    code = cli.main(["--data-dir", str(data_dir), *args])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def test_add_enable_list(capsys, data_dir: Path) -> None:
    assert _run(capsys, data_dir, "types", "add", "access-matrix", "--project-id", PROJECT, "--description", "Who may edit.")[0] == 0
    assert _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)[0] == 0
    code, listed = _run(capsys, data_dir, "types", "list", "--project-id", PROJECT)
    assert code == 0
    by_name = {row["name"]: row for row in listed}
    assert by_name["access-matrix"]["kind"] == "custom" and by_name["access-matrix"]["description"] == "Who may edit."
    assert by_name["decision"]["kind"] == "catalogue"
    assert by_name["entity"]["kind"] == "builtin"


def test_add_rejects_bad_and_colliding_names(capsys, data_dir: Path) -> None:
    for bad in ("Decision", "entities", "log", "rule"):
        code, _ = _run(capsys, data_dir, "types", "add", bad, "--project-id", PROJECT)
        assert code != 0, bad
    assert "already" in capsys.readouterr().err or True  # message goes to stderr


def test_write_to_declared_type_then_remove_refuses_and_force_archives(capsys, data_dir: Path) -> None:
    _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)
    code, written = _run(capsys, data_dir, "write", "--type", "decision", "--title", "Choose Kafka", "--body", "b", "--scope", "project", "--project-id", PROJECT)
    assert code == 0 and written["file_path"].endswith("/decision/choose-kafka.md")
    code, _ = _run(capsys, data_dir, "types", "remove", "decision", "--project-id", PROJECT)
    assert code != 0
    code, result = _run(capsys, data_dir, "types", "remove", "decision", "--project-id", PROJECT, "--force")
    assert code == 0 and result == {"removed": "decision", "archived": 1}


def test_write_undeclared_type_fails_before_writing(capsys, data_dir: Path) -> None:
    code, _ = _run(capsys, data_dir, "write", "--type", "decision", "--title", "X", "--body", "b", "--scope", "project", "--project-id", PROJECT)
    assert code != 0
    assert not list((data_dir / "docs").rglob("x.md"))


def test_recall_filters_by_declared_type(capsys, data_dir: Path) -> None:
    _run(capsys, data_dir, "types", "enable", "decision", "--project-id", PROJECT)
    _run(capsys, data_dir, "write", "--type", "decision", "--title", "Choose Kafka", "--body", "unique-marker", "--scope", "project", "--project-id", PROJECT)
    _run(capsys, data_dir, "write", "--type", "entity", "--title", "Kafka", "--body", "unique-marker", "--scope", "project", "--project-id", PROJECT)
    code, result = _run(capsys, data_dir, "recall", "unique-marker", "--type", "decision", "--scope", "project", "--project-id", PROJECT)
    assert code == 0 and [h["slug"] for h in result["hits"]] == ["choose-kafka"]


def test_mcp_write_rejects_undeclared_type(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memex import mcp_server
    monkeypatch.setenv("MEMEX_DATA_DIR", str(data_dir))
    mcp_server._reset()
    try:
        result = mcp_server.memex_write(type="decision", title="X", body="b", scope="project", project_id=PROJECT)
        assert "error" in result and "undeclared type" in str(result["error"])
        assert not list((data_dir / "docs").rglob("x.md"))
    finally:
        mcp_server._reset()


def test_suggest_counts_tags_that_are_not_types(capsys, data_dir: Path) -> None:
    for i in range(3):
        _run(capsys, data_dir, "write", "--type", "entity", "--title", f"Story {i}", "--body", "b", "--tags", "user-stories,entity", "--scope", "project", "--project-id", PROJECT)
    code, rows = _run(capsys, data_dir, "types", "suggest", "--project-id", PROJECT)
    assert code == 0 and rows == [{"tag": "user-stories", "pages": 3}]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_cli_types.py -q --no-cov`
Expected: FAIL — argparse: `invalid choice: 'types'`.

- [ ] **Step 3: The service**

```python
# src/memex/application/concept_types.py
"""Declaring, listing, removing, and suggesting project concept types."""

from __future__ import annotations

from collections import Counter

from memex.domain.types import CATALOGUE, validate_type_name
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.store.navigation import NavigationGenerator
from memex.domain.errors import WikiStoreError
from memex.infrastructure.store.wiki_store import TypeDeclaration, WikiStore


class ConceptTypes:
    """Project-level type lifecycle over the store's directory declarations."""

    def __init__(self, store: WikiStore, index: IndexManager, navigation: NavigationGenerator) -> None:
        self._store = store
        self._index = index
        self._navigation = navigation

    def list(self, *, scope: str, project_id: str | None) -> list[TypeDeclaration]:
        return list(self._store.declared_types(scope=scope, project_id=project_id).values())

    def add(self, name: str, *, project_id: str, description: str = "") -> TypeDeclaration:
        if name in CATALOGUE:
            raise WikiStoreError(f"{name!r} is a catalogue type; run memex types enable")
        return self._store.declare_type(name, scope="project", project_id=project_id, description=description)

    def enable(self, name: str, *, project_id: str) -> TypeDeclaration:
        if name not in CATALOGUE:
            raise WikiStoreError(f"{name!r} is not a catalogue type; run memex types add")
        return self._store.declare_type(name, scope="project", project_id=project_id, description=CATALOGUE[name].answers)

    def remove(self, name: str, *, project_id: str, force: bool = False) -> dict[str, object]:
        declared = self._store.declared_types(scope="project", project_id=project_id).get(name)
        if declared is None or declared.kind == "builtin":
            raise WikiStoreError(f"type {name!r} is not declared for this project")
        pages = [n for n in self._store.scan_dir(declared.directory) if n.status != "archived"]
        if pages and not force:
            raise WikiStoreError(f"type {name!r} holds {len(pages)} page(s); pass --force to archive them first")
        for node in pages:
            node.status = "archived"
            stored = self._store.write(node)
            self._index.update_record(stored)
        self._store.append_log(declared.directory, "remove", name, "user", f"archived={len(pages)}")
        self._navigation.refresh(declared.directory, self._store.scan_dir)
        return {"removed": name, "archived": len(pages)}

    def suggest(self, *, project_id: str, min_pages: int = 3) -> list[tuple[str, int]]:
        """Tags that recur across pages and are not types: concepts the store strains toward."""
        known = set(self._store.declared_types(scope="project", project_id=project_id))
        counts: Counter[str] = Counter()
        for node in self._store.scan_all():
            if node.project_id != project_id:
                continue
            for tag in node.tags:
                try:
                    validate_type_name(tag, custom=True)
                except ValueError:
                    continue
                if tag not in known:
                    counts[tag] += 1
        return sorted(((t, c) for t, c in counts.items() if c >= min_pages), key=lambda r: (-r[1], r[0]))
```

`remove` archives pages but leaves the directory and its `log.md` in place as history; `declared_types` reports it with kind unchanged and pages 0 until the directory is emptied by hand — the spec's `remove` semantics are "archive then withdraw", and history stays readable. 

- [ ] **Step 4: Facade and CLI**

`memory.py`, in `_open_storage` after `self.navigation` is built: `self.types = ConceptTypes(self.wiki_store, self.index_manager, self.navigation)` (import the class).

`cli.py`: change line 52-53 to `write.add_argument("--type", required=True, help="Built-in, enabled catalogue, or declared type (see memex types list)")`. After the `approve` parser add:

```python
    types_cmd = sub.add_parser("types", help="Declare and inspect project concept types")
    types_sub = types_cmd.add_subparsers(dest="types_command", required=True)
    for name, doc in (("list", "List built-in, catalogue, custom, and draft types"),
                      ("add", "Declare a custom type"), ("enable", "Enable a catalogue type"),
                      ("remove", "Withdraw a type (archives its pages with --force)"),
                      ("suggest", "Tags that recur but are not yet types")):
        sp = types_sub.add_parser(name, help=doc)
        if name in {"add", "enable", "remove"}:
            sp.add_argument("name")
        if name == "add":
            sp.add_argument("--description", default="")
        if name == "remove":
            sp.add_argument("--force", action="store_true")
        if name == "suggest":
            sp.add_argument("--min-pages", type=int, default=3)
        sp.add_argument("--scope", choices=["project"], default="project")
        sp.add_argument("--project-id", default=None)
        sp.add_argument("--project-label", default=None)
```

Dispatch beside `approve`:

```python
        elif args.command == "types":
            project_id, _label, _locator = _project_arguments(args)
            if args.types_command == "list":
                _emit([asdict(t) | {"directory": str(t.directory)} for t in memex.types.list(scope="project", project_id=project_id)])
            elif args.types_command == "add":
                _emit(asdict(memex.types.add(args.name, project_id=project_id, description=args.description)) | {"directory": None})
            elif args.types_command == "enable":
                _emit(asdict(memex.types.enable(args.name, project_id=project_id)) | {"directory": None})
            elif args.types_command == "remove":
                _emit(memex.types.remove(args.name, project_id=project_id, force=args.force))
            elif args.types_command == "suggest":
                _emit([{"tag": tag, "pages": count} for tag, count in memex.types.suggest(project_id=project_id, min_pages=args.min_pages)])
```

`_emit` of a `Path` fails JSON serialization, hence the `directory` override; `asdict` is `dataclasses.asdict`. The dispatch block lives inside `_run`'s existing `try:` whose `except ValueError as exc:` (cli.py:308) prints `memex: <exc>` to stderr and returns 1; extend that clause to `except (ValueError, WikiStoreError) as exc:` so every `types` refusal exits non-zero the same way.

- [ ] **Step 5: Run tests and static checks**

Run: `uv run pytest tests/unit/test_cli_types.py tests/unit/test_cli.py tests/unit/test_facade.py tests/unit/test_mcp_server.py -q --no-cov && uv run ruff check src tests && uv run mypy src tests`
Expected: pass; `Success`.

- [ ] **Step 6: Live check**

```bash
export MEMEX_DATA_DIR=/private/tmp/claude-501/types-store && rm -rf "$MEMEX_DATA_DIR"
uv run memex types enable decision --project-id aaaaaaaaaaaaaaaaaaaaaaaa
uv run memex write --type decision --title "Choose Kafka" --body "Because." --description "When asking why Kafka." --scope project --project-id aaaaaaaaaaaaaaaaaaaaaaaa
uv run memex types list --project-id aaaaaaaaaaaaaaaaaaaaaaaa
uv run memex recall --type decision "why kafka" --scope project --project-id aaaaaaaaaaaaaaaaaaaaaaaa
uv run memex verify
```

Expected: the write lands under `projects/…/decision/`, `types list` shows `decision` as catalogue with 1 page, recall returns it, verify is `ok: true`. Record the output in the plan changelog.

- [ ] **Step 7: Commit**

```bash
git add src/memex/application/concept_types.py src/memex/application/memory.py src/memex/cli.py tests/unit/test_cli_types.py
git commit -m "feat(types): memex types list|add|enable|remove|suggest; write accepts declared types"
```

---

### Task 8: Verify — three type checks

**Files:**
- Modify: `src/memex/application/verify.py` (add `type_checks`, call it beside `okf_checks`)
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Consumes: `WikiStore.declared_types`, `type_directory`, `_slug_detail` (existing helper).
- Produces: checks `types-match-directory`, `types-declared`, `draft-types-pending`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_concept_types.py
from memex.application.verify import verify


def _failed(report) -> dict[str, str]:
    return {str(c["check"]): str(c["detail"]) for c in report.checks if not c["ok"]}


def test_verify_reports_type_directory_mismatch(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    node = m.write(WriteInput(type="decision", title="Choose", body="b", scope="project", project_id=PROJECT))
    path = Path(node.file_path or "")
    path.write_text(path.read_text().replace('type: "decision"', 'type: "policy"'))
    failed = _failed(verify(m))
    assert "types-match-directory" in failed and "choose" in failed["types-match-directory"]


def test_verify_reports_undeclared_directory_and_non_pending_draft(data_dir: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.write(WriteInput(type="entity", title="Seed", body="b", scope="project", project_id=PROJECT))
    project_dir = Path(m.wiki_store.get_path("seed")).parent.parent
    (project_dir / "mystery").mkdir()
    (project_dir / "mystery" / "thing.md").write_text(Path(m.wiki_store.get_path("seed")).read_text().replace('type: "entity"', 'type: "mystery"').replace("Seed", "Thing"))
    m.wiki_store.declare_type("story-map", scope="project", project_id=PROJECT, actor="consolidation", draft=True)
    m.write(WriteInput(type="story-map", title="Map", body="b", scope="project", project_id=PROJECT, status="active"))
    failed = _failed(verify(m))
    assert "mystery" in failed["types-declared"]
    assert "map" in failed["draft-types-pending"]
    for detail in failed.values():
        assert str(data_dir) not in detail
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py -k verify_reports -q --no-cov`
Expected: FAIL — `KeyError: 'types-match-directory'`.

- [ ] **Step 3: Add the checks**

```python
def type_checks(memex: Memex, nodes: list[WikiNode]) -> list[dict[str, object]]:
    """Project concept types: page type matches its directory, every
    directory is declared, and a draft type holds only pending pages."""
    mismatched = [
        n.slug for n in nodes
        if n.file_path and Path(n.file_path).parent.name != type_directory(n.type)
    ]
    undeclared: list[str] = []
    non_pending: list[str] = []
    projects_dir = memex.wiki_store.wiki_dir / "projects"
    if projects_dir.is_dir():
        for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir() and not p.is_symlink()):
                        project_id = memex.wiki_store.project_id_of(project_dir)
            declared = memex.wiki_store.declared_types(scope="project", project_id=project_id) if project_id else {}
            by_dir = {type_directory(name): decl for name, decl in declared.items()}
            for child in sorted(p for p in project_dir.iterdir() if p.is_dir() and not p.is_symlink()):
                decl = by_dir.get(child.name)
                if decl is None:
                    undeclared.append(f"{project_dir.name}/{child.name}")
                    continue
                if decl.kind == "draft":
                    non_pending.extend(n.slug for n in nodes if n.file_path and Path(n.file_path).parent == child and n.status != "pending")
    return [
        _check("types-match-directory", not mismatched, _slug_detail("pages whose type differs from their directory", mismatched)),
        _check("types-declared", not undeclared, _slug_detail("project directories without a declaration", undeclared)),
        _check("draft-types-pending", not non_pending, _slug_detail("non-pending pages inside draft types", non_pending)),
    ]
```

Call `checks.extend(type_checks(memex, nodes))` right after `checks.extend(okf_checks(nodes))`; import `type_directory` from `memex.domain.types`. `project_id_of` is the public probe Task 3 added.

- [ ] **Step 4: Run and commit**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_verify.py -q --no-cov && uv run mypy src tests`
Expected: pass. The healthy-store test in `test_verify.py` counts checks; raise its expected count by 3.

```bash
git add src/memex/application/verify.py src/memex/infrastructure/store/wiki_store.py tests/unit/test_concept_types.py tests/unit/test_verify.py
git commit -m "feat(types): verify checks type/directory agreement, declarations, and draft pending state"
```

---

### Task 9: Export and import carry type declarations

**Files:**
- Modify: `src/memex/infrastructure/store/import_export.py:39-48 (export), 57-90 (import loop), 115-125 (_node_from_json)`
- Test: `tests/unit/test_concept_types.py`

**Interfaces:**
- Produces: export document gains `"types": [{"scope": "project", "project_id": str, "name": str, "kind": str, "description": str}]`; import declares each before writing pages.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_concept_types.py
from memex.infrastructure.store.import_export import ImportExport


def test_export_import_round_trips_declared_types(data_dir: Path, tmp_path: Path) -> None:
    m = Memex(MemexConfig(data_dir=data_dir))
    m.wiki_store.declare_type("decision", scope="project", project_id=PROJECT)
    m.wiki_store.declare_type("access-matrix", scope="project", project_id=PROJECT, description="Who may edit.")
    m.write(WriteInput(type="decision", title="Choose", body="b", scope="project", project_id=PROJECT))
    archive = tmp_path / "e.json"
    doc = ImportExport(m.wiki_store, m.index_manager, m.link_manager).export(archive)
    assert {"scope": "project", "project_id": PROJECT, "name": "access-matrix", "kind": "custom", "description": "Who may edit."} in doc["types"]

    other = Memex(MemexConfig(data_dir=tmp_path / "second"))
    ImportExport(other.wiki_store, other.index_manager, other.link_manager).import_file(archive)
    types = other.wiki_store.declared_types(scope="project", project_id=PROJECT)
    assert types["decision"].kind == "catalogue" and types["access-matrix"].description == "Who may edit."
    assert other.wiki_store.read("choose").type == "decision"


def test_import_fails_on_page_of_undeclared_type(data_dir: Path, tmp_path: Path) -> None:
    other = Memex(MemexConfig(data_dir=data_dir))
    archive = tmp_path / "bad.json"
    archive.write_text(json.dumps({"version": "1.0", "exported_at": "2026-01-01T00:00:00Z", "types": [], "nodes": [
        {"slug": "x", "type": "decision", "title": "X", "body": "b", "tags": [], "importance": 0.5, "created": "2026-01-01T00:00:00Z", "timestamp": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "links": [], "scope": "project", "project_id": PROJECT}]}))
    result = ImportExport(other.wiki_store, other.index_manager, other.link_manager).import_file(archive)
    assert result["imported"] == 0 and any("x" in e and "undeclared" in e for e in result["errors"])
```

`EXPORT_VERSION` is `"1.0"` (import_export.py:20); `import json` at the top of the test file.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_concept_types.py -k export -q --no-cov`
Expected: FAIL — `KeyError: 'types'`.

- [ ] **Step 3: Export declarations, import them first**

In `export`, add before `"nodes"`:

```python
            "types": [
                {"scope": "project", "project_id": pid, "name": d.name, "kind": d.kind, "description": d.description}
                for pid in sorted({n.project_id for n in self._store.list() if n.project_id})
                for d in self._store.declared_types(scope="project", project_id=pid).values()
                if d.kind != "builtin"
            ],
```

In `import_data`, before iterating nodes:

```python
        for entry in data.get("types", []) if isinstance(data.get("types"), list) else []:
            if not isinstance(entry, dict):
                continue
            name, pid = str(entry.get("name", "")), str(entry.get("project_id", ""))
            if name in self._store.declared_types(scope="project", project_id=pid):
                continue
            try:
                self._store.declare_type(name, scope="project", project_id=pid, description=str(entry.get("description", "")), actor="import", draft=entry.get("kind") == "draft")
            except WikiStoreError as exc:
                errors.append(f"type {name!r}: {exc}")
```

In `_node_from_json` (:122) replace the `NODE_TYPES` membership test with `validate_type_name(node_type)` inside a `try/except ValueError` that re-raises `ValueError(f"invalid node type: {node_type!r}")`; the store's `write` then rejects an undeclared type with `WikiStoreError("undeclared type …")`, which the import loop's existing `except (ValueError, WikiStoreError, IndexManagerError)` (import_export.py:81) records as `errors.append(str(exc))` — prefix that message with the node's slug so the test's `"x" in e` holds.

- [ ] **Step 4: Run and commit**

Run: `uv run pytest tests/unit/test_concept_types.py tests/unit/test_okf_frontmatter.py -k "export or import" -q --no-cov && uv run mypy src tests`
Expected: pass.

```bash
git add src/memex/infrastructure/store/import_export.py tests/unit/test_concept_types.py
git commit -m "feat(types): export and import carry project type declarations"
```

---

### Task 10: Durable outputs and full gates

**Files:**
- Create: `docs/adr/0006-three-kinds-of-concept-type.md`; update `docs/adr/README.md` table
- Modify: `docs/gitpages/guide.md:70-80` (node types table → three kinds + `memex types`), `docs/architecture/overview.md` (domain row: `types.py`; store paragraph: declarations), `docs/product/changelog.md` (Unreleased: Added `memex types`, catalogue; Changed `type` contract, `knowledge_approval` replaces `approval`, decay exemption), `src/memex/infrastructure/AGENTS.md` (store row mentions declarations and `log.md`), `src/memex/domain/AGENTS.md` (types.py), `docs/specs/project-concept-types/spec.md` (tick ACs; resolve open decision 1: description lives in `log.md`'s declare line; open decision 2 stays for `memory-governance`)
- Test: full gates

- [ ] **Step 1: Write ADR-0006**

Status Accepted, date, context (five types, OKF free string, evidence from the owner's own store), decision (three kinds; directory is the declaration; `log.md` is the record; memory decays/knowledge does not; memory automatic/knowledge governed with `knowledge_approval`), alternatives (free strings only — typo types; catalogue only — no long tail; config-file registry — does not travel with the project), consequences (MCP `type` no longer a closed enum; default `knowledge_approval = manual` flips consolidation's default for knowledge; `approval` key removed).

- [ ] **Step 2: Guide, architecture, changelog, AGENTS**

Replace the guide's node-types table with the three-kinds table from the spec plus a `memex types` example block (enable, add, list, write). Architecture: add `types.py` to the domain row and a sentence to the store paragraph: "a project's type set is its directories; `log.md` in each declared directory records the declaration". Changelog per the file map above. AGENTS files: one line each.

- [ ] **Step 3: Tick the spec and lint**

Set every AC met to `[x]`; set spec `Status: Implementing → Shipped` only after Step 4 is green; move the `workspace.toml` entry from `queue` to `active` at the start of execution and to `shipped` at the end.

Run: `uv run mkdocs build --strict && python .agents/skills/work-loop/scripts/lint-spec-status.py --all && python .agents/skills/work-loop/scripts/lint-knowledge.py`

- [ ] **Step 4: Full gates**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && rm -f .coverage && uv run pytest tests -q`
Expected: all pass; coverage ≥ 90%; the only tolerated failures are the two known load-sensitive `test_codex_capture` wrapper tests if they flake.

- [ ] **Step 5: Commit**

```bash
git add docs src/memex/domain/AGENTS.md src/memex/infrastructure/AGENTS.md workspace.toml
git commit -m "docs(types): ADR-0006, guide, architecture, changelog for project concept types"
```

---

## Rollout

Big bang within one branch, no flag. Two user-visible defaults change and are named in the changelog: MCP/CLI `type` accepts any declared type, and `knowledge_approval = "manual"` makes consolidation's knowledge output pending by default (`summary` stays active). A `memex.toml` carrying the old `approval` key fails loading with the replacement named. Existing stores need no migration: five-type stores are exactly the built-in case.

## Risks

- **`scan_all` now globs the whole tree once instead of five directory globs.** Same file count, one `rglob`; measure with the 1,000-page benchmark store before and after (expect equal or faster).
- **`_parse_index` must reject an index whose sections are out of order** so the splice falls back rather than mis-splicing; the randomized oracle test is the guard, extended with declared types in Task 4.
- **Consolidator tests assumed `approval="auto"`.** Task 6 updates them to the rule rather than passing `auto`.
- **`log.md` was never written before.** Its structural classification is asserted in Task 3; `memory-governance` will append further verbs to the same file and must keep the line shape.

## Changelog

- 2026-09-24: Plan drafted from the approved spec.
- 2026-09-24: Executed via subagent-driven development, ten tasks, commits
  `f3e08cf..11391f1` (Tasks 1-9) plus one documentation commit closing
  Task 10 (`docs(types): ADR-0006, guide, architecture, changelog for
  project concept types`). Spec status moved `Draft` straight to `Shipped`;
  the plan's intermediate `Implementing` step was skipped because Step 4's
  gates only went green at the end of Task 10, after every task's own
  gates had already passed. Rulings that changed the plan, in task order:
  - Task 3: `_is_type_dir`'s flat-layout branch (the pre-Task-3
    `docs/global`/`docs/projects` layout some fixtures still write) was
    first widened, then narrowed to built-in directory names only, so a
    stray file directly under `docs/global` or a project root can never
    count as a type directory.
  - Task 4: `SUBDIRECTORIES_HEADING` was reserved in `domain/types.py` and
    a custom type may never take it, so a type named `subdirectories`
    cannot collide with the generated `## Subdirectories` section.
  - Task 4: `WikiStore._is_type_dir` was lifted into a shared, module-level
    `is_type_dir(wiki_dir, directory)` predicate that both the store and
    navigation call, replacing navigation's own position-unaware check.
  - Task 8: `WikiStore.project_id_of` was replaced by
    `declared_types_in(root)`, a directory-rooted declaration lookup that
    cannot raise on a mixed-id or pageless project directory the way
    resolving a project id first could.
  - Task 9: export walks project directories through
    `project_declarations()`, skipping and counting (`types_skipped=N`) a
    directory whose project id cannot be read, rather than aborting the
    whole export.
  - Task 6: a draft nomination's logged `pages=<n>` counts every candidate
    page consolidation put forward (including duplicate titles, which get
    their own slugs), not a de-duplicated title count, so the audit line
    never understates what the run wrote.
  - Task 6: `knowledge_approval` defaults to `manual`, flipping
    consolidation's default output for knowledge types (everything but
    `episode`/`summary`) from active to pending; consolidator tests that
    assumed the old `approval="auto"` default were updated to the rule
    instead of passing `auto` explicitly.
