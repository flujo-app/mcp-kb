"""Cold start, the background refresh, and what a client is told when it moves."""

import asyncio
import logging
import os
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.context import Context

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.catalogue import refresh, snapshot
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.announce import _can_remember
from kubed.mcp_kb.mcp.scope import Scope
from kubed.mcp_kb.sources import SourceError
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
    first = KnowledgeBase(make_config(skills_dir), cache)
    assert (cache / "index.json").exists()

    def must_not_run(source, cache_dir):
        raise AssertionError("must not run")

    monkeypatch.setattr(snapshot, "materialise", must_not_run)
    second = KnowledgeBase(make_config(skills_dir), cache)

    assert len(second.index) == len(first.index)
    assert [p.name for p in second.prompts] == [p.name for p in first.prompts]
    assert second.generation == 0


@pytest.mark.unit
def test_a_changed_config_invalidates_the_index(skills_dir, cache, monkeypatch):
    """A different config throws the whole index away, not just the stale rows."""
    KnowledgeBase(make_config(skills_dir, libraries=["flatsource"]), cache)

    seen = _counting_materialise(monkeypatch)
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    assert sorted(seen) == ["deepsource", "flatsource"]
    assert sorted(knowledge_base.status) == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_a_cold_start_rebuilds_a_source_whose_root_is_gone(
    skills_dir, cache, monkeypatch
):
    """A reusable record is one whose tree is still there; the rest are rebuilt."""
    config = make_config(skills_dir)
    KnowledgeBase(config, cache)

    gone = skills_dir / "flatsource"
    for path in sorted(gone.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    gone.rmdir()

    seen = _counting_materialise(monkeypatch)
    knowledge_base = KnowledgeBase(config, cache)

    assert seen == ["flatsource"]
    assert knowledge_base.status["flatsource"]["status"] == "failed"
    assert knowledge_base.status["deepsource"]["status"] == "ok"


# -- refresh ------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_rebuilds_only_the_source_whose_fingerprint_changed(skills_dir, cache):
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    before = knowledge_base.status["deepsource"]["built"]

    _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")

    assert knowledge_base.refresh() == ["flatsource"]
    assert knowledge_base.generation == 1
    assert knowledge_base.status["deepsource"]["built"] == before


@pytest.mark.unit
def test_refresh_is_a_noop_when_nothing_changed(skills_dir, cache):
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    written = (cache / "index.json").stat().st_mtime_ns

    assert knowledge_base.refresh() == []
    assert knowledge_base.generation == 0
    assert (cache / "index.json").stat().st_mtime_ns == written


@pytest.mark.unit
def test_force_rebuilds_everything(skills_dir, cache):
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    assert knowledge_base.refresh(force=True) == ["deepsource", "flatsource"]
    assert knowledge_base.generation == 1


@pytest.mark.unit
def test_refresh_narrows_to_the_sources_it_was_given(skills_dir, cache):
    """The background loop rebuilds only what is due, so ``only`` is a hard filter."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    assert knowledge_base.refresh(force=True, only=["deepsource"]) == ["deepsource"]
    assert knowledge_base.generation == 1


@pytest.mark.unit
def test_a_source_that_keeps_failing_the_same_way_is_not_a_change(skills_dir, cache):
    """Otherwise a missing directory bumps the generation on every single pass."""
    raw = {
        "sources": [s.model_dump(mode="json") for s in make_config(skills_dir).sources]
    }
    raw["sources"].append({"name": "gone", "url": f"file://{skills_dir / 'nope'}"})
    config = Config.model_validate(raw)

    knowledge_base = KnowledgeBase(config, cache)
    assert knowledge_base.status["gone"]["status"] == "failed"
    assert knowledge_base.refresh() == []
    assert knowledge_base.generation == 0


@pytest.mark.unit
def test_a_failed_refresh_keeps_the_last_good_catalogue(skills_dir, cache, monkeypatch):
    """An unreachable source must not empty the catalogue it filled a minute ago."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    served = sorted(s.name for s in knowledge_base.index.visible())

    _breaking_materialise(monkeypatch, "flatsource")
    assert knowledge_base.refresh(force=True, only=["flatsource"]) == ["flatsource"]

    assert sorted(s.name for s in knowledge_base.index.visible()) == served
    assert knowledge_base.status["flatsource"]["status"] == "stale"
    assert "unreachable" in knowledge_base.status["flatsource"]["error"]
    assert knowledge_base.status["flatsource"]["skills"] == 2


@pytest.mark.unit
def test_an_unreadable_file_in_one_source_fails_only_that_source(
    skills_dir, cache, monkeypatch
):
    """I2: a `PermissionError` (any `OSError`) out of one source's harvest must
    become a record for that source, not an exception that aborts the refresh
    pass over every other source.

    The record is `stale` rather than `failed` because this source had a good
    one to keep: an unreadable file is exactly the transient the stale rule
    exists for, and the two rules compose. What I2 is really about is that
    `deepsource` is untouched and nothing escapes.
    """
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    real_load_skills = snapshot.load_skills

    def flaky(dirs, *, library, source, root, tags=()):
        if source == "flatsource":
            raise PermissionError("[Errno 13] Permission denied: SKILL.md")
        return real_load_skills(dirs, library=library, source=source, root=root, tags=tags)

    monkeypatch.setattr(snapshot, "load_skills", flaky)

    assert knowledge_base.refresh(force=True) == ["deepsource", "flatsource"]
    assert knowledge_base.status["flatsource"]["status"] == "stale"
    assert "Permission denied" in knowledge_base.status["flatsource"]["error"]
    assert knowledge_base.status["deepsource"]["status"] == "ok"
    assert sorted(s.name for s in knowledge_base.index.visible()) == [
        "alpha",
        "beta",
        "delta",
        "gamma",
    ]


@pytest.mark.unit
def test_an_unreadable_file_with_no_prior_record_is_a_failed_source(
    skills_dir, cache, monkeypatch
):
    """The other half of I2: with nothing good to keep, an `OSError` is still a
    failed record and still must not empty the catalogue of everything else.
    """
    real_load_skills = snapshot.load_skills

    def flaky(dirs, *, library, source, root, tags=()):
        if source == "flatsource":
            raise PermissionError("[Errno 13] Permission denied: SKILL.md")
        return real_load_skills(dirs, library=library, source=source, root=root, tags=tags)

    monkeypatch.setattr(snapshot, "load_skills", flaky)
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    assert knowledge_base.status["flatsource"]["status"] == "failed"
    assert knowledge_base.status["deepsource"]["status"] == "ok"
    assert sorted(s.name for s in knowledge_base.index.visible()) == ["delta", "gamma"]


@pytest.mark.unit
def test_a_source_that_stays_stale_the_same_way_is_not_a_change(
    skills_dir, cache, monkeypatch
):
    """As with a failure: one unreachable remote must not churn the generation."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")

    assert knowledge_base.refresh(force=True, only=["flatsource"]) == ["flatsource"]
    assert knowledge_base.generation == 1
    assert knowledge_base.refresh(force=True, only=["flatsource"]) == []
    assert knowledge_base.generation == 1


@pytest.mark.unit
def test_a_stale_source_serves_again_once_it_recovers(skills_dir, cache, monkeypatch):
    """The fingerprint has not moved, so only ``status`` can say it is back."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")
    knowledge_base.refresh(force=True, only=["flatsource"])
    assert knowledge_base.status["flatsource"]["status"] == "stale"

    monkeypatch.undo()
    assert knowledge_base.refresh(only=["flatsource"]) == ["flatsource"]
    assert knowledge_base.status["flatsource"]["status"] == "ok"
    assert "error" not in knowledge_base.status["flatsource"]


@pytest.mark.unit
def test_a_cold_start_with_no_prior_record_still_fails(skills_dir, cache, monkeypatch):
    """Nothing good to keep, so the failure is a failure and the source is empty."""
    _breaking_materialise(monkeypatch, "flatsource")
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    assert knowledge_base.status["flatsource"]["status"] == "failed"
    assert sorted(s.name for s in knowledge_base.index.visible()) == ["delta", "gamma"]


@pytest.mark.unit
async def test_health_reports_a_stale_source_with_its_error(
    skills_dir, cache, monkeypatch
):
    """An operator has to be able to see that what is served is no longer fresh."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    _breaking_materialise(monkeypatch, "flatsource")
    knowledge_base.refresh(force=True, only=["flatsource"])

    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://knowledge_base") as http:
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
    first = KnowledgeBase(config, cache)
    _breaking_materialise(monkeypatch, "flatsource")
    first.refresh(force=True, only=["flatsource"])

    knowledge_base = KnowledgeBase(config, cache)

    assert knowledge_base.status["flatsource"]["status"] == "stale"
    assert sorted(s.name for s in knowledge_base.index.visible()) == [
        "alpha",
        "beta",
        "delta",
        "gamma",
    ]
def test_persistence_failure_does_not_leave_records_ahead_of_the_snapshot(
    skills_dir, cache, monkeypatch
):
    """M5: `_write_index` is best-effort and must run after the commit, not
    inside it -- otherwise a raise there leaves `_records` advanced while
    `snapshot` stays on the old generation forever.
    """
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    def boom():
        raise TypeError("boom")

    monkeypatch.setattr(knowledge_base, "_write_index", boom)

    with pytest.raises(TypeError):
        knowledge_base.refresh(force=True)

    # The swap already happened; only persisting it to disk failed.
    assert knowledge_base.generation == 1
    assert knowledge_base.snapshot.status["deepsource"]["built"] == knowledge_base._records["deepsource"].built


@pytest.mark.unit
def test_a_snapshot_is_swapped_not_mutated(skills_dir, cache):
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    old = knowledge_base.snapshot
    listing = old.catalogue.entries()

    knowledge_base.refresh(force=True)

    assert knowledge_base.snapshot is not old
    assert old.generation == 0
    assert knowledge_base.snapshot.generation == 1
    assert old.catalogue is not knowledge_base.snapshot.catalogue
    assert old.catalogue.entries() == listing


@pytest.mark.unit
def test_listings_are_memoised_per_scope(skills_dir, cache):
    """A catalogue lives and dies with its snapshot, so its memo cannot go stale."""
    catalogue = KnowledgeBase(make_config(skills_dir), cache).snapshot.catalogue

    assert catalogue.entries(Scope(), False) is catalogue.entries(Scope(), False)
    assert catalogue.entries(Scope("flatsource"), False) is not catalogue.entries(Scope(), False)
    assert catalogue.entries(Scope(), True) is not catalogue.entries(Scope(), False)


# -- what a live client sees ---------------------------------------------------


@pytest.mark.unit
async def test_a_rebuild_is_visible_to_a_connected_client(skills_dir, cache):
    """A provider holding a ``Catalogue`` would serve generation 0 forever."""
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    async with Client(knowledge_base.mcp) as client:
        assert "skill://deepsource/plugin-c/_index.md" not in [
            str(r.uri) for r in await client.list_resources()
        ]
        assert "deepsource_extra" not in [p.name for p in await client.list_prompts()]

        _add_skill(
            skills_dir / "deepsource" / "plugin-c" / "epsilon", "epsilon", "New."
        )
        _add_prompt(skills_dir / "deepsource" / "prompts" / "extra.md")
        knowledge_base.refresh()

        assert "skill://deepsource/plugin-c/_index.md" in [str(r.uri) for r in await client.list_resources()]
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
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")

    async with Client(knowledge_base.mcp):
        assert await _until(lambda: knowledge_base.generation == 1)
    assert knowledge_base.status["flatsource"]["status"] == "ok"


@pytest.mark.unit
def test_tick_seconds_is_bounded_by_the_shortest_configured_refresh(skills_dir):
    """A `refresh: 1s` source ticking every `TICK_SECONDS` (5s) can be late by
    up to a tick; the loop must sleep no longer than the shortest interval any
    source actually configured.
    """
    raw = {
        "sources": [
            {**s.model_dump(mode="json"), "refresh": "1s"}
            for s in make_config(skills_dir).sources
        ]
    }
    config = Config.model_validate(raw)
    assert config.min_refresh_seconds == 1
    assert refresh.tick_seconds(config.min_refresh_seconds) == 1


@pytest.mark.unit
def test_tick_seconds_defaults_when_nothing_is_scheduled(skills_dir):
    config = make_config(skills_dir)
    assert config.min_refresh_seconds is None
    assert refresh.tick_seconds(config.min_refresh_seconds) == refresh.TICK_SECONDS


class _Slept(Exception):
    """Raised by the stand-in sleep to end the loop after its first tick."""


@pytest.mark.unit
@pytest.mark.parametrize(("interval", "seconds"), [(1, 1), (3600, refresh.TICK_SECONDS)])
async def test_the_loop_sleeps_the_shorter_of_a_tick_and_the_shortest_refresh(
    skills_dir, cache, monkeypatch, interval, seconds
):
    """Through `loop` itself, not the helper: a `refresh: 1s` source sleeps 1s,
    and an hourly one still wakes every tick to ask what is due."""
    raw = {
        "sources": [
            {**s.model_dump(mode="json"), "refresh": f"{interval}s"}
            for s in make_config(skills_dir).sources
        ]
    }
    knowledge_base = KnowledgeBase(Config.model_validate(raw), cache)
    slept = []

    async def sleep(delay):
        slept.append(delay)
        raise _Slept

    async def no_pass(*_args, **_kwargs):
        return None

    monkeypatch.setattr(refresh, "_pass", no_pass)
    monkeypatch.setattr(refresh.asyncio, "sleep", sleep)
    with pytest.raises(_Slept):
        await refresh.loop(knowledge_base)
    assert slept == [seconds]


@pytest.mark.unit
async def test_the_loop_keeps_rebuilding_a_source_whose_refresh_is_due(
    skills_dir, cache, monkeypatch
):
    monkeypatch.setattr(refresh, "TICK_SECONDS", 0.01)
    raw = {
        "sources": [
            {**s.model_dump(mode="json"), "refresh": "1s"}
            for s in make_config(skills_dir).sources
        ]
    }
    knowledge_base = KnowledgeBase(Config.model_validate(raw), cache)

    async with Client(knowledge_base.mcp):
        # Ticks with nothing to do must not churn the generation.
        await asyncio.sleep(0.1)
        assert knowledge_base.generation == 0

        _touch(skills_dir / "flatsource" / "alpha" / "SKILL.md")
        assert await _until(lambda: knowledge_base.generation >= 1)

    assert knowledge_base.status["flatsource"]["status"] == "ok"


@pytest.mark.unit
async def test_an_unchanged_source_is_examined_once_per_interval_not_every_tick(
    skills_dir, cache, monkeypatch
):
    """I1: `refresh: 1s` on an unchanged tree must mean "check once a second",
    not "check every tick forever". The bug: `_due()` scheduled off
    `record.built`, which only moves when a rebuild actually replaces a
    record -- so a source that is checked and found unchanged never advances
    it, and is due again on the very next tick, forever.
    """
    monkeypatch.setattr(refresh, "TICK_SECONDS", 0.02)
    raw = {
        "sources": [
            {**s.model_dump(mode="json"), "refresh": "1s"}
            for s in make_config(skills_dir).sources
        ]
    }
    knowledge_base = KnowledgeBase(Config.model_validate(raw), cache)

    # The fingerprint walk, not `materialise`, is the expensive step an
    # unchanged source repeats -- `build_source` (and `materialise` with it)
    # is only ever reached once a fingerprint actually moves.
    real_fingerprint = refresh.fingerprint
    calls = []

    def counting_fingerprint(source, cache_dir, root):
        calls.append(source.name)
        return real_fingerprint(source, cache_dir, root)

    monkeypatch.setattr(refresh, "fingerprint", counting_fingerprint)

    async with Client(knowledge_base.mcp):
        await asyncio.sleep(1.3)  # a little over one interval, at 50 ticks/s

    # ~1 interval elapsed for 2 sources: a handful of walks is right. A
    # per-tick walk over 1.3s at TICK_SECONDS=0.02 would be ~130.
    assert len(calls) <= 8
    assert knowledge_base.generation == 0


@pytest.mark.unit
async def test_a_persistently_failing_source_is_not_retried_every_tick(
    skills_dir, cache, monkeypatch
):
    """I1's second face: `_same_failure` skips storing the fresh record, so a
    source that keeps failing the same way never advances `built` either --
    which must not turn into a per-tick retry storm once a source is remote.
    """
    monkeypatch.setattr(refresh, "TICK_SECONDS", 0.02)
    raw = {"sources": [s.model_dump(mode="json") for s in make_config(skills_dir).sources]}
    raw["sources"].append(
        {"name": "gone", "url": f"file://{skills_dir / 'nope'}", "refresh": "1s"}
    )
    knowledge_base = KnowledgeBase(Config.model_validate(raw), cache)
    seen = _counting_materialise(monkeypatch)

    async with Client(knowledge_base.mcp):
        await asyncio.sleep(1.3)

    assert len([n for n in seen if n == "gone"]) <= 4
    assert knowledge_base.status["gone"]["status"] == "failed"


@pytest.mark.unit
async def test_can_remember_is_true_only_with_a_session_header(skills_dir, cache):
    """M3: `_can_remember()` must actually read the `mcp-session-id` header --
    replacing its whole body with `return True` left all 206 tests green.
    """
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    @knowledge_base.mcp.custom_route("/can-remember", methods=["GET"])
    async def can_remember(_request):
        from starlette.responses import JSONResponse

        return JSONResponse({"can_remember": _can_remember()})

    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://knowledge_base") as http:
        without_header = await http.get("/can-remember")
        with_header = await http.get(
            "/can-remember", headers={"mcp-session-id": "abc123"}
        )

    assert without_header.json()["can_remember"] is False
    assert with_header.json()["can_remember"] is True


@pytest.mark.unit
async def test_reindex_answers_with_an_error_payload_instead_of_a_500(
    skills_dir, cache, monkeypatch
):
    """I2: the only manual recovery lever this server has must still answer,
    even when a refresh raises something `build_source` did not turn into a
    failed record.
    """
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)

    async def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(knowledge_base, "refresh_async", boom)
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://knowledge_base") as http:
        response = await http.post("/reindex")

    assert response.status_code == 500
    assert response.json()["status"] == "error"


@pytest.mark.unit
async def test_reindex_failure_body_never_carries_the_exception_text(
    skills_dir, cache, monkeypatch, caplog
):
    """`/reindex` is unauthenticated, so `str(exc)` in its body can hand any
    caller an internal path or a remote URL. The response is a fixed message.

    The log keeps the traceback, because that is how a failure gets fixed, but
    never a credential: every configured `{env:}` secret and any URL userinfo are
    redacted before the line is written.
    """
    monkeypatch.setenv("KB_TEST_TOKEN", "s3cr3t-deploy-token")
    config = make_config(skills_dir).model_dump(mode="json", exclude_none=True)
    config["sources"].append(
        {
            "name": "private",
            "url": "git+file:///nonexistent/repo.git",
            "auth": {"username": "deploy", "password": {"env": "KB_TEST_TOKEN"}},
        }
    )
    knowledge_base = KnowledgeBase(Config.model_validate(config), cache)
    leak = "https://deploy:s3cr3t-deploy-token@example.com/repo.git"

    async def boom(**kwargs):
        raise RuntimeError(f"cloning {leak} with s3cr3t-deploy-token failed")

    monkeypatch.setattr(knowledge_base, "refresh_async", boom)
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://knowledge_base"
        ) as http:
            response = await http.post("/reindex")

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "error"
    assert "s3cr3t" not in response.text
    assert "s3cr3t-deploy-token" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "Traceback" in caplog.text
    assert "***@example.com" in caplog.text


@pytest.mark.unit
async def test_reindex_endpoint_rebuilds_and_reports(skills_dir, cache):
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://knowledge_base") as http:
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
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    seen: list[str] = []

    async def handler(message):
        seen.append(type(message).__name__)

    async with Client(knowledge_base.mcp, message_handler=handler) as client:
        await client.list_resources()
        assert _changed(seen) == []

        knowledge_base.refresh(force=True)
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
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    seen: list[str] = []

    async def handler(message):
        seen.append(type(message).__name__)

    async with Client(knowledge_base.mcp, message_handler=handler) as client:
        await client.list_resources()
        knowledge_base.refresh(force=True)
        assert len(await client.list_resources()) > 0

    assert _changed(seen) == []


@pytest.mark.unit
async def test_a_failed_background_pass_logs_no_credential(
    skills_dir, cache, monkeypatch, caplog
):
    """The loop's failure log redacts the same way `/reindex`'s does."""
    monkeypatch.setenv("KB_TEST_TOKEN", "s3cr3t-loop-token")
    config = make_config(skills_dir).model_dump(mode="json", exclude_none=True)
    config["sources"].append(
        {
            "name": "private",
            "url": "git+file:///nonexistent/repo.git",
            "auth": {"username": "deploy", "password": {"env": "KB_TEST_TOKEN"}},
        }
    )
    knowledge_base = KnowledgeBase(Config.model_validate(config), cache)

    async def boom(**kwargs):
        raise RuntimeError("pulling https://deploy:s3cr3t-loop-token@example.com")

    monkeypatch.setattr(knowledge_base, "refresh_async", boom)
    with caplog.at_level(logging.ERROR):
        await refresh._pass(knowledge_base)

    assert "refresh pass failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "s3cr3t-loop-token" not in caplog.text


# -- what the log says ----------------------------------------------------------


def _source_lines(caplog):
    return [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == "kubed.mcp_kb.catalogue.snapshot"
    ]


@pytest.mark.unit
def test_a_source_going_stale_and_recovering_is_logged_once_each_way(
    skills_dir, cache, monkeypatch, caplog
):
    """/health says what a source is; the log says when that changed, once."""
    caplog.set_level(logging.INFO)
    knowledge_base = KnowledgeBase(make_config(skills_dir), cache)
    boot = _source_lines(caplog)
    assert ("INFO", "source flatsource is serving 2 skills, 0 prompts and 0 files") in boot
    assert all(level == "INFO" for level, _ in boot)

    caplog.clear()
    _breaking_materialise(monkeypatch, "flatsource")
    knowledge_base.refresh(force=True, only=["flatsource"])
    knowledge_base.refresh(force=True, only=["flatsource"])
    assert _source_lines(caplog) == [
        (
            "WARNING",
            "source flatsource is stale, still serving its last harvest:"
            " flatsource: the remote is unreachable",
        )
    ]

    caplog.clear()
    monkeypatch.undo()
    knowledge_base.refresh(only=["flatsource"])
    assert _source_lines(caplog) == [
        ("INFO", "source flatsource is serving 2 skills, 0 prompts and 0 files")
    ]


@pytest.mark.unit
def test_a_source_failing_at_boot_is_logged_as_an_error(
    skills_dir, cache, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    _breaking_materialise(monkeypatch, "flatsource")
    KnowledgeBase(make_config(skills_dir), cache)
    assert (
        "ERROR",
        "source flatsource failed: flatsource: the remote is unreachable",
    ) in _source_lines(caplog)


@pytest.mark.unit
def test_a_skipped_path_is_logged_when_first_skipped_and_not_on_every_rebuild(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    root = tmp_path / "src"
    (root / "skills" / "bad").mkdir(parents=True)
    (root / "skills" / "bad" / "SKILL.md").write_text(
        "---\nname: Bad\ndescription: x\n---\nbody\n"
    )
    config = Config.model_validate(
        {"sources": [{"name": "lib", "url": f"file://{root}"}]}
    )
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")
    skips = [m for level, m in _source_lines(caplog) if "skips" in m]
    assert len(skips) == 1 and skips[0].startswith("source lib skips skills/bad: ")

    caplog.clear()
    knowledge_base.refresh(force=True)
    assert [m for _, m in _source_lines(caplog) if "skips" in m] == []
