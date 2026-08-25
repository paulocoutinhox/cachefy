import json
from hashlib import blake2b

from cachefy.errors import CacheError
from cachefy.store.base import KEY_LIMIT, SPACE_LIMIT

# How wide a digest is, in bytes, which is sixty four characters written out.
DIGEST = 32


def about(what: str, value) -> str:
    """Answers how a refusal names the thing it is refusing, which has to be something that can itself be written down."""
    # A name carrying a character no store can write would otherwise carry it into the message, and reading that message out raises where the refusal was meant to be read.
    if not isinstance(value, str):
        return what

    spelled = value.encode("utf-8", "backslashreplace").decode("utf-8").replace("\x00", "\\x00")

    return f"{what} '{spelled}'"


def writable(value: str, what: str) -> None:
    """Refuses text no store could write down."""
    # A lone surrogate is what a POSIX path carries when the bytes behind it were never UTF-8, and every real store refuses one deep inside a driver.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as refusal:
        raise CacheError(f"{what} holds a character at {refusal.start} that no store can write down") from refusal

    # A nul byte is one PostgreSQL refuses on its own while every other store keeps it.
    if "\x00" in value:
        raise CacheError(f"{what} holds a nul byte, which postgres refuses while every other store keeps it")


def holdable(value: str, limit: int, what: str) -> None:
    """Refuses a name a store could not write down or could not hold whole."""
    writable(value, what)

    if not value:
        raise CacheError(f"{what} is empty, and what tells one entry from another is a name no other entry has")

    # A database in strict mode refuses a value past the column, and one that is not quietly cuts it short.
    if len(value) > limit:
        raise CacheError(f"{what} is {len(value)} characters and a store keeps {limit} of them")


def plain(value, what: str) -> str:
    """Answers the text a name holds, refusing anything that is not text at all."""
    # Anything that is not text would reach the encoder instead of a guard and raise under a name `except CacheError` never catches.
    if not isinstance(value, str):
        raise CacheError(f"{what} is {type(value).__name__} and what tells one entry from another is text")

    # A str subclass renders as it likes and answers its own `__len__`, `__contains__` and `__bool__`, so the text is taken here and every guard below measures what a store will really hold.
    return str.__str__(value)


def named(key: str) -> str:
    """Refuses a key a store could not tell one entry from another by, and answers it as the text every store reads."""
    settled = plain(key, "the key")
    holdable(settled, KEY_LIMIT, about("the key", settled))

    return settled


def spaced(space: str) -> str:
    """Refuses a space name a store could not tell one family of names from another by, and answers it as the text every store reads."""
    settled = plain(space, "the space")
    holdable(settled, SPACE_LIMIT, about("the space", settled))

    # A colon is what joins a space to a key wherever the entries live, so one inside a space name would let two different pairs spell the same entry.
    if ":" in settled:
        raise CacheError(f"{about('the space', settled)} holds a colon, which is what joins a space to a key wherever the entries live")

    return settled


def digested(text: str) -> str:
    """Answers a fixed length name for text of any length."""
    # The digest is never drawn from `hash`, which Python salts per process: a key built with that one names an entry only the process that wrote it can read.
    return blake2b(text.encode("utf-8"), digest_size=DIGEST).hexdigest()


def joined(space: str, key: str) -> str:
    """Answers the one text that names a space and a key together and can never name another pair."""
    return f"{len(space)}:{space}:{key}"


def built(arguments: dict) -> str:
    """Answers the one name a call is remembered under, computed the same way by every process."""
    try:
        # The arguments are written down sorted, so the same call spelled in another order is the same name.
        spelled = json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as refusal:
        raise CacheError(f"a call was made with arguments that cannot be written down: {refusal} — give this one a `key` of its own instead") from refusal

    writable(spelled, "the arguments of this call")

    # What is written out opens on a brace and what is digested is hexadecimal, so the two spellings can never meet.
    if len(spelled) <= KEY_LIMIT:
        return spelled

    return digested(spelled)
