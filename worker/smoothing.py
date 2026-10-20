# worker/smoothing.py
"""N-of-M temporal vote, and the committed state it feeds.

The vote is causal: the decision at frame i covers frames [i-M+1, i]. A
centred window scores better in a benchmark and cannot exist in a worker,
which has no future frames.

During warm-up the requirement scales to ceil((N/M) * w) for a window of w
observations, so M=1, N=1 is the raw classifier. This is the same rule the
benchmark used (ml/apms_ml/smoothing.py), so the parameters mean the same
thing here as they did there.

What they do NOT mean is the same duration. The sweep ran on PKLot frames five
minutes apart; this runs at the configured sampling rate. M=9 was 45 minutes
there and is 4.5 seconds at 2 fps. window_seconds() exists so the figure that
is actually in force gets logged instead of assumed.

Committed state is what has been reported to the server, not what the model
last said. A transition is emitted only when the vote's verdict differs from
the committed state, which is what keeps a 50-slot lot at a few dozen events
an hour instead of 750 a second.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

VACANT = "VACANT"
OCCUPIED = "OCCUPIED"
UNKNOWN = "UNKNOWN"
DECISION_THRESHOLD = 0.5


@dataclass
class SlotVoter:
    """One slot's vote history and last reported state."""

    slot_id: UUID
    label: str
    window: int
    min_agreement: int
    committed: str = UNKNOWN
    history: deque[int] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.window)

    def verdict(self) -> str:
        """The smoothed status given the observations so far."""
        filled = len(self.history)
        if filled == 0:
            return UNKNOWN
        required = math.ceil((self.min_agreement / self.window) * filled)
        return OCCUPIED if sum(self.history) >= required else VACANT

    def observe(self, probability: float) -> str | None:
        """Records one classification. Returns the new status when the
        committed state changes, otherwise None."""
        self.history.append(1 if probability >= DECISION_THRESHOLD else 0)
        decided = self.verdict()
        if decided == self.committed:
            return None
        self.committed = decided
        return decided

    def forget_committed(self) -> None:
        """Server-requested resync. The vote history is kept: the server's
        request says nothing about what the camera is seeing, and discarding it
        would add a whole window of latency to every recovery."""
        self.committed = UNKNOWN


def window_seconds(window: int, target_fps: float) -> float:
    return window / target_fps


class VoterSet:
    """The voters for one camera, rebuilt when the slot set changes."""

    def __init__(self, slots, window: int, min_agreement: int) -> None:
        self.window = window
        self.min_agreement = min_agreement
        self.voters: dict[UUID, SlotVoter] = {
            slot.id: SlotVoter(slot_id=slot.id, label=slot.label,
                               window=window, min_agreement=min_agreement)
            for slot in slots
        }

    def __len__(self) -> int:
        return len(self.voters)

    def carry_over(self, previous: "VoterSet | None") -> None:
        """Keeps committed state for slots that survived a config change, so a
        re-annotation does not re-announce every unchanged bay. History is not
        carried: a moved polygon is looking at different pixels."""
        if previous is None:
            return
        for slot_id, voter in self.voters.items():
            older = previous.voters.get(slot_id)
            if older is not None:
                voter.committed = older.committed

    def observe(self, order: list[UUID], probabilities) -> list[tuple[SlotVoter, str, float]]:
        """Feeds one frame's scores in slot order. Returns (voter, new status,
        probability) for each slot whose committed state changed."""
        changes = []
        for slot_id, probability in zip(order, probabilities, strict=True):
            voter = self.voters[slot_id]
            previous = voter.committed
            changed = voter.observe(float(probability))
            if changed is not None:
                changes.append((voter, previous, float(probability)))
        return changes

    def forget_committed(self, slot_ids) -> int:
        wanted = set(slot_ids)
        touched = 0
        for slot_id, voter in self.voters.items():
            if slot_id in wanted and voter.committed != UNKNOWN:
                voter.forget_committed()
                touched += 1
        return touched