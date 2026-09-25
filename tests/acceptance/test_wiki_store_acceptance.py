"""Spec acceptance tests 1-5: WikiStore filesystem CRUD."""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.domain.errors import WikiStoreError
from memex.domain.models import WikiNode
from memex.infrastructure.store.wiki_store import WikiStore


def _node(**overrides: object) -> WikiNode:
    fields: dict[str, object] = {
        "type": "entity",
        "title": "Ruff linter",
        "body": "Ruff is a fast Python linter written in Rust.",
        "tags": ["tool", "linter"],
        "id": "11111111-1111-1111-1111-111111111111",
    }
    fields.update(overrides)
    return WikiNode(**fields)  # type: ignore[arg-type]


def test_wiki_write_and_read(data_dir: Path) -> None:
    store = WikiStore(data_dir)
    stored = store.write(_node())
    read_back = store.read("ruff-linter")

    assert read_back is not None
    assert read_back.title == "Ruff linter"
    assert read_back.body == "Ruff is a fast Python linter written in Rust."
    assert read_back.type == "entity"
    assert read_back.id == "11111111-1111-1111-1111-111111111111"
    assert read_back.tags == ["tool", "linter"]
    assert read_back.importance == 0.5
    assert read_back.created
    assert read_back.updated_at
    assert stored.file_path is not None
    assert Path(stored.file_path).exists()


def test_wiki_slug_derivation(data_dir: Path) -> None:
    store = WikiStore(data_dir)
    long_title = "A Very Long Title " * 10
    stored = store.write(_node(title=long_title, id="22222222-2222-2222-2222-222222222222"))
    assert stored.slug == "a-very-long-title-a-very-long-title-a-very-long-title-a-very-lon"
    assert len(stored.slug) == 64

    store.write(_node(title="Ruff linter", id="33333333-3333-3333-3333-333333333333"))
    third = store.write(_node(title="Ruff linter!!!", id="44444444-4444-4444-4444-444444444444"))
    assert third.slug == "ruff-linter-2"

    untitled = store.write(_node(title="???", id="abc12345-9999"))
    assert untitled.slug.startswith("abc12345")


def test_wiki_list_filter_by_type(data_dir: Path) -> None:
    store = WikiStore(data_dir)
    store.write(_node(title="Tool X", id="a1"))
    store.write(_node(title="Prefer dark", type="preference", id="a2"))
    store.write(_node(title="Deploy rule", type="procedure", id="a3"))

    entities = store.list("entity")
    assert [node.title for node in entities] == ["Tool X"]
    assert len(store.list()) == 3


def test_wiki_delete(data_dir: Path) -> None:
    store = WikiStore(data_dir)
    store.write(_node())
    store.delete("ruff-linter")

    assert store.read("ruff-linter") is None
    assert not (data_dir / "docs/entities/ruff-linter.md").exists()


def test_wiki_front_matter_roundtrip(data_dir: Path) -> None:
    store = WikiStore(data_dir)
    node = _node(
        importance=0.9,
        stale_after="2027-01-01T00:00:00Z",
        valid_from="2026-09-15T10:00:00Z",
        valid_until="2026-12-31T23:59:59Z",
        transcript_ref=None,
        links=["python-3-12"],
        access_count=3,
        last_access="2026-09-15T12:00:00Z",
    )
    store.write(node)
    read_back = store.read("ruff-linter")

    assert read_back is not None
    assert read_back.importance == 0.9
    assert read_back.stale_after == "2027-01-01T00:00:00Z"
    assert read_back.valid_from == "2026-09-15T10:00:00Z"
    assert read_back.valid_until == "2026-12-31T23:59:59Z"
    assert [link.to_mapping() for link in read_back.links] == [
        {"target": "python-3-12", "rel": "relates-to"}
    ]
    assert read_back.access_count == 3
    assert read_back.last_access == "2026-09-15T12:00:00Z"
    assert read_back.content_hash.startswith("sha256:")


@pytest.mark.parametrize("bad_type", ["", "Entity"])
def test_invalid_shape_type_rejected_at_the_model(data_dir: Path, bad_type: str) -> None:
    with pytest.raises(ValueError, match="type"):
        _node(type=bad_type)


def test_undeclared_shape_valid_type_rejected_at_the_store(data_dir: Path) -> None:
    # "folder" is shape-valid, so the model accepts it by design (Task 2);
    # membership (built-in or declared) is the store's job (Task 3).
    with pytest.raises(WikiStoreError, match="undeclared type"):
        WikiStore(data_dir).write(_node(type="folder"))
