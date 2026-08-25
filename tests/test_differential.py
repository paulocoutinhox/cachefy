"""One script of operations, answered by every store and compared against the same script answered in memory.

A suite written by hand only ever asks the questions somebody thought to ask, and this asks the ones nobody did.
"""

import random
from datetime import timedelta

import pytest

from cachefy.clock import now
from cachefy.entry import VERSION_BITS, Entry
from cachefy.store.memory import MemoryStore

SPACES = ("users", "posts", "limits")

# Every way a store is asked to change an entry, drawn one at a time.
# Writing is drawn most often, because everything else needs an entry to work on.
OPERATIONS = ("write", "write", "write", "write", "add", "add", "swap", "swap", "read", "read", "read", "read_many", "read_many", "drop", "touch", "touch", "bump", "bump", "purge", "count", "clear")

STEPS = 150

# What an entry is drawn from, held out here so the call that writes one stays on a line.
KEYS = ("42", "43", "Bob", "bob", "café", "cafe", "grüßen 😀", "")

# Values a suite of round numbers would never notice a store changing.
VALUES = (None, True, False, 0, -1, "", "five", [], {}, [1, "two", None], {"deep": {"er": [1.5, -0.0]}}, "grüßen 😀", 2**63 - 1, -(2**63), 2**64 - 1, 0.30000000000000004, 1.7976931348623157e308, 7)

# The instants an entry dies at, drawn either side of the script so a store is asked about living and dead entries alike.
# Nothing here dies within the seconds the script itself takes to run, because redis gives the memory back on its own clock while every other store waits for a sweep.
# Living is weighted over dead, because what this suite is for is comparing every field of an entry across stores and a name holding nothing has one field to compare.
LIVES = (None, None, None, -300, -30, 75, 3600, 3600, 3600)

FRESH = (None, -10, 40, 600)

AMOUNTS = (1, -1, 5, -3, 0)


def readable(entry: Entry | None):
    """Answers every field of an entry, because one a store quietly forgets is a policy the cache stops honouring."""
    if entry is None:
        return None

    return (entry.space, entry.key, entry.value, entry.expires_at, entry.stale_at, entry.created_at, entry.version)


def drawn(dice: random.Random, base, step: int) -> Entry:
    """Answers the entry this step of the script writes."""
    dies = dice.choice(LIVES)
    stales = dice.choice(FRESH)

    # The version is drawn from the script and never left to the entry, so the same step hands every store the same one to write down.
    return Entry(
        space=dice.choice(SPACES), key=dice.choice(KEYS) or f"k{step}", value=dice.choice(VALUES), expires_at=None if dies is None else base + timedelta(seconds=dies), stale_at=None if stales is None else base + timedelta(seconds=stales), created_at=base + timedelta(seconds=step), version=dice.getrandbits(VERSION_BITS)
    )


async def script(store, seed: int, base):
    """Answers everything one store said while it was asked the same sequence every other store is asked."""
    dice = random.Random(seed)
    trail = []

    for step in range(STEPS):
        moment = base + timedelta(seconds=step)
        choice = dice.choice(OPERATIONS)
        entry = drawn(dice, base, step)

        if choice == "write":
            trail.append((step, choice, readable(await store.write(entry, moment))))

            continue

        if choice == "add":
            trail.append((step, choice, readable(await store.add(entry, moment))))

            continue

        if choice == "swap":
            # Half of them are aimed at the version that name really carries, or a swap that lands is one no store is ever compared on.
            held = await store.read(entry.space, entry.key, moment)
            against = held.version if held is not None and dice.random() < 0.5 else dice.getrandbits(VERSION_BITS)
            trail.append((step, choice, readable(await store.swap(entry, against, moment))))

            continue

        if choice == "read":
            trail.append((step, choice, readable(await store.read(entry.space, entry.key, moment))))

            continue

        if choice == "read_many":
            wanted = tuple(dice.choice(KEYS) or f"k{step}" for _ in range(dice.randint(0, 4)))
            found = await store.read_many(entry.space, wanted, moment)
            trail.append((step, choice, sorted((name, readable(held)) for name, held in found.items())))

            continue

        if choice == "drop":
            trail.append((step, choice, await store.drop(entry.space, entry.key)))

            continue

        if choice == "touch":
            trail.append((step, choice, await store.touch(entry.space, entry.key, entry.expires_at, entry.stale_at, entry.version, moment)))

            continue

        if choice == "bump":
            trail.append((step, choice, await store.bump(entry.space, entry.key, dice.choice(AMOUNTS), entry.expires_at, entry.version, moment)))

            continue

        if choice == "purge":
            # The sweep is asked for more than anything could ever be dead, because which of them a bounded batch takes is the one thing the contract leaves to the store.
            trail.append((step, choice, await store.purge(moment, STEPS)))

            continue

        if choice == "count":
            trail.append((step, choice, await store.count(entry.space, moment), await store.count(None, moment)))

            continue

        trail.append((step, choice, await store.clear(entry.space)))

    settled = base + timedelta(seconds=STEPS)
    left = [readable(await store.read(space, key, settled)) for space in SPACES for key in KEYS if key]

    return trail, left, await store.count(None, settled), [await store.count(space, settled) for space in SPACES]


@pytest.mark.parametrize("seed", range(8))
async def test_every_store_answers_the_same_script_the_same_way(store, seed):
    """A store that drifts from the others is a promise this library keeps on one backend and breaks on another."""
    reference = MemoryStore()
    await reference.setup()

    base = now()

    assert await script(store, seed, base) == await script(reference, seed, base), f"{type(store).__name__} answered the script differently from the store the whole library is defined by"
