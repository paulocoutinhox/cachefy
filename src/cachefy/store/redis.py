import json
from datetime import datetime, timedelta

from redis.asyncio import Redis

from cachefy.clock import EPOCH, as_utc
from cachefy.entry import Entry
from cachefy.errors import CacheError
from cachefy.store.base import COUNTER_CEILING, COUNTER_FLOOR, Store

# Every key this store owns starts here, so it shares a database with an application without ever meeting it.
PREFIX = "cachefy"

# What an instant is counted in here, because it is the finest every store keeps one to.
MICROSECOND = timedelta(microseconds=1)

# What every script that names one entry opens with.
# The keys are built out of `ARGV` rather than declared in `KEYS`, because a name is joined from two halves the caller gives separately.
COMMON = """
local prefix = ARGV[1]
local space = ARGV[2]
local key = ARGV[3]
local name = space .. ':' .. key
local hash = prefix .. ':entry:' .. name

local function living(moment)
  if redis.call('EXISTS', hash) == 0 then return false end

  local dies = redis.call('HGET', hash, 'expires_at')

  return dies == '' or tonumber(dies) > tonumber(moment)
end

-- The name is listed where a whole space is read out of, and the instant it dies at where a sweep reads.
-- The expiry beside them is what gives the memory back and never what says an entry is dead, because redis runs it off its own clock while the library decides every instant off the caller's.
-- It is rounded up to the millisecond redis counts in, so the hash outlives the instant the field names rather than going a fraction of a millisecond before it.
local function place(expires_at)
  redis.call('SADD', prefix .. ':spaces', space)
  redis.call('SADD', prefix .. ':names:' .. space, key)

  if expires_at == '' then
    redis.call('ZREM', prefix .. ':dying', name)
    redis.call('PERSIST', hash)

    return
  end

  redis.call('ZADD', prefix .. ':dying', tonumber(expires_at), name)
  redis.call('PEXPIREAT', hash, math.ceil(tonumber(expires_at) / 1000))
end
"""

# Writes the value under the name whatever was there.
WRITE = COMMON + """
redis.call('HSET', hash, unpack(cjson.decode(ARGV[4])))
place(ARGV[5])

return 1
"""

# Writes the value only while the name is free or what holds it has already died.
ADD = COMMON + """
if living(ARGV[6]) then return nil end

redis.call('HSET', hash, unpack(cjson.decode(ARGV[4])))
place(ARGV[5])

return 1
"""

# Writes the value only while the name still holds the living version the caller read.
# The version is compared as the text it was written down as, which is exact however wide the number behind it is.
SWAP = COMMON + """
if not living(ARGV[6]) then return nil end
if redis.call('HGET', hash, 'version') ~= ARGV[7] then return nil end

redis.call('HSET', hash, unpack(cjson.decode(ARGV[4])))
place(ARGV[5])

return 1
"""

# Takes the name away.
# The answer is read off the listing and never off the hash, because an expiry redis has already acted on takes the hash and leaves the name.
DROP = COMMON + """
local held = redis.call('SREM', prefix .. ':names:' .. space, key)

redis.call('DEL', hash)
redis.call('ZREM', prefix .. ':dying', name)

if redis.call('SCARD', prefix .. ':names:' .. space) == 0 then redis.call('SREM', prefix .. ':spaces', space) end

return held
"""

# Moves the instants a living entry dies and goes stale at, under a new version.
TOUCH = COMMON + """
if not living(ARGV[6]) then return 0 end

redis.call('HSET', hash, 'expires_at', ARGV[4], 'stale_at', ARGV[5], 'version', ARGV[7])
place(ARGV[4])

return 1
"""

# Reads a count and writes it back in one step, because a rate limit two callers each read as one lets twice as much through.
# What is there is a count only when it was written down as a bare whole number.
BUMP = COMMON + """
local standing = living(ARGV[6])
local counted = 0

if standing then
  local held = redis.call('HGET', hash, 'value')

  if string.match(held, '^%-?%d+$') == nil then return nil end

  counted = tonumber(held)

  if counted < tonumber(ARGV[7]) or counted > tonumber(ARGV[8]) then return nil end
end

local total = counted + tonumber(ARGV[4])

if total < tonumber(ARGV[7]) or total > tonumber(ARGV[8]) then return nil end

if standing then
  redis.call('HSET', hash, 'value', string.format('%d', total), 'version', ARGV[9])
else
  redis.call('DEL', hash)
  redis.call('HSET', hash, 'space', space, 'key', key, 'value', string.format('%d', total), 'expires_at', ARGV[5], 'stale_at', '', 'created_at', ARGV[6], 'version', ARGV[9])
  place(ARGV[5])
end

return string.format('%d', total)
"""

# Drops everything one space holds, in the one step that makes the answer exact.
CLEAR = """
local prefix = ARGV[1]
local listing = prefix .. ':names:' .. ARGV[2]
local names = redis.call('SMEMBERS', listing)

for _, key in ipairs(names) do
  redis.call('DEL', prefix .. ':entry:' .. ARGV[2] .. ':' .. key)
  redis.call('ZREM', prefix .. ':dying', ARGV[2] .. ':' .. key)
end

redis.call('DEL', listing)
redis.call('SREM', prefix .. ':spaces', ARGV[2])

return #names
"""

# Drops what was already dead at that instant, a batch at a time because a script holds the whole server while it works.
# The range takes the instant itself, because an entry dying exactly then is one every read already answers as gone.
PURGE = """
local prefix = ARGV[1]
local gone = redis.call('ZRANGEBYSCORE', prefix .. ':dying', '-inf', ARGV[2], 'LIMIT', 0, tonumber(ARGV[3]))

for _, name in ipairs(gone) do
  local at = string.find(name, ':')
  local space = string.sub(name, 1, at - 1)
  local listing = prefix .. ':names:' .. space

  redis.call('DEL', prefix .. ':entry:' .. name)
  redis.call('ZREM', prefix .. ':dying', name)
  redis.call('SREM', listing, string.sub(name, at + 1))

  if redis.call('SCARD', listing) == 0 then redis.call('SREM', prefix .. ':spaces', space) end
end

return #gone
"""

# Counts what is alive by walking it, because redis has no index over a hash.
# It is for an operator watching what a cache holds, and never for a hot path.
COUNT = """
local prefix = ARGV[1]
local spaces = {ARGV[2]}

if ARGV[2] == '' then spaces = redis.call('SMEMBERS', prefix .. ':spaces') end

local found = 0

for _, space in ipairs(spaces) do
  for _, key in ipairs(redis.call('SMEMBERS', prefix .. ':names:' .. space)) do
    local dies = redis.call('HGET', prefix .. ':entry:' .. space .. ':' .. key, 'expires_at')

    if dies and (dies == '' or tonumber(dies) > tonumber(ARGV[3])) then found = found + 1 end
  end
end

return found
"""

# Reads many names in one step, because a round trip for each of them is what a cache is there to save.
READ_MANY = """
local found = {}

for index = 4, #ARGV do
  local hash = ARGV[1] .. ':entry:' .. ARGV[2] .. ':' .. ARGV[index]

  if redis.call('EXISTS', hash) == 1 then
    local dies = redis.call('HGET', hash, 'expires_at')

    if dies == '' or tonumber(dies) > tonumber(ARGV[3]) then table.insert(found, redis.call('HGETALL', hash)) end
  end
end

return found
"""


def stamp(moment: datetime | None) -> str:
    """Answers an instant as the whole microseconds since the epoch, which is exact where Python reads it and a score wherever Lua sorts it."""
    return "" if moment is None else str((as_utc(moment) - EPOCH) // MICROSECOND)


def moment_of(value: str) -> datetime | None:
    """Answers the instant those microseconds name, counted forward from the epoch and never off the clock of the machine."""
    return None if not value else EPOCH + timedelta(microseconds=int(value))


def fields_of(pairs) -> dict:
    """Answers every field of a hash and what it holds, as text."""
    return {name.decode(): value.decode() for name, value in pairs}


def paired(flat: list) -> dict:
    """Answers the same for a hash out of a script, which arrives as one flat array."""
    return fields_of(zip(flat[::2], flat[1::2]))


class RedisStore(Store):
    """Entries held in Redis, which is where a cache belongs when reads and writes are both hot. Every mutation is one Lua step on the server."""

    def __init__(self, client: Redis, *, prefix: str = PREFIX) -> None:
        # This store reads what redis answers as bytes, and a client that decodes for itself is one an application sharing it very often has.
        if client.connection_pool.connection_kwargs.get("decode_responses"):
            raise CacheError("the client was built with decode_responses, and this store reads what redis answers as bytes")

        self.client = client
        self.prefix = prefix

        # Registering a script asks redis nothing: it digests the body here and sends it on first use, so the store is whole as soon as it is built.
        self.scripts = {name: client.register_script(body) for name, body in (("write", WRITE), ("add", ADD), ("swap", SWAP), ("drop", DROP), ("touch", TOUCH), ("bump", BUMP), ("clear", CLEAR), ("purge", PURGE), ("count", COUNT), ("read_many", READ_MANY))}

    async def setup(self) -> None:
        return None

    def to_hash(self, entry: Entry) -> dict:
        """Answers the fields a hash holds one entry as."""
        return {"space": entry.space, "key": entry.key, "value": json.dumps(entry.value), "expires_at": stamp(entry.expires_at), "stale_at": stamp(entry.stale_at), "created_at": stamp(entry.created_at), "version": str(entry.version)}

    def to_entry(self, stored: dict) -> Entry:
        """Answers the entry those fields hold."""
        return Entry(space=stored["space"], key=stored["key"], value=json.loads(stored["value"]), expires_at=moment_of(stored["expires_at"]), stale_at=moment_of(stored["stale_at"]), created_at=moment_of(stored["created_at"]), version=int(stored["version"]))

    def arguments(self, entry: Entry) -> list:
        """Answers what every script writing one entry is called with."""
        flat = [piece for name, value in self.to_hash(entry).items() for piece in (name, value)]

        return [self.prefix, entry.space, entry.key, json.dumps(flat), stamp(entry.expires_at)]

    async def read(self, space: str, key: str, moment: datetime) -> Entry | None:
        stored = await self.client.hgetall(f"{self.prefix}:entry:{space}:{key}")

        if not stored:
            return None

        found = self.to_entry(fields_of(stored.items()))

        return found if found.alive(moment) else None

    async def read_many(self, space: str, keys: tuple[str, ...], moment: datetime) -> dict[str, Entry]:
        if not keys:
            return {}

        found = await self.scripts["read_many"](keys=[], args=[self.prefix, space, stamp(moment), *keys])

        return {entry.key: entry for entry in (self.to_entry(paired(stored)) for stored in found)}

    async def write(self, entry: Entry, moment: datetime) -> Entry:
        await self.scripts["write"](keys=[], args=[*self.arguments(entry), stamp(moment)])

        return entry

    async def add(self, entry: Entry, moment: datetime) -> Entry | None:
        taken = await self.scripts["add"](keys=[], args=[*self.arguments(entry), stamp(moment)])

        return entry if taken is not None else None

    async def swap(self, entry: Entry, version: int, moment: datetime) -> Entry | None:
        written = await self.scripts["swap"](keys=[], args=[*self.arguments(entry), stamp(moment), str(version)])

        return entry if written is not None else None

    async def drop(self, space: str, key: str) -> bool:
        return await self.scripts["drop"](keys=[], args=[self.prefix, space, key]) == 1

    async def touch(self, space: str, key: str, expires_at: datetime | None, stale_at: datetime | None, version: int, moment: datetime) -> bool:
        return await self.scripts["touch"](keys=[], args=[self.prefix, space, key, stamp(expires_at), stamp(stale_at), stamp(moment), str(version)]) == 1

    async def bump(self, space: str, key: str, amount: int, expires_at: datetime | None, version: int, moment: datetime) -> int | None:
        total = await self.scripts["bump"](keys=[], args=[self.prefix, space, key, amount, stamp(expires_at), stamp(moment), COUNTER_FLOOR, COUNTER_CEILING, str(version)])

        return None if total is None else int(total)

    async def clear(self, space: str) -> int:
        return await self.scripts["clear"](keys=[], args=[self.prefix, space])

    async def purge(self, before: datetime, limit: int) -> int:
        return await self.scripts["purge"](keys=[], args=[self.prefix, stamp(before), limit])

    async def count(self, space: str | None, moment: datetime) -> int:
        return await self.scripts["count"](keys=[], args=[self.prefix, space or "", stamp(moment)])
