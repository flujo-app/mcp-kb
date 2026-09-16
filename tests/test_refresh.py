"""Cold start, the background refresh, and what a client is told when it moves."""

import asyncio
import os
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.context import Context

import mcp_school.server
from mcp_school import School, snapshot
from mcp_school.config import Config
from mcp_school.sources import SourceError
from tests.conftest import make_config


@pytest.fixture
def cache(tmp_path_factory):
    """A cache directory outside the skills tree.

    ``make_config`` turns every directory under the skills root into a source,
    so an ``index.json`` written inside it would earn the cache a source of its
    own the next time a config is built.
    """
    return tmp_path_factory.mktemp("cache")


@pytest.fixture
def one_session(monkeypatch):
    """Give the in-memory transport the session continuity it does not have.

    FastMCP 4.0.3 negotiates MCP 2026-07-28, which has no sessions: it mints a
    fresh session id per request, so ``Context.set_state`` is never read back.
    A client on a session-era connection does have that continuity, and pinning
    the state key is how a test gets it without a second process. Only the
    keying is faked -- the middleware, the notifications and their delivery to
    the client are all the real ones.
    """
    monkeypatch.setattr(Context, "_make_state_key", lambda self, key: key)


def _changed(seen):
    """The list-changed notifications among everything the client received."""
    return sorted(m for m in seen if "ListChanged" in m)


def _touch(path):
    """Give ``path`` an mtime far enough in the future to move a fingerprint."""
    later = time.time_ns() + 5_000_000_000
    os.utime(path, ns=(later, later))


def _add_skill(root, name, description):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    )


def _add_prompt(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ndescription: An extra prompt.\n---\nDo the thing.\n")


def _counting_materialise(monkeypatch):
    """Record every source ``build_source`` materialises, and keep materialising."""
    real = snapshot.materialise
    seen = []

    def spy(source, cache_dir):
        seen.append(source.name)
        return real(source, cache_dir)

    monkeypatch.setattr(snapshot, "materialise", spy)
    return seen


def _breaking_materialise(monkeypatch, name, error="the remote is unreachable"):
    """Make one source's ``materialise`` raise, leaving every other source alone."""
    real = snapshot.materialise

    def broken(source, cache_dir):
        if source.name == name:
            raise SourceError(f"{name}: {error}")
        return real(source, cache_dir)

    monkeypatch.setattr(snapshot, "materialise", broken)


# -- cold start ---------------------------------------------------------------


@pytest.mark.unit
def test_a_cold_start_reads_the_index_and_does_not_materialise(
    skills_dir, cache, monkeypatch
):
    """The whole point of the index: serve without touching a source."""
    first = School(make_config(skills_dir), cache)
    assert (cache / "index.json").exists()

    def must_not_run(source, cache_dir):
        raise AssertionError("must not run")

    monkeypatch.setattr(snapshot, "materialise", must_not_run)
    second = School(make_config(skills_dir), cache)

    assert len(second.index) == len(first.index)
    assert [p.name for p in second.prompts] == [p.name for p in first.prompts]
    assert second.generation == 0


@pytest.mark.unit
def test_a_changed_config_invalidates_the_index(skills_dir, cache, monkeypatch):
    """A different config throws the whole index away, not just the stale rows."""
    School(make_config(skills_dir, packs=["flatsource"]), cache)

    seen = _counting_materialise(monkeypatch)
    school = School(make_config(skills_dir), cache)

    assert sorted(seen) == ["deepsource", "flatsource"]
    assert sorted(school.status) == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_a_cold_start_rebuilds_a_source_whose_root_is_gone(
    skills_dir, cache, monkeypatch
):
    """A reusable record is one whose tree is still there; the rest are rebuilt."""
    config = make_config(skills_dir)
    School(config, cache)

    gone = skills_dir / "flatsource"
    for path in sorted(gone.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    gone.rmdir()

    seen = _counting_materialise(monkeypatch)
    school = School(config, cache)

    assert seen == ["flatsource"]
    assert school.status["flatsource"]["status"] == "failed"
    assert school.status["deepsource"]["status"] == "ok"


# -- refresh ------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_rebuilds_only_the_source_whose_fingerprint_changed(skills_dir, cache):
    school = School(make_config(skills_dir), cache)
    before = school.status["deepsource"]["built"]

    _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")

    assert school.refresh() == ["flatsource"]
    assert school.generation == 1
    assert school.status["deepsource"]["built"] == before


@pytest.mark.unit
def test_refresh_is_a_noop_when_nothing_changed(skills_dir, cache):
    school = School(make_config(skills_dir), cache)
    written = (cache / "index.json").stat().st_mtime_ns

    assert school.refresh() == []
    assert school.generation == 0
    assert (cache / "index.json").stat().st_mtime_ns == written


@pytest.mark.unit
def test_force_rebuilds_everything(skills_dir, cache):
    school = School(make_config(skills_dir), cache)
    assert school.refresh(force=True) == ["deepsource", "flatsource"]
    assert school.generation == 1


@pytest.mark.unit
def test_refresh_narrows_to_the_sources_it_was_given(skills_dir, cache):
    """The background loop rebuilds only what is due, so ``only`` is a hard filter."""
    school = School(make_config(skills_dir), cache)
    assert school.refresh(force=True, only=["deepsource"]) == ["deepsource"]
    assert school.generation == 1


@pytest.mark.unit
def test_a_source_that_keeps_failing_the_same_way_is_not_a_change(skills_dir, cache):
    """Otherwise a missing directory bumps the generation on every single pass."""
    raw = {
        "sources": [s.model_dump(mode="json") for s in make_config(skills_dir).sources]
    }
    raw["sources"].append({"name": "gone", "url": f"file://{skills_dir / 'nope'}"})
    config = Config.model_validate(raw)

    school = School(config, cache)
    assert school.status["gone"]["status"] == "failed"
    assert school.refresh() == []
    assert school.generation == 0


@pytest.mark.unit
def test_a_failed_refresh_keeps_the_last_good_catalogue(skills_dir, cache, monkeypatch):
    """An unreachable source must not empty the catalogue it filled a minute ago."""
    school = School(make_config(skills_dir), cache)
    served = sorted(s.name for s in school.index.visible())

    _breaking_materialise(monkeypatch, "flatsource")
    assert school.refresh(force=True, only=["flatsource"]) == ["flatsource"]

    assert sorted(s.name for s in school.index.visible()) == served
    assert school.status["flatsource"]["status"] == "stale"
    assert "unreachable" in school.status["flatsource"]["error"]
    assert school.status["flatsource"]["skills"] == 2
    assert school.status["deepsource"]["status"] == "ok"


@pytest.mark.unit
def test_a_source_that_stays_stale_the_same_way_is_not_a_change(
    skills_dir, cache, monkeypatch
):
    """As with a failure: one unreachable remote must not churn the generation."""
    school = School(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")

    assert school.refresh(force=True, only=["flatsource"]) == ["flatsource"]
    assert school.generation == 1
    assert school.refresh(force=True, only=["flatsource"]) == []
    assert school.generation == 1


@pytest.mark.unit
def test_a_stale_source_serves_again_once_it_recovers(skills_dir, cache, monkeypatch):
    """The fingerprint has not moved, so only ``status`` can say it is back."""
    school = School(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")
    school.refresh(force=True, only=["flatsource"])
    assert school.status["flatsource"]["status"] == "stale"

    monkeypatch.undo()
    assert school.refresh(only=["flatsource"]) == ["flatsource"]
    assert school.status["flatsource"]["status"] == "ok"
    assert "error" not in school.status["flatsource"]


@pytest.mark.unit
def test_a_cold_start_with_no_prior_record_still_fails(skills_dir, cache, monkeypatch):
    """Nothing good to keep, so the failure is a failure and the source is empty."""
    _breaking_materialise(monkeypatch, "flatsource")
    school = School(make_config(skills_dir), cache)

    assert school.status["flatsource"]["status"] == "failed"
    assert sorted(s.name for s in school.index.visible()) == ["delta", "gamma"]


@pytest.mark.unit
async def test_health_reports_a_stale_source_with_its_error(
    skills_dir, cache, monkeypatch
):
    """An operator has to be able to see that what is served is no longer fresh."""
    school = School(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")
    school.refresh(force=True, only=["flatsource"])

    transport = httpx.ASGITransport(app=school.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://school") as http:
        body = (await http.get("/health")).json()

    assert body["status"] == "ok"
    assert body["skills"] == 4
    assert body["sources"]["flatsource"]["status"] == "stale"
    assert "unreachable" in body["sources"]["flatsource"]["error"]
    assert "flatsource" in body["libraries"]


@pytest.mark.unit
def test_a_restart_keeps_serving_what_a_stale_record_still_names(
    skills_dir, cache, monkeypatch
):
    """The stale record is in ``index.json``; a restart must not discard it."""
    config = make_config(skills_dir)
    first = School(config, cache)
    _breaking_materialise(monkeypatch, "flatsource")
    first.refresh(force=True, only=["flatsource"])

    school = School(config, cache)

    assert school.status["flatsource"]["status"] == "stale"
    assert sorted(s.name for s in school.index.visible()) == [
        "alpha",
        "beta",
        "delta",
        "gamma",
    ]


@pytest.mark.unit
def test_a_snapshot_is_swapped_not_mutated(skills_dir, cache):
    school = School(make_config(skills_dir), cache)
    old = school.snapshot
    listing = old.catalogue.entries()

    school.refresh(force=True)

    assert school.snapshot is not old
    assert old.generation == 0
    assert school.snapshot.generation == 1
    assert old.catalogue is not school.snapshot.catalogue
    assert old.catalogue.entries() == listing


@pytest.mark.unit
def test_listings_are_memoised_per_scope(skills_dir, cache):
    """A catalogue lives and dies with its snapshot, so its memo cannot go stale."""
    catalogue = School(make_config(skills_dir), cache).snapshot.catalogue

    assert catalogue.entries("", False) is catalogue.entries("", False)
    assert catalogue.entries("flatsource", False) is not catalogue.entries("", False)
    assert catalogue.entries("", True) is not catalogue.entries("", False)


# -- what a live client sees ---------------------------------------------------


@pytest.mark.unit
async def test_a_rebuild_is_visible_to_a_connected_client(skills_dir, cache):
    """A provider holding a ``Catalogue`` would serve generation 0 forever."""
    school = School(make_config(skills_dir), cache)
    async with Client(school.mcp) as client:
        assert "skill://plugin-c" not in [
            str(r.uri) for r in await client.list_resources()
        ]
        assert "deepsource_extra" not in [p.name for p in await client.list_prompts()]

        _add_skill(
            skills_dir / "deepsource" / "plugin-c" / "epsilon", "epsilon", "New."
        )
        _add_prompt(skills_dir / "deepsource" / "prompts" / "extra.md")
        school.refresh()

        assert "skill://plugin-c" in [str(r.uri) for r in await client.list_resources()]
        assert "deepsource_extra" in [p.name for p in await client.list_prompts()]


async def _until(predicate, seconds=5.0):
    """Wait for the background loop, which runs its passes in a worker thread."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.unit
async def test_the_lifespan_verifies_the_index_the_server_started_from(
    skills_dir, cache
):
    """Cold start skipped the fingerprints; this is where they are checked."""
    school = School(make_config(skills_dir), cache)
    _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")

    async with Client(school.mcp):
        assert await _until(lambda: school.generation == 1)
    assert school.status["flatsource"]["status"] == "ok"


@pytest.mark.unit
async def test_the_loop_keeps_rebuilding_a_source_whose_refresh_is_due(
    skills_dir, cache, monkeypatch
):
    monkeypatch.setattr(mcp_school.server, "TICK_SECONDS", 0.01)
    raw = {
        "sources": [
            {**s.model_dump(mode="json"), "refresh": "1s"}
            for s in make_config(skills_dir).sources
        ]
    }
    school = School(Config.model_validate(raw), cache)

    async with Client(school.mcp):
        # Ticks with nothing to do must not churn the generation.
        await asyncio.sleep(0.1)
        assert school.generation == 0

        _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")
        assert await _until(lambda: school.generation >= 1)

    assert school.status["flatsource"]["status"] == "ok"


@pytest.mark.unit
async def test_reindex_endpoint_rebuilds_and_reports(skills_dir, cache):
    school = School(make_config(skills_dir), cache)
    transport = httpx.ASGITransport(app=school.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://school") as http:
        response = await http.post("/reindex")

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["rebuilt"]) == ["deepsource", "flatsource"]
    assert body["generation"] == 1
    assert body["status"] == "ok"
    assert body["skills"] == 4
    assert body["sources"]["flatsource"]["fingerprint"]["files"]
    assert body["sources"]["flatsource"]["built"]


@pytest.mark.unit
async def test_a_session_is_told_once_when_the_generation_moves(
    skills_dir, cache, one_session
):
    school = School(make_config(skills_dir), cache)
    seen: list[str] = []

    async def handler(message):
        seen.append(type(message).__name__)

    async with Client(school.mcp, message_handler=handler) as client:
        await client.list_resources()
        assert _changed(seen) == []

        school.refresh(force=True)
        await client.list_resources()
        assert _changed(seen) == [
            "PromptListChangedNotification",
            "ResourceListChangedNotification",
        ]

        await client.list_resources()
        assert len(_changed(seen)) == 2


@pytest.mark.unit
async def test_a_sessionless_connection_is_told_nothing_and_still_works(
    skills_dir, cache
):
    """Without ``one_session`` this is the real MCP 2026-07-28 connection.

    It has no session to remember what it was told, so it is told nothing --
    and every request still succeeds, which is the part that matters.
    """
    school = School(make_config(skills_dir), cache)
    seen: list[str] = []

    async def handler(message):
        seen.append(type(message).__name__)

    async with Client(school.mcp, message_handler=handler) as client:
        await client.list_resources()
        school.refresh(force=True)
        assert len(await client.list_resources()) > 0

    assert _changed(seen) == []
