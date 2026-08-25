"""Separate interpreters against one store, which is what containers are."""

import asyncio
import json
import pathlib
import sys
from uuid import uuid4

import pytest

from tests.conftest import SERVERS, STORES
from tests.fleet import prepared

MACHINES = 4
ROUNDS = 3
COUNTS = 5

# Long enough that every process is inside the call before the first of them answers.
COMPUTING = 0.4

PATIENCE = 120


def urls(tmp_path) -> dict:
    """Answers the stores a fleet can be pointed at, which is every one this run could reach."""
    reachable = {name: SERVERS[name] for name in ("redis", "mysql", "postgres") if name in STORES}

    return {"sqlite": f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}", **reachable}


async def spawn(url: str, tmp_path, machines: int, rounds: int, counts: int, computing: float) -> list[dict]:
    """Starts a fleet of processes against one store and answers what each of them saw."""
    # The name is this run's own, because a server the suite shares still holds what the run before it wrote — and pytest hands out the same temporary directory to the same test on every run.
    name = f"{tmp_path.name}-{uuid4().hex}"
    outputs = [tmp_path / f"machine-{index}.json" for index in range(machines)]
    running = []

    await prepared(url)

    for index, output in enumerate(outputs):
        settings = {"url": url, "output": str(output), "machines": machines, "name": name, "rounds": rounds, "counts": counts, "computing": computing}
        running.append(await asyncio.create_subprocess_exec(sys.executable, "-m", "tests.machine", json.dumps(settings), cwd=str(pathlib.Path.cwd()), stderr=asyncio.subprocess.PIPE))

    for machine in running:
        async with asyncio.timeout(PATIENCE):
            said = (await machine.communicate())[1].decode()

            assert machine.returncode == 0, f"a machine of the fleet against {url} did not come back: {said}"
            assert "could not " not in said, f"a machine of the fleet against {url} could not reach the store: {said}"

    return [json.loads(output.read_text()) for output in outputs]


@pytest.mark.parametrize("store", ["sqlite", "redis", "mysql", "postgres"])
async def test_many_machines_compute_one_cold_value_once_between_them(store, tmp_path):
    """Every process of a fleet coming up against one cold name is the moment a stampede happens."""
    where = urls(tmp_path)

    if store not in where:
        pytest.skip(f"a {store} nobody can reach is not a store this suite collects")

    seen = await spawn(where[store], tmp_path, MACHINES, ROUNDS, COUNTS, COMPUTING)

    assert sum(machine["computed"] for machine in seen) == 1, "one machine computed it and the rest were handed what it wrote"

    for machine in seen:
        assert machine["answers"] == [{"name": "Paulo"}] * ROUNDS


@pytest.mark.parametrize("store", ["sqlite", "redis", "mysql", "postgres"])
async def test_many_machines_counting_one_name_never_lose_a_count(store, tmp_path):
    """A rate limit two machines each read as one is a rate limit that lets twice as much through."""
    where = urls(tmp_path)

    if store not in where:
        pytest.skip(f"a {store} nobody can reach is not a store this suite collects")

    seen = await spawn(where[store], tmp_path, MACHINES, 1, COUNTS, 0.0)
    counted = sorted(number for machine in seen for number in machine["counts"])

    assert counted == list(range(1, MACHINES * COUNTS + 1)), "every machine was handed a number no other machine was"
