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
from pathlib import Path
from typing import Literal

from memex.domain.reserved import RESERVED_SLUGS

NODE_TYPES: tuple[str, ...] = ("entity", "preference", "procedure", "summary", "episode")
MEMORY_TYPES: tuple[str, ...] = ("episode", "summary")

# Stored description budget: one line, at most 512 UTF-8 bytes (spec AC-0002).
# Enforced on the stored (post-scrub) value, so redaction growth cannot land
# an over-budget field on disk with a misleading boundary error. Lives here,
# not in domain/models.py, so a type declaration's log.md text (this module)
# can share the same budget without models.py importing back into types.py.
DESCRIPTION_MAX_BYTES = 512

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

# The generated navigation section that lists child directories; a custom
# type may never take this name, or two index sections would share a heading.
SUBDIRECTORIES_HEADING = "Subdirectories"

TYPE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
KNOWLEDGE_APPROVAL_VALUES: tuple[str, ...] = ("manual", "auto")

Kind = Literal["memory", "knowledge"]

# A project directory's lifecycle state, read from its log.md (WikiStore
# _log_state): "draft" is a model nomination pending approval, "withdrawn" is
# a removed type whose directory and history stay on disk as a re-declarable
# state rather than a lie that it was never declared.
TypeKind = Literal["builtin", "catalogue", "custom", "draft", "withdrawn"]


@dataclass(frozen=True, slots=True)
class TypeDeclaration:
    """One type as the filesystem declares it for a scope and project."""

    name: str
    kind: TypeKind
    description: str
    pages: int
    directory: Path


@dataclass(frozen=True, slots=True)
class TypeContract:
    """What a catalogue type promises: who may author it, and whether it ages
    (``authorship`` is not enforced here — the memory-governance spec consumes it).
    """

    name: str
    answers: str
    authorship: tuple[str, ...]
    decays: bool


CATALOGUE: dict[str, TypeContract] = {
    contract.name: contract
    for contract in (
        TypeContract(
            "domain",
            "what exists in the business and what its words mean",
            ("user", "consolidation"),
            False,
        ),
        TypeContract("architecture", "how the system is put together and why", ("user",), False),
        TypeContract("rule", "a constraint that can be checked", ("user",), False),
        TypeContract("policy", "a principle that guides judgment", ("user",), False),
        TypeContract(
            "decision",
            "what was chosen, when, and why",
            ("user", "consolidation"),
            False,
        ),
    )
}


def _echo(name: str) -> str:
    """Bound an untrusted, possibly-arbitrary-length name for an error message."""
    return name if len(name) <= 64 else name[:64] + "…"


def validate_type_name(name: str, *, custom: bool = False) -> str:
    """Return ``name`` when it may be a type; raise ValueError naming the rule.

    With ``custom=True`` the name must also avoid every built-in name and
    directory, every catalogue name, and the reserved slugs, so a custom type
    can never shadow one and two index sections can never share a heading.
    """
    if not TYPE_NAME.fullmatch(name):
        raise ValueError(f"type name {_echo(name)!r} must match [a-z][a-z0-9-]{{0,63}}")
    if custom:
        taken = (
            set(NODE_TYPES)
            | set(TYPE_DIRS.values())
            | set(CATALOGUE)
            | set(RESERVED_SLUGS)
            | {SUBDIRECTORIES_HEADING.lower()}
        )
        if name in taken:
            raise ValueError(
                f"type name {name!r} is already a built-in, catalogue, or reserved name"
            )
    return name


def type_kind(name: str) -> Kind:
    return "memory" if name in MEMORY_TYPES else "knowledge"


def decays(name: str) -> bool:
    """Whether ``name`` ages on recency: the catalogue contract when one
    exists (refined or superseded knowledge never ages out), ``True``
    otherwise."""
    if name in CATALOGUE:
        return CATALOGUE[name].decays
    return True


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
            f"knowledge_approval must be one of {KNOWLEDGE_APPROVAL_VALUES}, "
            f"got {knowledge_approval!r}"
        )
    if type_kind(name) == "memory" or source == "user" or knowledge_approval == "auto":
        return "active"
    return "pending"
