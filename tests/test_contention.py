"""What InnoDB does under load, and what the store owes it: a deadlock is the database asking for the transaction again."""

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from cachefy.clock import now
from cachefy.entry import Entry
from cachefy.store.sqlalchemy import TRIES, contended, under_contention


class Refused(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)

        self.args = (code, "refused")


def refusal(code: int) -> DBAPIError:
    """Answers the refusal a database raises under that code."""
    return DBAPIError("UPDATE", {}, Refused(code))


@pytest.mark.parametrize("code", [1205, 1213])
def test_the_two_answers_innodb_gives_under_contention_are_recognised(code):
    assert contended(refusal(code)) is True


@pytest.mark.parametrize("code", [1062, 1146])
def test_anything_else_is_not_contention_and_is_never_asked_again(code):
    assert contended(refusal(code)) is False


def test_a_refusal_with_nothing_to_say_is_not_contention():
    assert contended(DBAPIError("UPDATE", {}, Exception())) is False


async def test_a_write_the_database_asked_to_repeat_is_repeated():
    tries = []

    async def once():
        tries.append(1)

        if len(tries) < 3:
            raise refusal(1213)

        return "written"

    assert await under_contention(once) == "written"
    assert len(tries) == 3


async def test_a_write_that_keeps_deadlocking_stops_and_says_so():
    tries = []

    async def once():
        tries.append(1)

        raise refusal(1213)

    with pytest.raises(DBAPIError):
        await under_contention(once)

    assert len(tries) == TRIES, "it gave up instead of asking for ever"


async def test_anything_that_is_not_contention_is_raised_at_once():
    tries = []

    async def once():
        tries.append(1)

        raise refusal(1062)

    with pytest.raises(DBAPIError):
        await under_contention(once)

    assert len(tries) == 1, "a duplicate key never gets better by being tried again"


async def test_a_write_the_database_refused_leaves_the_session_usable(sql_store, monkeypatch):
    """A statement that failed and was left in the session would take the write after it down as well."""
    moment = now()
    calls = []
    executing = AsyncSession.execute

    async def refused_once(self, *arguments, **options):
        calls.append(1)

        if len(calls) == 1:
            raise refusal(1213)

        return await executing(self, *arguments, **options)

    monkeypatch.setattr(AsyncSession, "execute", refused_once)

    written = await sql_store.write(Entry(space="users", key="42", value=1, created_at=moment), moment)

    monkeypatch.undo()

    assert (await sql_store.read("users", "42", moment)).version == written.version
    assert (await sql_store.read("users", "42", moment)).value == 1
    assert len(calls) > 1, "the refusal was the first statement, and the write that landed came after it"
