"""``export.py`` on its own: the rules every backend's copy into the cache keeps.

The backends' own suites prove that a git tree and a WebDAV folder come out
right. What is proved here is the part that is neither of those — that
publishing an export is one rename onto a name nothing holds, that a delete
leaves the version-named space before it starts removing files, and that the
staging directory a live read writes through is swept but never yanked out from
under a fetch in flight.

Each of these is a *property*, and each of them survived the suite as a mutant
before this file existed.
"""

import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from mcp_school.sources.export import (
    DISCARD_PREFIX,
    GRACE_SECONDS,
    WORK_DIR,
    Exports,
    discard,
    workspace,
)

pytestmark = pytest.mark.unit

STAMP = ".stamp"
VERSION = re.compile(r"^[0-9a-f]{4}$")
V = "abcd"


def _exports(home):
    return Exports(name="n", home=home, stamp=STAMP, version=VERSION)


def _build(body):
    def build(tmp):
        (tmp / "skills").mkdir(parents=True)
        (tmp / "skills" / "SKILL.md").write_text(body)

    return build


# -- publishing ----------------------------------------------------------------


def test_an_export_is_published_at_the_version_it_holds(tmp_path):
    root = _exports(tmp_path).ensure(V, _build("first"))

    assert root == tmp_path / V
    assert (root / STAMP).read_text().split()[0] == V


def test_an_export_already_whole_is_returned_untouched(tmp_path):
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))

    again = exports.ensure(V, _build("rebuilt"))

    assert again == root
    assert (root / "skills" / "SKILL.md").read_text() == "first"


def test_a_rebuild_of_the_same_version_lands_beside_the_tree_being_served(tmp_path):
    """The one case a rebuild happens at all, and the one a reader is in.

    An export is named by its version, so an export that lost files is rebuilt
    at the name a snapshot is *currently serving out of*. Publishing over it
    means deleting it first, which is E3's C1 one level up. The repair goes to
    a free name instead and the old tree is retired the ordinary way.
    """
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    # A file the stamp does not account for: the export is no longer the
    # version it claims, which is what makes the next pass rebuild it.
    (root / "skills" / "extra.md").write_text("unaccounted for")

    repaired = exports.ensure(V, _build("second"))

    assert repaired != root, "a rebuild must not be published over a served tree"
    assert repaired == tmp_path / f"{V}.1"
    assert (repaired / STAMP).read_text().split()[0] == V
    assert (root / "skills" / "SKILL.md").read_text() == "first", "the old tree stands"


def test_a_repaired_export_is_the_one_the_next_pass_reuses(tmp_path):
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    (root / "skills" / "extra.md").write_text("unaccounted for")
    repaired = exports.ensure(V, _build("second"))

    assert exports.ensure(V, _build("third")) == repaired
    assert exports.current(V) == repaired


def test_a_second_repair_takes_the_next_free_name(tmp_path):
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    (root / "skills" / "extra.md").write_text("x")
    first = exports.ensure(V, _build("second"))
    (first / "skills" / "extra.md").write_text("x")

    assert exports.ensure(V, _build("third")) == tmp_path / f"{V}.2"


def test_a_reader_holding_an_export_is_never_interrupted_by_a_rebuild(tmp_path):
    """The measured failure, as a test: readers on the served tree, rebuilds
    of the same version underneath them, and not one read that fails."""
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    served = root / "skills" / "SKILL.md"
    errors: list[str] = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            try:
                if served.read_text() != "first":
                    errors.append("torn")
            except OSError as exc:
                errors.append(type(exc).__name__)

    readers = [threading.Thread(target=read, daemon=True) for _ in range(6)]
    for reader in readers:
        reader.start()
    try:
        current = root
        for round_ in range(5):
            # Dirty whatever is current, so every round is a real rebuild --
            # and never the file the readers are holding.
            (current / "skills" / f"extra{round_}.md").write_text("unaccounted for")
            current = exports.ensure(V, _build("first"))
            time.sleep(0.05)
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=5)

    assert errors == []


# -- deleting ------------------------------------------------------------------


def test_a_delete_leaves_the_version_named_space_before_it_removes_anything(
    tmp_path, monkeypatch
):
    """"A delete is a rename first": interrupt the rmtree and what is left must
    be a ``.discard-`` nothing will read, never a truncated tree at a version's
    own name that a later export would trust."""
    root = _exports(tmp_path).ensure(V, _build("first"))
    removed: list[Path] = []
    real = shutil.rmtree

    def watched(path, *args, **kwargs):
        removed.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", watched)

    discard(root)

    assert not root.exists()
    assert removed, "discard removed nothing, so this test proved nothing"
    assert root not in removed, "the tree was deleted at its version's own name"
    assert tmp_path / (DISCARD_PREFIX + V) in removed


def test_a_discard_that_was_interrupted_is_finished_next_time(tmp_path):
    grave = tmp_path / (DISCARD_PREFIX + V)
    grave.mkdir(parents=True)
    (grave / "half.md").write_text("interrupted")

    discard(grave)

    assert not grave.exists()


# -- the staging directory -----------------------------------------------------


def test_the_workspace_is_beside_the_exports_and_not_inside_one(tmp_path):
    root = _exports(tmp_path).ensure(V, _build("first"))

    work = workspace(tmp_path)

    assert work == tmp_path / WORK_DIR
    assert not work.is_relative_to(root)


def test_a_file_left_in_the_workspace_does_not_make_an_export_incomplete(tmp_path):
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    (workspace(tmp_path) / ".tmp-killed").write_text("a fetch that was killed")

    assert exports.complete(root, V)
    assert exports.ensure(V, _build("rebuilt")) == root


def test_the_collector_sweeps_a_stale_workspace_file_but_not_a_fresh_one(tmp_path):
    exports = _exports(tmp_path)
    root = exports.ensure(V, _build("first"))
    work = workspace(tmp_path)
    stale = work / ".tmp-killed"
    stale.write_text("a fetch that was killed")
    old = time.time() - GRACE_SECONDS - 60
    os.utime(stale, (old, old))
    fresh = work / ".tmp-running"
    fresh.write_text("a fetch in flight")

    exports.collect(root)

    assert not stale.exists(), "a temp file the collector cannot reach is permanent"
    assert fresh.exists(), "a fetch in flight must keep the file it is writing"
    assert work.is_dir(), "the staging directory itself is never taken away"
