"""The address a plugin is declared at, and its grammar.

``<scheme>://<path>[//<subdir>][?ref=<ref>]``, where the scheme is a source's
name (or ``file``), the path is what that source is asked for and the subdir is
where the plugin sits inside it. The ``//`` and the ``?ref=`` are hashicorp
go-getter's, which is what kustomize and Terraform read, so
``github://owner/repo//sub?ref=<sha>`` means there what it means here.

The split matters because ``path`` and ``ref`` together are the *fetch* -- one
clone, one export -- while the subdir only says where to look inside it. Two
plugins in one repository share everything but their subdir, and ``key`` is
what makes that shared identity explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

SCHEME = re.compile(r"([a-z0-9][a-z0-9+.-]*)://(.*)", re.DOTALL)

GRAMMAR = "<source>://<path>[//<subdir>][?ref=<ref>]"


@dataclass(frozen=True)
class Address:
    scheme: str
    path: str
    subdir: str = ""
    ref: str | None = None

    def __str__(self) -> str:
        text = f"{self.scheme}://{self.path}"
        if self.subdir:
            text += f"//{self.subdir}"
        if self.ref:
            text += f"?ref={self.ref}"
        return text

    @property
    def key(self) -> str:
        """What is fetched, without where to look inside it: the fetch identity."""
        ref = f"?ref={self.ref}" if self.ref else ""
        return f"{self.scheme}://{self.path}{ref}"


def parse_address(text: str) -> Address:
    """One address, or a ValueError saying what is wrong with it."""
    match = SCHEME.fullmatch(text.strip())
    if not match:
        raise ValueError(f"{text!r} is not a plugin address: expected {GRAMMAR}")
    scheme, remainder = match.group(1), match.group(2)

    remainder, _, query = remainder.partition("?")
    ref = _ref(query, text)

    # The *first* "//" is the split: a subdir may contain slashes, a path may
    # not contain "//" without meaning one.
    path, _, subdir = remainder.partition("//")
    path, subdir = path.rstrip("/"), subdir.strip("/")

    if not path:
        raise ValueError(f"address {text!r} has no path: expected {GRAMMAR}")
    for part in (path, subdir):
        if ".." in PurePosixPath(part).parts:
            raise ValueError(f"address {text!r} must not contain '..'")
    if scheme == "file" and not path.startswith("/"):
        raise ValueError(f"a file:// address is absolute: file:///path, not {text!r}")
    if scheme != "file" and path.startswith("/"):
        msg = (
            f"address {text!r} is a path under {scheme}://, "
            "so it takes no leading '/'"
        )
        raise ValueError(msg)

    return Address(scheme=scheme, path=path, subdir=subdir, ref=ref)


def _ref(query: str, text: str) -> str | None:
    """The ``?ref=`` value, and a refusal for any other query key.

    A query string that quietly ignores what it does not know is how a typo'd
    ``?rev=`` serves the default branch and nobody finds out.
    """
    if not query:
        return None
    ref: str | None = None
    for item in query.split("&"):
        key, _, value = item.partition("=")
        if key != "ref":
            msg = (
                f"address {text!r}: the only query an address takes is ?ref=, "
                f"not {key!r}"
            )
            raise ValueError(msg)
        if not value:
            raise ValueError(f"address {text!r}: ?ref= needs a value")
        ref = value
    return ref
