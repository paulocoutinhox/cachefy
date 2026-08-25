from datetime import datetime, timedelta, timezone
from math import isfinite

from cachefy.errors import CacheError

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# The last instant a datetime holds, which every span this library is given is measured against.
WIDEST_INSTANT = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


def now() -> datetime:
    """Answers the current instant in UTC, which is the only zone anything here writes down."""
    return datetime.now(timezone.utc)


def real(value: float, what: str) -> None:
    """Refuses a number an instant cannot be worked out from."""
    # A boolean is an int to Python and the word 'True' to a store, so the type is asked for and not the interface.
    if type(value) not in (int, float):
        raise CacheError(f"{what} is {value!r}, and a span is counted in a real number of seconds")

    # Comparisons are false against `nan` and true past `infinity`, so both pass every other guard and raise in the arithmetic instead.
    if not isfinite(value):
        raise CacheError(f"{what} is {value}, and what it has to be is a real number")


def spanned(value, what: str) -> float:
    """Answers the seconds of a span, refusing anything that is not a timedelta."""
    # A plain number would reach `total_seconds` instead of a guard and raise under a name `except CacheError` never catches.
    if not isinstance(value, timedelta):
        raise CacheError(f"{what} is {value!r}, and a span is a timedelta")

    # The base method is called on it, because a subclass answers `total_seconds` however it likes while every instant below is worked out from the span it really holds.
    return timedelta.total_seconds(value)


def waited(seconds: float, what: str) -> None:
    """Refuses a span whose instant ahead of now does not exist."""
    real(seconds, what)

    widest = (WIDEST_INSTANT - now()) // timedelta(seconds=1)

    if seconds > widest:
        raise CacheError(f"{what} is {seconds}s, and only {widest}s are left between now and the last instant a datetime holds")


def as_utc(value: datetime | None) -> datetime | None:
    """Answers the same instant as an aware UTC datetime, reading a naive one as UTC."""
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def naive_utc(value: datetime | None) -> datetime | None:
    """Answers the same instant with its offset stripped, which is what a column holding no offset takes."""
    converted = as_utc(value)

    return None if converted is None else converted.replace(tzinfo=None)
