"""The registry, and everything it refuses where it is written."""

from datetime import timedelta

import pytest

from cachefy.app import Cachefy
from cachefy.errors import CacheError, UnknownSpace
from cachefy.space import DECLARED, Declared, generative
from cachefy.store.memory import MemoryStore


@pytest.fixture
def cache():
    return Cachefy(MemoryStore())


def test_a_space_is_found_by_the_name_it_was_declared_as(cache):
    users = cache.space("users")

    assert cache.space_for("users") is users


def test_a_space_nobody_declared_says_so(cache):
    with pytest.raises(UnknownSpace):
        cache.space_for("users")


def test_a_name_declared_twice_is_refused(cache):
    cache.space("users")

    with pytest.raises(CacheError, match="declared twice"):
        cache.space("users")


@pytest.mark.parametrize("name", ["", "x" * 65, None, 7, "with:colon", "with\x00nul"])
def test_a_space_no_store_could_tell_apart_is_refused_where_it_is_declared(cache, name):
    with pytest.raises(CacheError):
        cache.space(name)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), 60, 60.0, "an hour", timedelta(days=4_000_000)])
def test_a_lifetime_a_value_could_never_be_kept_for_is_refused(cache, ttl):
    with pytest.raises(CacheError):
        cache.space("users", ttl=ttl)


@pytest.mark.parametrize("stale", [timedelta(0), timedelta(seconds=-1), 60])
def test_a_freshness_a_value_could_never_reach_is_refused(cache, stale):
    with pytest.raises(CacheError):
        cache.space("users", stale=stale)


def test_a_value_that_would_die_before_it_was_ever_refreshed_is_refused(cache):
    with pytest.raises(CacheError, match="die before"):
        cache.space("users", ttl=timedelta(minutes=1), stale=timedelta(minutes=1))


def test_a_value_kept_for_ever_may_still_be_refreshed(cache):
    users = cache.space("users", stale=timedelta(minutes=1))

    assert (users.ttl, users.stale) == (None, timedelta(minutes=1))


@pytest.mark.parametrize("lease", [None, timedelta(0), timedelta(seconds=-1), 30])
def test_a_lease_nobody_could_hold_a_name_for_is_refused(cache, lease):
    with pytest.raises(CacheError):
        cache.space("users", lease=lease)


async def test_a_call_that_overrides_the_policy_is_answered_for_where_it_is_made(cache):
    users = cache.space("users", ttl=timedelta(minutes=5))

    with pytest.raises(CacheError):
        await users.set("42", 1, ttl=timedelta(seconds=-1))

    with pytest.raises(CacheError):
        await users.set("42", 1, stale=timedelta(hours=9))


def test_a_call_that_says_nothing_about_a_lifetime_is_answered_by_the_space(cache):
    users = cache.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))

    assert users.spans(DECLARED, DECLARED) == (timedelta(minutes=5), timedelta(minutes=1))
    assert users.spans(timedelta(hours=1), DECLARED) == (timedelta(hours=1), timedelta(minutes=1))
    assert users.spans(None, None) == (None, None)


@pytest.mark.parametrize("amount", [1.5, True, "one", None, 2**53])
async def test_an_amount_a_counter_could_not_move_by_is_refused(cache, amount):
    limits = cache.space("limits")

    with pytest.raises(CacheError):
        await limits.incr("paulo", amount)


def test_what_it_means_to_say_nothing_reads_as_itself():
    assert repr(DECLARED) == "DECLARED"
    assert isinstance(DECLARED, Declared)


def test_a_generator_is_refused_where_it_is_declared(cache):
    with pytest.raises(CacheError, match="generator"):

        @cache.cached("counting")
        def counting(upto: int):
            yield from range(upto)


def test_an_asynchronous_generator_is_refused_the_same_way(cache):
    with pytest.raises(CacheError, match="generator"):

        @cache.cached("counting")
        async def counting(upto: int):
            for number in range(upto):
                yield number


def test_an_object_whose_call_yields_is_refused_as_well():
    """What `inspect` reads is the code of what it is handed, and an instance has none."""

    class Counting:
        def __call__(self, upto: int):
            yield from range(upto)

    assert generative(Counting()) is True
    assert generative(lambda: 1) is False
