"""Two plugins of one library that would serve one address: the later one fails there.

A library's plugins are taken in its order. A skill URI, a library-level file
URI or a prompt name already served by an earlier plugin fails the later
plugin *in that library* -- ``/health`` names the conflict under the library,
and nothing of it is served there, its other skills and files included, while
the earlier plugin carries on. The same plugin serves normally in any other
library, because addresses are per library.
"""

import httpx
import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config

pytestmark = pytest.mark.unit


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(root, rel, name, description):
    frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n"
    _write(root, f"{rel}/SKILL.md", frontmatter)


def _pair(tmp_path, first, second, files=("shared/**",), libraries=None):
    """Two plugins feeding library `lib`, in that order; each gets a tree.

    ``libraries`` replaces the one-library default, for the test that serves
    the second plugin elsewhere as well.
    """
    plugins = []
    for name, build in (("first", first), ("second", second)):
        root = tmp_path / name
        root.mkdir()
        build(root)
        plugins.append(
            {"name": name, "source": f"file://{root}", "files": list(files)}
        )
    config = Config.model_validate(
        {
            "plugins": plugins,
            "libraries": libraries or [{"name": "lib", "plugins": ["first", "second"]}],
        }
    )
    return KnowledgeBase(config, tmp_path / "c")


async def _health(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        return (await http.get("/health")).json()


async def _serves(knowledge_base, uri):
    async with Client(knowledge_base.mcp) as client:
        return (await client.read_resource(uri))[0].text


def _conflict(health, library="lib"):
    """What `second` collided on in ``library``, and that `first` is untouched."""
    assert health["plugins"]["first"]["status"] == "ok"
    assert health["plugins"]["second"]["status"] == "ok", "the harvest itself is fine"
    return health["libraries"][library]["conflicts"]


async def test_a_second_plugin_serving_a_claimed_skill_uri_fails(tmp_path):
    kb = _pair(
        tmp_path,
        lambda r: _skill(r, "skills/folder/x", "x", "From first."),
        lambda r: (
            _skill(r, "skills/folder/x", "x", "From second."),
            _skill(r, "skills/folder/y", "y", "Only second."),
        ),
    )
    health = await _health(kb)

    assert _conflict(health) == {
        "second": "conflict: skill://lib/folder/x/SKILL.md is already served by"
        " plugin 'first'"
    }
    assert health["libraries"]["lib"]["skills"] == 1
    assert "From first." in await _serves(kb, "skill://lib/folder/x/SKILL.md")
    async with Client(kb.mcp) as client:
        with pytest.raises(Exception, match="not found"):
            await client.read_resource("skill://lib/folder/y/SKILL.md")


async def test_a_conflict_fails_the_plugin_in_that_library_alone(tmp_path):
    """The same two plugins in another library are another question: served
    there in the other order, it is `first` that loses; served alone, `second`
    serves everything it has."""
    kb = _pair(
        tmp_path,
        lambda r: _skill(r, "skills/folder/x", "x", "From first."),
        lambda r: (
            _skill(r, "skills/folder/x", "x", "From second."),
            _skill(r, "skills/folder/y", "y", "Only second."),
        ),
        libraries=[
            {"name": "lib", "plugins": ["first", "second"]},
            {"name": "reversed", "plugins": ["second", "first"]},
            {"name": "alone", "plugins": ["second"]},
        ],
    )
    health = await _health(kb)

    assert _conflict(health, "lib") == {
        "second": "conflict: skill://lib/folder/x/SKILL.md is already served by"
        " plugin 'first'"
    }
    assert _conflict(health, "reversed") == {
        "first": "conflict: skill://reversed/folder/x/SKILL.md is already served by"
        " plugin 'second'"
    }
    assert "conflicts" not in health["libraries"]["alone"]
    assert health["libraries"]["alone"]["skills"] == 2
    assert "From first." in await _serves(kb, "skill://lib/folder/x/SKILL.md")
    assert "From second." in await _serves(kb, "skill://reversed/folder/x/SKILL.md")
    assert "Only second." in await _serves(kb, "skill://reversed/folder/y/SKILL.md")
    assert "Only second." in await _serves(kb, "skill://alone/folder/y/SKILL.md")
    assert health["plugins"]["second"]["libraries"] == ["lib", "reversed", "alone"]


async def test_a_second_plugin_serving_a_claimed_file_uri_fails(tmp_path):
    kb = _pair(
        tmp_path,
        lambda r: _write(r, "shared/a.md", "first a\n"),
        lambda r: (
            _write(r, "shared/a.md", "second a\n"),
            _skill(r, "skills/y", "y", "Only second."),
        ),
    )
    health = await _health(kb)

    assert _conflict(health) == {
        "second": "conflict: skill://lib/shared/a.md is already served by"
        " plugin 'first'"
    }
    assert await _serves(kb, "skill://lib/shared/a.md") == "first a\n"
    assert kb.index.visible() == []


async def test_a_second_plugin_serving_a_claimed_prompt_name_fails(tmp_path):
    kb = _pair(
        tmp_path,
        lambda r: _write(r, "prompts/same.md", "---\ndescription: 1\n---\nfirst\n"),
        lambda r: (
            _write(r, "prompts/same.md", "---\ndescription: 2\n---\nsecond\n"),
            _write(r, "shared/b.md", "second b\n"),
        ),
    )
    health = await _health(kb)

    assert _conflict(health) == {
        "second": "conflict: prompt lib_same is already served by plugin 'first'"
    }
    async with Client(kb.mcp) as client:
        prompts = await client.list_prompts()
        rendered = await client.get_prompt("lib_same")
        with pytest.raises(Exception, match="not found"):
            await client.read_resource("skill://lib/shared/b.md")
    assert [p.name for p in prompts] == ["lib_same"]
    assert "first" in rendered.messages[0].content.text


async def test_a_later_file_inside_an_earlier_plugins_skill_fails_its_plugin(tmp_path):
    kb = _pair(
        tmp_path,
        lambda r: _skill(r, "skills/x", "x", "From first."),
        lambda r: (
            _write(r, "x/notes.md", "second notes\n"),
            _write(r, "shared/b.md", "second b\n"),
        ),
        files=("shared/**", "x/**"),
    )
    health = await _health(kb)

    assert _conflict(health) == {
        "second": "conflict: skill://lib/x/notes.md is already served by"
        " plugin 'first'"
    }
    async with Client(kb.mcp) as client:
        with pytest.raises(Exception, match="not found"):
            await client.read_resource("skill://lib/shared/b.md")


async def test_a_later_skill_holding_an_earlier_plugins_file_fails_its_plugin(tmp_path):
    kb = _pair(
        tmp_path,
        lambda r: _write(r, "x/notes.md", "first notes\n"),
        lambda r: _skill(r, "skills/x", "x", "From second."),
        files=("x/**",),
    )
    health = await _health(kb)

    assert _conflict(health) == {
        "second": "conflict: skill://lib/x/notes.md is already served by"
        " plugin 'first'"
    }
    assert await _serves(kb, "skill://lib/x/notes.md") == "first notes\n"


@pytest.mark.parametrize(
    ("outer", "inner", "later"),
    [("first", "second", "skill://lib/x/y/SKILL.md"), ("second", "first", "skill://lib/x/SKILL.md")],
)
async def test_a_skill_nested_in_another_plugins_skill_fails_the_later_plugin(
    tmp_path, outer, inner, later
):
    """Either way round: the nested skill would hide the outer one's files."""
    trees = {
        outer: lambda r: _skill(r, "skills/x", "x", f"Outer from {outer}."),
        inner: lambda r: _skill(r, "skills/x/y", "y", f"Inner from {inner}."),
    }
    kb = _pair(tmp_path, trees["first"], trees["second"])
    health = await _health(kb)

    assert _conflict(health) == {
        "second": f"conflict: {later} is already served by plugin 'first'"
    }


@pytest.mark.parametrize("file_first", [True, False])
async def test_a_file_at_a_skill_folders_address_fails_the_later_plugin(
    tmp_path, file_first
):
    """A library file `guides` and a skill under `guides/` claim overlapping
    addresses: `skill://lib/guides` would serve the file where the skill's folder
    is a directory that serves nothing. One plugin cannot hold both — a name is a
    file or a directory — so only two plugins can, and the later one fails.
    """

    def with_file(r):
        _write(r, "guides", "a file named like a folder")

    def with_skill(r):
        _skill(r, "skills/guides/y", "y", "Under guides.")

    first, second = (with_file, with_skill) if file_first else (with_skill, with_file)
    kb = _pair(tmp_path, first, second, files=("guides",))
    health = await _health(kb)

    assert "skill://lib/guides" in _conflict(health)["second"]


@pytest.mark.parametrize("short_first", [True, False])
async def test_a_file_at_another_files_folder_fails_the_later_plugin(
    tmp_path, short_first
):
    """`guides` from one plugin and `guides/a.md` from another cannot both be
    served: the first makes `skill://lib/guides` a file, the second a directory.
    """

    def short(r):
        _write(r, "guides", "a file")

    def long(r):
        _write(r, "guides/a.md", "a file in a folder")

    first, second = (short, long) if short_first else (long, short)
    kb = _pair(tmp_path, first, second, files=("guides", "guides/**/*"))
    health = await _health(kb)

    assert "skill://lib/guides" in _conflict(health)["second"]
