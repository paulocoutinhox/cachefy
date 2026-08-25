"""Many machines against a real server, which is minutes and not seconds.

Load is the one thing no ordinary run ever applies, and the interleavings it reaches are not ones a graded suite can.
"""

import pytest

from tests.conftest import SERVERS, STORES
from tests.test_many_machines import spawn

MACHINES = 8
ROUNDS = 40
COUNTS = 60

# Short enough that the machines race each other over and over rather than waiting out one long computation.
COMPUTING = 0.05

pytestmark = pytest.mark.stress


def where(name: str, tmp_path) -> str:
    """Answers the url of that store, and leaves the test out when nobody can reach it."""
    if name == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"

    if name not in STORES:
        pytest.skip(f"a {name} nobody can reach is not a store this suite collects")

    return SERVERS[name]


@pytest.mark.parametrize("store", ["sqlite", "redis", "mysql", "postgres"])
async def test_a_fleet_under_load_computes_one_cold_value_once_between_them(store, tmp_path):
    seen = await spawn(where(store, tmp_path), tmp_path, MACHINES, ROUNDS, 1, COMPUTING)

    assert sum(machine["computed"] for machine in seen) == 1

    for machine in seen:
        assert machine["answers"] == [{"name": "Paulo"}] * ROUNDS


@pytest.mark.parametrize("store", ["sqlite", "redis", "mysql", "postgres"])
async def test_a_fleet_under_load_never_loses_a_count(store, tmp_path):
    seen = await spawn(where(store, tmp_path), tmp_path, MACHINES, 1, COUNTS, 0.0)
    counted = sorted(number for machine in seen for number in machine["counts"])

    assert counted == list(range(1, MACHINES * COUNTS + 1)), "every machine was handed a number no other machine was"
