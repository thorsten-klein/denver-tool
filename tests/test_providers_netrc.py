"""Tests for providers.netrc.NetrcProvider. No test sends a request: netrc_verify.check is faked."""

from __future__ import annotations

import logging
import sys

import pytest

from denver_errors import DenverError
from denver_providers import netrc_verify
from denver_providers.netrc import MAX_PROMPTS, NetrcProvider, ask_token, host_of

SECRET = "s3cr3t-token"


@pytest.fixture
def ctx_for(make_context, tmp_path):
    """Build a context holding the resolved config, like denver.py does."""

    def _make(stage_cfg=None, **kwargs):
        config = {"netrc": {"provider": "netrc", "path": str(tmp_path / "netrc"), **(stage_cfg or {})}}
        ctx = make_context(config=config, **kwargs)
        ctx.env_workdir = tmp_path / "state"
        return ctx

    return _make


def run(ctx):
    """Resolve the stage's defaults, then run its setup()."""
    provider = NetrcProvider(ctx.config)
    provider.stage = "netrc"
    ctx.config["netrc"] = NetrcProvider.resolve_defaults(ctx, ctx.config["netrc"], ctx.config)
    provider.setup(ctx)
    return provider


def resolved(ctx, **cfg):
    return NetrcProvider.resolve_defaults(ctx, cfg, {})


class FakeCheck:
    """Stands in for netrc_verify.check: answers by host, one label per call (the last one repeats)."""

    def __init__(self, monkeypatch, labels=None):
        self.labels = labels or {}
        self.calls = []
        monkeypatch.setattr(netrc_verify, "check", self)

    def __call__(self, entries, custom, timeout, expiry_warning):
        self.calls.append([e.host for e in entries])
        self.custom = custom
        out = []
        for e in entries:
            queue = self.labels.get(e.host, ["valid"])
            label = queue.pop(0) if len(queue) > 1 else queue[0]
            out.append(netrc_verify.Verdict(e.host, e.login, label, "note"))
        return out


@pytest.fixture
def fake_check(monkeypatch):
    return FakeCheck(monkeypatch)


def interactive(monkeypatch, answers):
    """Pretend there is a terminal; each prompt takes the next answer."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []

    def fake_ask(prompt):
        asked.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr("denver_providers.netrc.ask_token", fake_ask)
    return asked


def machine(url="artifacts.corp", token="${TOK}", **extra):
    return {"url": url, "username": "bot", "token": token, **extra}


# ---- helpers ----------------------------------------------------------------#
@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://a.b:8443/x/y", "a.b"),
        ("A.B", "a.b"),
        ("a.b/artifactory", "a.b"),
        ("https://", ""),
    ],
)
def test_host_of(url, host):
    assert host_of(url) == host


def test_ask_token_reads_without_echo(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt: f"  {SECRET} ")
    assert ask_token("?") == SECRET


# ---- config defaults ----------------------------------------------------------#
def test_defaults_fill_every_key(make_context, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = resolved(make_context())
    assert set(cfg) == set(NetrcProvider.KEYS)
    assert cfg["path"] == str(tmp_path / ".netrc")
    assert cfg["seed-from"] is None
    assert cfg["machines"] == []
    assert cfg["endpoints"] == {}
    assert cfg["verify"] is True
    assert cfg["prompt-interactive"] is True


def test_paths_resolve(make_context):
    ctx = make_context()
    cfg = resolved(ctx, path="sub/netrc", **{"seed-from": "seed"})
    assert cfg["path"] == str(ctx.env_dir / "sub" / "netrc")
    assert cfg["seed-from"] == str(ctx.env_dir / "seed")


def test_machine_is_complete_and_token_stays_raw(make_context):
    ctx = make_context(env={"USER_X": "bot"})
    cfg = resolved(ctx, machines=[{"url": "https://a.b/x", "username": "${USER_X}", "token": "${TOK}"}])
    assert cfg["machines"] == [
        {"url": "https://a.b/x", "username": "bot", "token": "${TOK}", "overwrite": False, "verify": None}
    ]


def test_endpoints_are_interpolated(make_context):
    ctx = make_context(env={"BASE": "https://x"})
    assert resolved(ctx, endpoints={"h": "${BASE}/ping"})["endpoints"] == {"h": "https://x/ping"}


@pytest.mark.parametrize(
    "cfg",
    [
        {"path": 1},
        {"path": " "},
        {"seed-from": 1},
        {"verify": "yes"},
        {"prompt-interactive": 1},
        {"expiry-warning": 1.5},
        {"expiry-warning": -1},
        {"expiry-warning": True},
        {"recheck-after": "1d"},
        {"timeout": 0},
        {"endpoints": ["h"]},
        {"endpoints": {"h": 1}},
        {"machines": "a"},
        {"machines": ["a"]},
        {"machines": [{"url": "a", "username": "u", "token": "t", "nope": 1}]},
        {"machines": [{"username": "u", "token": "t"}]},
        {"machines": [{"url": "a", "token": "t"}]},
        {"machines": [{"url": "a", "username": "u"}]},
        {"machines": [{"url": "https://", "username": "u", "token": "t"}]},
        {"machines": [{"url": "a", "username": "${UNSET_USER}", "token": "t"}]},
        {"machines": [{"url": "a", "username": "u", "token": "t", "overwrite": "yes"}]},
        {"machines": [{"url": "a", "username": "u", "token": "t", "verify": "no"}]},
        {
            "machines": [
                {"url": "a.b", "username": "u", "token": "t"},
                {"url": "https://A.B/x", "username": "v", "token": "t"},
            ]
        },
    ],
)
def test_bad_config_dies(make_context, cfg):
    ctx = make_context()
    with pytest.raises(DenverError):
        resolved(ctx, **cfg)


# ---- setup(): the file ----------------------------------------------------------#
def test_creates_file_and_exports(ctx_for, fake_check, tmp_path):
    ctx = ctx_for()
    run(ctx)
    path = tmp_path / "netrc"
    assert path.is_file()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ctx.env["DENVER_NETRC_FILE"] == str(path)
    assert ctx.env["NETRC"] == str(path)


def test_creates_parent_directories(ctx_for, fake_check, tmp_path):
    run(ctx_for({"path": str(tmp_path / "a" / "b" / "netrc")}))
    assert (tmp_path / "a" / "b" / "netrc").is_file()


def test_fixes_loose_permissions(ctx_for, fake_check, tmp_path):
    path = tmp_path / "netrc"
    path.write_text("")
    path.chmod(0o644)
    run(ctx_for())
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_directory_as_path_dies(ctx_for, tmp_path):
    (tmp_path / "netrc").mkdir()
    ctx = ctx_for()
    with pytest.raises(DenverError, match="is a directory"):
        run(ctx)


def test_seeded_once(ctx_for, fake_check, tmp_path):
    seed = tmp_path / "seed"
    seed.write_text("machine a login x password y\n")
    ctx = ctx_for({"seed-from": str(seed)})
    run(ctx)
    path = tmp_path / "netrc"
    assert path.read_text() == seed.read_text()
    path.write_text("machine b login x password z\n")
    run(ctx)
    assert path.read_text() == "machine b login x password z\n"  # not seeded again


def test_missing_seed_is_fine(ctx_for, fake_check, tmp_path):
    run(ctx_for({"seed-from": str(tmp_path / "absent")}))
    assert (tmp_path / "netrc").read_text() == ""


def test_seed_from_itself_is_fine(ctx_for, fake_check, tmp_path):
    run(ctx_for({"seed-from": str(tmp_path / "netrc")}))
    assert (tmp_path / "netrc").read_text() == ""


def test_invalid_netrc_dies(ctx_for, tmp_path):
    (tmp_path / "netrc").write_text("machine\n")
    ctx = ctx_for({"machines": [machine(token="t")]})
    with pytest.raises(DenverError, match="not a valid netrc"):
        run(ctx)


def test_inside_the_container_nothing_happens(ctx_for, tmp_path):
    ctx = ctx_for(in_container=True)
    run(ctx)
    assert not (tmp_path / "netrc").exists()
    assert "DENVER_NETRC_FILE" not in ctx.env


def test_dry_run_only_exports(ctx_for, tmp_path, capsys):
    ctx = ctx_for(dry_run=True)
    run(ctx)
    assert not (tmp_path / "netrc").exists()
    assert ctx.env["DENVER_NETRC_FILE"] == str(tmp_path / "netrc")
    assert "would prepare and verify" in capsys.readouterr().err


def test_fast_needs_the_file(ctx_for, fake_check, tmp_path):
    ctx = ctx_for(fast=True)
    with pytest.raises(DenverError, match="--fast"):
        run(ctx)
    (tmp_path / "netrc").write_text("")
    run(ctx_for(fast=True))
    assert fake_check.calls == []


# ---- setup(): machines ------------------------------------------------------------#
def test_machine_is_written(ctx_for, fake_check, tmp_path):
    run(ctx_for({"machines": [machine()]}, env={"TOK": SECRET}))
    assert (tmp_path / "netrc").read_text() == f"machine artifacts.corp login bot password {SECRET}\n"


def test_literal_token_works(ctx_for, fake_check, tmp_path):
    run(ctx_for({"machines": [machine(token="plain")]}))
    assert "password plain" in (tmp_path / "netrc").read_text()


def test_existing_entry_is_kept_without_overwrite(ctx_for, fake_check, tmp_path):
    (tmp_path / "netrc").write_text("# mine\nmachine artifacts.corp login old password oldpw\n")
    run(ctx_for({"machines": [machine()]}))  # TOK unset, but not needed
    assert (tmp_path / "netrc").read_text() == "# mine\nmachine artifacts.corp login old password oldpw\n"


def test_overwrite_replaces_and_keeps_the_rest(ctx_for, fake_check, tmp_path):
    (tmp_path / "netrc").write_text(
        "# mine\nmachine artifacts.corp login old password oldpw\nmachine other login o password p\n"
    )
    run(ctx_for({"machines": [machine(overwrite=True)]}, env={"TOK": SECRET}))
    assert (tmp_path / "netrc").read_text() == (
        f"# mine\nmachine artifacts.corp login bot password {SECRET}\nmachine other login o password p\n"
    )


def test_same_value_is_not_written_again(ctx_for, fake_check, tmp_path, monkeypatch, caplog):
    (tmp_path / "netrc").write_text(f"machine artifacts.corp login bot password {SECRET}\n")
    monkeypatch.setenv("TOK", SECRET)
    with caplog.at_level(logging.INFO, logger="denver"):
        run(ctx_for({"machines": [machine(overwrite=True)]}))
    assert "updated" not in caplog.text


def test_empty_token_dies_without_a_terminal(ctx_for):
    ctx = ctx_for({"machines": [machine()]})
    with pytest.raises(DenverError, match=r"\$\{TOK\} is empty"):
        run(ctx)


def test_empty_token_is_asked_for_on_a_terminal(ctx_for, fake_check, monkeypatch, tmp_path):
    asked = interactive(monkeypatch, [SECRET])
    run(ctx_for({"machines": [machine()]}))
    assert "bot@artifacts.corp" in asked[0]
    assert f"password {SECRET}" in (tmp_path / "netrc").read_text()


def test_empty_answer_still_dies(ctx_for, monkeypatch):
    interactive(monkeypatch, [""])
    ctx = ctx_for({"machines": [machine()]})
    with pytest.raises(DenverError, match="is empty"):
        run(ctx)


@pytest.mark.parametrize("how", ["ci", "off"])
def test_no_question_when_not_allowed(ctx_for, monkeypatch, how):
    asked = interactive(monkeypatch, [SECRET])
    ctx = ctx_for({"machines": [machine()], **({"prompt-interactive": False} if how == "off" else {})}, ci=how == "ci")
    with pytest.raises(DenverError):
        run(ctx)
    assert asked == []


@pytest.mark.parametrize("token", ["has space", 'has"quote', "back\\slash"])
def test_unsafe_token_dies_and_is_not_shown(ctx_for, token):
    ctx = ctx_for({"machines": [machine(token=token)]})
    with pytest.raises(DenverError) as error:
        run(ctx)
    assert token not in str(error.value)


def test_unsafe_username_dies(ctx_for):
    bad = machine(token="t")
    bad["username"] = "a b"
    ctx = ctx_for({"machines": [bad]})
    with pytest.raises(DenverError, match="username"):
        run(ctx)


def test_asked_token_is_checked_before_it_is_written(ctx_for, monkeypatch, tmp_path):
    check = FakeCheck(monkeypatch, {"artifacts.corp": ["REJECTED", "valid"]})
    asked = interactive(monkeypatch, ["bad", SECRET])
    run(ctx_for({"machines": [machine()]}))
    assert len(asked) == 2
    assert (tmp_path / "netrc").read_text() == f"machine artifacts.corp login bot password {SECRET}\n"
    assert check.calls[0] == ["artifacts.corp"]  # checked before anything was written


def test_asked_token_rejected_three_times_is_never_written(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"artifacts.corp": ["REJECTED"]})
    asked = interactive(monkeypatch, ["a", "b", "c", "d"])
    ctx = ctx_for({"machines": [machine()]})
    with pytest.raises(DenverError, match="no working token"):
        run(ctx)
    assert len(asked) == MAX_PROMPTS
    assert (tmp_path / "netrc").read_text() == ""


@pytest.mark.parametrize("off", ["provider", "machine"])
def test_asked_token_is_not_checked_when_verify_is_off(ctx_for, monkeypatch, tmp_path, off):
    check = FakeCheck(monkeypatch)
    interactive(monkeypatch, [SECRET])
    extra = {"verify": False} if off == "provider" else {}
    run(ctx_for({"machines": [machine(**({"verify": False} if off == "machine" else {}))], **extra}))
    assert SECRET in (tmp_path / "netrc").read_text()
    assert check.calls == []


# ---- setup(): verify -----------------------------------------------------------------#
def entry_file(tmp_path, text="machine h login u password p\n"):
    (tmp_path / "netrc").write_text(text)


def test_verify_off(ctx_for, fake_check, tmp_path):
    entry_file(tmp_path)
    run(ctx_for({"verify": False}))
    assert fake_check.calls == []


def test_nothing_to_verify(ctx_for, fake_check):
    run(ctx_for())
    assert fake_check.calls == []


def test_endpoints_are_passed_on(ctx_for, fake_check, tmp_path):
    entry_file(tmp_path)
    run(ctx_for({"endpoints": {"h": "https://x/ping"}}))
    assert fake_check.custom == {"h": "https://x/ping"}


def test_machine_with_verify_false_is_skipped(ctx_for, fake_check, tmp_path):
    entry_file(tmp_path, "machine h login u password p\nmachine skip login u password p\n")
    run(ctx_for({"machines": [machine("skip", "p", verify=False)]}))
    assert fake_check.calls == [["h"]]


def test_good_result_is_remembered(ctx_for, fake_check, tmp_path):
    entry_file(tmp_path)
    run(ctx_for())
    run(ctx_for())
    assert len(fake_check.calls) == 1  # second run: remembered


def test_changed_file_is_checked_again(ctx_for, fake_check, tmp_path):
    entry_file(tmp_path)
    run(ctx_for())
    entry_file(tmp_path, "machine h login u password q\n")
    run(ctx_for())
    assert len(fake_check.calls) == 2


def test_unchecked_is_not_remembered(ctx_for, monkeypatch, tmp_path):
    check = FakeCheck(monkeypatch, {"h": ["unchecked"]})
    entry_file(tmp_path)
    run(ctx_for())
    run(ctx_for())
    assert len(check.calls) == 2


def test_rejected_fails_without_a_terminal(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED"]})
    entry_file(tmp_path)
    ctx = ctx_for()
    with pytest.raises(DenverError, match="rejected for h") as error:
        run(ctx)
    assert "password p" not in str(error.value)


def test_rejected_token_is_replaced_on_a_terminal(ctx_for, monkeypatch, tmp_path):
    check = FakeCheck(monkeypatch, {"h": ["REJECTED", "valid"]})
    asked = interactive(monkeypatch, [SECRET])
    entry_file(tmp_path, "# keep\nmachine h login u password p\n")
    run(ctx_for())
    assert (tmp_path / "netrc").read_text() == f"# keep\nmachine h login u password {SECRET}\n"
    assert "u@h" in asked[0]
    assert check.calls == [["h"], ["h"]]


def test_skipping_the_question_still_fails(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED"]})
    interactive(monkeypatch, [""])
    entry_file(tmp_path)
    ctx = ctx_for()
    with pytest.raises(DenverError, match="rejected for h"):
        run(ctx)


def test_gives_up_after_three_tries_and_writes_nothing(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED"]})
    asked = interactive(monkeypatch, ["bad1", "bad2", "bad3", "bad4"])
    entry_file(tmp_path)
    ctx = ctx_for()
    with pytest.raises(DenverError, match="rejected for h"):
        run(ctx)
    assert len(asked) == MAX_PROMPTS
    assert (tmp_path / "netrc").read_text() == "machine h login u password p\n"


def test_a_later_try_is_saved_and_a_rejected_one_never(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED", "REJECTED", "valid"]})
    asked = interactive(monkeypatch, ["bad", SECRET])
    entry_file(tmp_path)
    run(ctx_for())
    assert len(asked) == 2
    assert (tmp_path / "netrc").read_text() == f"machine h login u password {SECRET}\n"


def test_unchecked_new_token_is_saved(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED", "unchecked"]})
    interactive(monkeypatch, [SECRET])
    entry_file(tmp_path)
    run(ctx_for())
    assert SECRET in (tmp_path / "netrc").read_text()


def test_only_the_renewed_host_is_checked_again(ctx_for, monkeypatch, tmp_path):
    check = FakeCheck(monkeypatch, {"a": ["REJECTED", "valid"], "b": ["REJECTED"]})
    interactive(monkeypatch, [SECRET, "", ""])
    entry_file(tmp_path, "machine a login u password p\nmachine b login u password p\n")
    ctx = ctx_for()
    with pytest.raises(DenverError, match="rejected for b"):
        run(ctx)
    assert check.calls[1] == ["a"]


def test_unsafe_new_token_dies(ctx_for, monkeypatch, tmp_path):
    FakeCheck(monkeypatch, {"h": ["REJECTED"]})
    interactive(monkeypatch, ["has space"])
    entry_file(tmp_path)
    ctx = ctx_for()
    with pytest.raises(DenverError, match="token contains"):
        run(ctx)


def test_warns_when_overwrite_will_undo_the_new_token(ctx_for, monkeypatch, tmp_path, caplog):
    FakeCheck(monkeypatch, {"h": ["REJECTED", "valid"]})
    interactive(monkeypatch, [SECRET])
    monkeypatch.setenv("TOK", "from-env")
    with caplog.at_level(logging.WARNING, logger="denver"):
        run(ctx_for({"machines": [machine("h", overwrite=True)]}))
    assert "overwrite: true" in caplog.text
    assert SECRET not in caplog.text
