"""The config file: which sources to serve, validated before anything is read.

The file says *what* to serve. Environment variables and flags say *how* to
run (transport, port, cache directory) and never overlap with it. A typo'd key
is an error rather than silently ignored, because a config that serves nothing
is the worst failure a config file has. The models are also the published
schema: ``config.schema.json`` is ``Config.model_json_schema()`` and a test
fails when the two drift.

A source is provenance -- where bytes come from. A library is subject -- what
they are about. A source joins a library (its own name by default), so several
sources can present as one grouping and every component carries both names as
tags.

The shape of a source follows PEP 610's direct-URL data structure: a URL, a
requested revision as its own field rather than packed into the string, and a
credential that is a *reference* (``{env: NAME}``) rather than a value, so the
file is safe to commit and to publish as a ConfigMap.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SecretStr,
    Tag,
    ValidationError,
    field_validator,
)

NAME = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"

# The userinfo slot of a URL, which is where a token gets smuggled in. A source
# URL is refused for carrying one -- but pydantic quotes the offending value
# back in its error, so the refusal has to be scrubbed before it is reported.
# Greedy up to the last '@' before the path, because a password may contain an
# unencoded one and half a password echoed is a password echoed.
USERINFO = re.compile(r"(?<=://)[^/\s'\"]+@")


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


class Include(Strict):
    """Globs per kind, relative to the source root. None means the conventions."""

    skills: list[str] | None = None
    prompts: list[str] | None = None
    instructions: list[str] | None = None
    agents: list[str] | None = None
    files: list[str] | None = None

    @field_validator(
        "skills", "prompts", "instructions", "agents", "files", mode="after"
    )
    @classmethod
    def _relative(cls, patterns: list[str] | None) -> list[str] | None:
        # harvest.py's glob() would otherwise hand an absolute pattern straight
        # to Path.glob(), which raises NotImplementedError rather than failing
        # the config -- and a ".." pattern is the same escape harvest.files()
        # already guards against, caught here instead so it never reaches glob.
        for p in patterns or []:
            if not p or p.startswith("/") or ".." in PurePosixPath(p).parts:
                msg = f"include pattern {p!r} must be a relative path without '..'"
                raise ValueError(msg)
        return patterns


class Library(Strict):
    """A named grouping several sources can join, carrying its own tags."""

    name: str = Field(pattern=NAME)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class SourceBase(Strict):
    """Fields every source scheme shares: its name, the library it joins, its tags."""

    name: str = Field(pattern=NAME)
    url: str
    library: str | None = Field(default=None, pattern=NAME)
    tags: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _no_embedded_credential(cls, url: str) -> str:
        # A URL is handed to a client and interpolated into the errors /health
        # publishes, and git saves it verbatim into the clone's config. Whatever
        # the backend, a credential belongs in `auth`, where it stays a
        # reference to an environment variable rather than a value on disk.
        parts = urlsplit(url)
        if parts.username or parts.password:
            msg = "a source URL must not embed a credential; use auth instead"
            raise ValueError(msg)
        return url

    @property
    def library_name(self) -> str:
        return self.library or self.name


class MirrorSource(SourceBase):
    """A source that becomes a local directory: has include globs and a refresh."""

    include: Include = Include()
    refresh: str | None = Field(default=None, pattern=r"^[1-9]\d*[smh]$")

    @property
    def refresh_seconds(self) -> int | None:
        if self.refresh is None:
            return None
        seconds_per_unit = {"s": 1, "m": 60, "h": 3600}
        return int(self.refresh[:-1]) * seconds_per_unit[self.refresh[-1]]


class FileSource(MirrorSource):
    """A directory on this machine, served in place."""

    @field_validator("url")
    @classmethod
    def _absolute(cls, url: str) -> str:
        # A netloc means the "//host" slot was filled -- file://relative/path
        # parses to netloc="relative", path="/path", which looks absolute by
        # path alone. Only file:///path (no host) is a local absolute path.
        parts = urlsplit(url)
        if parts.netloc or not parts.path.startswith("/"):
            raise ValueError("a file:// URL must be absolute: file:///path")
        return url

    @property
    def path(self) -> Path:
        return Path(urlsplit(self.url).path)


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


class GitSource(MirrorSource):
    """A git repository, cloned bare and shallow, exported at a ref."""

    ref: str | None = None  # PEP 610 requested_revision: branch, tag or commit
    subdirectory: str | None = None  # PEP 610: harvest under this path of the export
    # GitHub: username "x-access-token", password {env: GITHUB_TOKEN}
    auth: BasicAuth | None = None

    @field_validator("url")
    @classmethod
    def _well_formed(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme == "github":
            org = parts.netloc
            repo = parts.path.lstrip("/")
            if not org or not repo or "/" in repo:
                msg = f"a github:// URL must be github://org/repo, got {url!r}"
                raise ValueError(msg)
        return url

    @field_validator("subdirectory")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        # Same escape harvest.files() guards against, caught here instead so
        # it never reaches an export rooted one directory above the tree.
        if value is not None and (
            not value or value.startswith("/") or ".." in PurePosixPath(value).parts
        ):
            msg = f"subdirectory {value!r} must be a relative path without '..'"
            raise ValueError(msg)
        return value

    @property
    def clone_url(self) -> str:
        parts = urlsplit(self.url)
        if parts.scheme == "github":
            org = parts.netloc
            repo = parts.path.lstrip("/")
            if not repo.endswith(".git"):
                repo += ".git"
            return f"https://github.com/{org}/{repo}"
        # git+https://h/p.git -> https://h/p.git ; git+http -> http ; git+file:///p -> file:///p
        return self.url.removeprefix("git+")


class WebdavSource(MirrorSource):
    """A WebDAV folder -- Nextcloud above all -- copied into the cache.

    The URL names the folder itself, so ``base_url`` is the client's root and
    every path under it is relative: a Nextcloud share is
    ``webdav+https://cloud/remote.php/dav/files/<user>/<folder>``. ``auth`` is
    required, because WebDAV has no useful anonymous mode and an omitted
    credential is a typo rather than a choice -- a Nextcloud *app password*,
    never the account's own.

    ``cache`` is the dial. ``snapshot`` is every other mirrored source: the
    folder is copied at index time and read from disk thereafter, so a request
    touches no network at all. ``live`` keeps that copy and revalidates a file
    against the server by ETag as it is read, so an edit made in Nextcloud is
    served on the next read -- a file that did not exist at index time still
    needs a refresh, because a URI only exists for what was harvested.
    """

    auth: BasicAuth
    cache: Literal["snapshot", "live"] = "snapshot"

    @property
    def base_url(self) -> str:
        # webdav+https://h/p -> https://h/p ; webdav+http -> http
        return self.url.removeprefix("webdav+")


SCHEMES: dict[str, str] = {  # url scheme -> union tag
    "file": "file",
    "git+https": "git",
    "git+http": "git",
    "git+file": "git",
    "github": "git",
    "webdav+https": "webdav",
    "webdav+http": "webdav",
}


def _tag(value: object) -> str | None:
    url = value.get("url", "") if isinstance(value, dict) else getattr(value, "url", "")
    return SCHEMES.get(urlsplit(str(url)).scheme)


Source = Annotated[
    Annotated[FileSource, Tag("file")]
    | Annotated[GitSource, Tag("git")]
    | Annotated[WebdavSource, Tag("webdav")],
    Discriminator(_tag),
]


class Config(Strict):
    """The whole config file: the libraries declared and the sources that join them."""

    libraries: list[Library] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @field_validator("sources", mode="before")
    @classmethod
    def _known_scheme(cls, raw: object) -> object:
        for item in raw if isinstance(raw, list) else []:
            url = (
                item.get("url", "")
                if isinstance(item, dict)
                else getattr(item, "url", "")
            )
            scheme = urlsplit(str(url)).scheme
            if scheme not in SCHEMES:
                known = ", ".join(f"{s}://" for s in SCHEMES)
                raise ValueError(
                    f"unknown source scheme {scheme!r}; source schemes: {known}"
                )
        return raw

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, sources: list[Source]) -> list[Source]:
        _unique("source", [s.name for s in sources])
        return sources

    @field_validator("libraries")
    @classmethod
    def _unique_libraries(cls, libraries: list[Library]) -> list[Library]:
        _unique("library", [lib.name for lib in libraries])
        return libraries

    def library(self, name: str) -> Library:
        """The declared library, or an implicit one carrying only its name."""
        for lib in self.libraries:
            if lib.name == name:
                return lib
        return Library(name=name)

    @property
    def min_refresh_seconds(self) -> int | None:
        """The smallest refresh interval declared by any mirrored source, or None."""
        seconds = [
            s.refresh_seconds
            for s in self.sources
            if isinstance(s, MirrorSource) and s.refresh_seconds is not None
        ]
        return min(seconds) if seconds else None


def _unique(kind: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate {kind} name: {name}")
        seen.add(name)


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
