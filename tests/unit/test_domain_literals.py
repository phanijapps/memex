"""Wire enums must stay pinned to the runtime domain constants."""

import typing

from memex.domain import models


def test_turn_role_literal_matches_constant() -> None:
    assert set(typing.get_args(models.TurnRole)) == set(models.TURN_ROLES)


def test_forget_mode_literal_matches_constant() -> None:
    assert set(typing.get_args(models.ForgetMode)) == set(models.FORGET_MODES)


def test_turn_without_ts_is_valid() -> None:
    turn = models.TurnStreamEntry.from_dict({"role": "user", "content": "x", "turn": 1})
    assert turn.ts == ""
    assert turn.role == "user"


def test_turn_with_ts_roundtrips() -> None:
    turn = models.TurnStreamEntry.from_dict(
        {"role": "user", "content": "x", "turn": 1, "ts": "2026-09-15T10:00:00Z"}
    )
    assert turn.ts == "2026-09-15T10:00:00Z"


def test_turn_bad_ts_still_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="ts"):
        models.TurnStreamEntry.from_dict(
            {"role": "user", "content": "x", "turn": 1, "ts": "yesterday"}
        )
