"""The generated OpenAPI document.

Built from nothing but literal response schemas -- there is no tool schema to
derive a request body from, unlike the sibling `selenium-flow` -- so what
these tests hold the document against is `routes.py` itself: the paths it
actually registers, and the shape of what a real request to each of them
returns.
"""

import httpx
import jsonschema
import pytest
import yaml

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.catalogue import snapshot
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.sources import SourceError
from kubed.mcp_kb.spec import build_spec
from tests.conftest import key_of, make_config

pytestmark = pytest.mark.unit


@pytest.fixture
def cache(tmp_path_factory):
    return tmp_path_factory.mktemp("cache")


@pytest.fixture
def knowledge_base(skills_dir, cache):
    return KnowledgeBase(make_config(skills_dir), cache)


@pytest.fixture
def every_status(skills_dir, cache, monkeypatch):
    """A catalogue holding one fetch of each status at once, and every plugin
    and library shape.

    `deepsource` harvests and stays `ok`. `flatsource` harvests, then its
    backend is broken and it is refreshed, so it keeps serving the tree it
    already had as `stale` -- which is the only status carrying both the full
    field set and `error`; `deepsource` also ships a file named like an index,
    so its plugin entry carries `skipped`. `gone` names a directory that is
    not there, so it never had a tree and is `failed`, which carries `status`
    and `error` and nothing else, and its plugin fails with it. `clash` serves
    `flatsource`'s tree again in the same library, so that library carries a
    `conflicts` entry; `ghost` names a marketplace plugin that does not exist,
    so its library carries an `error`. A document checked against nothing but
    a healthy body says nothing about the shapes an operator actually goes to
    `/health` for.
    """
    config = make_config(skills_dir)
    raw = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    raw["plugins"].append({"name": "gone", "source": f"file://{skills_dir / 'nope'}"})
    raw["plugins"].append(
        {
            "name": "clash",
            "source": f"file://{skills_dir / 'flatsource'}",
            "skills": ["**/SKILL.md"],
            "version": "1",
        }
    )
    raw["libraries"].append({"name": "gone", "plugins": ["gone"]})
    flatsource = next(lib for lib in raw["libraries"] if lib["name"] == "flatsource")
    flatsource["plugins"].append("clash")
    raw["libraries"].append({"name": "ghost", "plugins": ["nope@ghost"]})
    # A file named like an index: `deepsource` serves on, and lists it skipped.
    (skills_dir / "deepsource" / "_index.md").write_text("not an index\n")
    knowledge_base = KnowledgeBase(Config.model_validate(raw), cache)

    real = snapshot.materialise
    key = key_of(config, "flatsource")

    def broken(fetch, cache_dir):
        if fetch.key == key:
            raise SourceError(f"{key}: the remote is unreachable")
        return real(fetch, cache_dir)

    monkeypatch.setattr(snapshot, "materialise", broken)
    knowledge_base.refresh(force=True, only=[key])
    return knowledge_base


def validate(name: str, body: dict) -> None:
    """Hold a real response body to the schema the document publishes for it.

    The schema is handed to the validator with the whole `components` block
    attached, so the `$ref`s inside it -- `Health.fetches` points at
    `FetchStatus`, `Reindex` is an `allOf` over `Health` -- resolve against
    the same document `GET /openapi.yaml` serves.
    """
    spec = build_spec()
    schema = {**spec["components"]["schemas"][name], "components": spec["components"]}
    jsonschema.validate(body, schema)


def test_it_is_openapi_31():
    assert build_spec()["openapi"] == "3.1.0"


def test_the_document_validates():
    """Validated with a real validator, not just eyeballed."""
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(build_spec())


def test_every_registered_route_except_openapi_yaml_is_documented(knowledge_base):
    """The path list comes from the LIVE app, not a second hand-kept list.

    `mcp._additional_http_routes` is exactly what `@mcp.custom_route`
    populates in `routes.py`, so a third endpoint added there with no
    matching entry in `build_spec()` fails this test -- proved by adding one
    temporarily (`@mcp.custom_route("/throwaway", methods=["GET"])`) and
    watching it fail before removing it again.
    """
    registered = {route.path for route in knowledge_base.mcp._additional_http_routes}
    assert registered - {"/openapi.yaml"} == set(build_spec()["paths"])


async def test_the_served_document_equals_the_built_one(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        response = await http.get("/openapi.yaml")
    assert response.status_code == 200
    assert "yaml" in response.headers["content-type"]
    assert yaml.safe_load(response.text) == build_spec()


def test_each_status_documents_every_field_report_can_produce():
    """Every key `snapshot.status[...]` or `snapshot.stats(key)` can add."""
    schemas = build_spec()["components"]["schemas"]
    assert set(schemas["FetchStatus"]["properties"]) == {
        "status", "built", "fingerprint", "live", "error", "revalidated",
        "fetched", "cooling",
    }
    assert set(schemas["PluginStatus"]["properties"]) == {
        "status", "fetch", "root", "category", "tags", "keywords", "version",
        "libraries", "skills", "prompts", "files", "built", "error", "skipped",
    }
    assert set(schemas["LibraryStatus"]["properties"]) == {
        "description", "plugins", "skills", "prompts", "files", "error",
        "skipped", "conflicts",
    }


def test_health_documents_every_top_level_field():
    props = set(build_spec()["components"]["schemas"]["Health"]["properties"])
    assert props == {
        "status", "generation", "built", "skills", "prompts",
        "libraries", "plugins", "fetches",
    }


def test_reindex_extends_health_with_rebuilt():
    """`/reindex` answers with `/health`'s own body plus one field."""
    reindex = build_spec()["components"]["schemas"]["Reindex"]
    ref, extra = reindex["allOf"]
    assert ref == {"$ref": "#/components/schemas/Health"}
    assert set(extra["properties"]) == {"rebuilt"}


def test_reindex_500_uses_the_error_schema():
    op = build_spec()["paths"]["/reindex"]["post"]
    schema = op["responses"]["500"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ReindexError"}


def test_info_is_drawn_from_pyproject():
    from importlib.metadata import metadata

    spec = build_spec()
    dist = metadata("kubed-mcp-kb")
    assert spec["info"]["description"] == dist["Summary"]
    assert spec["info"]["version"] == dist["Version"]
    assert spec["info"]["title"] == "mcp-kb"


def test_servers_are_the_in_cluster_service_and_localhost():
    urls = {s["url"] for s in build_spec()["servers"]}
    assert urls == {"http://mcp-kb.flow.svc.cluster.local:8000", "http://localhost:8000"}


def test_operations_are_tagged_and_carry_a_summary():
    for path, ops in build_spec()["paths"].items():
        for method, op in ops.items():
            assert op["tags"] == ["ops"], f"{method} {path}"
            assert op["summary"], f"{method} {path} has no summary"
            assert op["operationId"], f"{method} {path} has no operationId"


async def test_a_real_health_response_matches_the_documented_fields(every_status):
    """The anti-drift guarantee: what `report()` actually returns is exactly
    what `Health` and the three status schemas say it may -- names *and* types.

    The names are compared as sets because neither schema closes itself with
    `additionalProperties: false`, and closing them would break every consumer
    the first time a field is added; the set comparison is what catches a field
    `report()` grew and the document never heard about. The types need the
    validator: `fingerprint` was declared a string and served an object for as
    long as this test compared key sets alone. Re-introduce `"string"` in
    `spec/builder.py` and this fails.
    """
    transport = httpx.ASGITransport(app=every_status.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        body = (await http.get("/health")).json()

    assert {info["status"] for info in body["fetches"].values()} == {
        "ok",
        "stale",
        "failed",
    }
    assert {info["status"] for info in body["plugins"].values()} == {"ok", "failed"}
    assert body["plugins"]["deepsource"]["skipped"]
    assert body["plugins"]["clash"]["version"] == "1"
    assert body["libraries"]["flatsource"]["conflicts"]
    assert body["libraries"]["ghost"]["error"]
    validate("Health", body)

    schemas = build_spec()["components"]["schemas"]
    assert set(body) == set(schemas["Health"]["properties"])
    for part, schema in (
        ("fetches", "FetchStatus"),
        ("plugins", "PluginStatus"),
        ("libraries", "LibraryStatus"),
    ):
        for info in body[part].values():
            assert set(info) <= set(schemas[schema]["properties"]), (part, info)


async def test_a_failed_fetch_carries_only_its_status_and_error(every_status):
    """`built` and `fingerprint` describe the tree being served, which a fetch
    that never materialised one has not got -- and the document says they are
    absent only when `status` is `failed`, so the published contract is what
    is checked here rather than the field list, which a superset satisfies."""
    transport = httpx.ASGITransport(app=every_status.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        fetches = (await http.get("/health")).json()["fetches"]

    assert [
        set(info) for info in fetches.values() if info["status"] == "failed"
    ] == [{"status", "error"}]
    for info in fetches.values():
        if info["status"] != "failed":
            assert info["built"] and info["fingerprint"] is not None


async def test_a_real_reindex_response_matches_the_documented_fields(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        body = (await http.post("/reindex")).json()

    validate("Reindex", body)
    schemas = build_spec()["components"]["schemas"]
    documented = set(schemas["Health"]["properties"]) | set(
        schemas["Reindex"]["allOf"][1]["properties"]
    )
    assert set(body) == documented
