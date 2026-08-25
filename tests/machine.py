"""One machine of a fleet, run as its own process by the tests that prove many of them compute a value once."""

import asyncio
import json
import sys

from tests.fleet import work

if __name__ == "__main__":
    asyncio.run(work(json.loads(sys.argv[1])))
