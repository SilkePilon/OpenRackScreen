"""What the shipped container is, asserted without a Docker daemon.

Nothing here shells out to `docker`. A suite that needs a daemon to run is a
suite that stops running on the first machine without one, and every assertion
below is about a *decision* recorded in a file -- which port is published, which
directory survives the container, which stage carries a compiler -- rather than
about the bytes of a built image. `docker build` still has to happen, on the
hardware, and it belongs on the deployment checklist rather than in here.

The assertions that matter are the ones that cross a file boundary. A test that
reads `8080` out of `compose.pi.yaml` and asserts it is `8080` protects nothing;
a test that reads the port out of the compose file, the `ENV` out of the
Dockerfile, and the default out of `__main__.py`, and asserts the three agree,
fails whenever any one of them is edited alone -- which is the only way this
particular deployment has ever broken.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient
from ors_server import __main__ as entrypoint
from ors_server.__main__ import main
from ors_server.app import AppSettings, create_app
from ors_server.secrets import load_or_create_key

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

COMPOSE_FILES = ["compose.pi.yaml", "compose.remote.yaml"]

# The override that pulls the published image instead of building the
# checkout. Kept out of COMPOSE_FILES on purpose: it is not one of the two
# files an operator brings up on its own, and every test that walks
# COMPOSE_FILES asserting properties of *the server this container runs*
# would otherwise have to special-case the one file whose `server` service
# has no `build:` at all.
COMPOSE_IMAGE = DEPLOY / "compose.image.yaml"

# Where the database and the key live inside the container, and the one string
# that has to be the same in three files. Named once here so that a test which
# disagrees with the Dockerfile is a failure rather than two tests quietly
# agreeing with each other about the wrong path.
DATA_DIR = "/var/lib/openrackscreen"


def compose(name: str) -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / name).read_text())


def server_service(name: str) -> dict[str, Any]:
    return compose(name)["services"]["server"]


@pytest.fixture
def dockerfile() -> str:
    return (DEPLOY / "Dockerfile").read_text()


def stages(text: str) -> list[str]:
    """The Dockerfile split at each `FROM`, in order, last one last.

    Split on a `FROM` at the start of a line rather than anywhere, so that the
    word appearing in a comment -- which it does, in the comment explaining why
    the compiler is in the stage it is in -- does not invent a stage boundary
    and hand every later test the wrong half of the file.
    """
    return re.split(r"^FROM ", text, flags=re.MULTILINE)[1:]


def instructions(text: str) -> str:
    """The Dockerfile with every comment line removed.

    Every "must not contain" assertion below runs against this rather than
    against the raw text, and so does every "must contain". Both directions
    matter and they fail in opposite ways. A comment saying *why* there is no
    curl in this image would otherwise fail the test that there is no curl --
    which gets the comment deleted, or worse, the test weakened. And a comment
    saying `gcc` is needed on armv7 would otherwise satisfy the test that gcc is
    installed, all on its own, leaving the assertion green with the `apt-get`
    line gone.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def dockerfile_env(text: str) -> dict[str, str]:
    """Every `ENV KEY=VALUE` in the file, continuations joined.

    A hand-rolled parser rather than a regex over the raw text because the
    variables this asserts on are written as one `ENV` with backslash
    continuations, and a line-at-a-time reader sees three of the four as
    unprefixed noise.
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    found: dict[str, str] = {}
    for line in joined.splitlines():
        if not line.startswith("ENV "):
            continue
        for token in line[len("ENV ") :].split():
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            found[key] = value.strip('"').strip("'")
    return found


def the_servers_own(environment: dict[str, str]) -> dict[str, str]:
    """The `ORS_*` half of an image environment, which is all `main` reads.

    `dockerfile_env` also returns `PATH`, whose value in the Dockerfile is the
    literal `/app/.venv/bin:$PATH` -- unexpanded, because it is the image that
    expands it. Exporting that verbatim into this process puts a directory that
    does not exist and a literal `$PATH` on the front of the test runner's own
    `PATH`. It is inert today (`main` shells out to nothing, and `monkeypatch`
    puts it back either way) and it is a live trap for the first person who adds
    a subprocess anywhere on the startup path, so it never gets exported at all.
    """
    return {key: value for key, value in environment.items() if key.startswith("ORS_")}


class Copy(NamedTuple):
    """One `COPY`, split into the three things an assertion ever asks about."""

    flags: set[str]
    sources: list[str]
    destination: str


def copies(text: str) -> list[Copy]:
    """Every `COPY` in a chunk of Dockerfile, continuations joined.

    A parser rather than a regex per test because the interesting questions --
    *where does this land*, *which stage did it come from* -- are about the last
    token and the flags, and a regex that gets `--chown` and `--from` in either
    order right is longer than this.
    """
    found: list[Copy] = []
    for line in re.sub(r"\\\n\s*", " ", instructions(text)).splitlines():
        if not line.startswith("COPY "):
            continue
        tokens = line.split()[1:]
        paths = [token for token in tokens if not token.startswith("--")]
        found.append(Copy({t for t in tokens if t.startswith("--")}, paths[:-1], paths[-1]))
    return found


class Stage(NamedTuple):
    index: int
    alias: str
    body: str


def from_arguments(header: str) -> str:
    """A `FROM` line with its flags removed, leaving `<image> [AS <alias>]`.

    `FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS interface` is a
    perfectly ordinary `FROM`, and a reader that starts matching at the first
    token sees a flag where the image should be -- so it finds no Node stage at
    all and every assertion below it disappears rather than fails.
    """
    return " ".join(token for token in header.split() if not token.startswith("--"))


def node_stage(text: str) -> Stage:
    """The stage that builds the interface, found by its base image.

    By the image and not by the alias: the alias is a label somebody chooses and
    can rename to anything, while `FROM node:` is what makes a stage a Node
    stage. A test that went looking for a stage called `interface` would pass on
    a Dockerfile that renamed it and failed to build one at all.
    """
    found = [
        (index, stage)
        for index, stage in enumerate(stages(text))
        if re.match(r"(?:[\w.\-/]+/)?node:", from_arguments(stage.splitlines()[0]))
    ]
    assert len(found) == 1, (
        f"expected exactly one Node stage in the Dockerfile, found {len(found)};"
        " the interface has to be built somewhere and it has to be built once"
    )
    index, body = found[0]
    alias = re.match(r"\S+\s+AS\s+(\S+)", from_arguments(body.splitlines()[0]), flags=re.IGNORECASE)
    assert alias, "the Node stage has no `AS <name>`, so no later stage can copy out of it"
    return Stage(index, alias[1], body)


def configured(monkeypatch, tmp_path, environment: dict[str, str]) -> AppSettings:
    """The `AppSettings` `main` builds under a given environment.

    The sibling of `served` below, which captures what goes to uvicorn; this
    captures what goes to `create_app`, because the directory the interface is
    served out of is settled there and never reaches uvicorn at all. Both exist
    so that a Dockerfile assertion can be made against a path production code
    computed rather than against one this test remembered.
    """
    captured: list[AppSettings] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(entrypoint, "create_app", lambda settings: captured.append(settings))
    for key in ("ORS_DATA_DIR", "ORS_HOST", "ORS_PORT", "ORS_SECRET_KEY", "ORS_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    # Deleted separately and deliberately *not* re-set below: a developer with
    # `ORS_WEB_DIR=web/dist` exported would otherwise have this test assert the
    # Dockerfile copies to their checkout.
    monkeypatch.delenv("ORS_WEB_DIR", raising=False)
    for key, value in the_servers_own(environment).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    assert main() == 0

    return captured[0]


@pytest.fixture(autouse=True)
def restore_logging():
    """`main` calls `setup_logging`, which is global and outlives the test.

    Lifted from `test_main.py`, which carries the same fixture for the same
    measured reason: `setup_logging` puts a handler on the `ors_server` logger
    and clears `propagate`, and a suite that let that stand changes what every
    later test's `caplog` can see. It did exactly that once already, as two
    failures in `test_ws_daemon` about a record that had never been emitted.
    """
    names = ("ors_server", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = [(logging.getLogger(name), list(logging.getLogger(name).handlers)) for name in names]
    levels = [(logger, logger.level, logger.propagate) for logger, _ in saved]
    yield
    for logger, handlers in saved:
        logger.handlers[:] = handlers
    for logger, level, propagate in levels:
        logger.setLevel(level)
        logger.propagate = propagate


def served(monkeypatch, tmp_path, environment: dict[str, str]) -> dict[str, Any]:
    """What `main` hands uvicorn under a given environment, without binding a port.

    The environment is the image's, read out of the Dockerfile's own `ENV`, so
    what comes back is what this container would actually listen on rather than
    what the test remembers the defaults being.
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.append(kwargs))
    for key in ("ORS_DATA_DIR", "ORS_HOST", "ORS_PORT", "ORS_SECRET_KEY", "ORS_LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in the_servers_own(environment).items():
        monkeypatch.setenv(key, value)
    # After the image's own variables, so the database lands somewhere writable
    # rather than in the container's `/var/lib`, which this test cannot create.
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    assert main() == 0

    return captured[0]


# --------------------------------------------------------------------------
# `image:` versus `build:`
# --------------------------------------------------------------------------


def test_no_compose_file_names_both_an_image_and_a_build():
    """Compose prefers `image:`, so naming both never builds your checkout.

    Measured, not theorised: on this machine a 45-hour-old copy of
    `ghcr.io/silkepilon/openrackscreen:latest` was started instead of the
    working tree, went `healthy`, answered `/api/health`, and returned 404 for
    every page -- because it predated the interface being in the image. The
    README had to grow a paragraph telling people to pass `--build`. This test
    is that paragraph, enforced.
    """
    for name in COMPOSE_FILES:
        document = compose(name)
        for service_name, service in document["services"].items():
            assert not ("image" in service and "build" in service), (
                f"{name}:{service_name} names both; compose will silently use the image"
            )


def test_the_image_override_pins_the_published_tag():
    """Pulling a published image stays possible, and stays a thing you ask for."""
    document = yaml.safe_load(COMPOSE_IMAGE.read_text())
    service = document["services"]["server"]
    assert service["image"] == "ghcr.io/silkepilon/openrackscreen:latest"
    assert "build" not in service


# --------------------------------------------------------------------------
# The data directory: three files that have to name the same path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_database_lives_in_a_volume(name):
    """Without this the database dies with the container and the rack unpairs.

    The server keeps only a fingerprint of the key it minted, so a lost database
    is not "re-enter the config": it is a new pairing token per rack, typed in
    at each Pi, because nothing on either side can recover the old one.
    """
    mounts = [volume.split(":")[1] for volume in server_service(name)["volumes"]]

    assert DATA_DIR in mounts, (
        "without this the database dies with the container and the rack unpairs itself"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_directory_that_is_mounted_is_the_one_the_server_writes_to(name, dockerfile):
    """A mount and an `ORS_DATA_DIR` that disagree is the failure with no symptom.

    The container comes up, answers, accepts a pairing and writes `ors.db` into
    its own writable layer beside the empty volume -- and loses all of it on the
    next `docker compose up`, having never once logged anything wrong.
    """
    mounts = [volume.split(":")[1] for volume in server_service(name)["volumes"]]

    assert dockerfile_env(dockerfile)["ORS_DATA_DIR"] in mounts, (
        "the volume is mounted somewhere the server never writes; the database is"
        " in the container layer and dies with it, silently"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_both_compose_files_set_the_data_directory_explicitly(name):
    """The code default moved to the user's state directory in M3c.

    A compose file that relied on the old `/var/lib` default now puts the
    database somewhere inside the container's root home, and the named volume
    it mounts is a directory nothing writes to -- so every restart is a fresh
    server with no password set, and the volume looks healthy and empty.
    """
    environment = server_service(name)["environment"]

    assert isinstance(environment, list), (
        "use the list form; see test_an_unset_key_is_passed_as_absent_rather_than_as_empty"
    )
    assert f"ORS_DATA_DIR={DATA_DIR}" in environment, (
        f"{name} never sets ORS_DATA_DIR; the code default moved off /var/lib in M3c,"
        " so this compose file's named volume would sit empty while the database"
        " landed inside the container's own root home"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_database_is_a_named_volume_and_not_the_daemons_state_directory(name):
    """`/var/lib/openrackscreen` on the host belongs to the daemon, not to this.

    The daemon's systemd unit takes `StateDirectory=openrackscreen`, which is
    that exact path, created 0700 and owned by the `openrackscreen` user -- and
    on the Pi deployment both run on one machine. A bind mount of the host path
    into this container therefore drops the server's `ors.db` and `secret.key`
    into a directory its own uid cannot enter, and on a rack that is already
    paired it does so on top of `link.json`. A named volume has no such
    argument to have: Docker owns it, and the `chown` in the image is what
    decides who may write in it.
    """
    service = server_service(name)
    declared = set(compose(name).get("volumes") or {})

    sources = [volume.split(":")[0] for volume in service["volumes"]]
    assert sources, "the server service mounts nothing at all"
    for source in sources:
        assert not source.startswith(("/", ".", "~")), (
            f"{source} is a host path; /var/lib/openrackscreen on a Pi is the daemon's"
            " StateDirectory, 0700 and owned by another user"
        )
        assert source in declared, f"{source} is mounted but never declared as a volume"


# --------------------------------------------------------------------------
# The secret key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_secret_key_is_passed_through_and_not_baked_in(name, dockerfile):
    """It must be injectable, and it must not be *in* anything that is committed.

    The check is on the value rather than on the syntax: a bare `ORS_SECRET_KEY`
    in the list form and a `${ORS_SECRET_KEY}` substitution both take the key
    from the host, and only a literal is wrong. A Fernet key is 44 url-safe
    base64 characters, so a literal is recognisable even if somebody names the
    variable something else on the way past.
    """
    environment = server_service(name)["environment"]
    named = [entry for entry in environment if str(entry).startswith("ORS_SECRET_KEY")]

    assert named, "the key must be injectable"
    for entry in named:
        _, _, value = str(entry).partition("=")
        assert not value or value.startswith("${"), (
            f"the key is written into the compose file as {value!r}; it comes from the"
            " host environment or from nowhere"
        )
    assert not re.search(r"[A-Za-z0-9_-]{43}=", str(environment)), "that is a literal Fernet key"
    assert "ORS_SECRET_KEY" not in instructions(dockerfile), (
        "a key in the image is a key in the registry"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_an_unset_key_is_passed_as_absent_rather_than_as_empty(name):
    """`ORS_SECRET_KEY: ${ORS_SECRET_KEY:-}` is a trap and this is the check for it.

    Compose substitutes an unset variable to the empty string, and
    `load_or_create_key` refuses an empty value on purpose -- `is not None`, not
    truthiness, because treating `ORS_SECRET_KEY=` as "unset" would silently
    fall back to the file's key and make every stored credential undecryptable
    with no signal. So the default-to-empty form turns "I did not set this" into
    a startup crash naming a variable the operator never touched. The list form
    -- `- ORS_SECRET_KEY` -- passes the variable through when it is set and
    omits it entirely when it is not, which is the behaviour the server wants.
    """
    environment = server_service(name)["environment"]

    assert isinstance(environment, list), (
        "use the list form; the mapping form cannot express 'omit this variable'"
    )
    for entry in environment:
        if str(entry).startswith("ORS_SECRET_KEY"):
            assert entry == "ORS_SECRET_KEY", (
                "`ORS_SECRET_KEY: ${ORS_SECRET_KEY:-}` substitutes to the empty string"
                " when unset, and load_or_create_key refuses an empty key by design:"
                " the container would crash on a first boot that set nothing"
            )


@pytest.mark.parametrize("umask_bits", [0o000, 0o022, 0o027, 0o077])
def test_the_key_it_writes_survives_its_own_permission_check(tmp_path, umask_bits):
    """Second boot, in the volume, under a umask the container did not choose.

    `load_or_create_key` refuses a key file with any group or other bit set --
    which is right, and is also a check the writer has to be able to pass. It
    passes for a reason worth writing down: the mode comes from `os.open`'s
    third argument, and the umask can only ever *clear* bits from it, so 0600
    masked by anything is still at most 0600 and `mode & 0o077` is always zero.
    Had the key been written with `write_bytes` instead, the default 022 would
    have produced 0644, the first boot would have looked fine, and the container
    would have refused to start ever again.

    umask 0 is in the list because it is the only value that removes nothing,
    and so the only one under which a wrong `open` mode would show.
    """
    import os

    previous = os.umask(umask_bits)
    try:
        first = load_or_create_key(tmp_path, None)
        second = load_or_create_key(tmp_path, None)
    finally:
        os.umask(previous)

    assert second == first, "the second boot did not read back the key the first one wrote"


def test_the_data_directory_the_app_creates_leaves_the_key_private(tmp_path):
    """`create_app` makes the data directory itself, and the volume is empty on
    the first boot of a fresh deployment. What is being pinned is the whole
    first-boot path -- mkdir, generate, chmod -- ending somewhere the check on
    the *second* boot accepts, rather than each half separately."""
    import os
    import stat

    data_dir = tmp_path / "volume"
    previous = os.umask(0o000)
    try:
        create_app(AppSettings(data_dir=data_dir))
    finally:
        os.umask(previous)

    mode = stat.S_IMODE((data_dir / "secret.key").stat().st_mode)
    assert not mode & 0o077, f"the key came out {mode:o}; the next boot refuses to start"


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_published_port_is_the_port_this_image_actually_binds(
    name, dockerfile, monkeypatch, tmp_path
):
    """Compose, `ENV`, and `__main__.py`'s default, asserted as one number.

    Reading `8080` out of the compose file and comparing it to a `8080` written
    in this test would pass forever. What is compared instead is the container
    port compose publishes against the port `main` hands uvicorn under the
    image's own environment -- so editing the `ENV`, the compose file or the
    default alone is a failure, and editing all three together is a decision.
    """
    bound = served(monkeypatch, tmp_path, dockerfile_env(dockerfile))["port"]
    published = server_service(name)["ports"]

    assert any(str(entry).endswith(f":{bound}") for entry in published), (
        f"the server binds {bound} in the container and compose publishes {published};"
        " a mismatch here is a connection refused that looks like a crashed server"
    )


def test_the_exposed_port_is_the_port_it_binds(dockerfile):
    """`EXPOSE` documents nothing the runtime enforces, which is why it drifts."""
    environment = dockerfile_env(dockerfile)
    exposed = re.findall(r"^EXPOSE (\d+)", dockerfile, flags=re.MULTILINE)

    assert exposed == [environment["ORS_PORT"]]


def test_the_server_listens_on_every_interface_in_the_container(dockerfile, monkeypatch, tmp_path):
    """`127.0.0.1` inside a container is the container, and nothing else.

    A published port in front of a loopback bind is a connection refused on
    every request, from the healthcheck outwards, with a server that is running
    perfectly and logging nothing.
    """
    assert served(monkeypatch, tmp_path, dockerfile_env(dockerfile))["host"] == "0.0.0.0"  # noqa: S104


# --------------------------------------------------------------------------
# The daemon is not in here
# --------------------------------------------------------------------------


def test_the_pi_compose_file_does_not_containerise_the_daemon():
    """It needs the rack's devices and its own groups; it stays a systemd unit.

    Measured against what the daemon needs today rather than against the plan's
    guess: `daemon/examples/openrackscreen.service` takes
    `SupplementaryGroups=spi gpio` for `/dev/spidev*` and `/dev/gpiochip*`, and
    a `StateDirectory` that survives a reboot. Containerising that buys nothing
    and costs a device list that has to be corrected every time a rack is
    rewired.
    """
    services = compose("compose.pi.yaml")["services"]

    assert list(services) == ["server"], (
        "the daemon needs /dev/spidev* and /dev/gpiochip* on the host; it stays a systemd unit"
    )


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_neither_compose_file_hands_the_container_the_racks_hardware(name):
    """The other half of the decision above, and the one that rots quietly.

    `test_the_pi_compose_file_does_not_containerise_the_daemon` counts services;
    it says nothing about someone giving the *server* the panels instead, which
    is the shape the mistake actually takes -- a `privileged: true` added to
    debug something and never removed.
    """
    service = server_service(name)

    for key in ("devices", "privileged", "group_add", "device_cgroup_rules"):
        assert key not in service, (
            f"{key} on the server service: nothing in this image touches the rack's"
            " panels, and the daemon that does is not in this file"
        )


def test_the_daemons_device_needs_are_still_what_this_decision_assumed():
    """If the unit stops needing host devices, the decision above is re-openable.

    Pinned against the unit itself rather than restated in a comment: the whole
    argument for keeping the daemon out of compose is that it needs `spi` and
    `gpio` group membership on the host, and this is the assertion that notices
    the day that stops being true.
    """
    unit = (ROOT / "daemon" / "examples" / "openrackscreen.service").read_text()

    assert "SupplementaryGroups=spi gpio" in unit
    assert "StateDirectory=openrackscreen" in unit, (
        "the daemon keeps its pairing in a host state directory, which is the"
        " other half of why it is not in the compose file"
    )


# --------------------------------------------------------------------------
# The image itself
# --------------------------------------------------------------------------


def test_the_final_stage_runs_as_a_non_root_user(dockerfile):
    """Root in the container is root on anything it is ever bind-mounted onto."""
    final = stages(dockerfile)[-1]
    users = re.findall(r"^USER (\S+)", final, flags=re.MULTILINE)

    assert users, "root in the container is root on the bind-mounted data dir"
    assert users[-1] not in ("root", "0"), f"the final stage runs as {users[-1]}"


def test_the_data_directory_is_owned_by_the_user_that_has_to_write_in_it(dockerfile):
    """A named volume inherits the ownership of the image path it shadows.

    Which is the whole reason this line exists: without it the mount point is
    root-owned, the non-root user cannot create `ors.db` or `secret.key`, and
    the server dies on its very first boot with a permission error on a
    directory the operator can see is right there.

    Asserting `"chown" in dockerfile` would not do -- the `COPY --chown` of the
    virtualenv satisfies that on its own, so deleting this line would leave the
    test green. What is asserted is a chown *of the data directory* to the user
    the final stage switches to.
    """
    final = stages(dockerfile)[-1]
    user = re.findall(r"^USER (\S+)", final, flags=re.MULTILINE)[-1]
    data_dir = dockerfile_env(dockerfile)["ORS_DATA_DIR"]

    chowned = re.findall(rf"chown {user}:\S* {re.escape(data_dir)}", final)
    assert chowned, (
        f"{data_dir} is never chowned to {user} in the final stage; a named volume"
        " inherits the ownership of the path it shadows, so the server cannot create"
        " ors.db and dies on first boot"
    )


def test_the_workspace_is_installed_into_the_image_and_not_referenced_from_it(dockerfile):
    """`uv sync` installs workspace members editable by default, and that is fatal here.

    Editable means site-packages gets `_editable_impl_ors_server.pth` containing
    the absolute path `/build/server/src`, and the package itself never leaves
    the builder. Copy that virtualenv into a stage that has no `/build` and the
    container dies at start with `ModuleNotFoundError: No module named
    'ors_server'` -- raised from inside its own console script, so it has
    plainly found the interpreter and the entry point, which sends you looking
    at `PATH` and the venv copy rather than at an install mode.

    Measured rather than reasoned: the first build of this image did exactly
    that. Every `uv sync` that installs the workspace needs `--no-editable`.
    """
    builder = instructions(stages(dockerfile)[0])
    syncs = re.findall(r"uv sync[^\n]*", builder)

    assert syncs, "the builder no longer runs uv sync"
    installing = [sync for sync in syncs if "--no-install-workspace" not in sync]
    assert installing, "no uv sync installs the workspace, so ors_server is not in the image"
    for sync in installing:
        assert "--no-editable" in sync, (
            f"`{sync.strip()}` installs ors_server editable, pointing at a /build path"
            " the runtime stage does not have: ModuleNotFoundError at start"
        )


def test_every_workspace_members_pyproject_is_copied_into_the_builder(dockerfile):
    """`uv sync` loads the whole workspace to resolve `workspace = true`
    sources, so every member's `pyproject.toml` has to be visible to it --
    including members this image never installs, like `daemon` and the
    `openrackscreen` meta-package (see the comment on the `COPY` block
    itself). Missing one is not a loud failure: `uv sync` inside the builder
    simply cannot resolve that member's `workspace = true` source, and the
    build fails on a resolution error naming none of the files actually at
    fault.
    """
    members = sorted((ROOT / "packages").glob("*/pyproject.toml"))
    members += [ROOT / "daemon" / "pyproject.toml", ROOT / "server" / "pyproject.toml"]
    assert len(members) >= 5, (
        "the workspace member glob found suspiciously few pyproject.toml files"
    )

    builder = copies(stages(dockerfile)[0])
    copied_sources = {source for copy in builder for source in copy.sources}
    for member in members:
        relative = member.relative_to(ROOT).as_posix()
        assert relative in copied_sources, (
            f"{relative} is a workspace member's manifest but the builder stage never COPYs it in"
        )


def test_the_image_does_not_ship_a_compiler(dockerfile):
    final = instructions(stages(dockerfile)[-1])

    assert "gcc" not in final, "build tooling belongs in the discarded builder stage"
    assert "build-essential" not in final
    assert "libc6-dev" not in final


def test_the_builder_installs_the_compiler_the_32_bit_pi_needs(dockerfile):
    """The other half of the test above, and the one that keeps it honest.

    `gcc not in the final stage` is satisfied perfectly by deleting the apt line
    altogether -- which is exactly the edit that breaks an armv7 build, and it
    breaks it with `error: command 'gcc' failed: No such file or directory`,
    which reads like a broken Dockerfile rather than a missing wheel. Both
    halves, or neither is worth having.
    """
    builder = instructions(stages(dockerfile)[0])

    assert "apt-get install" in builder, "nothing is installed in the builder at all"
    assert "gcc" in builder, "argon2-cffi-bindings compiles from C on 32-bit ARM"
    assert "libc6-dev" in builder, "gcc without headers fails later and more confusingly"


def test_the_reason_the_builder_carries_a_compiler_is_still_true():
    """Pinned against the lock file, so the day it stops being true is visible.

    `argon2-cffi-bindings` publishes aarch64 wheels and no 32-bit ARM wheel at
    all, while `cryptography` does ship `manylinux_2_31_armv7l`. So on 32-bit
    Raspberry Pi OS exactly one dependency compiles, and the builder stage
    carries `gcc` for it alone. When an armv7l wheel appears in this lock, this
    test fails and the apt layer can go -- which is a better prompt than a
    comment nobody re-measures.

    Read from `uv.lock` rather than from PyPI: a test that needs the network is
    a test that fails on a train, and the lock is what the image resolves from
    anyway.
    """
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in locked["package"]}

    argon2 = packages["argon2-cffi-bindings"]
    wheels = [wheel["url"] for wheel in argon2.get("wheels", [])]
    assert not any("armv7l" in url for url in wheels), (
        "argon2-cffi-bindings now has a 32-bit ARM wheel; the builder's gcc can go"
    )
    assert "sdist" in argon2, (
        "no armv7l wheel and no sdist either means a 32-bit build cannot resolve at all"
    )

    cryptography = [wheel["url"] for wheel in packages["cryptography"].get("wheels", [])]
    assert any("armv7l" in url for url in cryptography), (
        "cryptography's armv7l wheel is why gcc is needed for argon2 and nothing else"
    )


def test_the_image_pins_a_uv_that_can_read_this_lock_file(dockerfile):
    """A `uv` older than the lock's revision fails the build, and says so badly.

    `uv.lock` carries `revision = 3`, and a `uv` that predates it refuses the
    file wholesale rather than degrading -- so pinning the builder to a uv
    stream older than the lock turns every build into a resolution error about
    a file that is perfectly valid. The floor here is a major.minor, because
    that is the granularity the published image tags come in.
    """
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    assert locked["revision"] == 3, "the lock revision moved; re-check the uv floor below"

    tag = re.search(r"^FROM ghcr\.io/astral-sh/uv:(\d+)\.(\d+)", dockerfile, flags=re.MULTILINE)
    assert tag, "the builder no longer pins a uv image tag by version"
    assert (int(tag[1]), int(tag[2])) >= (0, 9), (
        f"uv {tag[1]}.{tag[2]} predates lock revision 3 and refuses to read uv.lock"
    )


def test_the_virtualenv_is_built_at_the_path_it_is_served_from(dockerfile):
    """Console scripts carry an absolute shebang, so the venv cannot be moved.

    `uv sync` writes `/app/.venv/bin/ors-server` with `#!/app/.venv/bin/python`
    on the first line -- an absolute path, baked in at install time. Build it at
    `/build/.venv` and copy it to `/app/.venv`, as the obvious two-stage recipe
    does, and every console script in the image points at an interpreter that
    does not exist: `CMD ["ors-server"]` fails with a `no such file or
    directory` naming *python*, not the script, on a line nobody wrote.

    So the builder is told where the venv goes -- `UV_PROJECT_ENVIRONMENT` --
    and that path, the `COPY` destination and the `PATH` all have to be one
    string. This is the test that they are.
    """
    builder = stages(dockerfile)[0]
    final = stages(dockerfile)[-1]

    built_at = re.search(r"UV_PROJECT_ENVIRONMENT=(\S+)", builder)
    assert built_at, (
        "the builder does not pin where the venv is created, so it lands beside the"
        " project and its console scripts get a shebang the runtime stage cannot honour"
    )
    venv = built_at[1]

    copied = re.search(rf"^COPY --from=\S+.*? {re.escape(venv)} (\S+)", final, flags=re.MULTILINE)
    assert copied, f"the final stage never copies {venv}"
    assert copied[1] == venv, (
        f"built at {venv} and copied to {copied[1]}; every console script's shebang"
        f" still says {venv}/bin/python"
    )
    assert f"{venv}/bin" in dockerfile_env(dockerfile)["PATH"]


def test_the_runtime_stage_is_not_alpine(dockerfile):
    """musl matches no manylinux wheel, so every C dependency rebuilds -- in a
    stage that deliberately has no compiler."""
    assert "alpine" not in instructions(stages(dockerfile)[-1]).lower()


def test_the_two_stages_agree_about_which_debian_they_are(dockerfile):
    """The virtualenv is copied between them, and it is not relocatable across
    interpreters. Both stages have to be the same Debian release and the same
    CPython minor, or the `.venv` the builder produces points at a `python3.12`
    the runtime does not have."""
    builder, final = stages(dockerfile)[0], stages(dockerfile)[-1]
    releases = {"bookworm", "trixie", "bullseye"}

    in_builder = {word for word in releases if word in builder.split("\n")[0]}
    in_final = {word for word in releases if word in final.split("\n")[0]}
    assert in_builder and in_builder == in_final, (
        f"builder is {in_builder or 'unpinned'} and runtime is {in_final or 'unpinned'};"
        " a virtualenv copied between two Debians is a venv pointing at a missing python"
    )
    assert "python3.12" in builder.split("\n")[0]
    assert "python:3.12" in final.split("\n")[0]


# --------------------------------------------------------------------------
# The interface in the image
#
# What every one of these is guarding: `create_app` mounts the interface only
# when the directory it is given holds an `index.html`, and when it does not it
# logs one warning and serves the API alone. So a Dockerfile that builds the SPA
# and puts it a directory to the left produces a container that starts, passes
# its own healthcheck, answers `/api/health` -- and returns 404 for every page.
# These are documents, not Docker: they read the Dockerfile as text and they
# cannot tell you the build succeeds. What they can do is fail the moment the
# file stops saying the thing the running image was checked to do.
# --------------------------------------------------------------------------


def test_the_interface_is_built_in_a_stage_that_is_not_the_one_shipped(dockerfile):
    """A Node toolchain is a build dependency, and this is where that is decided.

    `FROM node:` as the last stage would work -- it has a Python in it about as
    much as `python:3.12-slim` has a Node in it, which is to say not at all, so
    it would in fact not work; but the shape the mistake takes is subtler than
    that. It is appending a Node stage and then having the `CMD` end up in it
    because the new stage was added at the bottom of the file, which is where
    one adds things.
    """
    stage = node_stage(dockerfile)
    last = len(stages(dockerfile)) - 1

    assert stage.index != last, (
        "the Node stage is the last stage, so it is the image: `docker build` ships"
        " a Node base with a package manager, a node_modules and no Python in it"
    )


def test_the_shipped_stage_carries_no_node_and_no_package_manager(dockerfile):
    """The point of a build stage is that the tools stay in it.

    Read against `instructions` rather than the raw text, so that the comment
    above explaining *why* there is no pnpm down here does not itself fail the
    assertion that there is no pnpm down here.

    And with `--from=<alias>` removed, for the mirror-image reason. That flag
    names a *build stage*, not anything that ships, and the alias is a label
    somebody picks. Rename `AS interface` to `AS nodejs-assets` and the
    `COPY --from=nodejs-assets` in this stage matches `\\bnodejs\\b`; rename it
    to `AS npm-cache` and it matches the substring check above. Both fail a
    Dockerfile that is entirely correct, and the usual fix for a false alarm is
    to weaken the test.

    Measured, because the obvious rename is *not* one of them: `\\bnodejs?\\b`
    is `nodej` with an optional `s`, so `AS node-build` -- the first name anyone
    reaches for -- never tripped it. The hazard is narrower than it looks and it
    is real. What is copied *out* of that stage is asserted separately, by
    `test_nothing_copied_out_of_the_node_stage_is_a_node_modules`.
    """
    final = re.sub(r"--from=\S+", "", instructions(stages(dockerfile)[-1]))

    for tool in ("node_modules", "pnpm", "npm", "yarn", "corepack"):
        assert tool not in final, f"{tool} in the final stage; it belongs in the build stage"
    assert not re.search(r"\bnodejs?\b", final), "a Node runtime in an image that serves Python"


def test_nothing_copied_out_of_the_node_stage_is_a_node_modules(dockerfile):
    """The other half, and the one `pnpm not in the final stage` does not cover.

    `COPY --from=interface /build/web/node_modules /app/node_modules` contains
    neither the word `pnpm` nor the word `node` as a token the test above looks
    for -- it is one path -- and it would put three hundred megabytes of
    development dependencies into the shipped image while every assertion stayed
    green.
    """
    stage = node_stage(dockerfile)
    final = stages(dockerfile)[-1]

    from_node = [copy for copy in copies(final) if f"--from={stage.alias}" in copy.flags]
    assert from_node, f"the final stage copies nothing out of {stage.alias}; the build is discarded"
    for copy in from_node:
        for source in copy.sources:
            assert "node_modules" not in source, (
                f"{source} is copied into the shipped image; the whole point of the build"
                " stage is that its dependencies stay in it"
            )


def test_the_built_interface_lands_where_the_server_looks_for_it(dockerfile, monkeypatch, tmp_path):
    """The one assertion this task exists for, and it crosses two files.

    The destination is not a string this test remembers. It is what `main`
    hands `create_app` as `web_dir` under the image's own `ENV` -- so the day
    that default moves and the `COPY` does not, or the `COPY` moves and the
    default does not, this fails. Both edited together is a decision.

    What the mismatch costs is worth being precise about, because it has no
    other symptom: `mount_interface` finds no `index.html`, logs one warning
    into a stream nobody reads on a deployment that came up fine, and does not
    mount. The container starts, the healthcheck passes, `/api/health` answers
    JSON, and every page in the interface is a 404.
    """
    web_dir = configured(monkeypatch, tmp_path, dockerfile_env(dockerfile)).web_dir
    assert web_dir is not None, "the image serves no interface at all"
    stage = node_stage(dockerfile)

    landing = [copy for copy in copies(stages(dockerfile)[-1]) if copy.destination == str(web_dir)]
    assert landing, (
        f"nothing is copied to {web_dir}, which is where this image's own environment"
        " makes the server look; it would start, answer /api/health, and 404 every page"
    )
    for copy in landing:
        assert f"--from={stage.alias}" in copy.flags, (
            f"{web_dir} is filled from {copy.flags or 'the build context'} rather than from"
            f" the {stage.alias} stage, so it is not the build that was just made"
        )


def test_reading_the_images_environment_does_not_export_it_into_this_process(
    dockerfile, monkeypatch, tmp_path
):
    """The Dockerfile's `PATH` is not a `PATH`, and it must not become one here.

    `ENV PATH="/app/.venv/bin:$PATH"` is expanded by the *image*, so what
    `dockerfile_env` reads back is the literal string with a `$PATH` in it. The
    tests above hand that whole dictionary to `main`, and exporting it verbatim
    would put a directory that does not exist and an unexpanded `$PATH` on the
    front of the test runner's own.

    Inert today -- `main` shells out to nothing, `uvicorn.run` and `create_app`
    are both stubbed, and `monkeypatch` puts it back at teardown regardless --
    and precisely the sort of inert that stops being inert the first time
    anything on the startup path spawns a subprocess. So it is asserted rather
    than left to be noticed then.
    """
    import os

    before = os.environ.get("PATH")
    assert "PATH" in dockerfile_env(dockerfile), "the image no longer sets a PATH at all"

    configured(monkeypatch, tmp_path, dockerfile_env(dockerfile))

    assert os.environ.get("PATH") == before, (
        "reading the image's environment changed this process's PATH; the Dockerfile's"
        " value is the unexpanded literal `/app/.venv/bin:$PATH`"
    )


def test_what_is_copied_out_is_the_directory_vite_actually_writes(dockerfile):
    """`dist` is a default, not a promise, and the `COPY` spells it out.

    `web/vite.config.ts` sets no `build.outDir`, so the build lands in `dist`
    beside the config. Setting one is a normal-looking edit -- and it would move
    the assets out from under a `COPY` that still names `dist`, which builds
    fine and copies an empty directory. This is what fails then.
    """
    config = (ROOT / "web" / "vite.config.ts").read_text()
    assert not re.search(r"\boutDir\b", config), (
        "vite.config.ts now names an outDir; the Dockerfile's COPY still says dist"
    )

    stage = node_stage(dockerfile)
    workdir = re.findall(r"^WORKDIR (\S+)", stage.body, flags=re.MULTILINE)
    assert workdir, "the Node stage sets no WORKDIR, so the COPY out of it is a guess"

    out_of_the_stage = [
        source
        for copy in copies(stages(dockerfile)[-1])
        if f"--from={stage.alias}" in copy.flags
        for source in copy.sources
    ]
    assert out_of_the_stage, f"nothing is copied out of {stage.alias}"
    for source in out_of_the_stage:
        assert source.startswith(workdir[-1]), (
            f"{source} is outside the Node stage's WORKDIR {workdir[-1]}; `pnpm build`"
            " writes under the working directory and nowhere else"
        )
        assert source.rstrip("/").endswith("/dist"), f"{source} is not what `vite build` writes"


def test_the_node_stage_runs_the_build_the_repository_defines(dockerfile):
    """The step that produces the only thing this stage exists to produce.

    Every other assertion here is about *arrangement* -- what is copied before
    what, what lands where, which stage ships. Delete `RUN pnpm build` and the
    arrangement is all still perfect: a stage that installs 700 packages, copies
    the sources in and produces nothing, and a `COPY --from=interface
    /build/web/dist` that then fails the build. Loud, but the test that caught
    it was the one about layer caching, naming a step it does not own.

    Cross-checked against `web/package.json` rather than asserted as a string,
    because `pnpm build` in the Dockerfile means nothing on its own: it is a
    script name, and it is defined over there.
    """
    scripts = json.loads((ROOT / "web" / "package.json").read_text())["scripts"]
    assert "build" in scripts, "web/package.json has no `build` script for the image to run"
    assert "vite build" in scripts["build"], (
        "the `build` script no longer runs vite; the Dockerfile copies out vite's `dist`"
    )

    stage = instructions(node_stage(dockerfile).body)
    assert re.search(r"^RUN .*\bpnpm (?:run )?build\b", stage, flags=re.MULTILINE), (
        "the Node stage never runs `pnpm build`, so it installs the dependencies of an"
        " interface it does not build and the COPY out of it has nothing to copy"
    )


def test_the_interface_is_built_on_the_machine_doing_the_building(dockerfile):
    """`--platform=$BUILDPLATFORM`, or the documented Pi build emulates all of Node.

    `server/README.md` tells an operator to cross-build for the Pi with `buildx
    --platform linux/arm64`. Without this flag on the Node stage, that runs
    `pnpm install`, `tsc -b` *and* `vite build` under qemu-arm64 -- which is the
    slowest and by a distance the most fragile part of any cross-build -- to
    produce `dist`, which is HTML, JS and CSS and has no architecture in it at
    all. The builder stage above is the opposite case and must **not** carry
    this flag: it resolves and installs wheels for the target, and an amd64
    `.venv` copied into an arm64 runtime is an image that does not start.
    """
    assert re.match(r"#\s*syntax=", dockerfile), (
        "the `# syntax=` line is gone; $BUILDPLATFORM is a BuildKit built-in and that"
        " line is what guarantees BuildKit is parsing this file"
    )

    header = node_stage(dockerfile).body.splitlines()[0]
    assert "--platform=$BUILDPLATFORM" in header, (
        "the Node stage is not pinned to the build platform, so `buildx --platform"
        " linux/arm64` runs pnpm, tsc and vite under qemu to emit architecture-"
        "independent HTML and JS"
    )

    builder = stages(dockerfile)[0].splitlines()[0]
    assert "--platform" not in builder, (
        "the uv builder is pinned to the build platform too; it installs wheels for the"
        " target, and an amd64 .venv copied into an arm64 runtime does not start"
    )


def test_the_interface_is_installed_from_the_lock_file(dockerfile):
    """`pnpm install` without `--frozen-lockfile` resolves afresh, in the image.

    Which means the image can be built twice from one commit and get two
    different React minors -- and the failure surfaces as a rendering bug in a
    container nobody can reproduce, because the lockfile in the repository says
    something the image never installed. `--frozen-lockfile` makes a lockfile
    that disagrees with `package.json` a build failure instead.
    """
    stage = instructions(node_stage(dockerfile).body)

    installs = re.findall(r"pnpm (?:install|i|add)\b[^\n&|;]*", stage)
    assert installs, "the Node stage never installs anything"
    for install in installs:
        assert "--frozen-lockfile" in install, (
            f"`{install.strip()}` resolves dependencies afresh inside the image, so two"
            " builds of one commit can ship different code"
        )
    assert any(
        "pnpm-lock.yaml" in copy.destination or "pnpm-lock.yaml" in " ".join(copy.sources)
        for copy in copies(stage)
    ), "--frozen-lockfile with no lockfile copied in is a build that fails at install"


def test_the_lock_file_is_copied_before_the_sources(dockerfile):
    """Otherwise every edit to a component reinstalls every dependency.

    The same layer-caching argument the uv builder above is arranged around, and
    it is worth a test rather than a comment because the arrangement is the only
    thing that expresses it: a `COPY web/ web/` moved above the install is a
    one-line edit that changes nothing about what the image contains and several
    minutes about what it costs to rebuild.
    """
    stage = node_stage(dockerfile)
    lines = instructions(stage.body).splitlines()

    def first(what: str, predicate) -> int:
        for index, line in enumerate(lines):
            if predicate(line):
                return index
        # Named, because this test is about *order* and it is the one that
        # happens to look up every step: without the name, deleting `RUN pnpm
        # build` fails a test called "the lock file is copied before the
        # sources" over a missing build step, which sends the next person to
        # read the caching arrangement.
        raise AssertionError(f"the Node stage has no {what}")

    def copies_the_lock(line: str) -> bool:
        return line.startswith("COPY ") and "pnpm-lock.yaml" in line

    lock = first("COPY of pnpm-lock.yaml", copies_the_lock)
    install = first("`pnpm install`", lambda line: "pnpm install" in line)
    build = first("`pnpm build`", lambda line: "pnpm build" in line or "pnpm run build" in line)

    assert lock < install, "the lockfile is copied after the install that reads it"
    assert install < build, "the build runs before the dependencies are installed"
    sources = [
        index
        for index, line in enumerate(lines)
        if line.startswith("COPY ") and "pnpm-lock.yaml" not in line and "package.json" not in line
    ]
    assert sources, "the Node stage copies no sources, so it builds the previous build"
    assert min(sources) > install, (
        "the sources are copied before the install, so editing one component"
        " reinstalls every dependency"
    )


def test_the_stage_carries_the_answer_pnpm_11_demands_before_it_will_install(dockerfile):
    """`pnpm-workspace.yaml` is not optional here; without it the build exits 1.

    pnpm 11 refuses to run a dependency's install scripts until somebody has
    said so in writing, and until then every pnpm command fails with
    `ERR_PNPM_IGNORED_BUILDS`. The answer this project gives -- msw's
    `postinstall` writes a browser service worker nothing here uses -- lives in
    `web/pnpm-workspace.yaml`, because pnpm 11 reads it from there and not from
    `package.json`. Leave that file out of the build context and the image does
    not build; leave it out of the `COPY` and the image does not build either,
    which is at least the loud failure.
    """
    workspace = (ROOT / "web" / "pnpm-workspace.yaml").read_text()
    assert "allowBuilds" in workspace, (
        "the gate answer moved out of pnpm-workspace.yaml; the Dockerfile copies that file"
    )

    stage = instructions(node_stage(dockerfile).body)
    assert "pnpm-workspace.yaml" in stage, (
        "pnpm-workspace.yaml is never copied into the Node stage, so `pnpm install`"
        " exits 1 with ERR_PNPM_IGNORED_BUILDS over msw's postinstall"
    )


def test_the_image_builds_the_interface_with_the_pnpm_this_project_pins(dockerfile):
    """One version of pnpm, named in one place, which is `web/package.json`.

    CI resolves it with `pnpm/action-setup`, which reads `packageManager`; the
    image should get it from the same field rather than from a second literal
    that drifts. A literal is allowed if it agrees -- what is refused is a
    Dockerfile pinning a pnpm the repository does not use, because a different
    pnpm major reads `pnpm-lock.yaml` differently and `--frozen-lockfile` then
    fails on a lockfile that is perfectly current.
    """
    manifest = json.loads((ROOT / "web" / "package.json").read_text())
    name, _, version = manifest["packageManager"].partition("@")
    assert name == "pnpm", f"web/ is a {name} project now"

    stage = instructions(node_stage(dockerfile).body)
    literals = set(re.findall(r"pnpm@(\S+)", stage))
    if literals:
        assert literals == {version}, (
            f"the Dockerfile pins pnpm {literals} and web/package.json pins {version}"
        )
    else:
        assert "corepack" in stage, (
            "the stage neither pins a pnpm version nor uses corepack, so whichever pnpm"
            " the base image happens to carry builds the interface"
        )


def test_the_interface_is_built_on_the_node_ci_tests_it_on(dockerfile):
    """A major apart is a build that passes here and fails there, or worse.

    Vite and its plugins gate on the Node major; building the shipped assets on
    one and running the whole test suite on another means the artefact nobody
    tested is the one that ships.
    """
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    versions = {
        str(step["with"]["node-version"])
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "setup-node" in str(step.get("uses", "")) and "node-version" in (step.get("with") or {})
    }
    assert versions, "CI no longer pins a node-version; the assertion below has nothing to hold to"

    header = node_stage(dockerfile).body.splitlines()[0]
    major = re.search(r"node:(\d+)", header)
    assert major, f"`FROM {header}` does not pin a Node major"
    assert {major[1]} == versions, (
        f"the image builds the interface on Node {major[1]} and CI tests it on {versions}"
    )


def test_the_build_context_carries_the_interface_but_not_its_node_modules():
    """`.dockerignore`, which is the other file the Node stage depends on.

    Two failures, opposite in temperament. Excluding `web/` makes the build fail
    at the `COPY`, which is fine -- it is loud. Failing to exclude
    `node_modules` is the quiet one: the host's three hundred megabytes are sent
    to the daemon on every build and then copied *over* the ones `pnpm install`
    just made, so a cross-build for the Pi gets a tree of x86 binaries and the
    developer who has never run `pnpm install` gets a different image from the
    one who has.
    """
    patterns = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert not any(pattern.rstrip("/") == "web" for pattern in patterns), (
        "web/ is excluded from the build context; the Node stage has nothing to build"
    )
    assert any(re.fullmatch(r"(\*\*/|web/)?node_modules/?", pattern) for pattern in patterns), (
        "node_modules is not excluded; the host's copy is shipped to the daemon on every"
        " build and lands on top of the one the image installed"
    )


def test_the_build_context_leaves_the_previous_local_build_behind():
    """`web/dist`, the third line of `.dockerignore` the Node stage depends on.

    Small on purpose, and worth saying how small: vite's `emptyOutDir` defaults
    true for an `outDir` inside the project root, so `pnpm build` in the stage
    wipes a host `dist` copied in over it anyway. What this exclusion actually
    buys is that the host's build never enters the context at all -- it is not
    sent to the daemon, and it cannot be what ships if the stage ever stops
    rebuilding it. Untested until now, alongside two exclusions that were.
    """
    patterns = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert any(re.fullmatch(r"(\*\*/|web/)dist/?", pattern) for pattern in patterns), (
        "web/dist is not excluded from the build context; a local `pnpm build` is sent to"
        " the daemon on every build and lands on top of the one the image just made"
    )


def test_the_readme_says_where_the_interface_lives_in_the_image(dockerfile, monkeypatch, tmp_path):
    """An operator debugging a blank page needs the path, not the reasoning."""
    web_dir = configured(monkeypatch, tmp_path, dockerfile_env(dockerfile)).web_dir
    readme = (ROOT / "server" / "README.md").read_text()

    assert "ORS_WEB_DIR" in readme, "the variable that decides whether there is an interface"
    assert str(web_dir) in readme, f"the README never names {web_dir}"


# --------------------------------------------------------------------------
# The healthcheck
# --------------------------------------------------------------------------


def healthcheck_url(text: str) -> str:
    joined = re.sub(r"\\\n\s*", " ", text)
    found = re.search(r"HEALTHCHECK .*?(http://[^'\"\s]+)", joined, flags=re.DOTALL)
    assert found, "no HEALTHCHECK, or none naming a URL"
    return found[1]


def test_the_healthcheck_does_not_need_curl(dockerfile):
    body = instructions(dockerfile)

    assert "HEALTHCHECK" in body
    assert "curl" not in body, "python:3.12-slim has no curl; the check would always fail"
    assert "wget" not in body, "nor wget, for the same reason"


def test_the_healthcheck_asks_the_cheap_route_and_not_the_expensive_one(dockerfile):
    """`GET /api/daemons` assembles a snapshot per rack. This must not go there.

    Carried out of task 13: filling `config_error` costs a handful of SQLite
    reads and a pydantic validation *per daemon*, which is nothing once and a
    standing per-interval cost when a healthcheck asks for it every thirty
    seconds forever. `/api/health` reads no rows at all.
    """
    url = healthcheck_url(dockerfile)

    assert url.endswith("/api/health"), f"the healthcheck asks for {url}"
    assert "/api/daemons" not in instructions(dockerfile), (
        "GET /api/daemons assembles a snapshot per rack; it is not a liveness probe"
    )


def test_the_healthcheck_reaches_the_port_the_server_binds(dockerfile):
    environment = dockerfile_env(dockerfile)
    url = healthcheck_url(dockerfile)

    assert f":{environment['ORS_PORT']}/" in url, f"{url} is not the port this image binds"


def test_the_healthcheck_route_answers_without_a_session(tmp_path, dockerfile):
    """Asserted against the running app, not against the string in the file.

    Every route on the guarded router answers 401 without a cookie, and a
    healthcheck pointed at one of those reports a perfectly healthy server as
    unhealthy for ever. `/api/health` is on the public router today; this is
    what fails if it is ever moved.
    """
    path = "/" + healthcheck_url(dockerfile).split("/", 3)[3]
    app = create_app(AppSettings(data_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200, f"{path} answers {response.status_code} without a session"
    assert response.json()["status"] == "ok"


def test_the_healthcheck_reads_no_rows(tmp_path, dockerfile):
    """Cheap is the claim; this is the measurement.

    A liveness probe that runs every thirty seconds for the life of a
    deployment has to not touch the database, and the way that stops being true
    is somebody adding a "check the database too" clause to `health` -- which is
    a defensible-sounding change and exactly the one that makes the probe cost
    scale with the rack.
    """
    path = "/" + healthcheck_url(dockerfile).split("/", 3)[3]
    app = create_app(AppSettings(data_dir=tmp_path))

    statements: list[str] = []
    app.state.database.connect().set_trace_callback(statements.append)
    original = app.state.database.connect

    def counting_connect():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection

    app.state.database.connect = counting_connect
    with TestClient(app) as client:
        statements.clear()
        client.get(path)

    assert statements == [], f"the healthcheck ran {statements}"


# --------------------------------------------------------------------------
# The README an operator actually follows
# --------------------------------------------------------------------------


def test_the_readme_names_compose_files_that_exist():
    """The one part of a README a test can keep true.

    A rename that leaves the instructions pointing at a file that is gone is
    the failure mode here, and it is silent until somebody follows them.
    """
    readme = (ROOT / "server" / "README.md").read_text()

    named = set(re.findall(r"deploy/(\S+?\.yaml)", readme))
    assert named, "the README does not say which compose file to bring up"
    for name in named:
        assert (DEPLOY / name).exists(), f"the README names deploy/{name}, which does not exist"


def test_the_readme_says_the_secret_key_cannot_be_recovered():
    """The one sentence whose absence costs an operator every stored credential."""
    readme = (ROOT / "server" / "README.md").read_text().lower()

    assert "ors_secret_key" in readme
    assert "unrecoverable" in readme or "cannot be recovered" in readme
    assert "systemd" in readme, "the daemon is not in compose and the README has to say so"


def test_the_image_does_not_announce_itself_over_mdns(dockerfile: str) -> None:
    """`ORS_ANNOUNCE=0`, because from a bridge the announcement is worse than none.

    mDNS is link-layer -- multicast to 224.0.0.251 with a TTL of 1 -- and a
    Docker bridge is a NAT, so the datagram never reaches the LAN whatever
    address is written in it. What a bridged container would register is
    loopback and its own bridge address, neither of which names anything a rack
    can reach.

    That matters more than being merely useless. A rack that finds nothing is
    told to pass `--server`, which is the message that names the fix; a rack
    that finds an unreachable entry files a claim that fails instead, and two
    such entries trip the "more than one server, pair with none" refusal on a
    LAN that has exactly one.

    `deploy/compose.mdns.yaml` turns it back on alongside `network_mode: host`.
    Both halves are needed and neither is enough: host networking without the
    variable is a silent server, and the variable without host networking is
    the announcement this line exists to suppress.
    """
    assert dockerfile_env(dockerfile)["ORS_ANNOUNCE"] == "0"
