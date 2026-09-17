"""What slice of the catalogue a request may see.

A ``library`` is one choice and gives that whole library -- or, named by one of
its groups, that group. ``tags`` cut across libraries: anything carrying *any*
of them. Given both, the tags narrow within the library.

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

    def admits(self, library: str, tags: Iterable[str], group: str = "") -> bool:
        """Whether ``library`` (and ``group``) carrying any of ``tags`` is in scope."""
        if self.library and self.library not in (library, group):
            return False
        return not self.tags or not self.tags.isdisjoint(tags)

    @classmethod
    def parse(cls, library: str = "", tags: str | Iterable[str] = "") -> Scope:
        """Build one from request text: ``tags`` as ``a,b`` or an iterable of those."""
        raw = [tags] if isinstance(tags, str) else list(tags)
        found = frozenset(
            tag.strip() for chunk in raw for tag in chunk.split(",") if tag.strip()
        )
        return cls(library=library.strip(), tags=found)


# The unscoped default. Frozen, so one shared instance is safe as a default.
EVERYTHING = Scope()
