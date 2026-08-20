"""How the shipped server is actually served.

Everything else about the server is tested through `create_app` and a test
client, which never goes near uvicorn -- so the transport settings, which are
the ones a browser and a rack meet first, had nothing asserting them at all.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest
import uvicorn
import uvicorn.config
from fastapi import FastAPI
from ors_schema.link import MAX_FRAME_BYTES
from ors_server.__main__ import WS_MAX_MESSAGE_BYTES, main
from ors_server.app import AppSettings

_TOUCHED = ("ors_server", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging():
    """`main` calls `setup_logging`, which is global and outlives the test.

    It sets `ors_server` to INFO with `propagate = False`, and a suite that let
    that stand would change what every later test's `caplog` can see -- which it
    did, in `test_ws_daemon`, as two failures about a record that had never been
    emitted before. The same fixture guards `test_logging` for the same reason.
    """
    saved = [(logging.getLogger(name), list(logging.getLogger(name).handlers)) for name in _TOUCHED]
    levels = [(logger, logger.level, logger.propagate) for logger, _ in saved]
    yield
    for logger, handlers in saved:
        logger.handlers[:] = handlers
    for logger, level, propagate in levels:
        logger.setLevel(level)
        logger.propagate = propagate


def served(monkeypatch, tmp_path) -> dict[str, Any]:
    """The keyword arguments `main` hands uvicorn, without binding a port."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.append(kwargs))
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    # `[]` and not `None`: `main` parses its argument list now, and `None` means
    # "read `sys.argv`", which under pytest is pytest's own command line. The
    # empty list is what the console script's `sys.argv[1:]` actually is when
    # somebody types `ors-server` -- `test_bare_ors_server_with_no_arguments_
    # still_runs_the_server` is what pins that the two agree.
    assert main([]) == 0

    return captured[0]


def settings_assembled(monkeypatch, tmp_path) -> AppSettings:
    """The `AppSettings` `main` builds out of the environment, and no app at all.

    `create_app` is replaced rather than run: the point of these two tests is
    which *path* `main` chose, and building the real app against it would either
    warn about a missing interface or, on a machine that happens to have one
    there, serve it -- neither of which is what is being asked.
    """
    captured: list[AppSettings] = []

    def capture(settings: AppSettings) -> FastAPI:
        captured.append(settings)
        return FastAPI()

    monkeypatch.setattr("ors_server.__main__.create_app", capture)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    assert main([]) == 0

    return captured[0]


def test_the_interface_defaults_to_the_packaged_copy_when_main_assembles_settings(
    monkeypatch, tmp_path
) -> None:
    """`main` wires `resolve_web_dir` into `AppSettings`, not a literal path.

    Not `/app/web`: that path exists only inside the container, and `main`
    itself has no business knowing it. `deploy/Dockerfile` pins
    `ORS_WEB_DIR=/app/web` explicitly instead, which is what
    `test_deploy.py::test_the_built_interface_lands_where_the_server_looks_for_it`
    checks agrees with the image's own `COPY`. A checkout that wants its own
    build sets `ORS_WEB_DIR=web/dist`; `create_app` warns once and serves the
    API alone when the directory holds no build, which is the ordinary
    developer state.
    """
    from ors_server.__main__ import packaged_web_dir

    monkeypatch.delenv("ORS_WEB_DIR", raising=False)

    assert settings_assembled(monkeypatch, tmp_path).web_dir == packaged_web_dir()


def test_the_interface_directory_can_be_moved_by_the_environment(monkeypatch, tmp_path) -> None:
    """`ORS_WEB_DIR=web/dist` is what a developer serving their own build sets,
    and what an image that lays the file system out differently would."""
    monkeypatch.setenv("ORS_WEB_DIR", str(tmp_path / "elsewhere" / "dist"))

    assert settings_assembled(monkeypatch, tmp_path).web_dir == tmp_path / "elsewhere" / "dist"


def test_the_server_bounds_a_websocket_message_rather_than_inheriting_16_mib(
    monkeypatch, tmp_path
) -> None:
    """The bound that runs before the application sees anything.

    `Frame.webp`'s `max_length` counts the decoded payload, so it can only
    refuse a message this process has already read and base64-decoded. Left at
    uvicorn's default that is 16 MiB of allocation per message, at whatever rate
    a peer can write them, for anybody who can open `/ws/daemon` -- and the
    daemon on the other end of a valid key is one of them.
    """
    assert served(monkeypatch, tmp_path)["ws_max_size"] == WS_MAX_MESSAGE_BYTES

    default = inspect.signature(uvicorn.config.Config.__init__).parameters["ws_max_size"].default
    assert default == 16 * 1024 * 1024, "uvicorn's default moved; re-read this decision"
    assert WS_MAX_MESSAGE_BYTES < default


def test_a_frame_the_schema_allows_still_fits_on_the_wire() -> None:
    """The bound is above `MAX_FRAME_BYTES` and not equal to it, deliberately.

    Base64 is 4/3 of what it carries, so a payload at the schema's bound is
    349,528 bytes before the JSON envelope. A transport limit set to the
    schema's number would close the socket over a frame the schema accepts,
    which is exactly the reconnect loop `MAX_FRAME_BYTES` exists to prevent --
    reached from the transport instead of from the encoder.
    """
    on_the_wire = -(-MAX_FRAME_BYTES // 3) * 4

    assert on_the_wire == 349_528
    assert WS_MAX_MESSAGE_BYTES > on_the_wire


def test_the_server_reads_forwarded_headers_from_a_reverse_proxy(monkeypatch, tmp_path) -> None:
    """`FORWARDED_ALLOW_IPS` is inert without this, and it is what the deploy
    notes tell an operator to set. It is already uvicorn's default, so what is
    being pinned is that a future release flipping it cannot silently turn every
    client address in this deployment into the proxy's own."""
    assert served(monkeypatch, tmp_path)["proxy_headers"] is True

    default = inspect.signature(uvicorn.config.Config.__init__).parameters["proxy_headers"].default
    assert default is True, "uvicorn's default moved; the deploy notes depend on this"


def test_the_access_log_is_off_because_a_claim_id_is_a_bearer_credential(
    monkeypatch, tmp_path
) -> None:
    """`GET /api/racks/claims/{claim_id}` puts a bearer credential in a URL.

    That is the design (spec S6.3 step 4): a rack that has not been approved
    holds nothing to authenticate with, so the claim id in the path *is* the
    authentication, and `ClaimFiled` says it exactly once for that reason --
    `PendingClaim` omits it on the same principle. uvicorn's access log
    defaults to on and said it again on every poll:

        INFO: 127.0.0.1:45252 - "GET /api/racks/claims/2POtYYc... 200 OK

    into `StandardOutput=journal` under the generated unit and into `docker
    logs` under the image, so anyone who could read a log held the credential
    of every rack that ever paired.

    The default is asserted alongside, exactly as `ws_max_size` and
    `proxy_headers` do above: this is only worth passing because it is *not*
    what uvicorn does on its own, and the day that changes this line is dead
    weight that reads as a guarantee.
    """
    assert served(monkeypatch, tmp_path)["access_log"] is False

    default = inspect.signature(uvicorn.config.Config.__init__).parameters["access_log"].default
    assert default is True, "uvicorn's default moved; re-read this decision"


def test_an_empty_port_is_the_default_and_not_a_restart_loop(monkeypatch, tmp_path) -> None:
    """`ORS_PORT=` is a typo, not an answer -- the rule the two resolvers above
    already follow (`5d3771c`).

    It matters more here than there, because of where the failure lands.
    `int("")` raised out of `resolve_port` before a single line was logged, and
    the unit `ors-server install` generates is `Restart=always` with
    `RestartSec=5` and `StartLimitIntervalSec=0`: a service that crashes on
    every start under that never latches into `failed`, so `systemctl
    is-failed` answers no and the only symptom is a journal nobody is watching.

    A value that is present and not a number still raises, and that is the
    other half of this: `ORS_PORT=8O8O` was meant to be a port, and binding
    8080 instead would announce one number to every rack on the LAN while the
    operator reads another in their config.
    """
    from ors_server.__main__ import resolve_port

    monkeypatch.setenv("ORS_PORT", "")
    assert resolve_port() == 8080
    monkeypatch.setenv("ORS_PORT", "   ")
    assert resolve_port() == 8080

    monkeypatch.setenv("ORS_PORT", "8O8O")
    with pytest.raises(ValueError):
        resolve_port()


def test_a_missing_binary_is_an_exit_code_and_not_a_traceback(monkeypatch) -> None:
    """`_SubprocessRunner` is the one `install.Runner` that touches the
    machine, and it used to raise straight through `install()`.

    Realistic, not exotic: astral's installer puts `uv` in `~/.local/bin` and
    `sudo` resets PATH to `secure_path`, so `sudo ors-server install` finds
    `useradd` in `/usr/sbin`, creates the system user and `/var/lib/ors-server`
    at 0700, and then dies on `uv venv` with a `FileNotFoundError` that never
    names `uv` -- half-configured, and with none of `install()`'s warnings
    printed. `README.md` promises the opposite for exactly that scenario.

    `subprocess.run` is replaced with one that raises rather than a real
    command that is really missing, so this stays inside the rule the rest of
    this suite is written under: nothing here executes anything.
    """
    from ors_server.__main__ import _SubprocessRunner

    def fake_run(argv, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr("ors_server.__main__.subprocess.run", fake_run)

    assert _SubprocessRunner().run(["uv", "venv", "/opt/ors-server"]) == 127


def test_the_web_directory_defaults_to_the_one_inside_the_wheel(monkeypatch):
    """Not `/app/web`, which exists only in the container.

    A pip-installed server whose `web_dir` pointed at a container path would
    serve `/api/*`, pass its health check, and answer 404 for every page --
    the exact shape of the failure a stale published image already produced
    on this project once, and one nobody diagnoses quickly, because the
    server looks healthy from every angle except a browser.
    """
    from ors_server.__main__ import packaged_web_dir, resolve_web_dir

    monkeypatch.delenv("ORS_WEB_DIR", raising=False)
    assert resolve_web_dir() == packaged_web_dir()
    # And it is inside the installed package, not beside the repository.
    assert packaged_web_dir().name == "web"
    assert packaged_web_dir().parent.name == "ors_server"


def test_the_environment_still_wins_over_the_packaged_directory(monkeypatch, tmp_path):
    """The container sets it explicitly, and must keep winning.

    Pinned because the packaged default is the *new* behaviour: a resolution
    order that consulted the wheel first would make `ORS_WEB_DIR` dead in
    every deployment that sets it, and the Dockerfile is one of those.
    """
    from ors_server.__main__ import resolve_web_dir

    monkeypatch.setenv("ORS_WEB_DIR", str(tmp_path))
    assert resolve_web_dir() == tmp_path


def test_the_data_directory_defaults_somewhere_a_normal_user_can_write(monkeypatch, tmp_path):
    """`/var/lib/openrackscreen` needs root, and `uv tool install` is not root.

    The whole point of publishing to PyPI is that `uv tool install ors-server
    && ors-server` works. A default that raises PermissionError on the first
    boot for anybody who has not read the environment table makes that false.
    """
    from ors_server.__main__ import resolve_data_dir

    monkeypatch.delenv("ORS_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert resolve_data_dir() == tmp_path / "openrackscreen"


def test_the_data_directory_falls_back_to_local_state_without_xdg(monkeypatch, tmp_path):
    from ors_server.__main__ import resolve_data_dir

    monkeypatch.delenv("ORS_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_data_dir() == tmp_path / ".local" / "state" / "openrackscreen"


def test_the_environment_still_wins_for_the_data_directory(monkeypatch, tmp_path):
    """The container and the systemd unit both set it, and both must keep winning."""
    from ors_server.__main__ import resolve_data_dir

    # XDG_STATE_HOME is set too, and deliberately not to the answer this test
    # asserts: on a host that happens to leave it unset, checking ORS_DATA_DIR
    # first and checking it second both land on the same path, and a reversed
    # resolution order would pass this test by accident rather than be caught
    # by it.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path / "explicit"))
    assert resolve_data_dir() == tmp_path / "explicit"


def test_an_empty_environment_variable_is_not_an_answer(monkeypatch, tmp_path):
    """`- ORS_DATA_DIR=` in a compose file, or `Environment=ORS_WEB_DIR=` in a
    unit, is a typo that reaches the process as an empty string rather than as
    an unset variable.

    Both resolvers treat it as unset and fall through to their default, which
    is the safe reading -- `Path("")` is `Path(".")`, so honouring it would put
    the database in whatever directory the server happened to be started from
    and look for the interface there too. Safe, and until now unpinned in
    either function: `if from_environment:` swapped for
    `if from_environment is not None:` passed every other test in this file.
    """
    from ors_server.__main__ import packaged_web_dir, resolve_data_dir, resolve_web_dir

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ORS_DATA_DIR", "")
    monkeypatch.setenv("ORS_WEB_DIR", "")

    assert resolve_data_dir() == tmp_path / "state" / "openrackscreen"
    assert resolve_web_dir() == packaged_web_dir()


def test_the_port_uvicorn_binds_is_the_port_the_announcement_will_name(monkeypatch, tmp_path):
    """One read of `ORS_PORT`, reaching both places that need it.

    They are not interchangeable and they cannot disagree: uvicorn binds the
    port, and `AppSettings.port` is the number the mDNS announcement tells every
    rack on the network to dial. Two reads is one number written twice, and the
    way two such numbers stop agreeing -- a default changed in one of them --
    produces a server that works perfectly for anybody typing its URL and is
    unpairable by discovery, which no test on this machine would have seen.
    """
    monkeypatch.setenv("ORS_PORT", "9443")
    assert served(monkeypatch, tmp_path)["port"] == 9443

    monkeypatch.setenv("ORS_PORT", "9443")
    assert settings_assembled(monkeypatch, tmp_path).port == 9443


def test_the_port_defaults_to_the_one_the_deploy_notes_publish(monkeypatch, tmp_path):
    """8080, in both places, when nothing says otherwise. `server/README.md`
    and `deploy/Dockerfile` both name it, and `AppSettings.port` defaults to the
    same constant `resolve_port` falls back to."""
    monkeypatch.delenv("ORS_PORT", raising=False)
    assert served(monkeypatch, tmp_path)["port"] == 8080

    monkeypatch.delenv("ORS_PORT", raising=False)
    assert settings_assembled(monkeypatch, tmp_path).port == 8080


# --- the argument list itself ----------------------------------------------
#
# `main` grew an `ArgumentParser` in M3c, for `install` and `uninstall`. Before
# that it read the environment and called `uvicorn.run`, and every deployment
# that exists -- the image's `CMD ["ors-server"]`, the `uv tool install`
# instructions in `server/README.md`, the generated systemd unit's `ExecStart`
# -- invokes it with no arguments at all. These three tests are what keep the
# parser from taking that away.


def _explode_if_served(monkeypatch) -> None:
    def boom(app, **kwargs):
        raise AssertionError("uvicorn.run reached")

    monkeypatch.setattr(uvicorn, "run", boom)


def test_bare_ors_server_with_no_arguments_still_runs_the_server(monkeypatch, tmp_path) -> None:
    """`ors-server`, exactly that, with nothing after it.

    It is what `deploy/Dockerfile`'s `CMD ["ors-server"]` runs, what
    `server/README.md` documents for a `uv tool install`, and what the
    `ExecStart=` of the unit `ors-server install` writes names -- there is no
    `serve` subcommand for any of them to say instead. A parser that made the
    subcommand required, or that printed usage when none was given, would break
    every one of those at once, and the container's symptom would be a
    restarting service with a usage message in its log.

    Driven through `sys.argv` rather than by passing `[]`, because `None` is
    what the console script actually hands `main` and the fallback from `None`
    to `sys.argv[1:]` is part of what is being pinned.
    """
    import sys

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(sys, "argv", ["ors-server"])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.append(kwargs))
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))

    assert main() == 0
    assert captured, "bare `ors-server` did not run the server"


def test_a_subcommand_typed_at_the_shell_is_actually_read_from_sys_argv(
    monkeypatch, capsys, tmp_path
) -> None:
    """The other half of the `argv=None` fallback, and the half that was not
    pinned.

    `test_bare_ors_server_with_no_arguments_still_runs_the_server` drives
    `sys.argv = ["ors-server"]`, whose `[1:]` is `[]` -- indistinguishable from
    a `main(argv=())` default that ignores `sys.argv` entirely. Under that
    mutant `ors-server install --port 9443` typed at a shell silently starts a
    server on 8080 instead of installing anything. A subcommand is the only
    argv that tells the two apart, and `uninstall` without root is the shortest
    one that reaches a distinguishable answer without touching the machine: the
    root guard returns 2 before a `Roots` is built or a command is run.
    """
    import os
    import sys

    class Boom:
        def run(self, argv: list[str]) -> int:
            raise AssertionError(f"runner reached: {argv}")

    # Three guards, and none of them optional. The point of this test is that a
    # broken `argv` default sends `uninstall` somewhere it should never get to,
    # so every one of those destinations has to raise rather than run: `_serve`
    # would bind a port, `_real_roots` names `/etc/systemd/system` and
    # `/var/lib`, and `_SubprocessRunner` would `systemctl stop` this machine's
    # own units. A test that only asserted the return code would, under exactly
    # the mutant it exists to catch, reconfigure the machine running it.
    _explode_if_served(monkeypatch)
    monkeypatch.setattr(
        "ors_server.__main__._real_roots",
        lambda prefix: (_ for _ in ()).throw(AssertionError(f"_real_roots reached: {prefix}")),
    )
    monkeypatch.setattr("ors_server.__main__._SubprocessRunner", Boom)
    monkeypatch.setenv("ORS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["ors-server", "uninstall"])
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    assert main() == 2
    assert "root" in capsys.readouterr().err.lower()


def test_help_prints_the_subcommands_instead_of_starting_the_server(monkeypatch, capsys) -> None:
    """`ors-server --help` used to start the server: `main` did not parse argv
    at all, so the flag was read by nobody and the process bound a port. It
    comes back as a plain `0` rather than as `SystemExit`, because `main`
    promises an `int` to `raise SystemExit(main())` at the bottom of the
    module."""
    _explode_if_served(monkeypatch)

    assert main(["--help"]) == 0

    text = capsys.readouterr().out
    assert "install" in text
    assert "uninstall" in text


def test_an_unknown_flag_is_a_usage_error_rather_than_a_started_server(monkeypatch, capsys) -> None:
    """Exit 2, the conventional "you typed it wrong" code -- and not a server
    on 8080 that silently ignored the flag, which is what a `main` with no
    parser did with anything anybody typed."""
    _explode_if_served(monkeypatch)

    assert main(["--nope"]) == 2

    assert "nope" in capsys.readouterr().err
