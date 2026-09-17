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

The grammar follows the MCP Skills extension (SEP-2640), which is the whole
API::

    skill://<library>/<folder>/<name>/SKILL.md    a skill's instructions
    skill://<library>/<folder>/<name>/_manifest   what else that skill ships
    skill://<library>/<folder>/<name>/<file>      one of those files
    skill://<library>/_index.md                   the folders and skills in a library
    skill://<library>/<folder>/_index.md          the folders and skills in a folder
    skill://<library>/_files.md                   files outside every skill
    skill://<library>/<file>                      one of those

``<folder>`` is the skill's directory below its plugin's skill root, and may
be empty or several segments deep; the last segment before the file is always
the skill's name. A skill is found by the longest prefix of the path that
names one, so a nested skill wins over the skill whose directory holds it.

An index lists what is *directly* under it: each folder as its own
``_index.md``, with how many skills are below it, and each skill in no deeper
folder as its ``SKILL.md``. A library of fifty skills in seven folders is then
seven lines, and the reader chooses which one to open, rather than paying for
every skill's description to find the folder it wanted.

Every other address is a directory -- a library, a folder, a skill's root --
and serves nothing, as on any other skill server. ``hint`` says which file to
read instead, for the error a caller sees.

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
INDEX = "_index.md"
LIBRARY_FILES = "_files.md"
# Distinct scopes a catalogue remembers listings for before starting over.
MEMO_LIMIT = 64


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
    its library and its plugin's labels.
    """

    uri: str
    name: str
    description: str
    mime_type: str
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


def parse(uri: str) -> tuple[str, str] | None:
    """Split a ``skill://`` URI into its library and the path below it.

    Returns ``(library, "")`` for a bare library and ``(library, path)`` for
    anything under one, or None when the string is not a ``skill://`` URI at
    all. Percent-decoding is not done here: the segments this server mints are
    already path-safe, and decoding would be one more way for ``%2e%2e`` to
    become ``..``.

    The path's dot segments are resolved here, once, for every read, hint and
    media type: a skill links its sibling as ``../other/SKILL.md``, and a
    client that resolves that without normalising sends the ``..`` as written.
    Every check downstream -- scope, containment, hidden files -- then runs on
    the address the URI names rather than on its spelling.
    """
    if not uri.startswith(SCHEME):
        return None
    rest = uri[len(SCHEME) :].strip("/")
    if not rest:
        return None
    head, _, tail = rest.partition("/")
    return head, _remove_dot_segments(tail)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 ``remove_dot_segments`` on the path below the library.

    ``.`` goes, ``..`` takes the segment before it, and a ``..`` with nothing
    before it is dropped -- clamped at the root exactly as a normalising client
    clamps it. The library is the URI's authority, not a path segment, so no
    number of ``..`` can reach a different one.
    """
    kept: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if kept:
                kept.pop()
        elif segment != ".":
            kept.append(segment)
    return "/".join(kept).strip("/")


def uri_for(skill: Skill, file: str = MAIN_FILE) -> str:
    """The address of one file of a skill, its instructions by default."""
    return f"{SCHEME}{skill.address}/{file}"


def index_uri(library: str, folder: str = "") -> str:
    """The index of a library, or of one folder in it."""
    return f"{SCHEME}{'/'.join(p for p in (library, folder) if p)}/{INDEX}"


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
    return json.dumps({"skill": skill.address, "files": files}, indent=2)


def _manifest_files(skill: Skill) -> list[Path]:
    """The files a manifest describes: every regular one the harvest would keep."""
    return [
        p
        for p in skill.path.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not hidden(p.relative_to(skill.path))
    ]


def _in(skill: Skill, folder: str) -> bool:
    """Whether ``skill`` sits in ``folder`` or anywhere below it; ``""`` is all."""
    return not folder or skill.folder == folder or skill.folder.startswith(f"{folder}/")


def _under(skills: list[Skill], folder: str) -> int:
    return sum(1 for s in skills if _in(s, folder))


def _children(skills: list[Skill], folder: str) -> dict[str, int]:
    """The folders directly in ``folder``, each with how many skills are below it.

    One definition for the indexes and the listing both, so a folder the listing
    offers is always one an index names, and the other way round.
    """
    prefix = f"{folder}/" if folder else ""
    found: dict[str, int] = {}
    for skill in skills:
        if skill.folder != folder and _in(skill, folder):
            child = prefix + skill.folder[len(prefix) :].split("/")[0]
            found[child] = found.get(child, 0) + 1
    return dict(sorted(found.items()))


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
        libraries = {s.library for s in visible} | {
            lib for lib in self._resources.libraries if self._library_files(lib, scope)
        }

        entries: dict[str, Entry] = {}
        for library in sorted(libraries):
            in_library = [s for s in visible if s.library == library]
            index_tags = tuple(sorted({library, "index"}))
            rows: list[Entry] = []
            # The listing is the top of the same tree the indexes are: the
            # library's index and its top-level folders.
            children = _children(in_library, "")
            if in_library:
                summary = _count(len(in_library))
                if children:
                    summary += (
                        f" in {_count(len(children), 'folder')}:"
                        f" {', '.join(children)}"
                    )
                rows.append(
                    Entry(
                        index_uri(library),
                        f"{library}/{INDEX}",
                        f"The {library} library — {summary}.",
                        "text/markdown",
                        index_tags,
                    )
                )
            rows += [
                Entry(
                    index_uri(library, folder),
                    f"{library}/{folder}/{INDEX}",
                    f"{_count(_under(in_library, folder))}"
                    f" in the {folder} folder of the {library} library.",
                    "text/markdown",
                    index_tags,
                )
                for folder in children
            ]
            if self._library_files(library, scope):
                rows.append(
                    Entry(
                        f"{SCHEME}{library}/{LIBRARY_FILES}",
                        f"{library}/{LIBRARY_FILES}",
                        f"Files the {library} library ships outside any skill,"
                        " which its skills reference.",
                        "text/markdown",
                        index_tags,
                    )
                )
            for row in rows:
                entries.setdefault(row.uri, row)

        if full:
            for skill in sorted(visible, key=lambda s: s.address):
                row = Entry(
                    uri_for(skill),
                    f"{skill.address}/{MAIN_FILE}",
                    skill.description,
                    "text/markdown",
                    tuple(sorted({skill.library, *skill.tags})),
                )
                entries.setdefault(row.uri, row)
        return list(entries.values())

    # -- what a scope may see -----------------------------------------------

    def _library_files(self, library: str, scope: Scope) -> list[str]:
        """The library-level files this caller may see, empty when none are.

        Both halves of the library-files rule in one place: the listing offers
        a ``_files.md`` row exactly when reading one would return a body, and a
        library out of scope entirely has neither. ``LibraryFiles`` checks each
        root's plugin against the scope's categories and tags; whether the
        library itself is in scope is decided here.
        """
        if scope.library and scope.library != library:
            return []
        return self._resources.files(library, scope)

    def _in_library(self, library: str, scope: Scope) -> list[Skill]:
        return [s for s in self._index.visible(scope) if s.library == library]

    def _skill_at(
        self, library: str, path: str, scope: Scope
    ) -> tuple[Skill, str] | None:
        """The skill whose address is the longest prefix of ``path``, and the rest.

        The rest is empty when ``path`` is the skill's own root.
        """
        segments = path.split("/")
        for cut in range(len(segments), 0, -1):
            address = "/".join([library, *segments[:cut]])
            skill = self._index.get(address, scope)
            if skill is not None:
                return skill, "/".join(segments[cut:])
        return None

    # -- reading ------------------------------------------------------------

    def read(self, uri: str, scope: Scope = EVERYTHING) -> str | None:
        """The content at ``uri``, or None when there is none to give.

        None covers absent, malformed, out-of-scope and directory addresses
        alike, and that conflation is deliberate: knowing a skill's exact
        address must not be enough to confirm it exists in a library the caller
        was not given.

        A skill is tried first, then the indexes, then the library's own files.
        The order never arbitrates between two things at one address, because
        the snapshot serves no such pair: a library file inside a skill of its
        own plugin, or named like an index, is skipped, and one inside another
        plugin's skill fails that plugin.
        """
        parsed = parse(uri)
        if parsed is None:
            return None
        library, path = parsed
        if not path:
            return None
        found = self._skill_at(library, path, scope)
        if found is not None and found[1]:
            body = self._skill_file(*found)
            if body is not None:
                return body
        if path == INDEX:
            return self._index_body(library, None, scope)
        if path == LIBRARY_FILES:
            return self._library_files_body(library, scope)
        folder, _, last = path.rpartition("/")
        if last == INDEX and folder:
            body = self._index_body(library, folder, scope)
            if body is not None:
                return body
        if scope.library and scope.library != library:
            return None
        return self._resources.read(library, path, scope)

    def hint(self, uri: str, scope: Scope = EVERYTHING) -> str | None:
        """What to read instead of an address that serves nothing, or None.

        Two kinds of miss get one. A directory -- a library, a folder, a skill's
        root -- names the file to read in its place. And a path under a skill
        that is really one of the library's own files: skills that factor
        material up out of themselves cite it from the repository root
        (``shared/tokens.md``), which resolved against the skill's directory
        is an address with nothing at it.

        Answered from what ``scope`` can see, so a hint never confirms an
        address the caller could not list.
        """
        parsed = parse(uri)
        if parsed is None:
            return None
        library, path = parsed
        skills = self._in_library(library, scope)
        files = self._library_files(library, scope)
        files_hint = f"read {SCHEME}{library}/{LIBRARY_FILES} for the files in it."
        if not path:
            if skills:
                return (
                    "It is a library, not a file:"
                    f" read {index_uri(library)} for its skills."
                )
            if files:
                return f"It is a library, not a file: {files_hint}"
            return None
        found = self._skill_at(library, path, scope)
        if found is not None and not found[1]:
            skill = found[0]
            return (
                "It is a skill's directory, not a file:"
                f" read {uri_for(skill)} for its instructions, or"
                f" {uri_for(skill, MANIFEST)} for the files it ships."
            )
        if found is not None:
            skill, rest = found
            if rest in files:
                return (
                    f"The {skill.name} skill ships no {rest}, but its library"
                    f" does: read {SCHEME}{library}/{rest}."
                )
        folder = path.strip("/")
        if any(s.folder == folder or s.folder.startswith(f"{folder}/") for s in skills):
            return (
                "It is a folder, not a file:"
                f" read {index_uri(library, folder)} for what is in it."
            )
        if any(f.startswith(f"{folder}/") for f in files):
            return f"It is a folder, not a file: {files_hint}"
        return None

    def _index_body(
        self, library: str, folder: str | None, scope: Scope
    ) -> str | None:
        """One index: what sits directly in a library, or in one folder of it.

        A folder below this one is one line, its own ``_index.md`` with a count
        of the skills anywhere under it; a skill in no deeper folder is its
        ``SKILL.md`` and description; and a library index ends with its
        ``_files.md`` when it has library files. So an index is as long as the
        level it describes is wide, not as long as the catalogue is deep.

        Lines are ``<uri>: <description>`` rather than ``<name>: ...`` so that
        reading an index teaches the grammar for the next call. A name would
        have to be translated back into an address anyway, and it is not even
        unique.
        """
        here = folder or ""
        under = [s for s in self._in_library(library, scope) if _in(s, here)]
        if not under:
            return None

        below = _children(under, here)
        lines = [
            f"{index_uri(library, child)}: {_count(count)}"
            for child, count in sorted(below.items())
        ]
        direct = sorted(
            (s for s in under if s.folder == here), key=lambda s: s.address
        )
        lines += [f"{uri_for(skill)}: {skill.description}" for skill in direct]
        files = [] if here else self._library_files(library, scope)
        if files:
            lines.append(
                f"{SCHEME}{library}/{LIBRARY_FILES}:"
                f" {_count(len(files), 'library-level file')}"
            )

        advice = []
        if below:
            advice.append(f"Read a folder's {INDEX} for what is in it.")
        if direct:
            advice.append(
                f"Read a skill's {MAIN_FILE} for its instructions, or {MANIFEST}"
                " in its place to see what else it ships, then read one of those"
                " paths under the same skill."
            )
        title = f"{library}/{here}" if here else library
        return (
            f"# {title} — {_count(len(under))}\n\n"
            + "\n".join(lines)
            + "\n\n"
            + " ".join(advice)
            + "\n"
        )

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
        library, path = parsed
        if path.endswith(f"/{MANIFEST}"):
            # Only a skill's manifest is JSON; a library file may share the name.
            found = self._skill_at(library, path[: -len(MANIFEST) - 1], EVERYTHING)
            if found is not None and found[1] == "":
                return "application/json"
        return mime_for(path)
