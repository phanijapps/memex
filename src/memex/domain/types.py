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
        TypeContract(
            "domain",
            "what exists in the business and what its words mean",
            ("user", "consolidation"),
            False,
        ),
        TypeContract(
            "architecture", "how the system is put together and why", ("user",), False
        ),
        TypeContract("rule", "a constraint that can be checked", ("user",), False),
        TypeContract(
            "policy", "a principle that guides judgment", ("user",), False
        ),
        TypeContract(
            "decision",
            "what was chosen, when, and why",
            ("user", "consolidation"),
            False,
        ),
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
        taken = (
            set(NODE_TYPES)
            | set(TYPE_DIRS.values())
            | set(CATALOGUE)
            | set(RESERVED_SLUGS)
        )
        if name in taken:
            raise ValueError(
                f"type name {name!r} is already a built-in, catalogue, or reserved name"
            )
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
            f"knowledge_approval must be one of {KNOWLEDGE_APPROVAL_VALUES}, "
            f"got {knowledge_approval!r}"
        )
    if type_kind(name) == "memory" or source == "user" or knowledge_approval == "auto":
        return "active"
    return "pending"
