# Address-space Plan — skill URIs on the MCP Skills extension

> **For agentic workers:** executed with superpowers:subagent-driven-development
> on the branch `address-space`. One PR at the end.

**Spec (binding):** `saga/Chapter_1_The_Card_Catalogue.md` §C1.23 (the
decisions) and §C1.24 (the plan: global constraints and tasks 0–8). Read both
before a task. This file only groups §C1.24's tasks into dispatches and fixes
exact values; where it and the saga disagree, the saga wins.

**Toolchain:** `PYTHONPATH=/home/coder/.cache/mcp-school-pylib` (no venv);
`python3 -m pytest -q`, `python3 -m ruff check .`,
`python3 scripts/generate_wiki.py [--check]` from the repo root.

## Global Constraints

- §C1.24's global constraints, verbatim, plus these.
- Baseline at branch start (`d17006c`): **393 passed, 1 skipped**, ruff clean.
- Behaviour-changing tasks update URI expectations in existing tests; they never
  delete a behavioural test to pass. Refactor-only commits change no assertion.
- Every new test is proved non-vacuous: break the thing it guards, watch it fail,
  restore. Say so in the report.
- Every served URI is tested by reading it back through a real `KnowledgeBase`
  over an MCP client, not a helper.
- Nothing outside `main.py` and `EnvRef.resolve` reads `os.environ`. Credentials
  never reach a log, an error, a path on disk, the index, `/health` or a body.
- `catalogue/` imports no FastMCP.
- Docs are present tense; history belongs in the saga. Comments earn their lines.
- No `kubed/__init__.py`. Ruff clean at 88 columns.
- Commit trailer: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- No implementer dispatches subagents or pushes anything, including `wiki/`.

## Exact values

| Thing | Value |
|---|---|
| skill URI | `skill://<library>/<path>/<name>/SKILL.md` |
| `<path>` | skill dir relative to the source's conventional skill root (`harvest.SKILL_ROOTS`); empty when the skill sits directly in it |
| skill file | `skill://<library>/<path>/<name>/<file>` |
| manifest | `skill://<library>/<path>/<name>/_manifest` |
| library index | `skill://<library>/_index.md` |
| folder index | `skill://<library>/<folder path>/_index.md` |
| library-level files index | `skill://<library>/_files.md` |
| library-level file | `skill://<library>/<path from source root>` |
| directory addresses | not found; message names `_index.md` or `_manifest` |
| served vocabulary | "library", "folder" — never "pack", "group" in served text |
| deleted | `X-Skill-Pack` header alias |

## Dispatches

1. **Rename** — §C1.24 task 1. Pure rename, own commit(s), no behaviour change.
2. **Task 0 code** — §C1.24 task 0 except the pack-file scope item (moves to
   dispatch 3): `/reindex` fixed error body, prompt row + loader into
   `catalogue/` with a FastMCP-blocked import test, refresh loop sleep bounded by
   the shortest interval, git+file:// wording in README/CHANGELOG.
3. **Grammar** — §C1.24 tasks 2, 3, 4, 5 and task 0's pack-file scope item.
4. **Docs, wiki, example** — §C1.24 tasks 6 and 7, plus the deferred review
   findings listed under "Also in this pull request".
5. Final whole-branch review, one fix wave, scoped re-review, PR.
