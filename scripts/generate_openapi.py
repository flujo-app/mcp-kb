#!/usr/bin/env python3
"""Regenerate openapi.yaml, the build artifact.

The spec is generated and NOT committed -- `.gitignore` holds it out, CI
writes it, lints it with redocly and uploads it. Nothing in a pull request
reviews this file; what is reviewed is the code that produces it, which
`tests/test_openapi.py` holds against `routes.py`'s registered paths. Run this
when you want to read the document, or when redocly failed in CI and you want
the same input it had.

    python scripts/generate_openapi.py
"""

from __future__ import annotations

import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from kubed.mcp_kb.spec import PLACEHOLDER_VERSION, build_spec

OUT = pathlib.Path(__file__).resolve().parent.parent / "openapi.yaml"


def main() -> None:
    spec = build_spec()
    # info.version is required, so it cannot simply be dropped -- but
    # stamping the real one would churn this file on every commit, since
    # setuptools_scm derives it from git. The artifact is version-agnostic;
    # the served copy at /openapi.yaml carries the true installed version.
    spec["info"]["version"] = PLACEHOLDER_VERSION
    OUT.write_text(yaml.safe_dump(spec, sort_keys=False, width=100))
    print(f"wrote {OUT} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
