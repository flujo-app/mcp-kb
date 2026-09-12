"""The ``skill://`` address space, and the catalogue that answers it.

Everything this server serves has one address. A pack index, a skill's
instructions, a file a skill references, a file the *pack* references -- each is
a ``skill://`` URI, and reading one is the only operation there is.

That is not a stylistic choice. MCP already has a read-a-thing primitive, and a
model already knows how to drive it: list the resources, read a URI. A server
that invents ``read_skill(skill, file)`` alongside it is asking the model to
learn a second vocabulary for the same act. So the catalogue below is written
once, in URIs, and both halves of the protocol are thin projections of it --
``resources.py`` serves it as resources, ``tools.py`` as two tools with the same
shape for clients that have no resources.

The grammar, which is the whole API::

    skill://<selector>                 an index: a pack, or one of its groups
    skill://<pack>/<skill>             that skill's instructions (SKILL.md)
    skill://<pack>/<skill>/SKILL.md    the same thing, said in full
    skill://<pack>/<skill>/_manifest   what else that skill ships
    skill://<pack>/<skill>/<path>      one of those files
    skill://<pack>/_files              what the pack ships outside its skills
    skill://<pack>/<path>              one of those

One segment is an index, two or more is content: that is the only rule needed to
tell them apart, and it is why the group indexes live in the pack's own
namespace (``skill://grafana-lgtm``) rather than under it. ``selector`` accepts a
pack or a group for the same reason the old ``pack`` argument did -- an agent
narrowing to ``grafana-lgtm`` should not first have to learn it lives in
``grafana``.

Pack-qualified, unlike the URIs FastMCP's ``SkillsDirectoryProvider`` mints.
Those are keyed on the folder name alone, so two packs shipping a ``testing/``
silently collapse into one and the loser disappears from the server entirely.
The qualification costs a path segment and buys a namespace that cannot collide.
It also stays compatible with ``fastmcp.utilities.skills``, which reads
everything before the trailing ``/SKILL.md`` as the name and never assumes that
name has one segment.

Nothing here imports FastMCP. Absent content is ``None``, never an exception, so
a caller in the resource half can raise and a caller in the tool half can
explain -- and the scoping rule stays in one place either way.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass

from .skills import PackResources, Skill, SkillIndex

SCHEME = "skill://"
MAIN_FILE = "SKILL.md"
MANIFEST = "_manifest"
PACK_FILES = "_files"


def _count(n: int, noun: str = "skill") -> str:
    """``3 skills`` / ``1 skill`` -- an index row is prose, so it reads as prose."""
    return f"{n} {noun}" + ("" if n == 1 else "s")

mimetypes.add_type("text/markdown", ".md")


@dataclass(frozen=True)
class Entry:
    """One row of a resource listing -- the shape both halves publish.

    Deliberately the four fields ``resources/list`` returns, so the tool mirror
    is the same rows and not a translation of them.
    """

    uri: str
    name: str
    description: str
    mime_type: str

    def as_dict(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


def parse(uri: str) -> tuple[str, str] | None:
    """Split a ``skill://`` URI into its first segment and the rest.

    Returns ``(selector, "")`` for an index and ``(pack, path)`` for content, or
    None when the string is not a ``skill://`` URI at all. Percent-decoding is
    not done here: the segments this server mints are already path-safe, and
    decoding would be one more way for ``%2e%2e`` to become ``..``.
    """
    if not uri.startswith(SCHEME):
        return None
    rest = uri[len(SCHEME) :].strip("/")
    if not rest:
        return None
    head, _, tail = rest.partition("/")
    return head, tail


def uri_for(skill: Skill, file: str = "") -> str:
    """The address of a skill, or of one file inside it."""
    base = f"{SCHEME}{skill.pack}/{skill.name}"
    return f"{base}/{file}" if file else base


def _manifest_json(skill: Skill) -> str:
    """A skill's file listing, in the shape the ecosystem already reads.

    Path, size and hash for every file -- the same three fields FastMCP's
    ``SkillProvider`` emits, because ``fastmcp.utilities.skills`` requires all
    three and raises on a manifest missing any of them. Dropping the hash would
    be cheaper in tokens and would quietly break every client that syncs skills
    to disk.
    """
    files = []
    for path in sorted(p for p in skill.path.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                digest.update(chunk)
        files.append(
            {
                "path": path.relative_to(skill.path).as_posix(),
                "size": path.stat().st_size,
                "hash": f"sha256:{digest.hexdigest()}",
            }
        )
    return json.dumps({"skill": skill.qualified, "files": files}, indent=2)


def mime_for(path: str) -> str:
    """The media type of a path, defaulting to markdown rather than octets.

    Everything here is prose unless it says otherwise, and a client told
    ``application/octet-stream`` may decline to show a file it could read.
    """
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "text/markdown"


class Catalogue:
    """The address space: what is listed, and what each URI resolves to.

    Wraps ``SkillIndex`` and ``PackResources`` rather than replacing them --
    they still own what a skill is and who may see one. This adds the one thing
    a URI space needs on top: a total function from address to content, with the
    request scope applied on every single call.

    ``pinned`` is threaded through every method for that reason. It is the pack
    an ``X-Skill-Pack`` client is restricted to, and a method that forgot it
    would hand that client somebody else's skills -- so there is no method here
    that can be called without deciding about it.
    """

    def __init__(self, index: SkillIndex, resources: PackResources):
        self._index = index
        self._resources = resources

    # -- listing ------------------------------------------------------------

    def entries(self, pinned: str = "", full: bool = False) -> list[Entry]:
        """The resource listing: indexes first, then optionally every skill.

        The default is the indexes alone -- a dozen rows for a catalogue of
        ninety skills. Listing every skill instead costs ~16k tokens and is paid
        on every ``resources/list`` by every client, which is the exact expense
        this server exists to avoid; ``full`` is for the clients that need it
        anyway (``fastmcp.utilities.skills`` finds skills only by scanning the
        listing for ``/SKILL.md``), not for agents.
        """
        visible = self._index.visible(pinned)
        if not visible:
            return []

        entries: list[Entry] = []
        for pack in sorted({s.pack for s in visible}):
            in_pack = [s for s in visible if s.pack == pack]
            # A flat pack's group is just the pack again, so it names nothing new.
            groups = sorted({s.group for s in in_pack} - {pack})
            summary = _count(len(in_pack))
            if groups:
                summary += f" in {len(groups)} groups: {', '.join(groups)}"
            entries.append(
                Entry(f"{SCHEME}{pack}", pack, f"The {pack} pack — {summary}.", "text/markdown")
            )
            entries += [
                Entry(
                    f"{SCHEME}{group}",
                    group,
                    f"{_count(sum(1 for s in in_pack if s.group == group))}"
                    f" in the {pack} pack.",
                    "text/markdown",
                )
                for group in groups
            ]
            if self._resources.files(pack):
                entries.append(
                    Entry(
                        f"{SCHEME}{pack}/{PACK_FILES}",
                        f"{pack}/{PACK_FILES}",
                        f"Files the {pack} pack ships outside any skill, which"
                        " its skills reference.",
                        "text/markdown",
                    )
                )

        if full:
            entries += [
                Entry(
                    uri_for(skill, MAIN_FILE),
                    f"{skill.qualified}/{MAIN_FILE}",
                    skill.description,
                    "text/markdown",
                )
                for skill in sorted(visible, key=lambda s: s.qualified)
            ]
        return entries

    # -- reading ------------------------------------------------------------

    def read(self, uri: str, pinned: str = "") -> str | None:
        """The content at ``uri``, or None when there is none to give.

        None covers absent, malformed and out-of-scope alike, and that conflation
        is deliberate: knowing a skill's exact name must not be enough to confirm
        it exists in a pack the caller was not given.
        """
        parsed = parse(uri)
        if parsed is None:
            return None
        head, path = parsed
        if not path:
            return self._index_body(head, pinned)
        return self._content(head, path, pinned)

    def _index_body(self, selector: str, pinned: str) -> str | None:
        """One index: every skill under a pack or group, addressed by URI.

        Lines are ``<uri>: <description>`` rather than ``<name>: ...`` so that
        reading an index teaches the grammar for the next call. A name would
        have to be translated back into an address anyway, and across packs it
        is not even unique.
        """
        selected = self._index.select(pinned, selector)
        if not selected:
            return None
        lines = [
            f"{uri_for(skill)}: {skill.description}"
            for skill in sorted(selected, key=lambda s: s.qualified)
        ]
        return (
            f"# {selector} — {_count(len(selected))}\n\n"
            + "\n".join(lines)
            + "\n\nRead any URI above for that skill's instructions. Append"
            f" /{MANIFEST} instead to see what else it ships, then read one of"
            " those paths under the same skill URI.\n"
        )

    def _content(self, pack: str, path: str, pinned: str) -> str | None:
        """Anything with two or more segments: a skill's file, or a pack's.

        Resolution order is skill first, pack file second. The two spaces cannot
        overlap -- a directory holding a SKILL.md is a skill and is therefore
        excluded from ``PackResources`` -- so the order settles the shape of the
        URI rather than arbitrating a genuine ambiguity.
        """
        head, _, rest = path.partition("/")
        skill = self._index.get(f"{pack}/{head}", pinned)
        if skill is not None:
            return self._skill_file(skill, rest or MAIN_FILE)
        if path == PACK_FILES:
            return self._pack_files(pack, pinned)
        # Not a skill, so it is pack-level material. PackResources applies its
        # own scoping, but the pin has to be checked here too: it knows which
        # packs exist, not which this caller may see.
        if pinned and not any(
            s.pack == pack for s in self._index.visible(pinned)
        ):
            return None
        return self._resources.read(pack, path)

    def _skill_file(self, skill: Skill, file: str) -> str | None:
        if file == MANIFEST:
            return _manifest_json(skill)
        # Resolve before comparing, which is what blocks ../ and a symlink
        # pointing out of the skill directory.
        target = (skill.path / file).resolve()
        if not target.is_relative_to(skill.path.resolve()) or not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def _pack_files(self, pack: str, pinned: str) -> str | None:
        if pinned and not any(s.pack == pack for s in self._index.visible(pinned)):
            return None
        files = self._resources.files(pack)
        if not files:
            return None
        lines = [f"{SCHEME}{pack}/{f}" for f in files]
        return (
            f"# {pack} — {_count(len(files), 'pack-level file')}\n\n"
            "These sit outside every skill in this pack, and its skills"
            " reference them.\n\n" + "\n".join(lines) + "\n"
        )

    def mime(self, uri: str) -> str:
        """The media type a read of ``uri`` will return."""
        parsed = parse(uri)
        if parsed is None or not parsed[1]:
            return "text/markdown"
        if parsed[1].endswith(MANIFEST):
            return "application/json"
        return mime_for(parsed[1])
