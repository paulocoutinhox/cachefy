"""The prose goes stale in silence, so the suite is what keeps it honest."""

import ast
import builtins
import importlib
import inspect
import pathlib
import re
from datetime import timedelta

import pytest

from cachefy.app import Cachefy
from cachefy.errors import CacheError
from cachefy.janitor import Janitor
from cachefy.memo import Memo
from cachefy.space import Space
from cachefy.store import redis as redis_store
from cachefy.store.base import Store
from cachefy.store.memory import MemoryStore
from cachefy.store.sqlalchemy import entries
from tests.conftest import REDIS_URL, STORES

DOCS = sorted(pathlib.Path("docs").glob("*.md")) + [pathlib.Path("README.md")]

# What the prose names and the code does not own: other people's libraries, and the identifiers an example invents for itself.
FOREIGN = {
    "JSON",
    "SET",
    "MIT",
    "AsyncIO",
    "PostgreSQL",
    "SQLAlchemy",
    "FastAPI",
    "Redis",
    "Lua",
    "BaseCommand",
    "Account.objects",
    "JsonResponse",
    "HTTPException",
    "FastAPI",
    "asyncio.Event",
    "asyncio.run",
    "asyncio.create_task",
    "asyncio.to_thread",
    "asyncio.new_event_loop",
    "asyncio.run_coroutine_threadsafe",
    "async_to_sync",
    "sys.exit",
    "SynchronousOnlyOperation",
    "transaction.on_commit",
    "update_fields",
    "load_account",
    "save_account",
    "read_user",
    "write_user",
    "read_account",
    "run_forever",
    "socket_timeout",
    "socket_connect_timeout",
    "health_check_interval",
    "command_timeout",
    "connect_args",
    "pool_pre_ping",
    "decode_responses",
    "from_url",
    "create_async_engine",
    "async_sessionmaker",
    "journal_mode",
    "CACHE_REDIS_URL",
    "REDIS_URL",
    "CACHEFY_REDIS_URL",
    "CACHEFY_MYSQL_URL",
    "CACHEFY_POSTGRES_URL",
    "pyproject.toml",
    "Makefile",
    "client.host",
    "asyncio.timeout",
    "asyncio.sleep",
}


def written() -> str:
    """Answers every line of python this repository holds."""
    root = pathlib.Path(".")
    files = [path for folder in ("src", "tests") for path in (root / folder).rglob("*.py")]

    return "\n".join(path.read_text() for path in files)


def prose() -> str:
    """Answers every page of the documentation at once."""
    return "\n".join(path.read_text() for path in DOCS)


def test_the_table_the_documentation_names_is_the_table_the_store_builds():
    named = set(re.findall(r"`(cachefy_\w+)`", prose()))
    built = {entries.name} | {column.name for column in entries.columns} | {index.name for index in entries.indexes}

    assert named <= built, f"the documentation names a table, column or index that does not exist: {sorted(named - built)}"
    assert entries.name in named, "the one table this store builds is worth naming somewhere"


def test_the_redis_keys_the_documentation_names_are_the_ones_the_store_writes():
    named = {piece.split(":")[0] for piece in re.findall(r"`cachefy:([\w{}:]+)`", prose())}
    source = pathlib.Path("src/cachefy/store/redis.py").read_text()
    keys = set(re.findall(r"':(\w+):?'", source)) | set(re.findall(r"':(\w+)'", source))

    assert named, "the redis layout is worth naming somewhere"
    assert named <= keys, f"the prose names a key family the store never writes: {sorted(named - keys)}"
    assert redis_store.PREFIX == "cachefy"


def test_every_symbol_the_documentation_names_still_exists():
    """A rename leaves the prose pointing at nothing, and whoever follows it looks for a function that is gone."""
    cited = {name for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`", prose()) if "_" in name or name[0].isupper()}
    code = written()

    missing = sorted(name for name in cited - FOREIGN if not re.search(rf"\b{re.escape(name.split('.')[-1])}\b", code))

    assert missing == [], f"the documentation names what the code no longer has: {missing}"


@pytest.mark.parametrize("page", DOCS)
def test_every_example_the_documentation_shows_is_python(page):
    """An example is the part of the prose somebody runs, so one that does not even parse is the loudest way a page can lie."""
    for number, block in enumerate(re.findall(r"```python\n(.*?)```", page.read_text(), re.DOTALL), 1):
        try:
            ast.parse(block)
        except SyntaxError as broken:
            raise AssertionError(f"{page} shows an example that does not parse in block {number}: {broken}") from broken


# The values a reader fills in rather than imports: a connection string, a client of their own, an instant they choose.
SUPPLIED = {"url", "database", "metrics", "audit", "api", "account", "name", "work"}


def bound(node, known: set) -> None:
    """Adds every way a name comes to exist inside an example, so what is left over is what the page never said where to get."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        known |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        known.add(node.name)

    if isinstance(node, ast.Assign):
        known |= {target.id for target in node.targets if isinstance(target, ast.Name)}

    if isinstance(node, ast.arg):
        known.add(node.arg)

    if isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
        known.add(node.optional_vars.id)


@pytest.mark.parametrize("page", DOCS)
def test_every_example_says_where_its_names_come_from(page):
    """An example that uses a name it never imports is one nobody can run, and the whole page has to answer that."""
    known, used = set(dir(builtins)) | SUPPLIED, set()

    for block in re.findall(r"```python\n(.*?)```", page.read_text(), re.DOTALL):
        for node in ast.walk(ast.parse(block)):
            bound(node, known)

            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.add(node.id)

    assert used <= known, f"{page} uses names it never imports: {sorted(used - known)}"


def spelled(node):
    """Answers what a keyword argument in an example means, and nothing at all when it is not something this can work out."""
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Lambda):
        return lambda *arguments, **keywords: "a name of the caller's own"

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "timedelta":
        return timedelta(**{keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords})

    return NOTHING


NOTHING = object()

# What a documented call builds, and the name of the thing it builds one of.
DECLARED = {"space": "space", "cached": "cached", "Janitor": "janitor"}


def declarations(page: pathlib.Path):
    """Answers every space, memo and janitor the examples on this page declare, with the policy each is given."""
    for block in re.findall(r"```python\n(.*?)```", page.read_text(), re.DOTALL):
        for node in ast.walk(ast.parse(block)):
            if not isinstance(node, ast.Call):
                continue

            named = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)

            if named not in DECLARED:
                continue

            policy = {keyword.arg: spelled(keyword.value) for keyword in node.keywords}

            if NOTHING not in policy.values():
                yield DECLARED[named], policy


@pytest.mark.parametrize("page", DOCS)
def test_every_policy_the_documentation_shows_is_one_the_library_takes(page):
    """An example showing a policy that is refused is one whoever copies it cannot run."""
    for kind, policy in declarations(page):
        app = Cachefy(MemoryStore())

        try:
            if kind == "janitor":
                Janitor(app, **policy)
            elif kind == "cached":
                app.cached("declared", **policy)(lambda who=1: who)
            else:
                app.space("declared", **policy)
        except CacheError as refused:
            raise AssertionError(f"{page} shows a {kind} the library refuses: {policy} — {refused}") from refused


def paragraphs(page: pathlib.Path):
    """Answers the prose of a page, wrapped lines joined back into the sentences they belong to and every fenced block left out."""
    fenced, block = False, []

    for line in page.read_text().splitlines():
        if line.startswith("```"):
            fenced = not fenced

            continue

        if fenced:
            continue

        if not line.strip() or line.startswith(("#", "|")) or (re.match(r"^\s*[-*]\s|^>\s*[-*]\s", line) and block):
            if block:
                yield " ".join(block)
                block = []

        if line.strip() and not line.startswith(("#", "|")):
            block.append(line.strip())

    if block:
        yield " ".join(block)


@pytest.mark.parametrize("page", DOCS)
def test_no_sentence_of_the_documentation_begins_with_code(page):
    """A sentence opening on a backtick opens on a lowercase identifier, which reads as a fragment and breaks the language rather than the code."""
    opening = []

    for block in paragraphs(page):
        body = re.sub(r"^[>\-*]\s*", "", block).strip()

        opening += [start for start in [body] + [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", body)[1:]] if re.match(r"^\*{0,2}`", start)]

    assert opening == [], f"{page} opens a sentence on code: {[start[:70] for start in opening]}"


@pytest.mark.parametrize("page", DOCS)
def test_every_page_the_index_points_at_exists(page):
    for link in re.findall(r"\]\((?!http)([^)#]+)\)", page.read_text()):
        target = (page.parent / link).resolve()

        assert target.exists(), f"{page} points at {link}, which is not there"


def headings(page: pathlib.Path) -> list[str]:
    """Answers the headings of a page, leaving out what is only a comment inside a fenced block."""
    lines, fenced = [], False

    for line in page.read_text().splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            lines.append(line)

    return lines


@pytest.mark.parametrize("page", DOCS)
def test_every_heading_of_every_page_is_marked(page):
    """The pages are read by eye before they are read by word, and one page out of step is the one nobody finds."""
    bare = [line for line in headings(page) if not re.match(r"^#{1,6} [^\w\s]", line)]

    assert bare == [], f"{page} has a heading with nothing to see it by: {bare}"


@pytest.mark.parametrize("page", DOCS)
def test_no_page_marks_two_headings_the_same_way(page):
    marks = [re.match(r"^#{1,6} (\S+)", line).group(1) for line in headings(page)]
    repeated = sorted({mark for mark in marks if marks.count(mark) > 1})

    assert repeated == [], f"{page} uses the same mark for more than one heading: {repeated}"


def tuned():
    """Answers the rows of the constants table, each naming what it tunes, the module holding it and the value the prose claims."""
    rows = re.findall(r"^\| `([^|]+)` \| `([\w/.]+\.py)` \| ([^|]+) \|", pathlib.Path("CLAUDE.md").read_text(), re.MULTILINE)

    return [([name.strip(" `") for name in names.split("/")], importlib.import_module("cachefy." + module.removesuffix(".py").replace("/", ".")), [piece.strip(" `") for piece in claimed.split("/")]) for names, module, claimed in rows]


def numeric(stated: list[str]) -> bool:
    """Answers whether every value that row claims is a plain number, which is the only kind that can be compared."""
    return all(re.fullmatch(r"-?\d+(\.\d+)?", piece) for piece in stated)


def counted(value) -> float:
    """Answers the number a constant holds, which for a span is the seconds of it."""
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def test_the_constants_the_documentation_tunes_are_the_ones_the_code_holds():
    """Every tuning number is written down twice, and the day one moves the table goes on saying the old one."""
    table = tuned()

    assert len(table) >= 10, "the table is where every tuning constant is explained, and it was not found"

    for named, module, stated in table:
        for name in named:
            assert hasattr(module, name), f"CLAUDE.md tunes `{name}` in {module.__name__}, which does not have it"

        if len(stated) != len(named) or not numeric(stated):
            continue

        for name, piece in zip(named, stated):
            assert counted(getattr(module, name)) == float(piece), f"CLAUDE.md says `{name}` is {piece} and {module.__name__} holds {getattr(module, name)}"


def test_the_layout_the_documentation_draws_is_the_package_that_exists():
    """The layout block is a map, and a map nobody checks is one that sends people to a module that moved."""
    drawn = re.search(r"```\nsrc/cachefy/\n(.*?)```", pathlib.Path("CLAUDE.md").read_text(), re.DOTALL)

    assert drawn, "the layout block is where the package is explained, and it was not found"

    named = set(re.findall(r"^\s{2,4}([a-z_]+\.py)", drawn.group(1), re.MULTILINE))
    present = {path.name for path in pathlib.Path("src/cachefy").rglob("*.py")}

    assert named == present, f"the layout names {sorted(named - present)} which is gone, and misses {sorted(present - named)}"


def test_every_page_of_the_documentation_is_linked_from_the_readme():
    """A page nothing points at is a page nobody reads."""
    linked = set(re.findall(r"\]\(docs/([\w-]+\.md)\)", pathlib.Path("README.md").read_text()))
    present = {path.name for path in pathlib.Path("docs").glob("*.md")} - {"index.md"}

    assert present <= linked, f"the readme does not point at: {sorted(present - linked)}"


@pytest.mark.skipif("redis" not in STORES, reason="redis is not answering")
def test_the_example_the_readme_shows_runs_from_top_to_bottom():
    """It is the first code anybody copies, and parsing it proves only that it is python."""
    shown = re.findall(r"```python\n(.*?)```", pathlib.Path("README.md").read_text(), re.DOTALL)[0]

    # Only the address it connects to is changed, because the suite owns a server of its own and the readme names the one a reader would have.
    program = shown.replace("redis://127.0.0.1:6379/0", REDIS_URL.rsplit("/", 1)[0] + "/9")

    assert "asyncio.run(main())" in program, "the readme no longer shows a program that runs itself"

    answered = []
    ran = compile(program, "README.md", "exec")

    exec(ran, {"__name__": "__readme__", "print": answered.append})

    assert answered == [{"name": "Paulo"}, {"id": 42, "name": "Paulo"}], f"the readme example answered {answered}"


def contract_table(page: pathlib.Path) -> dict:
    """Answers what one page says each store method is conditional on, keyed by the method."""
    held = {}

    for first, second, said in re.findall(r"^\| `(\w+)`(?: / `(\w+)`)? \| (.+?) \|$", page.read_text(), re.M):
        for name in (first, second):
            if name in Store.__abstractmethods__:
                held[name] = said.strip()

    return held


def test_the_contract_is_stated_the_same_way_wherever_it_is_stated():
    """It is written out twice, so the page somebody writes a store from can quietly say less than the one nobody outside reads."""
    standing = contract_table(pathlib.Path("CLAUDE.md"))
    published = contract_table(pathlib.Path("docs/stores.md"))

    assert set(standing) == set(Store.__abstractmethods__), f"CLAUDE.md does not state every method: {sorted(set(Store.__abstractmethods__) - set(standing))}"
    assert set(published) == set(Store.__abstractmethods__), f"the stores page does not state every method: {sorted(set(Store.__abstractmethods__) - set(published))}"

    for name in sorted(standing):
        assert standing[name] == published[name], f"'{name}' is documented as '{standing[name]}' in CLAUDE.md and '{published[name]}' in docs/stores.md"


def test_every_method_the_contract_states_answers_what_it_says_it_answers():
    """A table that says a method answers something the signature never returns is one a store is written wrong from."""
    answering = {"drop": bool, "touch": bool, "purge": int, "clear": int, "count": int, "setup": None}

    for name, shape in answering.items():
        hinted = inspect.signature(getattr(Store, name)).return_annotation

        assert hinted is shape or hinted == shape, f"'{name}' is documented as answering {shape} and is annotated {hinted}"


# What the prose calls its cache objects, so a call it shows can be read against the signature it would reach.
NAMED = {"app": Cachefy, "cache": Cachefy, "users": Space, "limits": Space, "accounts": Space, "profiles": Space, "sessions": Space, "pages": Space, "profile": Memo, "totals": Memo, "report": Memo, "janitor": Janitor}


@pytest.mark.parametrize("page", DOCS)
def test_every_call_the_documentation_shows_is_one_the_library_takes(page):
    """A policy is built for real already, and this is every other call: a renamed argument breaks each example silently."""
    for block in re.findall(r"```python\n(.*?)```", page.read_text(), re.DOTALL):
        for node in ast.walk(ast.parse(block)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)):
                continue

            owner = NAMED.get(node.func.value.id)
            method = getattr(owner, node.func.attr, None) if owner else None

            if method is None or not callable(method):
                continue

            parameters = inspect.signature(method).parameters.values()
            named = {parameter.name for parameter in parameters}
            positional = [parameter for parameter in parameters if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD) and parameter.name != "self"]

            unknown = {keyword.arg for keyword in node.keywords if keyword.arg} - named

            assert not unknown or any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters), f"{page} calls {node.func.value.id}.{node.func.attr} with {sorted(unknown)}, and it takes {sorted(named - {'self'})}"
            assert len(node.args) <= len(positional) or any(parameter.kind is parameter.VAR_POSITIONAL for parameter in parameters), f"{page} calls {node.func.value.id}.{node.func.attr} with {len(node.args)} positional arguments, and it takes {len(positional)}"
