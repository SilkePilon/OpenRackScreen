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
#
# This layer has been unreachable in practice since the `FROM` directly below it,
# and *not* since the interface stage further down: the tag
# `ghcr.io/astral-sh/uv:0.12-python3.12-trixie-slim` publishes linux/amd64 and
# linux/arm64 and **no linux/arm/v7**. So `buildx --platform linux/arm/v7` fails
# at the first `FROM` in this file -- this stage's own -- and has done since that
# tag was pinned, in the commit that created this Dockerfile. `node:24` publishes
# no armv7 either, so the interface stage is a second wall behind the first one;
# it is not what closed 32-bit ARM. arm64 has always been the only ARM target
# here. Kept rather than deleted, and honestly labelled: reopening 32-bit starts
# at the uv base image, not at Node. See server/README.md.
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
COPY packages/openrackscreen/pyproject.toml packages/openrackscreen/
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

# The interface, built in a stage of its own and thrown away. What ships out of
# here is `dist` -- static HTML, JS and CSS -- and nothing else: no Node, no
# pnpm, no `node_modules`. The server mounts that directory with `StaticFiles`,
# which has no opinion about what built it.
#
# Node 24 because that is the major CI runs `pnpm build` on
# (`.github/workflows/ci.yml`), and building the shipped assets on a Node nobody
# tested on is how the artefact that ships becomes the one nothing checked.
# `test_the_interface_is_built_on_the_node_ci_tests_it_on` is what notices when
# the two drift.
#
# Not the final stage, and the position in this file is the whole guard: a stage
# appended at the bottom -- which is where one appends things -- becomes the
# image, and `docker build` would then ship a Node base with no Python in it.
#
# `--platform=$BUILDPLATFORM` -- the machine doing the building, not the machine
# being built for. What leaves this stage is `dist`: HTML, JS and CSS, which are
# the same bytes whatever CPU emitted them. Without this pin the documented Pi
# cross-build (`buildx --platform linux/arm64`, server/README.md) runs `pnpm
# install`, `tsc -b` and `vite build` under qemu-arm64 -- the slowest and by far
# the most fragile part of any cross-build, for an artefact with no architecture
# in it. The uv builder above is the opposite case and is deliberately *not*
# pinned this way: it resolves and installs wheels *for* the target, and an
# amd64 `.venv` copied into an arm64 runtime is a broken image.
# `$BUILDPLATFORM` is a BuildKit built-in, which the `# syntax=` line at the top
# of this file guarantees is in play.
FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS interface

WORKDIR /build/web

# The manifest, the lock and the workspace file before the sources, so that
# editing a component reuses the install layer instead of resolving 700 packages
# again.
#
# `pnpm-workspace.yaml` is not optional and it is not a workspace declaration:
# pnpm 11 refuses to run any dependency's install script until somebody has
# approved it in writing, and until then *every* pnpm command exits 1 with
# `ERR_PNPM_IGNORED_BUILDS`. This project's answer -- msw's `postinstall` writes
# a browser service worker nothing here uses -- lives in that file because
# pnpm 11 reads `allowBuilds` from there and not from `package.json`. Leave it
# out and the build fails at `pnpm install`, loudly, which is at least the good
# kind of failure.
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./

# corepack rather than `npm i -g pnpm@11.1.2`, so that the version is named once
# in the repository -- `packageManager` in `web/package.json`, which is also
# what `pnpm/action-setup` reads in CI. A literal here would be a second place
# for it to drift from, and a pnpm major that disagrees with `pnpm-lock.yaml`
# makes `--frozen-lockfile` fail over a lockfile that is perfectly current.
# `--activate` with no argument is what reads that field.
RUN corepack enable pnpm && corepack prepare --activate

# `--frozen-lockfile` and not a bare install: without it the image resolves
# afresh, so one commit can be built twice and ship two different React minors,
# and the symptom is a rendering bug in a container that cannot be reproduced
# from the lockfile in the repository. With it, a lockfile that disagrees with
# `package.json` is a build failure instead.
RUN pnpm install --frozen-lockfile

# Then the sources. `web/node_modules` and `web/dist` are excluded in
# `.dockerignore`, so this cannot drop the host's install on top of the one
# above -- which on a `buildx --platform linux/arm64` would be a tree of x86
# rollup and esbuild binaries.
COPY web/ ./

# `tsc -b && vite build`, writing `dist` beside this file. There is no `outDir`
# in `web/vite.config.ts`, so `dist` is vite's default rather than a choice made
# twice; `test_what_is_copied_out_is_the_directory_vite_actually_writes` fails
# the day one is added.
RUN pnpm build

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

# `/app/web` because that is the default `ORS_WEB_DIR` in `__main__.py`, and
# deliberately not a path inside `/app/.venv`: the venv's layout carries the
# interpreter version in it (`lib/python3.12/site-packages/...`), so a `COPY`
# naming that path is a Dockerfile that breaks on a Python bump with a blank
# page and no error.
#
# Getting this destination wrong has no loud failure at all. `mount_interface`
# treats a missing build as the ordinary developer state -- a checkout that has
# never run `pnpm build` -- so it logs one warning and serves the API alone. The
# container comes up, the HEALTHCHECK passes, `/api/health` answers JSON, and
# every page is a 404.
# `test_the_built_interface_lands_where_the_server_looks_for_it` reads that
# default out of the running entry point rather than out of this comment.
COPY --from=interface --chown=ors:ors /build/web/dist /app/web

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
