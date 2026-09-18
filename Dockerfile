# The image is the wheel plus an example config — nothing is fetched at build
# time. What the config names is read at container START, into the cache volume;
# see examples/config.yaml for the marketplaces the example points at, and
# README.md for the scheme reference.
#
# Two stages, and the thing that passes between them is a VIRTUALENV.
#
# A venv inside a container looks like ceremony — the container is already an
# isolated box with one Python and one project in it. It is not here for
# isolation. It is here for RELOCATION: it makes the whole installed program one
# directory with a layout that does not depend on how the base image's Python
# was packaged, so `COPY --from` can move it in a single instruction.
#
# That is what removes the duplicate install this build used to do. `builder`
# installed the runtime dependencies in order to build a wheel that does not
# need them, and then `runner` installed them ALL OVER AGAIN out of that wheel —
# fastmcp's tree twice, and the second time on arm64 under emulation. Copying
# the venv means the runner installs nothing at all.
#
# Dependencies are installed before the source, so that layer is keyed on
# pyproject.toml rather than on every commit. .git is in the build context
# because setuptools_scm needs it, so `COPY . .` in front of the install
# invalidated the dependency layer even for a docs-only push.
#
# The layer cache that makes the ordering pay off is `type=gha`, configured in
# kubed-io/actions' build-image, which image.yml already calls. It had never
# worked when buildx was run from a plain shell step — the runner does not hand
# one ACTIONS_RUNTIME_TOKEN — and that is fixed in the action, so this repo gets
# the cache it had been configuring all along.
ARG PY_VERSION=3.14

# ---- builder: the FAT image, because nothing in it ships.
#      python:${PY_VERSION} already carries git — which setuptools_scm needs to
#      resolve the version — and a toolchain for any dependency without a wheel
#      for the target architecture. Using -slim here would mean an apt-get to
#      put git back.
FROM python:${PY_VERSION} AS builder

WORKDIR /app

# Everything installs in here, and this is what the runner receives.
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# The only two files that decide the dependency set. Keeping the source out of
# this layer is what stops a docs edit reinstalling fastmcp.
COPY pyproject.toml ./
COPY scripts/requirements.py ./scripts/

RUN <<'SHELL'
set -eu
pip install --no-cache-dir --upgrade pip
# Read out of pyproject.toml rather than restated here: a second copy is a
# second thing to keep in step, and the way that fails is an image built against
# dependencies nobody declared. No --extra: [build] and [test] are both tooling.
python scripts/requirements.py runtime > /tmp/requirements.txt
pip install --no-cache-dir -r /tmp/requirements.txt
SHELL

# Then our own code, which changes on every commit and installs in seconds.
COPY . .

RUN <<'SHELL'
set -eu
git config --global --add safe.directory /app
# An ordinary install: pip reports every dependency already satisfied by the
# layer above and installs only this project. It self-heals if the two ever
# drift, which `--no-deps` would instead ship as an ImportError.
pip install --no-cache-dir .
# pip is a build tool, and /opt/venv is copied into the runner WHOLE — so
# leaving it here ships it, at whatever version happened to be latest that day,
# into a production image with no use for it. Removing it is also what keeps
# .hadolint.yaml's DL3013 waiver true: the unpinned pip never reaches the image.
pip uninstall --yes pip
SHELL

# ---- runner: slim, and it receives one directory plus an example config.
#      No git, no toolchain, no source, and no second dependency install.
FROM python:${PY_VERSION}-slim AS runner

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# A worked example, not the deployed catalogue: four pinned marketplaces on
# GitHub. A real deployment mounts its own.
COPY examples/config.yaml /etc/mcp-kb/config.yaml

# The venv is copied to the SAME path it was created at, which is the one rule.
# It is this project's node_modules — one self-contained directory you move
# across and call it done — except that node_modules is relocatable and a venv
# is not: it records its own absolute path in pyvenv.cfg and in every console
# script's shebang. Land it anywhere else and it points at an interpreter that
# is not there.
#
# It is built against python:${PY_VERSION} and run on its -slim variant: same
# Debian, same interpreter at the same path, so the symlinks and pyvenv.cfg
# still resolve. That is an assumption worth failing the BUILD over rather than
# a running pod, so it is checked here — this imports the whole dependency tree,
# which is what would break if a wheel needed a shared library only the fat
# image has.
RUN python -c "from kubed.mcp_kb.server import KnowledgeBase"

# Where a source is fetched into at start — a git clone's export, a mirrored
# file:// tree, whatever the config names. Owned by the runtime user so a
# source can write its own cache without the container running as root.
RUN mkdir -p /var/cache/mcp-kb && chown 65534:65534 /var/cache/mcp-kb
VOLUME /var/cache/mcp-kb

ENV CONFIG=/etc/mcp-kb/config.yaml \
    CACHE_DIR=/var/cache/mcp-kb \
    TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
# Numeric UID, not the name: with runAsNonRoot set, the kubelet cannot verify a
# non-numeric USER and refuses to start the container.
USER 65534

ENTRYPOINT ["mcp-kb"]
