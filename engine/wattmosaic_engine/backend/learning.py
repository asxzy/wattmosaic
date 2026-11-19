"""Learning half of the backend: the L6 fast-path hook and learning status.

Training, validation and promotion run in the background
:class:`~wattmosaic_engine.learning.worker.LearningWorker` (L6), never here.
This mixin only hands confirmed evidence to that worker and reports the
learning accounting honestly.
"""

from __future__ import annotations

from typing import Any

from ..learning.reporting import build_learning_status


class LearningMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    #: The legacy sample pipeline is gone; ``None`` means "no in-backend
    #: learning pipeline" and the status surface reports learning as off here.
    learning: Any | None = None
    learning_active: bool = False

    def on_confirmed_evidence(self, observation_ids: Any) -> int:
        """Fast-path submit hook (L6/P6): enqueue evidence, never train inline.

        Only marks subjects dirty and schedules one drain on the background
        executor; training, validation, promotion and the registry swap run
        there, never on the asyncio loop (ADR-0033 / DEC-37).  Returns the number
        of newly dirtied subjects, or ``0`` when no worker is wired.
        """
        worker = getattr(self, "learning_worker", None)
        if worker is None:
            return 0
        added = worker.submit(observation_ids)
        # A cycle with no new work performs zero learning DB queries; schedule
        # freely and let the worker's own pending-cycle guard deduplicate.
        worker.schedule()
        return int(added)

    def learning_status(self) -> dict[str, Any]:
        """Honest learning status: counters + ``effective_state`` + remedy."""
        return build_learning_status(
            enabled=self.learning is not None,
            active=bool(self.learning is not None and self.learning_active),
        )

    def set_learning_active(self, active: bool) -> None:
        self.learning_active = bool(active)


__all__ = ["LearningMixin"]
