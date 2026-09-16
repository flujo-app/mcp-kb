"""What ships in the image, what rebuilds it, and what the build must not redo.

Two questions asked from opposite ends, and they drift apart quietly.

*What rebuilds the image* is `image.yml`'s `paths:` filter; *what is in the
image* is the Dockerfile's COPY lines and the wheel — and what is in the wheel
is the `packages` list, which setuptools takes literally and which a test that
only reads it back cannot check. A file that the Dockerfile
copies but the filter does not watch changes the image and builds nothing —
`:latest` then silently keeps serving the old build. `scripts/requirements.py`
is exactly that shape: it is not in the wheel, but it decides which
dependencies the venv gets.

The rest pins the build's *shape*, because the venv hand-off is an optimisation
and an optimisation with no test is a thing someone helpfully undoes. The
expensive mistake it replaced — the runner reinstalling every dependency out of
the wheel the builder had just built — reads as perfectly ordinary Dockerfile.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
IMAGE_WORKFLOW = REPO / ".github" / "workflows" / "image.yml"
TEST_WORKFLOW = REPO / ".github" / "workflows" / "test.yml"
DOCKERFILE = REPO / "Dockerfile"

sys.path.insert(0, str(REPO / "scripts"))
import requirements  # noqa: E402 - needs the path above

# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------


def dockerfile_stages() -> dict[str, list[str]]:
    """Each ``FROM ... AS <name>`` stage, as its list of instruction lines.

    Parsed rather than split on the word FROM, which also appears in the prose
    at the top of the file — a split finds the comment and silently returns an
    empty stage, and a test over an empty stage passes by testing nothing.
    """
    stages: dict[str, list[str]] = {}
    current = None
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith("FROM ") and " AS " in line:
            current = line.rsplit(" AS ", 1)[1]
            stages[current] = []
        elif current is not None and line and not line.startswith("#"):
            stages[current].append(line)
    return stages


def copied_paths() -> set[str]:
    """Repo-relative paths the Dockerfile COPYs out of the build context.

    Only real source paths: `COPY --from=` moves things between stages and
    reaches no file in the repo, and `COPY . .` is the whole context.
    """
    found: set[str] = set()
    for lines in dockerfile_stages().values():
        for line in lines:
            if not line.startswith("COPY ") or "--from=" in line:
                continue
            source = line.split()[1]
            if source != ".":
                found.add(source)
    return found


def image_trigger_paths() -> list[set[str]]:
    """The ``paths:`` filter of each trigger in image.yml that has one."""
    # `on` is parsed as the boolean True by YAML 1.1, which is what PyYAML
    # implements — so it cannot be looked up by the string "on".
    spec = yaml.safe_load(IMAGE_WORKFLOW.read_text())
    triggers = spec.get("on", spec.get(True))
    return [
        set(t["paths"])
        for t in triggers.values()
        if isinstance(t, dict) and "paths" in t
    ]


def covered(path: str, patterns: set[str]) -> bool:
    """Whether a `paths:` filter would fire for ``path``.

    Only the two forms this repo uses: an exact path, and a `dir/**` prefix,
    which also covers a COPY of the directory itself.
    """
    for pattern in patterns:
        if pattern == path:
            return True
        if pattern.endswith("/**") and (
            path == pattern[:-3] or path.startswith(pattern[:-2])
        ):
            return True
    return False


def interpreters() -> dict[str, set[str]]:
    """Every statement this repo makes about which interpreters it supports."""
    data = tomllib.loads(PYPROJECT.read_text())
    matrix = yaml.safe_load(TEST_WORKFLOW.read_text())["jobs"]["test"]["strategy"][
        "matrix"
    ]["python-version"]
    return {
        "requires-python": {data["project"]["requires-python"].lstrip(">=")},
        "classifiers": {
            c.rsplit(" :: ", 1)[1]
            for c in data["project"]["classifiers"]
            if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
        },
        "test.yml": set(re.findall(r"3\.\d+", matrix)),
    }


# --------------------------------------------------------------------------
# Which interpreters this is
# --------------------------------------------------------------------------


def test_the_supported_interpreters_are_the_same_everywhere():
    """`requires-python`, the classifiers and the CI matrix are three
    statements of one fact, made in three files that drift silently.

    A pull request runs 3.14 alone, so an interpreter named in the metadata but
    missing from the matrix is a claim nothing checks until the merge — or
    until the release, since publish.yml gates on this workflow.
    """
    said = interpreters()
    assert said["classifiers"] == said["test.yml"]
    assert min(said["classifiers"], key=_version) == min(said["requires-python"])


def test_the_pygit2_floor_has_the_api_the_git_source_calls():
    """`Remote.list_heads` first exists in pygit2 1.19.

    1.15 to 1.18 have `ls_remotes` and nothing else, so a lower floor resolves —
    on any interpreter old enough for pip to pick one — to a pygit2 that sends
    `School.__init__` out with an uncaught `AttributeError`. 1.19 is also the
    first release to require Python 3.11, which is why the floor here and the
    floor in `requires-python` move together.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    pin = next(d for d in data["project"]["dependencies"] if d.startswith("pygit2"))
    assert _version(pin.removeprefix("pygit2>=")) >= (1, 19)
    assert _version(data["project"]["requires-python"].lstrip(">=")) >= (3, 11)


def test_the_webdav4_floor_is_the_one_the_suite_was_run_against():
    """0.11 is a *tested* floor, not a known minimum.

    Nothing here can tell when `ls(detail=True)` started passing the ETag
    through, and the ETag is the whole of how a WebDAV source decides whether a
    file moved. 0.11.0 is both the newest release and the version the suite
    runs against, so CI resolving to newest resolves to what was tested —
    lowering the floor means proving the lower one works, not guessing.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    pin = next(d for d in data["project"]["dependencies"] if d.startswith("webdav4"))
    assert _version(pin.rsplit(">=", 1)[1]) >= (0, 11)
    assert "[fsspec]" in pin, "WebdavFileSystem comes from the extra, not the core"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


# --------------------------------------------------------------------------
# What is in the wheel
# --------------------------------------------------------------------------


def test_every_package_in_the_tree_is_declared():
    """`packages` is a literal list, and a subpackage is not implied by it.

    `mcp_school.sources` ships only because setuptools_scm's file finder sweeps
    the checkout, which a build from an exported tarball has no way to do — so
    the list has to name every package itself, and nothing but this notices
    when a new one is added.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    declared = set(data["tool"]["setuptools"]["packages"])
    found = {
        str(init.parent.relative_to(REPO)).replace("/", ".")
        for init in REPO.glob("mcp_school/**/__init__.py")
    }
    assert found, "no package found in the tree — this test proves nothing"
    assert found == declared


@pytest.mark.integration
def test_the_built_wheel_imports_with_no_checkout_to_sweep(tmp_path):
    """The end of the argument: build it, install it, import it.

    Built from a copy with no `.git`, because that is the case the file finder
    cannot rescue — and installed into a directory of its own, so what imports
    is the wheel's `mcp_school` and never this repository's.
    """
    source = tmp_path / "src"
    shutil.copytree(
        REPO,
        source,
        ignore=shutil.ignore_patterns(
            ".git", "dist", "build", "*.egg-info", "__pycache__", ".*_cache"
        ),
    )
    env = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0"}
    _run([sys.executable, "-m", "build", "--wheel", "--no-isolation"], source, env)
    wheel = next((source / "dist").glob("*.whl"))

    installed = tmp_path / "installed"
    _run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "-t", str(installed), str(wheel)],
        tmp_path,
        env,
    )

    env["PYTHONPATH"] = os.pathsep.join([str(installed), env.get("PYTHONPATH", "")])
    _run([sys.executable, "-c", "from mcp_school.server import School"], tmp_path, env)


def _run(command, cwd, env):
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    assert done.returncode == 0, f"{command[2:]} failed:\n{done.stdout}\n{done.stderr}"
    return done


# --------------------------------------------------------------------------
# What rebuilds the image
# --------------------------------------------------------------------------


def test_the_workflow_has_the_path_filters_this_is_about():
    """If the filters ever go away, the two tests below pass vacuously.

    There is exactly one, on `push: main`. `pull_request` has no filter because
    it has no trigger — a multi-arch build is most of a PR's wait for a signal
    that rarely differs from the merge build, which is the one anyone pulls.
    """
    filters = image_trigger_paths()
    assert len(filters) == 1, "expected exactly one paths-filtered trigger"
    assert filters[0], "image.yml has a paths filter with nothing in it"


def test_the_packaged_source_rebuilds_the_image():
    """Everything in the wheel is in the image, so it has to rebuild it."""
    data = tomllib.loads(PYPROJECT.read_text())
    packages = data["tool"]["setuptools"]["packages"]
    assert packages, "pyproject declares no packages — this test proves nothing"
    for filters in image_trigger_paths():
        for package in packages:
            directory = package.split(".")[0] + "/"
            assert covered(directory, filters), (
                f"{package} ships in the image, but image.yml will not rebuild"
                f" when it changes. Add it to the paths filter."
            )


def test_everything_the_dockerfile_copies_rebuilds_the_image():
    """The drift this file exists for.

    Neither `scripts/requirements.py` nor `examples/config.yaml` is in the
    wheel, so nothing about the package points at them — but the Dockerfile
    COPYs each, and each decides what the image contains. Watching some and
    not others is how a change to the rest builds nothing at all.
    """
    copied = copied_paths()
    assert {"examples/config.yaml", "scripts/requirements.py"} <= copied, (
        "the Dockerfile no longer copies what this test expects — revisit it"
    )
    for filters in image_trigger_paths():
        for path in copied:
            assert covered(path, filters), (
                f"the Dockerfile copies {path}, but image.yml will not rebuild"
                " when it changes"
            )


# --------------------------------------------------------------------------
# The shape of the build
# --------------------------------------------------------------------------


def test_the_dependencies_are_installed_before_the_source():
    """The whole point of the ordering.

    `COPY . .` brings .git with it, because setuptools_scm needs it — so putting
    it in front of the install meant every commit reinstalled fastmcp,
    docs-only ones included.
    """
    builder = dockerfile_stages()["builder"]
    deps_copy = builder.index("COPY pyproject.toml ./")
    install = next(i for i, ln in enumerate(builder) if "requirements.txt" in ln)
    source_copy = builder.index("COPY . .")
    assert deps_copy < install < source_copy


def test_the_runner_receives_a_venv_and_installs_nothing():
    """A venv is one directory holding the libraries and the console script, so
    `COPY --from` moves the whole installed program in one instruction.

    That is what lets the builder be fat and the runner slim WITHOUT the runner
    resolving and downloading every dependency a second time — which is what it
    used to do, out of the wheel the builder had just finished building.
    """
    runner = dockerfile_stages()["runner"]
    assert "COPY --from=builder /opt/venv /opt/venv" in runner
    assert not [ln for ln in runner if "pip install" in ln]
    assert "COPY . ." not in runner, "the source has no business in the runner"


def test_nothing_is_copied_from_a_skills_stage():
    """The image bakes nothing: there is no `skills` stage left to copy from."""
    body = DOCKERFILE.read_text()
    assert "COPY --from=skills" not in body
    assert "AS skills" not in body


def test_the_cache_directory_exists_for_the_runtime_user():
    """A source is fetched into this directory at start, as uid 65534 — the same
    uid `USER` switches to — so it has to be owned before that switch happens."""
    runner = dockerfile_stages()["runner"]
    assert any(
        "mkdir -p /var/cache/mcp-school" in ln and "chown 65534:65534" in ln
        for ln in runner
    )


def test_pip_is_removed_before_the_venv_is_copied():
    """/opt/venv is copied whole, so anything left in it ships.

    pip is a build tool at whatever version was latest that day, and removing it
    is also what keeps .hadolint.yaml's DL3013 waiver true.
    """
    builder = dockerfile_stages()["builder"]
    assert any(ln.startswith("pip uninstall") and "pip" in ln for ln in builder)


def test_no_stage_reaches_for_apt():
    """Nothing here clones or compiles: pygit2 ships prebuilt wheels, and
    `builder` needs git only for setuptools_scm, which python:${PY_VERSION}
    already carries. An apt-get appearing anywhere means a dependency stopped
    shipping a wheel for some target architecture."""
    for lines in dockerfile_stages().values():
        assert not any("apt-get" in ln for ln in lines)


def test_nothing_inherits_the_build_tooling():
    """A stage FROM the builder would carry git, the toolchain and the source
    into whatever it becomes."""
    body = DOCKERFILE.read_text()
    assert "FROM python:${PY_VERSION} AS builder" in body
    assert "FROM python:${PY_VERSION}-slim AS runner" in body
    assert "FROM builder" not in body


def test_the_env_defaults_match_mains():
    """The image's ``CONFIG``/``CACHE_DIR`` must be the same paths main.py falls
    back to when the env vars are unset, or the two silently drift apart."""
    from mcp_school.main import DEFAULT_CACHE_DIR, DEFAULT_CONFIG

    runner = "\n".join(dockerfile_stages()["runner"])
    config_match = re.search(r"\bCONFIG=(\S+?)\s*\\?$", runner, re.MULTILINE)
    cache_match = re.search(r"\bCACHE_DIR=(\S+?)\s*\\?$", runner, re.MULTILINE)
    assert config_match and pathlib.Path(config_match.group(1)) == DEFAULT_CONFIG
    assert cache_match and pathlib.Path(cache_match.group(1)) == DEFAULT_CACHE_DIR


def test_the_copied_venv_is_proved_to_work_at_build_time():
    """Copying a venv across image variants assumes the interpreter is at the
    same path and every wheel is self-contained.

    True here, and worth failing the BUILD over rather than a running pod: the
    import pulls the whole dependency tree.
    """
    runner = dockerfile_stages()["runner"]
    assert any("mcp_school.server" in ln for ln in runner)


def test_the_project_install_resolves_its_dependencies():
    """Not `--no-deps`.

    pip reports everything already satisfied by the layer above and installs
    only this project, so the ordinary install costs nothing — and it self-heals
    if pyproject and the generated list ever drift, which `--no-deps` would
    instead ship as an ImportError.
    """
    builder = dockerfile_stages()["builder"]
    install = next(
        ln for ln in builder if ln.startswith("pip install") and ln.endswith(" .")
    )
    assert "--no-deps" not in install


# --------------------------------------------------------------------------
# The requirement list the build installs from
# --------------------------------------------------------------------------


def test_the_runtime_list_is_what_pyproject_declares():
    """The list has to come out of pyproject, not be restated in the Dockerfile."""
    data = tomllib.loads(PYPROJECT.read_text())
    assert requirements.runtime(data, []) == data["project"]["dependencies"]


def test_no_extra_is_a_runtime_extra_here():
    """[build] and [test] are tooling. If a runtime extra is ever added, the
    Dockerfile has to opt into it explicitly and this test should be revisited."""
    data = tomllib.loads(PYPROJECT.read_text())
    assert set(data["project"]["optional-dependencies"]) == {"build", "test"}
    # Instructions, not the file text: the prose explains why there is no
    # --extra, and a substring search over the comments finds that explanation.
    generate = next(
        ln
        for ln in dockerfile_stages()["builder"]
        if "requirements.py runtime" in ln
    )
    assert "--extra" not in generate


def test_an_extra_that_does_not_exist_says_which_do():
    data = tomllib.loads(PYPROJECT.read_text())
    with pytest.raises(SystemExit) as raised:
        requirements.runtime(data, ["nope"])
    assert "build" in str(raised.value) and "test" in str(raised.value)


def test_the_build_list_is_the_pep_518_one():
    data = tomllib.loads(PYPROJECT.read_text())
    assert requirements.build(data, []) == data["build-system"]["requires"]


def test_the_build_kind_refuses_a_runtime_extra():
    with pytest.raises(SystemExit):
        requirements.main(["build", "--extra", "test"])
