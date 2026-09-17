"""Selecting plugins by category and tag, for a library's query and a request's scope.

Across the two fields it is AND, as every faceted search does it: a category
and a set of tags narrow each other. Within ``categories`` it can only be OR --
a plugin has exactly one category, so "in design *and* engineering" would match
nothing. Within tags a comma means *all of* and separate items mean *any of*,
which is one comma rule for the YAML and the query string alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Selector
    from . import Plugin


def parse_groups(items: Iterable[str]) -> frozenset[frozenset[str]]:
    """``["a,b", "c"]`` -> ``{{a, b}, {c}}``: a comma is all of, an item is any of."""
    groups = set()
    for item in items:
        group = frozenset(tag.strip() for tag in item.split(",") if tag.strip())
        if group:
            groups.add(group)
    return frozenset(groups)


def matches(
    plugin: Plugin, categories: frozenset[str], groups: frozenset[frozenset[str]]
) -> bool:
    """Whether one plugin answers a category list and a set of tag groups."""
    return (not categories or plugin.category in categories) and (
        not groups or any(group <= plugin.labels for group in groups)
    )


def select(plugins: Iterable[Plugin], selector: Selector) -> list[Plugin]:
    """The plugins a selector admits, in the order they were given."""
    categories, groups = selector.category_set, selector.tag_groups
    return [plugin for plugin in plugins if matches(plugin, categories, groups)]
