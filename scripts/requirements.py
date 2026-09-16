#!/usr/bin/env python3
"""Print a requirement list from pyproject.toml, for the image build to install.

The Dockerfile installs third-party dependencies in a stage that sees
pyproject.toml and nothing else, so that layer's cache key is one file's digest
and editing the source does not throw the install away. To do that it needs the
requirements as something pip can read — and that list has to come OUT of
pyproject.toml rather than be restated in the Dockerfile, because a second copy
is a second thing to keep in step and the way it fails is an image built against
dependencies nobody declared.

It lives here rather than inline in a `RUN` because a script can be linted,
imported and tested, and a heredoc can only be read.

    python scripts/requirements.py runtime    # what the image runs
    python scripts/requirements.py build      # what builds the wheel
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(path: pathlib.Path) -> dict:
    """pyproject.toml, parsed."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


def runtime(pyproject: dict, extras: list[str]) -> list[str]:
    """What the package needs to RUN: its dependencies, plus any named extras.

    This project ships no runtime extra today — ``build`` and ``test`` are both
    tooling and have no business in the image. The flag stays because which
    extras belong in an image is the Dockerfile's decision to make and to
    explain, and this only answers what they contain.
    """
    project = pyproject["project"]
    available = project.get("optional-dependencies") or {}
    wanted = list(project.get("dependencies") or [])
    for extra in extras:
        if extra not in available:
            known = ", ".join(sorted(available)) or "none"
            raise SystemExit(
                f"pyproject.toml declares no [{extra}] extra. It has: {known}"
            )
        wanted += available[extra]
    return wanted


def build(pyproject: dict, _extras: list[str]) -> list[str]:
    """What BUILDS the wheel.

    ``[build-system].requires`` already names and pins the backend — it is the
    one list that has to be right for ``pip install .`` to work anywhere, so
    there is nothing to add here.
    """
    return list(pyproject["build-system"]["requires"])


KINDS = {"runtime": runtime, "build": build}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="NAME",
        help="an optional-dependencies group to include as well; repeatable",
    )
    parser.add_argument(
        "--pyproject", type=pathlib.Path, default=ROOT / "pyproject.toml"
    )
    args = parser.parse_args(argv)
    if args.kind == "build" and args.extra:
        parser.error("--extra names a runtime extra; the build backend has none")
    for requirement in KINDS[args.kind](read(args.pyproject), args.extra):
        print(requirement)
    return 0


if __name__ == "__main__":
    sys.exit(main())
