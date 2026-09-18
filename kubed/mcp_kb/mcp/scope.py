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
        categories: str | Iterable[str] = (),
        tags: str | Iterable[str] = (),
    ) -> Scope:
        """Build one from request text: each item may carry commas."""
        return cls(
            library=library.strip(),
            categories=frozenset(c.strip() for c in _values(categories) if c.strip()),
            tags=parse_groups(_values(tags)),
        )


def _values(items: str | Iterable[str]) -> tuple[str, ...]:
    """The repeated form, accepting one bare string as one value.

    A request states a selector as a list because a parameter and a header may
    both be repeated, but a caller in Python writes ``tags="runbooks,oncall"``
    sooner or later -- and a string *is* an iterable, so without this it is
    iterated a character at a time into the scope ``{{r}, {u}, {n}, ...}``: a
    decision about who sees what, made silently, with no error anywhere. One
    string is one value, and the comma rule then applies to it as to any other.
    """
    return (items,) if isinstance(items, str) else tuple(items)


# The unscoped default. Frozen, so one shared instance is safe as a default.
EVERYTHING = Scope()
