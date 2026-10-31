import io
import sys

import pytest

from cave import sandbox, target
from cave._testing.repos import make_repo, repo_entry, run_git
from cave.errors import CaveError
from cave.paths import box_path

OLD, NEW = "20260101-000000-aaaa", "20260102-000000-bbbb"


def add_box(sid, root, repos=None, name=None):
    box_path(sid).mkdir(parents=True)
    entries = [repo_entry(f"code{i}", repo) for i, repo in enumerate(repos or [root])]
    sandbox.write_meta(sid, {"id": sid, "name": name or f"cave-p-{sid[-4:]}", "root": str(root), "repos": entries})


@pytest.fixture
def tree(isolated_env, tmp_path, monkeypatch):
    """plain/{a,b} are repos, plain/c a folder. A sandbox rooted at plain holds only a."""
    plain = tmp_path / "plain"
    plain.mkdir()
    a = make_repo(plain / "a")
    make_repo(plain / "b")
    (plain / "c/d").mkdir(parents=True)
    (a / "deep").mkdir()
    add_box(OLD, plain, [a])
    monkeypatch.chdir(plain)
    return plain


@pytest.mark.parametrize("folder,show", [
    (".", False),         # root
    ("c/d", True),        # below the root, in no repo
    ("a", True),          # a repo it includes
    ("a/deep", True),     # inside that repo
])
def test_a_single_match(tree, monkeypatch, capfd, folder, show):
    monkeypatch.chdir(tree / folder)
    assert target.resolve() == OLD
    assert ("using sandbox cave-p-aaaa" in capfd.readouterr().err) is show


def test_a_repo_the_sandbox_does_not_hold_is_not_covered_by_its_root(tree, monkeypatch):
    monkeypatch.chdir(tree / "b")
    with pytest.raises(CaveError, match="no sandbox covers"):
        target.resolve()


def test_a_sibling_is_not_matched(tree, monkeypatch):
    sandbox.rmtree(box_path(OLD))
    add_box(NEW, tree / "c")
    monkeypatch.chdir(tree / "a")
    with pytest.raises(CaveError, match="no sandbox covers"):
        target.resolve()


def test_a_sandbox_below_the_folder_is_never_targeted(tree):
    sandbox.rmtree(box_path(OLD))
    add_box(OLD, tree / "c")
    with pytest.raises(CaveError, match="no sandbox covers"):
        target.resolve()


def test_a_symlinked_folder_still_matches(tree, tmp_path, monkeypatch):
    (tmp_path / "link").symlink_to(tree)
    monkeypatch.chdir(tmp_path / "link")
    assert target.resolve() == OLD


def test_discard_always_shows_the_target(tree, capfd):
    target.resolve(always_show=True)
    assert "using sandbox cave-p-aaaa" in capfd.readouterr().err
    target.resolve(OLD, always_show=True)
    assert f"root {tree}" in capfd.readouterr().err


def test_several_without_a_terminal_lists_them_nearest_first(tree, monkeypatch):
    add_box(NEW, tree / "c")
    monkeypatch.chdir(tree / "c/d")
    with pytest.raises(CaveError, match="several sandboxes") as error:
        target.resolve()
    message = str(error.value)
    assert message.index("cave-p-bbbb") < message.index("cave-p-aaaa")
    assert str(tree / "a") not in message and "repos: code0" in message


@pytest.mark.parametrize("reply,expected", [("1\n", NEW), ("2\n", OLD), ("3\n", None), ("\n", None), ("", None)])
def test_the_picker(tree, monkeypatch, capsys, reply, expected):
    add_box(NEW, tree / "c")
    monkeypatch.chdir(tree / "c")
    stdin = io.StringIO(reply)
    stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    if expected:
        assert target.resolve() == expected
    else:
        with pytest.raises(CaveError, match="no sandbox chosen"):
            target.resolve()


def test_newest_comes_first_among_equals(tree):
    add_box(NEW, tree)
    with pytest.raises(CaveError) as error:
        target.resolve()
    assert str(error.value).index("cave-p-bbbb") < str(error.value).index("cave-p-aaaa")


@pytest.mark.parametrize("arg,expected", [
    (OLD, OLD),
    ("20260102", NEW),
    ("cave-p-b", NEW),
    ("cave-p-long", "20260103-000000-cccc"),    # exact, though it also starts the next one
    ("cave-p-longer", "20260104-000000-dddd"),
    ("20260109-000000-ffff", "20260109-000000-ffff"),  # a full id passes through, for orphan refs
])
def test_an_argument_works_from_anywhere(tree, tmp_path, monkeypatch, arg, expected):
    add_box(NEW, tree)
    add_box("20260103-000000-cccc", tree, name="cave-p-long")
    add_box("20260104-000000-dddd", tree, name="cave-p-longer")
    monkeypatch.chdir(tmp_path)
    assert target.resolve(arg) == expected


@pytest.mark.parametrize("arg,match", [
    ("2026", "matches several"), ("cave-p", "matches several"),
    ("", "not a sandbox id"), ("nope", "not a sandbox id"), ("../victim", "not a sandbox id"),
])
def test_a_bad_argument(tree, arg, match):
    add_box(NEW, tree)
    with pytest.raises(CaveError, match=match):
        target.resolve(arg)


def test_a_corrupt_sandbox_is_still_found_by_id_prefix_and_skipped_otherwise(tree):
    (box_path(OLD) / "meta.json").write_text("{")
    assert target.resolve("20260101") == OLD
    with pytest.raises(CaveError, match="no sandbox covers"):
        target.resolve()


def test_the_config_repo_counts_as_a_repo_the_sandbox_includes(tree, tmp_path, monkeypatch):
    config = tmp_path / "config"
    run_git(tree, "clone", "-q", str(tree / "a"), str(config))
    add_box(NEW, tree / "b", [tree / "b", config])
    monkeypatch.chdir(config)
    assert target.resolve() == NEW
