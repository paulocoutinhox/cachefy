import json

from cachefy.errors import UnwritableValue
from cachefy.store.base import VALUE_LIMIT, WHOLE_CEILING, WHOLE_FLOOR


def exact(value, what: str) -> None:
    """Refuses a value holding a whole number that some store would read back as a different number."""
    if isinstance(value, dict):
        for held in value.values():
            exact(held, what)

        return

    if isinstance(value, (list, tuple)):
        for held in value:
            exact(held, what)

        return

    # The interface is asked for and not the type, because json writes an int subclass out as the whole number it is — and one past the range slipped through where a plain int was refused.
    if isinstance(value, int) and not WHOLE_FLOOR <= value <= WHOLE_CEILING:
        raise UnwritableValue(f"{what} holds {value}, which is past the whole numbers a store keeps inside json")


def as_written(value, what: str):
    """Answers the value as every store reads it back, refusing one that no store could write down."""
    try:
        written = json.dumps(value, ensure_ascii=False, allow_nan=False)

        # A lone surrogate is refused by the encoding and by nothing above it, so the text is encoded for the refusal and never for the bytes.
        written.encode("utf-8")
    except (TypeError, ValueError, RecursionError) as refusal:
        raise UnwritableValue(f"{what} is not something a store can write down, and an entry is json wherever it lives: {refusal}") from refusal

    if len(written) > VALUE_LIMIT:
        raise UnwritableValue(f"{what} is {len(written)} characters written down and a store keeps {VALUE_LIMIT} of them")

    settled = json.loads(written)

    # What is walked is what json really wrote, because a `dict` subclass answering its own `values()` hides a number from the walk while the encoder writes the one it is holding.
    exact(settled, what)

    return settled
