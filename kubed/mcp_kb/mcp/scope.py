"""What slice of the catalogue a request may see.

A ``library`` selector is a path: ``grafana`` is that whole library, and
``grafana/grafana-lgtm`` the skills under that folder of it. A folder name on
its own selects nothing -- folder names repeat across libraries, and the path
is the address. ``tags`` cut across libraries: anything carrying *any* of them.
Given both, the tags narrow within the library.

A skill is admitted by its library and folder. Prompts and library-level files
have no folder, so under a folder selector they come only from the sources that
contribute a skill under it; under a library selector, every one of the
library's counts.

Every listing and every read takes a ``Scope`` and has to decide about it; an
empty one admits everything, and is falsy so ``if scope:`` reads as "is this
request narrowed at all".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    library: str = ""
    tags: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.library or self.tags)

    @property
    def library_name(self) -> str:
        """The library a selector names, without its folder."""
        return self.library.partition("/")[0]

    @property
    def folder(self) -> str:
        """The folder path a selector names under its library, or ``""``."""
        return self.library.partition("/")[2]

    def admits(self, library: str, tags: Iterable[str], folder: str = "") -> bool:
        """Whether a skill in ``library``/``folder`` carrying ``tags`` is in scope."""
        if self.library and self.library_name != library:
            return False
        wanted = self.folder
        if wanted and folder != wanted and not folder.startswith(f"{wanted}/"):
            return False
        return self.admits_tags(tags)

    def admits_tags(self, tags: Iterable[str]) -> bool:
        return not self.tags or not self.tags.isdisjoint(tags)

    @classmethod
    def parse(cls, library: str = "", tags: str | Iterable[str] = "") -> Scope:
        """Build one from request text: ``tags`` as ``a,b`` or an iterable of those."""
        raw = [tags] if isinstance(tags, str) else list(tags)
        found = frozenset(
            tag.strip() for chunk in raw for tag in chunk.split(",") if tag.strip()
        )
        path = "/".join(part for part in library.strip().split("/") if part)
        return cls(library=path, tags=found)


# The unscoped default. Frozen, so one shared instance is safe as a default.
EVERYTHING = Scope()
