"""The ``skill://`` address space, and the catalogue that answers it.

Everything this server serves has one address. A library index, a skill's
instructions, a file a skill references, a file the *library* references --
each is a ``skill://`` URI, and reading one is the only operation there is.

That is not a stylistic choice. MCP already has a read-a-thing primitive, and a
model already knows how to drive it: list the resources, read a URI. A server
that invents ``read_skill(skill, file)`` alongside it is asking the model to
learn a second vocabulary for the same act. So the catalogue below is written
once, in URIs, and both halves of the protocol are thin projections of it --
``resources.py`` serves it as resources, ``tools.py`` as two tools with the same
shape for clients that have no resources.

The grammar, which is the whole API::

    skill://<selector>                    an index: a library, or one of its groups
    skill://<library>/<skill>             that skill's instructions (SKILL.md)
    skill://<library>/<skill>/SKILL.md    the same thing, said in full
    skill://<library>/<skill>/_manifest   what else that skill ships
    skill://<library>/<skill>/<path>      one of those files
    skill://<library>/_files              what the library ships outside its skills
    skill://<library>/<path>              one of those

One segment is an index, two or more is content: that is the only rule needed to
tell them apart, and it is why the group indexes live in the library's own
namespace (``skill://grafana-lgtm``) rather than under it. ``selector`` accepts
a library or a group for the same reason: an agent narrowing to
``grafana-lgtm`` should not first have to learn that it lives in ``grafana``.

Library-qualified, unlike the URIs FastMCP's ``SkillsDirectoryProvider`` mints.
Those are keyed on the folder name alone, so two libraries shipping a
``testing/`` silently collapse into one and the loser disappears from the
server entirely. The qualification costs a path segment and buys a namespace
that cannot collide.
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..mcp.scope import EVERYTHING, Scope
from .harvest import hidden
from .skills import LibraryFiles, Skill, SkillIndex

SCHEME = "skill://"
MAIN_FILE = "SKILL.md"
MANIFEST = "_manifest"
# Distinct scopes a catalogue remembers listings for before starting over.
MEMO_LIMIT = 64
LIBRARY_FILES = "_files"


def _count(n: int, noun: str = "skill") -> str:
    """``3 skills`` / ``1 skill`` -- an index row is prose, so it reads as prose."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


mimetypes.add_type("text/markdown", ".md")


@dataclass(frozen=True)
class Entry:
    """One row of a resource listing -- the shape both halves publish.

    ``uri``, ``name``, ``description`` and ``mime_type`` are the four fields
    ``resources/list`` returns, so the tool mirror is the same rows and not a
    translation of them. ``tags`` rides alongside for ``resources.py`` to carry
    onto the ``TextResource`` it builds; it is not one of the four and never
    appears in ``as_dict``. An index row is tagged with its library and
    ``"index"``; a full row (one skill, in the ``full=True`` listing) carries
    that skill's own tags.
    """

    uri: str
    name: str
    description: str
    mime_type: str
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


def parse(uri: str) -> tuple[str, str] | None:
    """Split a ``skill://`` URI into its first segment and the rest.

    Returns ``(selector, "")`` for an index and ``(library, path)`` for content,
    or None when the string is not a ``skill://`` URI at all. Percent-decoding is
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
    base = f"{SCHEME}{skill.library}/{skill.name}"
    return f"{base}/{file}" if file else base


def _manifest_json(skill: Skill) -> str:
    """A skill's file listing, in the shape the ecosystem already reads.

    Path, size and hash for every file -- the same three fields FastMCP's
    ``SkillProvider`` emits, because ``fastmcp.utilities.skills`` requires all
    three and raises on a manifest missing any of them. Dropping the hash would
    be cheaper in tokens and would quietly break every client that syncs skills
    to disk.

    The same dot-file rule the harvest applies, so what a manifest advertises is
    what the skill ships: a client that syncs a skill to disk must not be sent
    after an editor's swap file.
    """
    files = []
    for path in sorted(_manifest_files(skill)):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
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


def _manifest_files(skill: Skill) -> list[Path]:
    """The files a manifest describes: every regular one the harvest would keep."""
    return [
        p
        for p in skill.path.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not hidden(p.relative_to(skill.path))
    ]


def mime_for(path: str) -> str:
    """The media type of a path, defaulting to markdown rather than octets.

    Everything here is prose unless it says otherwise, and a client told
    ``application/octet-stream`` may decline to show a file it could read.
    """
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "text/markdown"


class Catalogue:
    """The address space: what is listed, and what each URI resolves to.

    Wraps ``SkillIndex`` and ``LibraryFiles`` rather than replacing them --
    they still own what a skill is and who may see one. This adds the one thing
    a URI space needs on top: a total function from address to content, with the
    request scope applied on every single call.

    ``scope`` is threaded through every method for that reason. It is the slice
    a scoped client is restricted to, and a method that forgot it would hand
    that client somebody else's skills -- so there is no method here that can be
    called without deciding about it.
    """

    def __init__(
        self,
        index: SkillIndex,
        resources: LibraryFiles,
        revalidate: Callable[[Path], None] | None = None,
    ):
        self._index = index
        self._resources = resources
        self._revalidate = revalidate
        self._memo: dict[tuple[Scope, bool], list[Entry]] = {}

    # -- listing ------------------------------------------------------------

    def entries(self, scope: Scope = EVERYTHING, full: bool = False) -> list[Entry]:
        """The resource listing: indexes first, then optionally every skill.

        The default is the indexes alone -- a dozen rows for a catalogue of
        ninety skills. Listing every skill instead costs ~16k tokens and is paid
        on every ``resources/list`` by every client, which is the exact expense
        this server exists to avoid; ``full`` is for the clients that need it
        anyway (``fastmcp.utilities.skills`` finds skills only by scanning the
        listing for ``/SKILL.md``), not for agents.

        Memoised per ``(scope, full)``. A ``Catalogue`` is built per snapshot
        and thrown away with it, so the memo cannot outlive the data it
        summarises -- which is the only reason caching a listing is safe here
        at all. The list is shared with every other caller of the same scope:
        read it, never edit it.
        """
        key = (scope, full)
        memo = self._memo.get(key)
        if memo is not None:
            return memo
        entries = self._entries(scope, full)
        if not entries and scope:
            # An unknown scope yields nothing and cost nothing to find out.
            # Memoising it would let a client grow this dict one bogus header
            # at a time.
            return entries
        if len(self._memo) >= MEMO_LIMIT:
            # Tag combinations are the client's choice, so the set of real
            # scopes is not bounded by the catalogue. Dropping the lot is
            # cheaper than an LRU and a listing is quick to rebuild.
            self._memo.clear()
        self._memo[key] = entries
        return entries

    def _entries(self, scope: Scope, full: bool) -> list[Entry]:
        visible = self._index.visible(scope)
        if not visible:
            return []

        entries: list[Entry] = []
        for library in sorted({s.library for s in visible}):
            in_library = [s for s in visible if s.library == library]
            index_tags = tuple(sorted({library, "index"}))
            # A flat library's group is just the library again, so it names
            # nothing new.
            groups = sorted({s.group for s in in_library} - {library})
            summary = _count(len(in_library))
            if groups:
                summary += f" in {len(groups)} folders: {', '.join(groups)}"
            entries.append(
                Entry(
                    f"{SCHEME}{library}",
                    library,
                    f"The {library} library — {summary}.",
                    "text/markdown",
                    index_tags,
                )
            )
            entries += [
                Entry(
                    f"{SCHEME}{group}",
                    group,
                    f"{_count(sum(1 for s in in_library if s.group == group))}"
                    f" in the {library} library.",
                    "text/markdown",
                    index_tags,
                )
                for group in groups
            ]
            if self._library_files(library, scope):
                entries.append(
                    Entry(
                        f"{SCHEME}{library}/{LIBRARY_FILES}",
                        f"{library}/{LIBRARY_FILES}",
                        f"Files the {library} library ships outside any skill,"
                        " which its skills reference.",
                        "text/markdown",
                        index_tags,
                    )
                )

        if full:
            entries += [
                Entry(
                    uri_for(skill, MAIN_FILE),
                    f"{skill.qualified}/{MAIN_FILE}",
                    skill.description,
                    "text/markdown",
                    tuple(sorted(skill.tags)),
                )
                for skill in sorted(visible, key=lambda s: s.qualified)
            ]
        return entries

    # -- what a scope may see -----------------------------------------------

    def _admits(self, library: str, scope: Scope) -> bool:
        """Whether a caller restricted to ``scope`` may see ``library`` at all.

        Asked of the skills, because they are the only thing that knows: a
        library is in scope when some skill of it is. ``LibraryFiles`` scopes
        its own roots by tag and cannot answer this -- it knows which
        libraries exist, not which this caller was given.
        """
        return not scope or any(
            s.library == library for s in self._index.visible(scope)
        )

    def _library_files(self, library: str, scope: Scope) -> list[str]:
        """The library-level files this caller may see, empty when none are.

        Both halves of the library-files rule in one place: the listing offers
        a ``_files`` row exactly when reading one would return a body, and a
        library out of scope entirely has neither.
        """
        if not self._admits(library, scope):
            return []
        return self._resources.files(library, scope.tags)

    # -- reading ------------------------------------------------------------

    def read(self, uri: str, scope: Scope = EVERYTHING) -> str | None:
        """The content at ``uri``, or None when there is none to give.

        None covers absent, malformed and out-of-scope alike, and that conflation
        is deliberate: knowing a skill's exact name must not be enough to confirm
        it exists in a library the caller was not given.
        """
        parsed = parse(uri)
        if parsed is None:
            return None
        head, path = parsed
        if not path:
            return self._index_body(head, scope)
        return self._content(head, path, scope)

    def _index_body(self, selector: str, scope: Scope) -> str | None:
        """One index: every skill under a library or group, addressed by URI.

        Lines are ``<uri>: <description>`` rather than ``<name>: ...`` so that
        reading an index teaches the grammar for the next call. A name would
        have to be translated back into an address anyway, and across
        libraries it is not even unique.
        """
        selected = self._index.select(scope, selector)
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

    def _content(self, library: str, path: str, scope: Scope) -> str | None:
        """Anything with two or more segments: a skill's file, or a library's.

        Resolution order is skill first, library file second. The two spaces
        cannot overlap -- a directory holding a SKILL.md is a skill and is
        therefore excluded from ``LibraryFiles`` -- so the order settles the
        shape of the URI rather than arbitrating a genuine ambiguity.
        """
        head, _, rest = path.partition("/")
        skill = self._index.get(f"{library}/{head}", scope)
        if skill is not None:
            return self._skill_file(skill, rest or MAIN_FILE)
        if path == LIBRARY_FILES:
            return self._library_files_body(library, scope)
        # Not a skill, so it is library-level material. LibraryFiles applies
        # its own scoping by tag, but library visibility is this class's to
        # decide.
        if not self._admits(library, scope):
            return None
        return self._resources.read(library, path, scope.tags)

    def _skill_file(self, skill: Skill, file: str) -> str | None:
        if file == MANIFEST:
            # Every file first: a size and a hash are claims about the bytes,
            # and on a live source the ones on disk may be an edit behind the
            # body the very next read would serve. The TTL covers that read.
            for path in _manifest_files(skill):
                if self._revalidate is not None:
                    self._revalidate(path)
            return _manifest_json(skill)
        # The manifest leaves hidden files out, so a read does too: a `.env`
        # beside a SKILL.md is not served to whoever guesses its name.
        if hidden(Path(file)):
            return None
        # Resolve before comparing, which is what blocks ../ and a symlink
        # pointing out of the skill directory.
        target = (skill.path / file).resolve()
        if not target.is_relative_to(skill.path.resolve()) or not target.is_file():
            return None
        # A live source's file may have moved since it was copied, and this
        # is where that is noticed -- a read is the only thing that asks.
        if self._revalidate is not None:
            self._revalidate(target)
        return target.read_text(encoding="utf-8", errors="replace")

    def _library_files_body(self, library: str, scope: Scope) -> str | None:
        files = self._library_files(library, scope)
        if not files:
            return None
        lines = [f"{SCHEME}{library}/{f}" for f in files]
        return (
            f"# {library} — {_count(len(files), 'library-level file')}\n\n"
            "These sit outside every skill in this library, and its skills"
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
