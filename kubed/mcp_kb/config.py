"""The config file: sources, plugins and libraries, validated before anything is read.

The file says *what* to serve. Environment variables and flags say *how* to
run (transport, port, cache directory) and never overlap with it. A typo'd key
is an error rather than silently ignored, because a config that serves nothing
is the worst failure a config file has. The models are also the published
schema: ``config.schema.json`` is ``Config.model_json_schema()`` and a test
fails when the two drift.

Three lists, each answering one question:

- a **source** is a backend, named once: where bytes come from and how to log
  in. A plugin never carries a credential, so a token is declared in exactly
  one place however many plugins read through it.
- a **plugin** is a folder of skills and prompts -- a path under a source,
  written as ``<source>://<path>[//<subdir>][?ref=…]``, carrying Claude's
  marketplace-entry metadata (``category``, ``tags``, ``keywords``,
  ``version``) so a plugin declared here and one read out of a marketplace are
  the same kind of thing.
- a **library** is a saved query over plugins, and the first segment of every
  URI served: a marketplace, a list of plugin names, or a ``pluginSelector``.

The shape of a source follows PEP 610's direct-URL data structure: a URL and a
credential that is a *reference* (``{env: NAME}``) rather than a value, so the
file is safe to commit and to publish as a ConfigMap. The ref moves onto the
plugin's address, beside go-getter's ``//`` for a subdirectory, because the
identity of a fetch is the URL *and* the ref while the credential is the
source's.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:  # the one direction that would be a cycle at runtime
    from .plugins.address import Address

NAME = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"

BACKENDS: dict[str, str] = {  # source url scheme -> the backend that reads it
    "git+https": "git",
    "git+http": "git",
    "git+file": "git",
    "webdav+https": "webdav",
    "webdav+http": "webdav",
    "file": "file",
}

# Plugin address schemes that need no source entry: a local directory carries
# no credential and nothing to configure, so declaring one would be ceremony.
BUILTIN_SCHEMES = ("file",)

REFRESH = r"^[1-9]\d*[smh]$"

# The userinfo slot of a URL, which is where a token gets smuggled in. A source
# URL is refused for carrying one -- but pydantic quotes the offending value
# back in its error, so the refusal has to be scrubbed before it is reported.
# Greedy up to the last '@' before the path, because a password may contain an
# unencoded one and half a password echoed is a password echoed.
USERINFO = re.compile(r"(?<=://)[^/\s'\"]+@")


def scrub(text: str) -> str:
    """``text`` with any URL userinfo masked, for something about to be published.

    ``redact`` is this plus the config's own resolved secrets, which is what a
    log line or a traceback needs. This half is for what ``/health`` serves,
    where the hazard is a credential that arrived in *fetched* content -- a
    marketplace entry whose ``url`` carries ``user:token@`` -- rather than one
    of ours. The guard is deliberately not the only line of defence: a message
    is built from the host and not the URL where it can be.
    """
    return USERINFO.sub("***@", text)


def redact(text: str, secrets: list[str]) -> str:
    """``text`` with URL userinfo and every value in ``secrets`` masked."""
    text = scrub(text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


class ConfigError(ValueError):
    """The config file is unreadable or invalid."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvRef(Strict):
    """A secret named by the environment variable that holds it."""

    env: str

    def resolve(self) -> SecretStr:
        # The one read of os.environ outside main.py: a secret is resolved where
        # it is declared, so it never passes through a plain str on the way.
        if self.env not in os.environ:
            raise ConfigError(f"environment variable {self.env} is not set")
        return SecretStr(os.environ[self.env])


class BasicAuth(Strict):
    """Credentials for a remote that requires them: a git host, a WebDAV server.

    The username is usually a literal -- GitHub wants ``x-access-token`` -- so
    it takes one. But a service account's name arrives in the same secret as
    its password, and writing it out here as well is how the two drift apart
    the day the account is recreated, so it takes an ``{env:}`` reference too.
    """

    username: EnvRef | str
    password: EnvRef

    def user(self) -> str:
        """The username, resolved if it is an ``{env:}`` reference.

        Not a secret, and deliberately a plain ``str``: it is half of a Basic
        auth pair and every caller hands it straight to a client.
        """
        if isinstance(self.username, EnvRef):
            return self.username.resolve().get_secret_value()
        return self.username

    def pair(self) -> tuple[str, str]:
        """Both halves, resolved together, for a client that wants the pair.

        One call because either half may be an ``{env:}`` reference and so
        either may be unset: a backend turning that into a failure of its own
        source needs one guard around the resolution, not two.
        """
        return self.user(), self.password.resolve().get_secret_value()


class SourceConfig(Strict):
    """A named backend: where bytes come from and how to log in, declared once."""

    name: str = Field(pattern=NAME)
    url: str
    auth: BasicAuth | None = None
    cache: Literal["snapshot", "live"] = "snapshot"
    refresh: str | None = Field(default=None, pattern=REFRESH)

    @field_validator("name")
    @classmethod
    def _not_a_builtin_scheme(cls, name: str) -> str:
        # file:// resolves to a path, never to a source entry, so a source of
        # that name could never be addressed by a plugin.
        if name in BUILTIN_SCHEMES:
            msg = f"a source may not be named {name!r}: {name}:// is built in"
            raise ValueError(msg)
        return name

    @field_validator("url")
    @classmethod
    def _well_formed(cls, url: str) -> str:
        # A URL is handed to a client and interpolated into the errors /health
        # publishes, and git saves it verbatim into the clone's config. Whatever
        # the backend, a credential belongs in `auth`, where it stays a
        # reference to an environment variable rather than a value on disk.
        parts = urlsplit(url)
        if parts.username or parts.password:
            msg = "a source URL must not embed a credential; use auth instead"
            raise ValueError(msg)
        if parts.scheme not in BACKENDS:
            known = ", ".join(f"{scheme}://" for scheme in BACKENDS)
            msg = f"unknown source scheme {parts.scheme!r}; source schemes: {known}"
            raise ValueError(msg)
        # A netloc means the "//host" slot was filled -- file://relative/path
        # parses to netloc="relative", path="/path", which looks absolute by
        # path alone. Only file:///path (no host) is a local absolute path.
        if parts.scheme == "file" and (parts.netloc or not parts.path.startswith("/")):
            raise ValueError("a file:// URL must be absolute: file:///path")
        return url

    @model_validator(mode="after")
    def _webdav_is_authenticated(self) -> SourceConfig:
        # WebDAV has no useful anonymous mode -- Nextcloud's is a share link,
        # which is a different URL -- so an omitted credential is a typo rather
        # than a choice, and one that only shows up as a 401 at cold start.
        if self.backend == "webdav" and self.auth is None:
            msg = "a webdav source needs auth: there is no anonymous WebDAV here"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _live_is_webdavs_alone(self) -> SourceConfig:
        # Revalidation is per-file and ETag-driven; on git or a local directory
        # it would be a dial that silently does nothing.
        if self.cache == "live" and self.backend != "webdav":
            msg = f"cache: live needs a webdav source, not {self.backend}"
            raise ValueError(msg)
        return self

    @property
    def backend(self) -> str:
        """Which backend reads this source: ``git``, ``webdav`` or ``file``."""
        return BACKENDS[urlsplit(self.url).scheme]

    @property
    def refresh_seconds(self) -> int | None:
        if self.refresh is None:
            return None
        seconds_per_unit = {"s": 1, "m": 60, "h": 3600}
        return int(self.refresh[:-1]) * seconds_per_unit[self.refresh[-1]]


class Selector(Strict):
    """A query over plugins: categories narrow first, then tag groups.

    Across the two fields it is AND, as in every faceted search. Within
    ``categories`` it can only be OR -- a plugin has exactly one category --
    and within ``tags`` a comma means *all of* while separate items mean *any
    of*, which is the same comma rule a request's ``?tags=`` carries.
    """

    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("categories")
    @classmethod
    def _one_category_each(cls, categories: list[str]) -> list[str]:
        for category in categories:
            if "," in category:
                msg = "a plugin has one category; list several to mean any of them"
                raise ValueError(msg)
        return categories

    @property
    def category_set(self) -> frozenset[str]:
        return frozenset(c.strip() for c in self.categories if c.strip())

    @property
    def tag_groups(self) -> frozenset[frozenset[str]]:
        return _groups(self.tags)


class PluginConfig(Strict):
    """A folder of skills and prompts: a path under a source, plus its metadata.

    The metadata fields are Claude's marketplace-entry fields, so a plugin
    declared here and one read out of a ``marketplace.json`` carry the same
    shape and a selector cannot tell them apart. The glob lists are this
    server's own: omitted, a kind falls to the plugin's manifest and then to
    the conventions.
    """

    name: str = Field(pattern=NAME)
    description: str = ""
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    version: str | None = None
    source: str
    skills: list[str] | None = None
    prompts: list[str] | None = None
    files: list[str] | None = None
    dialect: Literal["auto", "mcp-kb", "claude", "copilot"] = "auto"

    @field_validator("source")
    @classmethod
    def _an_address(cls, source: str) -> str:
        _address(source)  # its ValueError is the message
        return source

    @field_validator("skills", "prompts", "files")
    @classmethod
    def _relative(cls, patterns: list[str] | None) -> list[str] | None:
        # harvest.py's glob() would otherwise hand an absolute pattern straight
        # to Path.glob(), which raises NotImplementedError rather than failing
        # the config -- and a ".." pattern is the same escape harvest.files()
        # already guards against, caught here instead so it never reaches glob.
        for pattern in patterns or []:
            if (
                not pattern
                or pattern.startswith("/")
                or ".." in PurePosixPath(pattern).parts
            ):
                msg = f"pattern {pattern!r} must be a relative path without '..'"
                raise ValueError(msg)
        return patterns

    @property
    def address(self) -> Address:
        return _address(self.source)


class LibraryConfig(Strict):
    """A library: a marketplace, plugins by name, or a query -- and their union.

    A library carries no tags and no category of its own. Tagging is what a
    plugin does, so ``?tags=`` reaches a library only through what its plugins
    say, and the selector gets an unambiguous name of its own after
    Kubernetes' ``selector``.
    """

    name: str = Field(pattern=NAME)
    description: str = ""
    source: str | None = None
    plugins: list[str] = Field(default_factory=list)
    plugin_selector: Selector | None = Field(default=None, alias="pluginSelector")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("source")
    @classmethod
    def _an_address(cls, source: str | None) -> str | None:
        if source is not None:
            _address(source)
        return source

    @model_validator(mode="after")
    def _selects_something_and_only_one_way(self) -> LibraryConfig:
        if self.source and self.plugin_selector is not None:
            msg = (
                f"library {self.name}: a marketplace library takes its shape from "
                "the marketplace; use plugins: to add beside it"
            )
            raise ValueError(msg)
        if not (self.source or self.plugins or self.plugin_selector):
            raise ValueError(f"library {self.name} selects nothing")
        return self


class Config(Strict):
    """The whole config file: the sources, the plugins under them, the libraries."""

    sources: list[SourceConfig] = Field(default_factory=list)
    plugins: list[PluginConfig] = Field(default_factory=list)
    libraries: list[LibraryConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_are_unique(self) -> Config:
        _unique("source", [s.name for s in self.sources])
        _unique("plugin", [p.name for p in self.plugins])
        _unique("library", [lib.name for lib in self.libraries])
        return self

    @model_validator(mode="after")
    def _addresses_resolve(self) -> Config:
        """Every address names a declared source, and only git takes a ref.

        Checked here rather than at the first fetch because a typo'd scheme is
        a config error whose blast radius is a whole library, and the message
        can only list the declared sources from up here.
        """
        for plugin in self.plugins:
            self._check(f"plugin {plugin.name}", plugin.address)
        for library in self.libraries:
            if library.source:
                self._check(f"library {library.name}", _address(library.source))
        return self

    @model_validator(mode="after")
    def _named_plugins_exist(self) -> Config:
        # An "<entry>@<library>" plugin lives in a marketplace that has not been
        # read yet, so it is resolved at assembly time; a bare name is ours and
        # a typo in one is silence -- a library that quietly serves less.
        declared = {plugin.name for plugin in self.plugins}
        for library in self.libraries:
            for plugin in library.plugins:
                if "@" not in plugin and plugin not in declared:
                    msg = (
                        f"library {library.name}: "
                        f"no plugin named {plugin!r} is declared"
                    )
                    raise ValueError(msg)
        return self

    def _check(self, where: str, address: Address) -> None:
        if address.scheme in BUILTIN_SCHEMES:
            backend = BACKENDS[address.scheme]
        else:
            try:
                backend = self.source(address.scheme).backend
            except KeyError:
                names = ", ".join(s.name for s in self.sources) or "none"
                msg = (
                    f"{where}: no source named {address.scheme!r} is declared; "
                    f"declared sources: {names}"
                )
                raise ValueError(msg) from None
        if address.ref and backend != "git":
            msg = f"{where}: ?ref= needs a git source; {address.scheme}:// is {backend}"
            raise ValueError(msg)

    def source(self, name: str) -> SourceConfig:
        """The declared source of that name.

        Raises ``KeyError`` for an undeclared one: validation has already
        proved every address names one, so a caller never has to ask twice.
        """
        for source in self.sources:
            if source.name == name:
                return source
        raise KeyError(name)

    def library(self, name: str) -> LibraryConfig | None:
        """The declared library of that name, or None -- a library is declared
        or it does not exist."""
        for library in self.libraries:
            if library.name == name:
                return library
        return None

    def secrets(self) -> list[str]:
        """Every credential value the config resolves, for redacting logs.

        A reference whose variable is unset contributes nothing: there is no
        value it could leak.
        """
        found: list[str] = []
        for source in self.sources:
            if source.auth is None:
                continue
            for ref in (source.auth.username, source.auth.password):
                if not isinstance(ref, EnvRef):
                    continue
                try:
                    found.append(ref.resolve().get_secret_value())
                except ConfigError:
                    continue
        return [value for value in found if value]

    @property
    def min_refresh_seconds(self) -> int | None:
        """The smallest refresh interval any source declares, or None."""
        seconds = [
            s.refresh_seconds for s in self.sources if s.refresh_seconds is not None
        ]
        return min(seconds) if seconds else None


def _unique(kind: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate {kind} name: {name}")
        seen.add(name)


def _address(text: str) -> Address:
    """``plugins.address.parse_address``, imported at call time.

    ``plugins/`` is built on these models, so the dependency runs one way and
    a module-level import here would close the loop.
    """
    from .plugins.address import parse_address

    return parse_address(text)


def _groups(items: list[str]) -> frozenset[frozenset[str]]:
    """``plugins.select.parse_groups``, imported at call time -- see ``_address``."""
    from .plugins.select import parse_groups

    return parse_groups(items)


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(raw)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not YAML: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path}: {USERINFO.sub('***@', str(exc))}") from exc


def schema() -> dict:
    return Config.model_json_schema()
