"""What guards a version that can never be taken back, because pypi never lets a number be used twice."""

import pathlib
import tomllib

import yaml

WORKFLOWS = pathlib.Path(".github/workflows")


def workflow(name: str) -> dict:
    """Answers the workflow that file declares, with the trigger key read as the word it is."""
    # A bare `on` is the boolean true to a yaml reader, which is why it is asked for both ways.
    spec = yaml.safe_load((WORKFLOWS / name).read_text())
    spec["triggers"] = spec.get("on", spec.get(True))

    return spec


def steps_of(spec: dict) -> list:
    """Answers every step of every job the workflow declares."""
    return [step for job in spec["jobs"].values() if isinstance(job, dict) for step in (job.get("steps") or [])]


def test_a_release_happens_on_a_tag_and_on_nothing_else():
    """Every other way in is one where the tag never matches a version, so the check below has nothing to compare."""
    triggers = workflow("release.yml")["triggers"]

    assert set(triggers) == {"push"}, f"a release can be started by {sorted(triggers)}, and only a tag carries a version to check"
    assert triggers["push"] == {"tags": ["v*"]}


def test_the_check_that_the_tag_is_the_version_can_never_be_skipped():
    """It was written `if: startsWith(github.ref, 'refs/tags/')`, so a run started by hand skipped it and published anyway."""
    spec = workflow("release.yml")
    checking = [step for step in steps_of(spec) if "tag" in (step.get("name") or "")]

    assert checking, "nothing in the release checks the tag against the version"

    for step in checking:
        assert "if" not in step, f"'{step['name']}' is conditional, and a release that skips it publishes a number nobody asked for"


def test_nothing_is_published_before_the_suite_and_the_load_have_answered():
    """A version on pypi is permanent, so what it carries is answered for before anything is built."""
    jobs = workflow("release.yml")["jobs"]

    assert jobs["build"]["needs"] == ["test", "stress"]
    assert jobs["publish"]["needs"] == "build"

    assert jobs["test"]["uses"].endswith("test.yml"), "the release runs a suite of its own rather than the one every push runs"
    assert jobs["stress"]["uses"].endswith("stress.yml")


def test_what_is_published_is_what_was_checked():
    """A job that built a second time would publish something no suite ever ran against."""
    publishing = steps_of({"jobs": {"publish": workflow("release.yml")["jobs"]["publish"]}})
    what = [step.get("uses", "") for step in publishing]

    assert any("download-artifact" in use for use in what), "the publish job does not take the artifact the build job made"
    assert not any("setup-python" in use for use in what), "the publish job sets python up, which is what building a second time needs"


def test_it_authenticates_by_a_token_nobody_stores():
    """An api token in a repository is a secret that outlives whoever added it."""
    publishing = workflow("release.yml")["jobs"]["publish"]

    assert publishing["permissions"]["id-token"] == "write"
    assert publishing["environment"]["name"] == "pypi"

    assert "secrets" not in (WORKFLOWS / "release.yml").read_text().replace("secrets: inherit", ""), "the release names a stored secret"


def test_every_store_is_answered_for_at_both_ends_of_the_range_it_documents():
    """A minimum nobody tests is a number in a table."""
    jobs = workflow("test.yml")["jobs"]
    oldest = {name: service["image"] for name, service in jobs["oldest"]["services"].items()}

    promised = pathlib.Path("docs/stores.md").read_text()

    for image, floor in ((oldest["redis"], "Redis 7.0"), (oldest["mysql"], "MySQL 8.0"), (oldest["postgres"], "PostgreSQL 14")):
        assert floor in promised, f"the documentation no longer promises {floor} while the suite still runs {image}"

    assert oldest["redis"] == "redis:7.0"
    assert oldest["mysql"] == "mysql:8.0"
    assert oldest["postgres"] == "postgres:14"


def test_every_python_the_project_claims_is_one_the_suite_runs():
    """A classifier nobody runs against is a claim, and the floor is where the interpreter differences actually live."""
    running = set(workflow("test.yml")["jobs"]["test"]["strategy"]["matrix"]["python"])
    declared = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]
    claimed = {said.rsplit("::", 1)[-1].strip() for said in declared["classifiers"] if said.startswith("Programming Language :: Python :: 3.")}

    assert claimed == running, f"the package claims {sorted(claimed)} and the suite runs {sorted(running)}"
    assert declared["requires-python"] == f">={min(running)}", "the floor the package requires is not the oldest one anything runs"
