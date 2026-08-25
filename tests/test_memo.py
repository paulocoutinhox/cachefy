"""A function whose answers live in a space of their own."""

import asyncio
from datetime import timedelta

import pytest

from cachefy.entry import MISS
from cachefy.errors import CacheError

SLOWLY = 0.15


async def test_a_call_is_computed_once_and_answered_from_then_on(app):
    calls = []

    @app.cached("profile", ttl=timedelta(minutes=5))
    async def profile(user_id: int) -> dict:
        calls.append(user_id)

        return {"id": user_id}

    assert await profile(7) == {"id": 7}
    assert await profile(7) == {"id": 7}
    assert calls == [7]


async def test_two_different_calls_are_two_different_names(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int) -> dict:
        calls.append(user_id)

        return {"id": user_id}

    assert await profile(7) == {"id": 7}
    assert await profile(8) == {"id": 8}
    assert calls == [7, 8]


async def test_the_same_call_spelled_either_way_is_one_name(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int, full: bool = False) -> dict:
        calls.append(user_id)

        return {"id": user_id, "full": full}

    assert await profile(7) == {"id": 7, "full": False}
    assert await profile(user_id=7) == {"id": 7, "full": False}
    assert await profile(7, full=False) == {"id": 7, "full": False}
    assert calls == [7]


async def test_a_plain_function_is_memoized_too(app):
    calls = []

    @app.cached("squared")
    def squared(number: int) -> int:
        calls.append(number)

        return number * number

    assert await squared(4) == 16
    assert await squared(4) == 16
    assert calls == [4]


async def test_a_call_that_answers_none_is_kept(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int):
        calls.append(user_id)

        return None

    assert await profile(7) is None
    assert await profile(7) is None
    assert calls == [7]


async def test_one_call_is_forgotten_without_touching_the_others(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int) -> dict:
        calls.append(user_id)

        return {"id": user_id}

    await profile(7)
    await profile(8)

    assert await profile.invalidate(7) is True
    assert await profile.invalidate(7) is False

    await profile(7)
    await profile(8)

    assert calls == [7, 8, 7]


async def test_every_call_of_a_function_is_forgotten_at_once(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int) -> dict:
        calls.append(user_id)

        return {"id": user_id}

    await profile(7)
    await profile(8)

    assert await profile.clear() == 2

    await profile(7)

    assert calls == [7, 8, 7]


async def test_a_call_is_computed_again_and_kept_when_it_is_refreshed(app):
    calls = []

    @app.cached("profile")
    async def profile(user_id: int) -> int:
        calls.append(user_id)

        return len(calls)

    assert await profile(7) == 1
    assert await profile.refresh(7) == 2
    assert await profile(7) == 2
    assert calls == [7, 7]


async def test_many_callers_of_one_call_compute_it_once_between_them(app):
    calls = []

    @app.cached("profile", ttl=timedelta(minutes=5))
    async def profile(user_id: int) -> dict:
        calls.append(user_id)
        await asyncio.sleep(SLOWLY)

        return {"id": user_id}

    answers = await asyncio.gather(*[profile(7) for _ in range(12)])

    assert answers == [{"id": 7}] * 12
    assert calls == [7]


async def test_a_call_names_itself_the_same_way_in_every_process(app):
    @app.cached("profile")
    async def profile(user_id: int, tags: list) -> int:
        return user_id

    assert profile.key_for(7, ["b", "a"]) == '{"tags":["b","a"],"user_id":7}'
    assert profile.key_for(user_id=7, tags=["b", "a"]) == profile.key_for(7, ["b", "a"])


async def test_a_call_too_long_to_name_is_named_by_its_digest(app):
    @app.cached("profile")
    async def profile(tags: list) -> int:
        return len(tags)

    long = profile.key_for([f"tag-{index}" for index in range(200)])

    assert len(long) == 64
    assert int(long, 16) >= 0, "a digest is hexadecimal, so it can never read as a call written out"
    assert await profile([f"tag-{index}" for index in range(200)]) == 200


async def test_a_call_nothing_could_name_says_so(app):
    @app.cached("profile")
    async def profile(who) -> int:
        return 1

    with pytest.raises(CacheError, match="cannot be written down"):
        await profile(object())


async def test_a_caller_may_name_its_own_calls(app):
    calls = []

    @app.cached("profile", key=lambda account: str(account.id))
    async def profile(account) -> dict:
        calls.append(account.id)

        return {"id": account.id}

    class Account:
        def __init__(self, number: int) -> None:
            self.id = number

    assert await profile(Account(7)) == {"id": 7}
    assert await profile(Account(7)) == {"id": 7}
    assert calls == [7]
    assert profile.key_for(Account(7)) == "7"


async def test_a_name_a_caller_chose_is_answered_for_like_any_other(app):
    @app.cached("profile", key=lambda who: who)
    async def profile(who) -> int:
        return 1

    with pytest.raises(CacheError):
        await profile("")


async def test_a_memo_still_reads_as_the_function_it_wraps(app):
    @app.cached("profile")
    async def profile(user_id: int) -> dict:
        """Answers the profile of one account."""
        return {"id": user_id}

    assert profile.__name__ == "profile"
    assert profile.__doc__ == "Answers the profile of one account."


async def test_a_stale_answer_is_computed_again_by_one_caller(app):
    calls = []

    @app.cached("profile", ttl=timedelta(minutes=5), stale=timedelta(milliseconds=20))
    async def profile(user_id: int) -> int:
        calls.append(user_id)

        return len(calls)

    assert await profile(7) == 1

    await asyncio.sleep(0.06)

    assert await profile(7) == 2, "the answer went stale and one caller computed it again"
    assert await profile.space.get(profile.key_for(7)) == 2, "and what that caller computed is what is kept"
    assert calls == [7, 7]


async def test_the_space_of_a_memo_is_the_one_it_was_declared_as(app):
    @app.cached("profile")
    async def profile(user_id: int) -> int:
        return user_id

    assert profile.space is app.space_for("profile")
    assert await app.space_for("profile").get(profile.key_for(7)) is MISS


async def test_calls_that_python_reads_as_equal_are_still_three_different_calls(app):
    """Python holds `1 == 1.0 == True`, so a name built without care would answer one call with another's value."""
    calls = []

    @app.cached("answered")
    async def answered(number) -> str:
        calls.append(number)

        return f"answered {number!r}"

    assert len({answered.key_for(1), answered.key_for(1.0), answered.key_for(True)}) == 3
    assert await answered(1) == "answered 1"
    assert await answered(1.0) == "answered 1.0"
    assert await answered(True) == "answered True"
    assert calls == [1, 1.0, True]


async def test_two_functions_remembered_under_two_names_never_share_one(app):
    async def handler(number) -> str:
        return f"answered {number}"

    one = app.cached("one")(handler)
    two = app.cached("two")(handler)

    assert one.space is not two.space
    assert await one(1) == "answered 1"
    assert await one.invalidate(1) is True
    assert await two.invalidate(1) is False, "forgetting one leaves the other alone"


async def test_a_default_a_caller_never_passed_is_part_of_the_name(app):
    @app.cached("counted")
    async def counted(items=[], depth=1) -> int:
        return len(items) + depth

    assert counted.key_for() == '{"depth":1,"items":[]}'
    assert counted.key_for() == counted.key_for([], 1)


async def test_a_method_is_named_by_what_says_which_instance_it_was_called_on(app):
    """A method is the commonest thing anybody memoizes, and `self` is the one argument nothing can write down."""

    class Accounts:
        def __init__(self, tenant: str) -> None:
            self.tenant = tenant

        @app.cached("accounts", ttl=timedelta(minutes=5), key=lambda self, account_id: f"{self.tenant}:{account_id}")
        async def profile(self, account_id: int) -> dict:
            computed.append((self.tenant, account_id))

            return {"id": account_id, "tenant": self.tenant}

    computed = []
    first, second = Accounts("one"), Accounts("two")

    assert await first.profile(7) == {"id": 7, "tenant": "one"}
    assert await first.profile(7) == {"id": 7, "tenant": "one"}
    assert await second.profile(7) == {"id": 7, "tenant": "two"}

    assert computed == [("one", 7), ("two", 7)], "two instances answered from one entry, or one instance computed twice"


async def test_a_method_left_to_name_itself_is_refused_rather_than_named_by_the_object(app):
    """Naming it by the object would name one entry per instance the process happens to build, which is a cache that never hits."""

    class Accounts:
        @app.cached("named-itself", ttl=timedelta(minutes=5))
        async def profile(self, account_id: int) -> dict:
            return {"id": account_id}

    with pytest.raises(CacheError, match="cannot be written down"):
        await Accounts().profile(7)


async def test_a_call_that_could_never_run_is_refused_before_it_takes_the_name(app):
    """A call missing an argument is one no producer could answer, so naming it and reaching the store for it is a round trip spent on nothing."""
    reached = []
    reading = app.store.read

    async def watched(*arguments, **options):
        reached.append(1)

        return await reading(*arguments, **options)

    @app.cached("arity", ttl=timedelta(minutes=5))
    async def totals(first: int, second: int) -> int:
        return first + second

    app.store.read = watched

    try:
        with pytest.raises(TypeError, match="missing a required argument"):
            await totals(1)
    finally:
        app.store.read = reading

    assert reached == [], "a call that could never run still reached the store"
