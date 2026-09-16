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
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

NAME = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"


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
    include: Include = Include()

    @property
    def library_name(self) -> str:
        return self.library or self.name


class FileSource(SourceBase):
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


SCHEMES: dict[str, type[SourceBase]] = {"file": FileSource}

# One member for now. When a second scheme lands this becomes
# Annotated[Annotated[FileSource, Tag("file")] | Annotated[GitSource, Tag("git")],
#           Discriminator(<scheme of url>)]
# -- a discriminator needs a union of at least two, so it arrives with the second.
Source = FileSource


class Config(Strict):
    """The whole config file: the libraries declared and the sources that join them."""

    libraries: list[Library] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @field_validator("sources", mode="before")
    @classmethod
    def _known_scheme(cls, raw: object) -> object:
        for item in raw if isinstance(raw, list) else []:
            url = item.get("url", "") if isinstance(item, dict) else ""
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
        raise ConfigError(f"{path}: {exc}") from exc


def schema() -> dict:
    return Config.model_json_schema()
