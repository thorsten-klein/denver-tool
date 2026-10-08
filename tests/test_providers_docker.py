"""Tests for providers.docker.DockerProvider."""

import json
import sys
import types
from pathlib import Path

import pytest

from denver_errors import DenverError
from denver_providers.docker import DockerProvider


def compose_config(volumes=None, service="dev", returncode=0, stderr=""):
    """A fake 'docker compose config' result with ``volumes`` on ``service``."""
    stdout = json.dumps({"services": {service: {"volumes": volumes or []}}})
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.fixture(autouse=True)
def _compose_config_without_volumes(run_recorder):
    """Answer 'compose config' with a service without mounts; bind-mount tests override it."""
    run_recorder.responses["config --format json"] = compose_config()


def run_docker(config, ctx, stage="docker"):
    """Resolve ``config[stage]``'s defaults exactly like denver.py's real
    pipeline would (see DockerProvider.resolve_defaults), then run the docker
    stage's setup() against it and return (ctx, provider)."""
    config[stage] = DockerProvider.resolve_defaults(ctx, config.get(stage) or {}, config)
    n = DockerProvider(config)
    n.stage = stage
    ctx.stage_id = stage  # denver.py sets this before setup()/wrap(); mirrored here for ctx.run(step=...)
    n.setup(ctx)
    return ctx, n


_COMPOSE_KEYS = {"file", "service", "build", "default-cmd", "image", "run-args", "check-bind-mounts"}


def docker_cfg(compose=None, **rest):
    """A minimal *explicit* docker section, nested the way denver.toml itself spells it.

    every test that expects setup() to get as far as compose has to name it,
    hence the 'file:' default below. Compose-level keys (file/service/build/
    default-cmd/image/run-args) passed via **rest are routed
    into the nested 'compose:' table automatically, same as passing them
    through ``compose=`` directly -- top-level keys (exe/registries) pass
    through onto the section itself.
    """
    section = {}
    compose_section = {"file": "docker-compose.yml", **(compose or {})}
    for key, value in rest.items():
        if key in _COMPOSE_KEYS:
            compose_section[key] = value
        else:
            section[key] = value
    section["compose"] = compose_section
    return section


def write_compose(ctx, name="docker-compose.yml"):
    p = ctx.env_dir / name
    p.write_text("services:\n  dev: {}\n")
    return p


# ---- guard clauses -------------------------------------------------------------#
def test_already_in_container_dies(make_context):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config, in_container=True)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_exe_missing_dies(make_context, which):
    which["docker"] = None
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_compose_v2_plugin_missing_dies(make_context, run_recorder, which):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["compose version"] = lambda cmd: type("R", (), {"returncode": 1})()
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_compose_file_missing_dies(make_context, run_recorder, which):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_compose_file_unconfigured_dies(make_context, which):
    # 'compose.file:' has no conventional default: even with a
    # docker-compose.yml sitting right next to the denver.toml, an env that
    # doesn't name it is a config error rather than a lucky guess.
    config = {"docker": {}}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_compose_unknown_key_dies(make_context, which):
    config = {"docker": docker_cfg(compose={"file": "docker-compose.yml", "bogus": True})}
    ctx = make_context(config=config)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


# ---- exe / build -----------------------------------------------------------------#
def test_exe_explicit(make_context, run_recorder, which):
    which["docker"] = None
    config = {"docker": docker_cfg(exe="/opt/docker", image="myapp:dev")}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_docker(config, ctx)
    assert any("/opt/docker compose" in c and "build" in c for c in run_recorder.commands())


def test_build_default_true(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev")}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_docker(config, ctx)
    assert any("build dev" in c for c in run_recorder.commands())


def test_build_never_runs_without_image(make_context, run_recorder, which, capsys):
    # 'compose.build: true' (the default) is a no-op without 'compose.image:' set --
    # there'd be nothing to check next run, so it would rebuild every time;
    # 'docker compose run' itself is left to build on demand instead.
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config, verbose=True)
    write_compose(ctx)
    run_docker(config, ctx)
    assert not any("build dev" in c for c in run_recorder.commands())
    assert "build (skipped: 'compose.image:' is not set)" in capsys.readouterr().err


def test_build_false_skips(make_context, run_recorder, which, capsys):
    config = {"docker": docker_cfg(compose={"build": False})}
    ctx = make_context(config=config, verbose=True)
    write_compose(ctx)
    run_docker(config, ctx)
    assert not any("build dev" in c for c in run_recorder.commands())
    assert "build (skipped: compose.build=false)" in capsys.readouterr().err


def test_fast_has_no_effect_on_build_decision(make_context, run_recorder, which):
    # --fast is not threaded through setup() at all here (unlike
    # uv/conan/zephyr): 'compose.build:' is read exactly as configured, so
    # a real build still runs under --fast, same as without it.
    config = {"docker": docker_cfg(image="myapp:dev")}
    ctx = make_context(config=config, fast=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    ctx, provider = run_docker(config, ctx)
    assert any("build dev" in c for c in run_recorder.commands())
    # relocation into the (freshly built) container still works as usual
    assert provider.wrap(ctx, ["echo", "hi"])[-2:] == ["echo", "hi"]


def test_compose_service(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", compose={"service": "custom"})}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_docker(config, ctx)
    build_argv = next(a for a in run_recorder.argvs() if "build" in a)
    assert build_argv[-2:] == ["build", "custom"]


def test_compose_file_list_produces_multiple_dash_f(make_context, run_recorder, which):
    config = {
        "docker": {
            "compose": {
                "file": ["docker-compose.yml", "docker-compose.override.yml"],
                "image": "myapp:dev",
            }
        }
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    write_compose(ctx, name="docker-compose.override.yml")
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    ctx, n = run_docker(config, ctx)
    build_argv = next(a for a in run_recorder.argvs() if "build" in a)
    assert build_argv.count("-f") == 2
    first = build_argv.index("-f")
    assert build_argv[first + 1] == str(ctx.env_dir / "docker-compose.yml")
    second = build_argv.index("-f", first + 1)
    assert build_argv[second + 1] == str(ctx.env_dir / "docker-compose.override.yml")

    cmd = n.wrap(ctx, ["fish"])
    assert cmd.count("-f") == 2


def test_compose_file_list_missing_file_dies(make_context, run_recorder, which):
    config = {"docker": {"compose": {"file": ["docker-compose.yml", "missing.yml"]}}}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


# ---- image / registries ------------------------------------------------------------------#
def test_registries_without_image_is_silently_ignored(make_context, run_recorder, which):
    # without 'image:', 'registries:' is ignored -- and so is the build,
    # since 'compose.build: true' also needs 'compose.image:' to mean anything here.
    config = {"docker": docker_cfg(**{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config)
    write_compose(ctx)

    run_docker(config, ctx)  # must not raise

    assert not any("manifest inspect" in c for c in run_recorder.commands())
    assert not any("build dev" in c for c in run_recorder.commands())


def test_registries_entry_without_url_dies(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"username": "u", "password": "p"}]})}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


@pytest.mark.parametrize("creds", [{"username": "u"}, {"password": "p"}], ids=["no-password", "no-username"])
def test_registries_entry_incomplete_credentials_dies(make_context, run_recorder, which, creds):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com", **creds}]})}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_image_found_locally_skips_build(make_context, run_recorder, which, capsys):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, verbose=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()

    run_docker(config, ctx)

    assert not any("manifest inspect" in c for c in run_recorder.commands())
    assert not any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "myapp:dev"  # unchanged: the local tag itself
    assert "found locally, skip build" in capsys.readouterr().err


def test_image_found_locally_skips_build_without_registries(make_context, run_recorder, which):
    # the local check runs whenever 'image:' is set, whether or not
    # 'registries:' is configured at all -- previously it required
    # 'registries:' to be non-empty, which meant a plain 'image:'-only env
    # rebuilt unconditionally every run even with nothing new to build.
    config = {"docker": docker_cfg(image="myapp:dev")}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()

    run_docker(config, ctx)

    assert not any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "myapp:dev"


def test_uses_first_registry_that_has_it(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{
                "registries": [
                    {"url": "registry1.example.com"},
                    {"url": "registry2.example.com"},
                    {"url": "registry3.example.com"},
                ]
            },
        )
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 1}
    )()
    run_recorder.responses["manifest inspect registry2.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 0}
    )()

    run_docker(config, ctx)

    commands = run_recorder.commands()
    assert any("manifest inspect registry1.example.com/myapp:dev" in c for c in commands)
    assert any("manifest inspect registry2.example.com/myapp:dev" in c for c in commands)
    assert not any("registry3.example.com" in c for c in commands)  # first hit wins, no further entry tried
    assert not any(" pull " in c for c in commands)  # setup() never runs a real pull
    assert not any("build dev" in c for c in commands)
    # $DENVER_DOCKER_IMAGE now points at the hit's own ref -- 'docker compose
    # run' pulls it lazily later, denver itself never does
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry2.example.com/myapp:dev"


def test_verbose_reports_every_checked_registry_and_its_stderr(make_context, run_recorder, which, capsys):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{"registries": [{"url": "registry1.example.com"}, {"url": "registry2.example.com"}]},
        )
    }
    ctx = make_context(config=config, verbose=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 1, "stderr": "unauthorized: authentication required\n"}
    )()
    run_recorder.responses["manifest inspect registry2.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 0}
    )()

    run_docker(config, ctx)

    err = capsys.readouterr().err
    assert "+ docker manifest inspect registry1.example.com/myapp:dev" in err
    assert "registry miss: 'registry1.example.com/myapp:dev' (exit 1)" in err
    assert "    unauthorized: authentication required" in err
    assert "+ docker manifest inspect registry2.example.com/myapp:dev" in err
    assert "registry hit: 'registry2.example.com/myapp:dev'" in err


def test_registry_checks_silent_without_verbose(make_context, run_recorder, which, capsys):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 1, "stderr": "boom\n"})()

    run_docker(config, ctx)

    err = capsys.readouterr().err
    assert "registry miss" not in err
    assert "boom" not in err


def test_all_registries_miss_falls_back_to_build(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{"registries": [{"url": "registry1.example.com"}, {"url": "registry2.example.com"}]},
        )
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 1})()

    run_docker(config, ctx)

    assert any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "myapp:dev"  # falls back to the local canonical tag


def test_all_registries_miss_and_build_false_dies(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            compose={"build": False}, image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]}
        )
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 1})()

    with pytest.raises(DenverError):
        run_docker(config, ctx)


def test_fast_still_resolves_registries_but_never_builds(make_context, run_recorder, which):
    # --fast skips the real `docker compose build` invocation, but the
    # local/registries lookup is a cheap read-only check, not a rebuild --
    # it still has to run so $DENVER_DOCKER_IMAGE ends up correct.
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, fast=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 0}
    )()

    run_docker(config, ctx)

    assert any("image inspect" in c for c in run_recorder.commands())
    assert any("manifest inspect registry1.example.com/myapp:dev" in c for c in run_recorder.commands())
    assert not any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry1.example.com/myapp:dev"


def test_fast_still_builds_when_registries_configured_and_nothing_found(make_context, run_recorder, which):
    # --fast has no special case: nothing found locally or on any
    # registry, and compose.build defaults true, so a real build still
    # runs -- exactly like a non-fast run would.
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, fast=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 1})()

    run_docker(config, ctx)

    assert any("build dev" in c for c in run_recorder.commands())


def test_fast_still_dies_when_registries_configured_nothing_found_and_build_false(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            compose={"build": False}, image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]}
        )
    }
    ctx = make_context(config=config, fast=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 1})()

    with pytest.raises(DenverError):
        run_docker(config, ctx)


# ---- registries: username/password (automated login) --------------------------------------#
def test_registry_login_runs_before_manifest_check(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{"registries": [{"url": "registry1.example.com", "username": "myuser", "password": "mysecret"}]},
        )
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": 0})()

    run_docker(config, ctx)

    commands = run_recorder.commands()
    login_idx = next(i for i, c in enumerate(commands) if "login registry1.example.com" in c)
    manifest_idx = next(i for i, c in enumerate(commands) if "manifest inspect registry1.example.com/myapp:dev" in c)
    assert login_idx < manifest_idx  # login happens before the check it's needed for
    assert "-u myuser" in commands[login_idx]
    assert "--password-stdin" in commands[login_idx]
    # the secret itself never appears in argv/echoed command text -- only via stdin
    assert not any("mysecret" in c for c in commands)
    login_call = run_recorder.calls[login_idx]
    assert login_call.kwargs["input"] == "mysecret"


def test_registry_login_password_from_env_var(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{"registries": [{"url": "docker.io", "username": "myuser", "password": "${DOCKER_PASSWORD_DOCKERHUB}"}]},
        )
    }
    ctx = make_context(config=config, env={"DOCKER_PASSWORD_DOCKERHUB": "from-env-secret"})
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()

    run_docker(config, ctx)

    login_call = next(c for c in run_recorder.calls if "login" in " ".join(str(p) for p in c.cmd))
    assert login_call.kwargs["input"] == "from-env-secret"


def test_registry_login_failure_dies_without_checking_manifest(make_context, run_recorder, which):
    config = {
        "docker": docker_cfg(
            image="myapp:dev",
            **{"registries": [{"url": "registry1.example.com", "username": "myuser", "password": "mysecret"}]},
        )
    }
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["login registry1.example.com"] = lambda cmd: type("R", (), {"returncode": 1})()

    with pytest.raises(DenverError):
        run_docker(config, ctx)

    assert not any("manifest inspect" in c for c in run_recorder.commands())


def test_registry_login_skipped_when_not_configured(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 0}
    )()

    run_docker(config, ctx)

    # whole args: the tmp path contains this test's name
    assert not any("login" in argv for argv in run_recorder.argvs())


# ---- authentication: true | false | may-fail ----------------------------------------------#
_PRIVATE_REGISTRY = {"url": "registry1.example.com", "username": "myuser", "password": "mysecret"}


def _auth_setup(make_context, run_recorder, registries, login_rc=1, manifest_rc=0, image_rc=1, **section):
    """A docker stage with ``registries``, a failing login (by default) and a local image miss -- returns ctx/config."""
    config = {"docker": docker_cfg(image="myapp:dev", registries=registries, **section)}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": image_rc})()
    run_recorder.responses["login registry1.example.com"] = lambda cmd: type("R", (), {"returncode": login_rc})()
    run_recorder.responses["manifest inspect"] = lambda cmd: type("R", (), {"returncode": manifest_rc})()
    return ctx, config


def test_authentication_default_is_true(make_context):
    resolved = DockerProvider.resolve_defaults(make_context(), docker_cfg(), {})
    assert resolved["authentication"] is True


@pytest.mark.parametrize("value", ["yes", "may_fail", 1], ids=["yes", "typo", "int"])
def test_authentication_invalid_value_dies(make_context, value):
    ctx = make_context()
    cfg = docker_cfg(authentication=value)
    with pytest.raises(DenverError, match="docker: 'authentication:' must be true, false or \"may-fail\""):
        DockerProvider.resolve_defaults(ctx, cfg, {})


def test_registry_authentication_invalid_value_dies(make_context, run_recorder, which):
    ctx, config = _auth_setup(make_context, run_recorder, [{**_PRIVATE_REGISTRY, "authentication": "maybe"}])
    with pytest.raises(DenverError, match=r"registries\[0\] \('registry1\.example\.com'\) 'authentication:' must be"):
        run_docker(config, ctx)


def test_authentication_false_never_logs_in_but_still_checks(make_context, run_recorder, which):
    ctx, config = _auth_setup(make_context, run_recorder, [_PRIVATE_REGISTRY], authentication=False)

    run_docker(config, ctx)

    commands = run_recorder.commands()
    assert not any(" login " in c for c in commands)
    assert any("manifest inspect registry1.example.com/myapp:dev" in c for c in commands)
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry1.example.com/myapp:dev"


def test_authentication_false_check_miss_falls_back_to_build(make_context, run_recorder, which):
    ctx, config = _auth_setup(make_context, run_recorder, [_PRIVATE_REGISTRY], manifest_rc=1, authentication=False)

    run_docker(config, ctx)

    assert any("compose" in c and " build dev" in c for c in run_recorder.commands())


def test_authentication_may_fail_login_failure_is_a_miss(make_context, run_recorder, which, caplog):
    ctx, config = _auth_setup(make_context, run_recorder, [_PRIVATE_REGISTRY], authentication="may-fail")

    run_docker(config, ctx)

    commands = run_recorder.commands()
    # the failed registry is never checked, and nothing else has it -> build
    assert not any("manifest inspect" in c for c in commands)
    assert any("compose" in c and " build dev" in c for c in commands)
    assert "login to 'registry1.example.com' failed -- treating it as a miss" in caplog.text


def test_authentication_may_fail_moves_on_to_the_next_registry(make_context, run_recorder, which):
    registries = [_PRIVATE_REGISTRY, {"url": "registry2.example.com"}]
    ctx, config = _auth_setup(make_context, run_recorder, registries, authentication="may-fail")

    run_docker(config, ctx)

    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry2.example.com/myapp:dev"


def test_authentication_may_fail_force_rebuilds_when_login_fails(make_context, run_recorder, which):
    # local hit + --force: the registry is checked, its login fails -> the forced rebuild still happens
    ctx, config = _auth_setup(make_context, run_recorder, [_PRIVATE_REGISTRY], image_rc=0, authentication="may-fail")
    ctx.force = True

    run_docker(config, ctx)

    assert any("compose" in c and " build dev" in c for c in run_recorder.commands())


def test_authentication_may_fail_dies_when_found_nowhere_and_build_false(make_context, run_recorder, which):
    ctx, config = _auth_setup(
        make_context, run_recorder, [_PRIVATE_REGISTRY], compose={"build": False}, authentication="may-fail"
    )

    with pytest.raises(DenverError, match="not found locally or on any of the configured registries"):
        run_docker(config, ctx)


def test_authentication_may_fail_successful_login_checks_registry(make_context, run_recorder, which):
    ctx, config = _auth_setup(make_context, run_recorder, [_PRIVATE_REGISTRY], login_rc=0, authentication="may-fail")

    run_docker(config, ctx)

    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry1.example.com/myapp:dev"


def test_registry_authentication_overrides_section(make_context, run_recorder, which):
    # section says may-fail, the entry says true -> its failing login is fatal again
    registries = [{**_PRIVATE_REGISTRY, "authentication": True}]
    ctx, config = _auth_setup(make_context, run_recorder, registries, authentication="may-fail")

    with pytest.raises(DenverError, match=r"login to 'registry1\.example\.com' failed"):
        run_docker(config, ctx)


def test_registry_authentication_false_overrides_section_default(make_context, run_recorder, which):
    ctx, config = _auth_setup(make_context, run_recorder, [{**_PRIVATE_REGISTRY, "authentication": False}])

    run_docker(config, ctx)

    assert not any(" login " in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry1.example.com/myapp:dev"


# ---- --force ----------------------------------------------------------------------------#
def test_force_rebuilds_local_hit_when_no_registries(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev")}
    ctx = make_context(config=config, force=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()

    run_docker(config, ctx)

    assert any("build dev" in c for c in run_recorder.commands())


def test_force_still_prefers_remote_hit_over_local_rebuild(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, force=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 0}
    )()

    run_docker(config, ctx)

    # --force still looks remotely even though the image is already local,
    # since a registry hit should win over a forced local rebuild
    assert any("manifest inspect registry1.example.com/myapp:dev" in c for c in run_recorder.commands())
    assert not any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "registry1.example.com/myapp:dev"


def test_force_rebuilds_when_remote_also_misses(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, force=True)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()
    run_recorder.responses["manifest inspect registry1.example.com/myapp:dev"] = lambda cmd: type(
        "R", (), {"returncode": 1}
    )()

    run_docker(config, ctx)

    assert any("manifest inspect registry1.example.com/myapp:dev" in c for c in run_recorder.commands())
    assert any("build dev" in c for c in run_recorder.commands())
    assert ctx.env["DENVER_DOCKER_IMAGE"] == "myapp:dev"  # remote missed too -- falls back to the local tag


def test_not_forced_skips_remote_check_on_local_hit(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev", **{"registries": [{"url": "registry1.example.com"}]})}
    ctx = make_context(config=config, force=False)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 0})()

    run_docker(config, ctx)

    assert not any("manifest inspect" in c for c in run_recorder.commands())


def test_denver_docker_image_exported_for_build(make_context, run_recorder, which):
    config = {"docker": docker_cfg(image="myapp:dev")}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()  # not present locally

    run_docker(config, ctx)

    build_call = next(c for c in run_recorder.calls if "build" in " ".join(str(p) for p in c.cmd))
    assert build_call.kwargs["env"]["DENVER_DOCKER_IMAGE"] == "myapp:dev"


def test_denver_docker_image_empty_when_unset(make_context, run_recorder, which):
    # no build runs without 'image:', so there's no build subprocess call to
    # inspect the env of -- check ctx.env directly instead.
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)

    run_docker(config, ctx)

    assert ctx.env["DENVER_DOCKER_IMAGE"] == ""


# ---- wrap() -----------------------------------------------------------------------------#
def test_wrap_before_setup_dies(make_context):
    n = DockerProvider({})
    ctx = make_context()
    with pytest.raises(DenverError):
        n.wrap(ctx, ["fish"])


def test_wrap_builds_run_command(make_context, run_recorder, which):
    config = {"docker": docker_cfg(compose={"service": "dev"}, **{"run-args": ["--rm", "-it"]})}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    cmd = n.wrap(ctx, ["fish", "-l"])
    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "--rm" in cmd
    assert "-it" in cmd
    assert cmd[-2:] == ["fish", "-l"]
    assert cmd[cmd.index("--workdir") + 1] == str(Path.cwd())


def test_wrap_tells_the_inner_denver_it_is_in_a_container(make_context, run_recorder, which):
    # a container's environment comes from the image and the compose file,
    # not from this process, so it has to be handed across explicitly --
    # otherwise the inner denver infers it from a runtime marker file that
    # only docker is guaranteed to write.
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    cmd = n.wrap(ctx, ["fish"])
    assert cmd[cmd.index("-e") + 1] == "DENVER_IN_CONTAINER=1"


def test_wrap_forwards_the_relocating_stage_ids(make_context, run_recorder, which):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    ctx.set("DENVER_RELOCATED", "docker")
    assert "DENVER_RELOCATED=docker" in n.wrap(ctx, ["fish"])


def test_wrap_forwards_cli_env_vars(make_context, run_recorder, which):
    # a container's environment comes from the image and the compose file,
    # not from this process -- so a -e/--env value has to be handed across
    # the boundary explicitly, the same way DENVER_RELOCATED is.
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    ctx.set("MY_VAR", "hello")
    ctx.set("DENVER_CLI_ENV_VAR_NAMES", "MY_VAR")
    cmd = n.wrap(ctx, ["fish"])
    assert cmd[cmd.index("MY_VAR=hello") - 1] == "-e"


def test_wrap_forwards_no_cli_env_vars_by_default(make_context, run_recorder, which):
    # nothing named in ctx.env is forwarded unless it went through -e/--env
    # (see DENVER_CLI_ENV_VAR_NAMES) -- forwarding ctx.env wholesale would
    # leak the whole host environment into the container.
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    ctx.set("MY_VAR", "hello")
    cmd = n.wrap(ctx, ["fish"])
    assert not any("MY_VAR" in str(part) for part in cmd)


def test_wrap_shows_run_banner(make_context, run_recorder, which, capsys):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config, verbose=True)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    capsys.readouterr()  # discard setup()'s own banner

    n.wrap(ctx, ["fish"])

    assert "run" in capsys.readouterr().err


def test_wrap_workdir_is_invocation_cwd_not_image_default(make_context, run_recorder, which, monkeypatch, tmp_path):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    monkeypatch.chdir(tmp_path)
    cmd = n.wrap(ctx, ["echo"])
    assert cmd[cmd.index("--workdir") + 1] == str(tmp_path)


# ---- relocation mounts ----------------------------------------------------------------------#
def test_wrap_does_not_mount_denver_when_it_runs_from_the_workspace(make_context, run_recorder, which, monkeypatch):
    # the invocation dir is bind-mounted at the same absolute path already
    # (that is what --workdir relies on), so a checkout or an editable install
    # is reachable inside the container without any help
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    monkeypatch.chdir(ctx.denver_pkg_dir.parent)

    assert "-v" not in n.wrap(ctx, ["echo"])

    monkeypatch.chdir(ctx.denver_pkg_dir)
    assert "-v" not in n.wrap(ctx, ["echo"])


def test_wrap_mounts_an_installed_denver_at_its_own_path(make_context, run_recorder, which, monkeypatch, tmp_path):
    # a wheel's site-packages is nowhere near the workspace, so the inner
    # denver would have nothing to re-invoke
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    site_packages = tmp_path / "venv" / "site-packages"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(ctx, "denver_pkg_dir", site_packages)
    monkeypatch.chdir(workspace)

    cmd = n.wrap(ctx, ["echo"])

    assert cmd[cmd.index("-v") + 1] == f"{site_packages}:{site_packages}:ro"


def test_wrap_mounts_the_frozen_executable_itself(make_context, run_recorder, which, monkeypatch, tmp_path):
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    exe = tmp_path / "usr" / "local" / "bin" / "denver"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.chdir(ctx.env_dir)

    cmd = n.wrap(ctx, ["echo"])

    # the executable, not its package dir: a one-file build has no importable
    # tree on disk to mount (see denver.py's reinvoke_command)
    assert cmd[cmd.index("-v") + 1] == f"{exe.resolve()}:{exe.resolve()}:ro"
    # ...and it lands before the service name, as a `docker compose run` flag
    assert cmd.index("-v") < cmd.index("dev")


# ---- UID/GID seeding ------------------------------------------------------------------------#
def test_uid_gid_defaults_not_overridden(make_context, run_recorder, which):
    config = {"docker": docker_cfg(**{"run-args": ["--label", "uid=${UID}"]})}
    ctx = make_context(config=config, env={"UID": "9999"})
    write_compose(ctx)
    ctx, n = run_docker(config, ctx)
    cmd = n.wrap(ctx, ["fish"])
    # UID was already set in the environment, so setdefault must not clobber it
    # before the "docker:" section is interpolated
    assert "uid=9999" in cmd


# ---- bind mount check ------------------------------------------------------------#
def test_bind_mount_check_runs_compose_config_with_every_file(make_context, run_recorder, which):
    """The check runs 'compose config' with every compose file."""
    config = {"docker": docker_cfg(file=["docker-compose.yml", "override.yml"])}
    ctx = make_context(config=config)
    write_compose(ctx)
    write_compose(ctx, "override.yml")
    run_docker(config, ctx)
    argvs = [argv for argv in run_recorder.argvs() if "config" in argv]
    assert argvs == [
        [
            "docker",
            "compose",
            "-f",
            str(ctx.env_dir / "docker-compose.yml"),
            "-f",
            str(ctx.env_dir / "override.yml"),
            "config",
            "--format",
            "json",
        ]
    ]


def test_bind_mount_check_passes_for_existing_sources(make_context, run_recorder, which, tmp_path):
    """Existing sources and non-bind entries pass."""
    existing_dir = tmp_path / "dir"
    existing_dir.mkdir()
    existing_file = tmp_path / "file"
    existing_file.write_text("")
    run_recorder.responses["config --format json"] = compose_config([
        {"type": "bind", "source": str(existing_dir), "target": "/d"},
        {"type": "bind", "source": str(existing_file), "target": "/f"},
        {"type": "volume", "source": "named", "target": "/n"},
        {"type": "tmpfs", "target": "/t"},
        "/legacy:/string",
    ])
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_docker(config, ctx)


def test_bind_mount_check_dies_naming_every_missing_source(make_context, run_recorder, which, tmp_path, caplog):
    """Every missing source is named, and nothing is built."""
    missing_a = tmp_path / "missing-a"
    missing_b = tmp_path / ".gitconfig"
    run_recorder.responses["config --format json"] = compose_config([
        {"type": "bind", "source": str(missing_a), "target": "/a"},
        {"type": "bind", "source": str(missing_b), "target": "/home/user/.gitconfig"},
    ])
    config = {"docker": docker_cfg(image="img:dev")}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_recorder.responses["image inspect"] = lambda cmd: type("R", (), {"returncode": 1})()
    with pytest.raises(DenverError) as exc:
        run_docker(config, ctx)
    message = str(exc.value)
    assert f"{missing_a} (mounted at /a)" in message
    assert f"{missing_b} (mounted at /home/user/.gitconfig)" in message
    assert "check-bind-mounts: false" in message
    assert not any("build" in argv for argv in run_recorder.argvs())


def test_bind_mount_check_only_looks_at_the_run_service(make_context, run_recorder, which, tmp_path):
    """Other services are not checked."""
    stdout = json.dumps({
        "services": {
            "dev": {"volumes": []},
            "other": {"volumes": [{"type": "bind", "source": str(tmp_path / "missing"), "target": "/m"}]},
        }
    })
    run_recorder.responses["config --format json"] = types.SimpleNamespace(stdout=stdout, returncode=0, stderr="")
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_docker(config, ctx)


def test_bind_mount_check_tolerates_a_service_without_volumes(make_context, run_recorder, which):
    """No services or no volumes is fine."""
    run_recorder.responses["config --format json"] = types.SimpleNamespace(stdout="{}", returncode=0, stderr="")
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_docker(config, ctx)


def test_bind_mount_check_disabled_skips_compose_config(make_context, run_recorder, which, tmp_path):
    """'check-bind-mounts: false' skips the check."""
    run_recorder.responses["config --format json"] = compose_config([
        {"type": "bind", "source": str(tmp_path / "missing"), "target": "/m"}
    ])
    config = {"docker": docker_cfg(**{"check-bind-mounts": False})}
    ctx = make_context(config=config)
    write_compose(ctx)
    run_docker(config, ctx)
    assert not any("config" in argv for argv in run_recorder.argvs())


def test_bind_mount_check_compose_config_failure_dies(make_context, run_recorder, which):
    """A failing 'compose config' dies with its stderr."""
    run_recorder.responses["config --format json"] = compose_config(returncode=1, stderr="yaml: line 3: oops\n")
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError, match="yaml: line 3: oops"):
        run_docker(config, ctx)


def test_bind_mount_check_invalid_json_dies(make_context, run_recorder, which):
    """Output that is not JSON dies with a clear error."""
    run_recorder.responses["config --format json"] = types.SimpleNamespace(stdout="services:", returncode=0, stderr="")
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config)
    write_compose(ctx)
    with pytest.raises(DenverError, match="invalid JSON"):
        run_docker(config, ctx)


@pytest.mark.parametrize(
    "result",
    [
        types.SimpleNamespace(stdout="", returncode=1, stderr="docker: not found"),
        types.SimpleNamespace(stdout="", returncode=0, stderr=""),
    ],
)
def test_bind_mount_check_skipped_under_dry_run_without_a_usable_answer(make_context, run_recorder, which, result):
    """Under --dry-run, no answer from compose skips the check."""
    run_recorder.responses["config --format json"] = result
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config, dry_run=True)
    write_compose(ctx)
    run_docker(config, ctx)


def test_bind_mount_check_still_dies_under_dry_run_on_missing_source(make_context, run_recorder, which, tmp_path):
    """Under --dry-run, a real answer is still checked."""
    run_recorder.responses["config --format json"] = compose_config([
        {"type": "bind", "source": str(tmp_path / "missing"), "target": "/m"}
    ])
    config = {"docker": docker_cfg()}
    ctx = make_context(config=config, dry_run=True)
    write_compose(ctx)
    with pytest.raises(DenverError, match="missing bind mount source"):
        run_docker(config, ctx)


def test_bind_mount_check_default_is_on(make_context):
    """The check is on by default."""
    ctx = make_context(config={})
    resolved = DockerProvider.resolve_defaults(ctx, docker_cfg(), {})
    assert resolved["compose"]["check-bind-mounts"] is True
