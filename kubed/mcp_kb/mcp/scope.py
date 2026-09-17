"""What slice of the catalogue a request may see.

Three selectors, and they narrow together: a ``library`` (the first segment of
every URI, matched exactly), ``categories`` and ``tags``. A library names a
library and nothing below it -- folders keep existing in URIs and in indexes,
they are simply not a selector, because what a client wants inside a library is
a subject rather than a path.

One comma rule throughout, in the config and in the query string alike: a comma
inside one value means *all of*, and separate values mean *any of*. So
``?tags=runbooks,oncall&tags=lgtm`` is (runbooks and oncall) or lgtm. Categories
are the exception that proves it -- a plugin has exactly one, so a list can only
mean any of them, and a comma in one is kept verbatim here so ``pins.py`` can
refuse it with a message rather than silently matching nothing.

Every listing and every read takes a ``Scope`` and has to decide about it; an
empty one admits everything, and is falsy so ``if scope:`` reads as "is this
request narrowed at all".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..plugins.select import parse_groups


@dataclass(frozen=True)
class Scope:
    library: str = ""
    categories: frozenset[str] = frozenset()
    tags: frozenset[frozenset[str]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.library or self.categories or self.tags)

    def admits(
        self, library: str, category: str | None, labels: Iterable[str]
    ) -> bool:
        """Whether something in ``library`` with that category and those labels
        is in scope."""
        if self.library and self.library != library:
            return False
        return self.admits_labels(category, labels)

    def admits_labels(self, category: str | None, labels: Iterable[str]) -> bool:
        """The category and tag halves alone, for what carries no library of its own."""
        if self.categories and category not in self.categories:
            return False
        if not self.tags:
            return True
        carried = frozenset(labels)
        return any(group <= carried for group in self.tags)

    @classmethod
    def parse(
        cls,
        library: str = "",
        categories: Iterable[str] = (),
        tags: Iterable[str] = (),
    ) -> Scope:
        """Build one from request text: each item may carry commas."""
        return cls(
            library=library.strip(),
            categories=frozenset(c.strip() for c in categories if c.strip()),
            tags=parse_groups(tags),
        )


# The unscoped default. Frozen, so one shared instance is safe as a default.
EVERYTHING = Scope()
