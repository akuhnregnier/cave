import subprocess
import sys

import pytest

from cave import cli, collect, target
from cave._testing.repos import SID, raises


@pytest.mark.parametrize("argv,expected,label", [
    ([], [], None),
    (["run"], [], None),
    (["run", "--", "-p", "hi"], ["-p", "hi"], None),
    (["run", "--continue"], ["--continue"], None),
    (["--name", "x"], [], "x"),
    (["--name=x", "--", "-p"], ["-p"], "x"),
    (["run", "--continue", "--name", "x", "-p", "hi"], ["--continue", "-p", "hi"], "x"),
    (["run", "--", "--name", "x"], ["--name", "x"], None),
    (["--", "-p", "hi"], ["-p", "hi"], None),
])
def test_launch_arguments(launched, argv, expected, label):
    assert cli.dispatch(argv) == 7
    assert launched == [(expected, label)]


def test_a_typo_is_an_error_not_a_prompt(launched, capsys):
    with pytest.raises(SystemExit):
        cli.dispatch(["dif", "x"])
    assert launched == []


@pytest.mark.parametrize("flags, expected", [
    ([], (False, False, False)),
    (["--force"], (True, False, False)),
    (["--yes"], (False, True, False)),
    (["-p"], (False, False, True)),
])
def test_merge_passes_its_flags(monkeypatch, flags, expected):
    seen = []
    monkeypatch.setattr(collect, "merge", lambda sid, *rest: seen.append(rest) or True)
    assert cli.dispatch(["merge", SID, *flags]) == 0
    assert seen == [expected]


def test_diff_takes_patch_and_never_yes(monkeypatch):
    seen = []
    monkeypatch.setattr(collect, "diff", lambda sid, patch: seen.append(patch) or True)
    assert cli.dispatch(["diff", SID, "--patch"]) == 0
    assert seen == [True]
    with pytest.raises(SystemExit):
        cli.dispatch(["diff", SID, "--yes"])


@pytest.mark.parametrize("command", ["diff", "stop", "merge", "discard"])
def test_the_target_is_optional_and_discard_always_shows_it(monkeypatch, command):
    resolved = []
    monkeypatch.setattr(target, "resolve", lambda arg, always_show: resolved.append((arg, always_show)) or SID)
    monkeypatch.setattr(collect, command, lambda sid, *rest: sid == SID)
    assert cli.dispatch([command]) == 0
    assert resolved == [(None, command == "discard")]


def test_main_reports_a_failed_command(monkeypatch, capsys):
    monkeypatch.setattr(cli, "dispatch", raises(subprocess.CalledProcessError(1, ["git", "clone"])))
    assert cli.main() == 1
    assert "command failed: git clone" in capsys.readouterr().err


def test_main_refuses_other_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit, match="macOS only"):
        cli.main()
