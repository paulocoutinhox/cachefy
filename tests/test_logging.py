"""What a cache writes down, which is on the path of every request and so has to be worth its room."""

import io
import logging

import pytest

from cachefy.app import Cachefy
from cachefy.store.memory import MemoryStore
from tests.test_resilience import Broken

READS = 200


@pytest.fixture
def written():
    """Answers everything the library logs while a test runs."""
    kept = io.StringIO()
    handler = logging.StreamHandler(kept)
    logger = logging.getLogger("cachefy")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    yield kept

    logger.removeHandler(handler)


def test_the_logger_is_named_so_an_operator_can_route_or_silence_it():
    """A library that logs under the root logger is one nobody can turn down without turning everything down."""
    from cachefy import app, space

    assert app.logger.name == "cachefy.app"
    assert space.logger.name == "cachefy.space"


async def test_a_store_that_is_gone_writes_one_line_a_call_and_never_a_traceback(written):
    """Measured at a thousand reads: a traceback each was nine lines and four hundred bytes, which drowns a log pipeline during the one outage anybody cares about."""
    users = Cachefy(Broken()).space("users")

    for index in range(READS):
        await users.get(f"k{index}")

    said = written.getvalue()

    assert said.count("\n") == READS, "one line for each call and nothing more"
    assert "Traceback" not in said
    assert len(said) / READS < 80, "and a short one"


async def test_a_failure_still_says_what_it_was(written):
    """A line that only says something went wrong is one an operator cannot act on."""
    users = Cachefy(Broken()).space("users")
    await users.get("42")

    said = written.getvalue()

    assert "could not read 'users:42'" in said, "it names the call and the entry"
    assert "Gone" in said, "and the failure that ended it"
    assert "the store is not there" in said, "and what that failure said"


async def test_whoever_wants_the_traceback_is_handed_the_failure_itself(written):
    app = Cachefy(Broken())
    caught = []

    app.on_error(lambda what, failure: caught.append(failure))

    await app.space("users").get("42")

    assert len(caught) == 1
    assert caught[0].__traceback__ is not None, "the hook carries what a traceback is made of"


async def test_a_listener_that_raises_keeps_its_traceback(written):
    """A store failing is a state this expects, and a listener raising is a bug — only one of them needs pointing at."""
    app = Cachefy(MemoryStore())

    @app.on_miss
    def unhelpful(space, key):
        raise RuntimeError("the metric is down")

    await app.space("users").get("42")

    said = written.getvalue()

    assert "a listener failed" in said
    assert "Traceback" in said
    assert "the metric is down" in said


async def test_nothing_a_caller_stored_is_ever_written_down(written):
    """A key is written down because it is what an operator needs, and a value never is."""
    app = Cachefy(Broken())
    users = app.space("users")

    await users.set("42", {"secret": "hunter2"})
    await users.get("42")

    said = written.getvalue()

    assert "users:42" in said, "the name is what makes the line worth reading"
    assert "hunter2" not in said, "and what it holds is not"
