# syntax=docker/dockerfile:1

# uv as a build tool in a stage of its own rather than as the runtime base: the
# final stage below wants plain `python:3.12-slim-trixie` with nothing extra
# in it, and building in an image that already has uv is cheaper than fetching
# and verifying a binary here.
#
# The tag is pinned to a uv *stream*, not to `latest`, and the floor is not
# arbitrary: `uv.lock` carries `revision = 3`, and a uv that predates that
# refuses the file outright rather than degrading -- a build failure about a
# lockfile that is perfectly valid. `test_the_image_pins_a_uv_that_can_read_this
# _lock_file` is what fails if the lock's revision moves past this pin.
#
# `trixie-slim` here and `slim-trixie` in the final stage are the same Debian on
# purpose. The virtualenv is copied between the two stages and a venv is not
# portable across interpreters: same Debian, same CPython, or the copied `.venv`
# points at a `python3.12` the runtime does not have. Trixie rather than
# bookworm because uv stopped publishing bookworm variants at 0.12 -- 0.11 is
# the last one with a `-bookworm-slim` tag -- and matching the two stages
# matters more than the codename.
FROM ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim AS builder

# `argon2-cffi-bindings` publishes aarch64 wheels and no 32-bit ARM wheel at
# all, so on 32-bit Raspberry Pi OS it compiles its vendored Argon2 from C.
# `python:3.12-slim` has no compiler, and the failure it produces --
# `error: command 'gcc' failed: No such file or directory` -- reads like a
# broken Dockerfile rather than a missing wheel, which is an hour of the wrong
# investigation. `cryptography` does ship `manylinux_2_31_armv7l`, so this is
# for exactly one dependency.
#
# Discarded with this stage. On arm64 and x86 it costs one apt layer in a stage
# nothing ships; do not move it into the runtime stage to save it.
# `test_the_reason_the_builder_carries_a_compiler_is_still_true` reads the lock
# and fails the day an armv7l wheel appears, which is when this can go.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Where the virtualenv is built, and it is the same path it is served from in
# the final stage. Console scripts get an absolute shebang at install time, so
# a venv built at `/build/.venv` and copied to `/app/.venv` ships an
# `ors-server` whose first line names an interpreter that is not in the image --
# and the error says `python`, not `ors-server`, on a line nobody wrote.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /build

# The lock and every member's manifest first, so that editing a source file
# reuses the cached dependency layer. `uv sync` loads the whole workspace to
# resolve `workspace = true`, so each member's `pyproject.toml` has to be here
# -- including `ors-daemon`'s, which this image never installs and never runs.
COPY uv.lock pyproject.toml ./
COPY packages/ors-schema/pyproject.toml packages/ors-schema/
COPY packages/ors-render/pyproject.toml packages/ors-render/
COPY daemon/pyproject.toml daemon/
COPY server/pyproject.toml server/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --no-editable --package ors-server

# Then the sources, and only the two packages `ors-server` actually needs plus
# the server itself. `daemon/src` is deliberately never copied: it is the half
# of this repo that talks to SPI panels and it has no business in an image that
# serves an API.
COPY packages/ packages/
COPY server/ server/
# `--no-editable` is not a preference, it is the difference between an image
# that runs and one that does not. `uv sync` installs workspace members
# *editable* by default: site-packages gets `_editable_impl_ors_server.pth`
# holding the absolute path `/build/server/src`, and the packages themselves
# stay in the builder. Copy that venv into a stage with no `/build` and the
# container dies at start with `ModuleNotFoundError: No module named
# 'ors_server'` -- from inside its own console script, having found the
# interpreter and the entry point and nothing else. Measured, not reasoned:
# this is what the first build of this image actually did.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package ors-server

FROM python:3.12-slim-trixie

# A named volume inherits the ownership of the image path it shadows, which is
# the entire reason this `chown` exists. Without it the mount point is
# root-owned, uid 10001 cannot create `ors.db` or `secret.key`, and the server
# dies on its first boot with a permission error on a directory that is plainly
# right there. Creating the directory *and* chowning it in the same layer as
# the user is what makes the volume come out owned correctly.
#
# A fixed uid because it is what a bind mount has to be chowned to on the host
# if anyone ever swaps the named volume for one, and an unpinned system uid
# would move between rebuilds.
RUN useradd --system --create-home --uid 10001 ors \
    && mkdir -p /var/lib/openrackscreen \
    && chown ors:ors /var/lib/openrackscreen

COPY --from=builder --chown=ors:ors /app/.venv /app/.venv

# `ORS_HOST=0.0.0.0` and not the loopback default a bare uvicorn would take:
# `127.0.0.1` inside a container is the container and nothing else, so a
# published port in front of it refuses every connection -- including the
# healthcheck's -- while the server runs perfectly and logs nothing.
ENV PATH="/app/.venv/bin:$PATH" \
    ORS_DATA_DIR=/var/lib/openrackscreen \
    ORS_HOST=0.0.0.0 \
    ORS_PORT=8080

USER ors
EXPOSE 8080
VOLUME ["/var/lib/openrackscreen"]

# `/api/health` and deliberately not `/api/daemons`: the daemon list assembles a
# snapshot per rack to fill `config_error`, which is a handful of SQLite reads
# and a validation per rack. That is nothing once and a standing cost every
# thirty seconds for the life of the deployment. `/api/health` reads no rows and
# needs no session.
#
# No curl, and none added: `python:3.12-slim` does not carry it, and installing
# a package and its CVE feed for one HTTP GET that the interpreter already in
# this image can make is a bad trade.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health').read()"]

CMD ["ors-server"]
