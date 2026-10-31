import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from cave import git
from cave.errors import CaveError
from cave._testing.repos import clone_bundle, raises, run_git


def test_query_returns_empty_on_failure(repo):
    assert git.query(repo, "rev-parse", "--verify", "-q", "refs/heads/nope") == ""


def test_toplevel(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    assert git.toplevel() == str(repo.resolve())
    monkeypatch.chdir(tmp_path)
    assert git.toplevel() == ""


def test_base_branch_prefers_main(repo):
    run_git(repo, "branch", "master")
    assert git.base_branch(repo) == "main"


def test_base_branch_falls_back_to_master(repo):
    run_git(repo, "branch", "-m", "main", "master")
    assert git.base_branch(repo) == "master"


def test_base_branch_needs_a_commit(tmp_path):
    run_git(tmp_path, "init", "-q", "-b", "main")
    with pytest.raises(CaveError, match="no commits"):
        git.base_branch(tmp_path)


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/a/b.git", "https://github.com/a/b.git"),
    ("git@github.com:a/b.git", "https://github.com/a/b.git"),
    ("ssh://git@github.com/a/b.git", "https://github.com/a/b.git"),
    ("https://user:secret@example.com/a/b.git", ""),
    ("https://token@example.com/a/b.git", ""),
    ("https://example.com/a/b.git?private_token=x", ""),
    ("https://example.com/a/b.git#secret", ""),
    ("git@example.com:a/b.git?token=x", ""),
    ("/some/local/path", ""),
    ("http://example.com/a/b.git", ""),
    (None, ""),
])
def test_public_origin(repo, url, expected):
    if url:
        run_git(repo, "remote", "add", "origin", url)
    assert git.public_origin(repo) == expected


def test_a_bundle_holds_only_the_base_branch(repo, tmp_path):
    main = git.query(repo, "rev-parse", "main")
    run_git(repo, "checkout", "-q", "-b", "private-wip")
    (repo / "secret.txt").write_text("x")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "secret")
    private = git.query(repo, "rev-parse", "HEAD")
    run_git(repo, "tag", "v1")
    run_git(repo, "update-ref", "refs/cave/other/key", "HEAD")
    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "replace", main, private)  # not followed, or private history would go along
    run_git(repo, "config", "user.secret", "hunter2")
    bundle = tmp_path / "x.bundle"
    git.create_bundle(repo, bundle, "main")
    assert git.query(repo, "bundle", "list-heads", str(bundle)) == f"{main} refs/heads/main"
    assert b"hunter2" not in bundle.read_bytes()
    clone = clone_bundle(repo, tmp_path / "clone")
    assert git.query(clone, "cat-file", "-t", private) == ""
    assert git.query(clone, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main", "refs/remotes/origin/main"]
    assert not (tmp_path / "clone/secret.txt").exists()


def test_what_a_branch_tracks(repo):
    (repo / "dir").mkdir()
    (repo / "dir/f").write_text("x")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "x")
    commit = git.query(repo, "rev-parse", "HEAD")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},link")
    run_git(repo, "commit", "-qm", "gitlink")
    assert git.tracks(repo, "main", "dir") and git.tracks(repo, "main", "file.txt")
    assert not git.tracks(repo, "main", "link")
    assert not git.tracks(repo, "main", "absent")


def test_submodules_and_lfs_are_found_on_the_base_branch_only(repo):
    assert git.left_out(repo, "main") == []
    run_git(repo, "checkout", "-q", "-b", "other")
    (repo / "sub").mkdir()
    (repo / "sub/.gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (repo / ".gitmodules").write_text("")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "x")
    assert git.left_out(repo, "main") == []
    assert git.left_out(repo, "other") == ["submodules", "Git LFS files"]


def fake_git_version(monkeypatch, output):
    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output, returncode=0))


@pytest.mark.parametrize("output", [
    "git version 2.50.1 (Apple Git-155)\n", "git version 2.50.2\n", "git version 2.51.0\n", "git version 3.0.0\n",
])
def test_tested_or_newer_git_is_accepted(monkeypatch, output):
    fake_git_version(monkeypatch, output)
    git.require_version()


@pytest.mark.parametrize("output", ["git version 2.50.0\n", "git version 2.9.5\n", "git version 1.99.9\n"])
def test_older_git_is_refused(monkeypatch, output):
    fake_git_version(monkeypatch, output)
    with pytest.raises(CaveError, match="2.50.1 or newer"):
        git.require_version()


def test_unreadable_git_version_is_refused(monkeypatch):
    fake_git_version(monkeypatch, "not git\n")
    with pytest.raises(CaveError, match="could not read"):
        git.require_version()


def test_missing_git_is_reported(monkeypatch):
    monkeypatch.setattr(git.subprocess, "run", raises(FileNotFoundError("git")))
    with pytest.raises(CaveError, match="git not found"):
        git.require_version()


def test_compare_and_swap_refuses_a_ref_that_moved(repo):
    head = git.strict(repo, "rev-parse", "HEAD")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "moved")
    moved = git.strict(repo, "rev-parse", "HEAD")
    assert not git.compare_and_swap(repo, "refs/heads/main", head, head)
    assert git.strict(repo, "rev-parse", "HEAD") == moved
    assert git.compare_and_swap(repo, "refs/heads/main", head, moved)


def test_refs_are_listed_and_deleted_per_sandbox(repo):
    keep, drop = "20260101-000000-aaaa", "20260101-000000-bbbb"
    for sid, name in ((keep, "a"), (drop, "a"), (drop, "b")):
        run_git(repo, "update-ref", f"refs/cave/{sid}/{name}", "HEAD")
    assert git.sandbox_ref_ids(repo) == [keep, drop]
    git.delete_sandbox_refs(repo, drop)
    git.delete_sandbox_refs(repo, drop)  # already gone: no error
    assert git.sandbox_ref_ids(repo) == [keep]


def test_a_stuck_fetch_is_killed_with_its_whole_process_group(repo, tmp_path, monkeypatch):
    pids = tmp_path / "pid"
    stuck = tmp_path / "stuck-git"
    stuck.write_text(f"#!/bin/bash\nsleep 300 &\necho $! > {pids}\nwait\n")
    stuck.chmod(0o755)
    monkeypatch.setattr(git, "NO_HOOKS", [str(stuck)])
    monkeypatch.setattr(git, "FETCH_TIMEOUT", 1)
    assert not git.fetch_bundle(repo, tmp_path / "bundle", "refs/heads/x", "refs/cave/x")
    pid = pids.read_text().strip()
    deadline = time.time() + 5
    while time.time() < deadline and subprocess.run(["ps", "-p", pid], capture_output=True).returncode == 0:
        time.sleep(0.05)
    assert subprocess.run(["ps", "-p", pid], capture_output=True).returncode != 0


def test_output_that_is_not_utf8_is_survivable(repo, tmp_path):
    message = tmp_path / "message"
    message.write_bytes(b"caf\xe9\n")
    run_git(repo, "commit", "-q", "--allow-empty", "-F", str(message))
    assert git.query(repo, "log", "-1", "--format=%s").startswith("caf")


def test_a_slow_git_is_stopped_with_a_clear_error():
    with pytest.raises(CaveError, match="rebasing it took over 0.2 seconds"):
        git._bounded(["sleep", "5"], 0.2, "rebasing it")
    assert git._bounded(["echo", "hi"], 5, "x").stdout == "hi\n"


def test_reading_a_bundle_has_a_timeout(repo, tmp_path, monkeypatch):
    bundle = tmp_path / "b"
    git.create_bundle(repo, bundle, "main")
    monkeypatch.setattr(git, "VERIFY_TIMEOUT", 0)
    with pytest.raises(CaveError, match="reading its bundle took over"):
        git.bundle_heads(repo, bundle)


def test_raw_with_a_limit_stops_git_and_says_it_was_cut(repo):
    (repo / "big").write_text("x" * 10000)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "big")
    assert len(git.raw(repo, "show", "HEAD", limit=100)) == 101
    assert len(git.raw(repo, "show", "HEAD", limit=1 << 20)) < 1 << 20


def test_the_listing_limit_counts_bytes_not_characters(repo, monkeypatch):
    run_git(repo, "checkout", "-q", "-b", "bomb")
    (repo / ("é" * 100)).write_text("x")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "bomb")
    size = len(git.raw(repo, "log", "--no-merges", "--no-renames", "-r", "--raw", "-z", "--format=", "main..bomb"))
    monkeypatch.setattr(git, "MAX_LISTING", size - 1)  # over by a byte, though under in characters
    assert git.written_bytes(repo, "main", "bomb") > git.MAX_WRITTEN
    monkeypatch.setattr(git, "MAX_LISTING", size)
    assert git.written_bytes(repo, "main", "bomb") == 1


def test_a_listing_that_takes_too_long_counts_as_over_the_limit(repo, monkeypatch, tmp_path):
    slow = tmp_path / "bin"
    slow.mkdir()
    (slow / "git").write_text("#!/bin/sh\nsleep 30\n")
    (slow / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{slow}:{os.environ['PATH']}")
    monkeypatch.setattr(git, "LISTING_TIMEOUT", 0.3)
    started = time.time()
    assert git.written_bytes(repo, "main", "HEAD") > git.MAX_WRITTEN
    assert time.time() - started < 10
