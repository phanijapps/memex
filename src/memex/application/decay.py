"""Time-based importance decay (spec §7 Utility 8)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from memex.domain.models import WikiNode, utc_now_iso
from memex.domain.types import decays
from memex.infrastructure.search.index_manager import IndexManager
from memex.infrastructure.store.wiki_store import WikiStore

_CHANGE_EPSILON = 1e-6


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class RecencyDecay:
    """Exponential decay: ``score = importance * exp(-lambda * days_idle)``.

    Applied only when explicitly invoked (cron / startup), never on access.
    """

    def __init__(self, half_life_days: int = 30, enabled: bool = True) -> None:
        if half_life_days < 1:
            raise ValueError("half_life_days must be >= 1")
        self.half_life_days = half_life_days
        self.enabled = enabled

    def decay_importance(self, node: WikiNode) -> float:
        """Decayed importance for one node based on time since last access."""
        reference = node.last_access or node.created
        try:
            last = _parse_iso(reference)
        except ValueError:
            return node.importance
        days_idle = (datetime.now(UTC) - last).total_seconds() / 86400
        lam = math.log(2) / self.half_life_days
        return round(max(node.importance * math.exp(-lam * days_idle), 0.0), 6)

    def apply_decay(
        self,
        wiki_store: WikiStore,
        index_manager: IndexManager | None = None,
        dry_run: bool = False,
    ) -> list[tuple[str, float, float]]:
        """Recompute importance for every node. Returns (slug, old, new) rows."""
        changes: list[tuple[str, float, float]] = []
        if not self.enabled:
            return changes
        for node in wiki_store.scan_all():
            if not decays(node.type):
                continue  # knowledge is refined or superseded, never aged out
            new_score = self.decay_importance(node)
            if abs(new_score - node.importance) < _CHANGE_EPSILON:
                continue
            changes.append((node.slug, node.importance, new_score))
            if dry_run:
                continue
            node.importance = new_score
            node.updated_at = utc_now_iso()
            wiki_store.write(node)
            if index_manager is not None:
                index_manager.update_record(node)
        return changes
