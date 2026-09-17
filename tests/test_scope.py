"""What a request may see: the scope grammar, and how a request states it.

A library, a category and a tag group -- and one comma rule throughout: a
comma inside one value means *all of*, a repeated value means *any of*.
"""

import pytest
from starlette.requests import Request

from kubed.mcp_kb.mcp import request as request_module
from kubed.mcp_kb.mcp.request import (
    CATEGORIES_HEADER,
    CATEGORIES_PARAM,
    client_reads_resources,
    client_uses_prompts,
    full_listing,
    http_request,
    requested_scope,
)
from kubed.mcp_kb.mcp.scope import EVERYTHING, Scope

pytestmark = pytest.mark.unit


def http(query="", headers=()):
    """A real Starlette request, which is what the readers read through."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp",
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        }
    )


@pytest.fixture
def on_http(monkeypatch):
    def install(query="", headers=()):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_request", lambda: http(query, headers)
        )

    return install


@pytest.fixture
def off_http(monkeypatch):
    def raise_it(*_args):
        raise RuntimeError("no active request")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", raise_it)
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", dict)


# -- the grammar ---------------------------------------------------------------


def test_an_empty_scope_is_falsy_and_admits_everything():
    assert not EVERYTHING
    assert Scope() == EVERYTHING
    assert EVERYTHING.admits("grafana", "observability", ["loki"])
    assert EVERYTHING.admits("anything", None, [])


def test_a_comma_in_a_tag_item_means_all_of_it():
    scope = Scope.parse(tags=["runbooks,oncall"])

    assert scope.admits("any", None, ["runbooks", "oncall", "extra"])
    assert not scope.admits("any", None, ["runbooks"])
    assert not scope.admits("any", None, ["oncall"])


def test_separate_tag_items_mean_any_of_them():
    scope = Scope.parse(tags=["runbooks", "oncall"])

    assert scope.admits("any", None, ["runbooks"])
    assert scope.admits("any", None, ["oncall"])
    assert not scope.admits("any", None, ["neither"])


def test_categories_are_any_of_and_narrow_with_the_tags():
    scope = Scope.parse(categories=["design", "observability"], tags=["loki"])

    assert scope.admits("any", "observability", ["loki"])
    assert not scope.admits("any", "design", ["tempo"])
    assert not scope.admits("any", "homelab", ["loki"])
    assert not scope.admits("any", None, ["loki"])


def test_a_library_is_matched_exactly_and_nothing_below_it():
    """A scope names a library; narrowing inside one is what the other two are
    for, so a second path segment is not a selector at all."""
    scope = Scope.parse(library="grafana")

    assert scope.admits("grafana", None, [])
    assert not scope.admits("grafana-skills", None, [])
    assert not scope.admits("n8n", None, [])


def test_a_library_with_a_slash_is_kept_as_given_so_it_can_be_refused():
    """`?library=grafana/grafana-lgtm` is no longer a thing; the refusal that
    says so needs the string as the client wrote it."""
    assert Scope.parse(library=" grafana/grafana-lgtm ").library == (
        "grafana/grafana-lgtm"
    )


def test_admits_labels_asks_only_the_category_and_tag_halves():
    scope = Scope.parse(library="grafana", categories=["design"])

    assert scope.admits_labels("design", [])
    assert not scope.admits_labels("homelab", [])
    assert not scope.admits("n8n", "design", [])


def test_a_scope_that_says_nothing_at_all_is_everything():
    assert Scope.parse("", [], []) == EVERYTHING
    assert Scope.parse(" ", ["", "  "], [",", ""]) == EVERYTHING


def test_a_scope_is_truthy_as_soon_as_it_narrows():
    assert Scope.parse(library="grafana")
    assert Scope.parse(categories=["design"])
    assert Scope.parse(tags=["loki"])


# -- how a request states it ---------------------------------------------------


def test_a_repeated_parameter_is_any_of_and_a_comma_is_all_of(on_http):
    on_http(
        query="categories=design&categories=engineering"
        "&tags=runbooks,oncall&tags=loki"
    )
    scope = requested_scope()

    assert scope.categories == frozenset({"design", "engineering"})
    assert scope.tags == frozenset(
        {frozenset({"runbooks", "oncall"}), frozenset({"loki"})}
    )


def test_a_repeated_header_is_any_of_too(on_http):
    on_http(headers=((CATEGORIES_HEADER, "design"), (CATEGORIES_HEADER, "homelab")))

    assert requested_scope().categories == frozenset({"design", "homelab"})


def test_a_header_wins_over_the_parameter(on_http):
    """The header is set in a credential by an admin; the parameter rides on a
    URL somebody may paste."""
    on_http(
        query=f"{CATEGORIES_PARAM}=design",
        headers=((CATEGORIES_HEADER, "homelab"),),
    )

    assert requested_scope().categories == frozenset({"homelab"})


def test_the_library_takes_the_first_value_given(on_http):
    on_http(query="library=grafana&library=n8n")

    assert requested_scope().library == "grafana"


def test_the_capability_readers_take_the_first_value_too(on_http):
    on_http(query="prompts=off&prompts=on&resources=off&skills=full")

    assert client_uses_prompts() is False
    assert client_reads_resources() is False
    assert full_listing() is True


def test_every_value_is_a_list_even_when_there_is_one(on_http):
    on_http(query="library=grafana", headers=(("x-skill-tags", "loki"),))
    params, headers = http_request()

    assert params == {"library": ["grafana"]}
    assert headers["x-skill-tags"] == ["loki"]


def test_headers_reachable_without_the_request_are_still_read(monkeypatch):
    """Off the request object, FastMCP may still have the headers as a plain
    mapping -- one value each, which the readers take as a one-item list."""

    def no_request():
        raise RuntimeError("no active request")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", no_request)
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda: {"X-Skill-Library": "grafana", "X-Skill-Tags": "loki,tempo"},
    )

    assert http_request() == (
        {},
        {"x-skill-library": ["grafana"], "x-skill-tags": ["loki,tempo"]},
    )
    assert requested_scope() == Scope.parse(library="grafana", tags=["loki,tempo"])


def test_off_http_there_is_nothing_to_narrow_by(off_http):
    assert http_request() is None
    assert requested_scope() == EVERYTHING
    assert client_uses_prompts() is True
    assert client_reads_resources() is True
    assert full_listing() is False


def test_the_readers_have_one_way_in(monkeypatch):
    """Everything in this module reads the request through `http_request`, so a
    second reader cannot drift from it."""
    monkeypatch.setattr(
        request_module,
        "http_request",
        lambda: ({"categories": ["design"]}, {}),
    )

    assert requested_scope().categories == frozenset({"design"})
