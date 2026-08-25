import secrets
from dataclasses import dataclass, field
from datetime import datetime

from cachefy.clock import now

# How wide a version is drawn, which is wide enough that one name never sees the same one twice and narrow enough for every store to hold it.
VERSION_BITS = 62


def minted() -> int:
    """Answers a fresh version, which is what a value written back is compared against."""
    # A version is drawn and never counted, because a count starts again wherever a name comes back from holding nothing — and a version seen twice is a value written back over one nobody read.
    # It is drawn from the system and never from `random`, whose state a worker pool carries identically into every process it forks.
    return secrets.randbits(VERSION_BITS)


class Missing:
    """What a read answers when a name holds nothing, which is never the same thing as a name holding `None`."""

    def __repr__(self) -> str:
        return "MISS"

    def __bool__(self) -> bool:
        return False


MISS = Missing()


@dataclass
class Entry:
    """One name and the value it holds until an instant, which is the only thing any store here writes."""

    space: str
    key: str
    value: object = None

    # Nothing at all is a name kept until somebody drops it.
    expires_at: datetime | None = None

    # The instant the value stops being worth serving without a refresh, which is what lets one caller recompute while everybody else is answered from what is there.
    stale_at: datetime | None = None

    created_at: datetime = field(default_factory=now)

    # What a value written back is compared against, drawn where the entry is built and written down unchanged by every store.
    version: int = field(default_factory=minted)

    def alive(self, moment: datetime) -> bool:
        """Answers whether this entry is still there at that instant."""
        return self.expires_at is None or self.expires_at > moment

    def stale(self, moment: datetime) -> bool:
        """Answers whether this entry is past the instant it should be recomputed at."""
        return self.stale_at is not None and self.stale_at <= moment
