"""Tests for netrc_file: editing machine entries in place."""

from __future__ import annotations

from denver_providers import netrc_file as nf


def test_has_machine():
    text = "machine a login x password y\n"
    assert nf.has_machine(text, "a")
    assert not nf.has_machine(text, "b")


def test_add_to_empty_file():
    assert nf.set_machine("", "a", "u", "p") == "machine a login u password p\n"


def test_add_after_text_without_newline():
    assert nf.set_machine("machine a login x password y", "b", "u", "p") == (
        "machine a login x password y\nmachine b login u password p\n"
    )


def test_replace_keeps_the_rest():
    text = "# top\nmachine a login x password y\n# about b\nmachine b login x password z\n"
    assert (
        nf.set_machine(text, "a", "u", "p")
        == "# top\nmachine a login u password p\n# about b\nmachine b login x password z\n"
    )


def test_replace_multiline_entry():
    text = "machine a\n  login x\n  password y\nmachine b login x password z\n"
    assert nf.set_machine(text, "a", "u", "p") == "machine a login u password p\nmachine b login x password z\n"


def test_replace_entries_on_one_line():
    text = "machine a login x password y machine b login x password z\n"
    assert nf.set_machine(text, "a", "u", "p") == "machine a login u password p\nmachine b login x password z\n"


def test_replace_last_entry():
    assert nf.set_machine("machine a login x password y\n", "a", "u", "p") == "machine a login u password p\n"


def test_new_entry_goes_before_default():
    text = "machine a login x password y\ndefault login d password q\n"
    assert nf.set_machine(text, "b", "u", "p") == (
        "machine a login x password y\nmachine b login u password p\ndefault login d password q\n"
    )


def test_a_password_named_machine_is_not_an_entry():
    text = "machine a login x password machine\n"
    assert nf.has_machine(text, "a")
    assert not nf.has_machine(text, "machine")


def test_comment_lines_are_ignored():
    text = "# machine a login x password y\nmachine b login x password z\n"
    assert not nf.has_machine(text, "a")


def test_unsafe_values():
    for bad in ("a b", 'a"b', "a'b", "a\\b"):
        assert nf.UNSAFE_VALUE.search(bad)
    assert not nf.UNSAFE_VALUE.search("ghp_abc-123.XYZ#!")
