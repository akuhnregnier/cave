"""The real cave-companion script, run against real repos."""
import json
import os
import subprocess

import pytest

from cave import git, sandbox
from cave._testing.harness import COMPANION
from cave._testing.repos import SID, make_clone, make_repo, run_companion, run_git
from cave.paths import box_path

BRANCH = f"claude/{SID}"


@pytest.fixture
def vm(tmp_path, repo, box):
    """A clone on claude/<id>, one commit ahead of the delivered main."""
    clone = make_clone(repo, tmp_path / "clone", SID)
    (clone / ".git/info").mkdir(exist_ok=True)
    return clone


def companion(clone, mode="--final", **others):
    return run_companion(SID, mode, {"code": clone, **others})


def report(kind, key="code"):
    return sandbox.repo_report(SID, key, kind)


def subject(clone):
    return git.query(clone, "log", "-1", "--format=%s", BRANCH)


def diverge(clone):
    """A branch `other` that changes what claude/<id> changed, ahead of its parent."""
    run_git(clone, "checkout", "-q", "-b", "other", "HEAD~1")
    (clone / "file.txt").write_text("other\n")
    run_git(clone, "commit", "-qam", "other")


def start_conflict(clone):
    """Leave a merge stopped on a conflict on claude/<id>."""
    diverge(clone)
    run_git(clone, "checkout", "-q", BRANCH)
    git.query(clone, "merge", "other")
    assert (clone / ".git/MERGE_HEAD").exists()


def start_rebase(clone):
    """Leave a rebase stopped on a conflict, off claude/<id>."""
    diverge(clone)
    git.query(clone, "rebase", BRANCH)
    assert (clone / ".git/rebase-merge").exists()


def test_final_commits_leftovers_bundles_and_marks_finished(vm, repo):
    (vm / "new.txt").write_text("x\n")
    assert companion(vm).returncode == 0
    assert subject(vm) == "WIP"
    assert sandbox.finished(SID, "code")
    assert git.bundle_heads(repo, report("bundle")).endswith(f" refs/heads/{BRANCH}")
    assert not list((box_path(SID) / "outbox").glob(".*"))


def test_bundle_mode_takes_committed_work_only(vm, repo):
    (vm / "new.txt").write_text("x\n")
    assert companion(vm, "bundle").returncode == 0
    assert subject(vm) == "work"
    assert git.query(vm, "status", "--porcelain", "new.txt") == "?? new.txt"
    assert report("bundle").exists() and not sandbox.finished(SID, "code")


def test_a_branch_with_nothing_new_reports_none(vm):
    run_git(vm, "reset", "-q", "--hard", "refs/cave/delivered")
    companion(vm, "bundle")
    assert report("none").exists() and not report("bundle").exists()
    run_git(vm, "commit", "-q", "--allow-empty", "-m", "more")
    companion(vm, "bundle")
    assert report("bundle").exists() and not report("none").exists()


def test_moving_main_in_the_vm_changes_nothing_about_the_bundle(vm, repo):
    run_git(vm, "branch", "-f", "main", BRANCH)
    companion(vm, "bundle")
    assert git.bundle_heads(repo, report("bundle"))


def test_no_mode_is_an_error_not_an_unbound_variable(vm):
    assert companion(vm, "").returncode == 2


@pytest.mark.parametrize("mode", ["bundle", "watch"])
def test_before_setup_finishes_it_fails(tmp_path, mode):
    env = dict(os.environ, CAVE_VM_HOME=str(tmp_path))
    result = subprocess.run(["bash", str(COMPANION), mode], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "isn't ready" in result.stderr


@pytest.mark.parametrize("mode", ["--final", "bundle"])
@pytest.mark.parametrize("start", [start_conflict, start_rebase])
def test_a_repo_mid_operation_is_skipped_and_listed(tmp_path, vm, start, mode):
    other = make_repo(tmp_path / "other")
    run_git(other, "update-ref", "refs/cave/delivered", "HEAD")
    run_git(other, "checkout", "-q", "-b", BRANCH)
    run_git(other, "commit", "-q", "--allow-empty", "-m", "more")
    companion(vm, "bundle", second=other)  # a bundle from before, which must not survive
    start(vm)
    tip = git.query(vm, "rev-parse", BRANCH)
    (vm / "work.txt").write_text("work\n")
    result = companion(vm, mode, second=other)
    assert result.returncode == 0  # it did all it could, so this is no failure
    assert f"{vm} has a merge or rebase in progress" in result.stderr
    assert sandbox.mid_operation(SID, "code")
    assert not report("bundle").exists() and not sandbox.finished(SID, "code")
    assert git.query(vm, "rev-parse", BRANCH) == tip
    assert git.query(vm, "status", "--porcelain", "work.txt") == "?? work.txt"
    assert report("bundle", "second").exists()


def test_finishing_the_operation_clears_the_report(vm):
    start_conflict(vm)
    companion(vm, "bundle")
    run_git(vm, "merge", "--abort")
    companion(vm, "bundle")
    assert not sandbox.mid_operation(SID, "code") and report("bundle").exists()


@pytest.mark.parametrize("leave", [["checkout", "-q", "main"], ["checkout", "-q", "--detach"],
                                   ["checkout", "-q", "-b", "feature"]])
def test_a_session_ending_off_its_branch_is_held_back_rather_than_committed_over(vm, leave):
    tip = git.query(vm, "rev-parse", BRANCH)
    run_git(vm, *leave)
    (vm / "left.txt").write_text("x\n")
    companion(vm, "bundle")
    assert report("bundle").exists() and not sandbox.mid_operation(SID, "code")  # the branch itself is intact
    assert companion(vm).returncode == 0
    assert sandbox.mid_operation(SID, "code") and not report("bundle").exists() and not sandbox.finished(SID, "code")
    assert git.query(vm, "rev-parse", BRANCH) == tip
    run_git(vm, "checkout", "-q", BRANCH)
    companion(vm)
    assert subject(vm) == "WIP" and sandbox.finished(SID, "code") and not sandbox.mid_operation(SID, "code")
    assert git.query(vm, "show", f"{BRANCH}:file.txt") == "sandbox"


def test_a_session_ending_in_a_bisect_is_held_back_with_no_wip_commit(vm):
    tip = git.query(vm, "rev-parse", BRANCH)
    run_git(vm, "bisect", "start")  # still on the branch, but the work is not the branch's to commit over
    (vm / "left.txt").write_text("x\n")
    assert companion(vm).returncode == 0
    assert sandbox.mid_operation(SID, "code") and not report("bundle").exists() and not sandbox.finished(SID, "code")
    assert git.query(vm, "rev-parse", BRANCH) == tip


def test_no_bundle_replaces_a_final_snapshot_once_it_has_begun(vm):
    companion(vm)
    (box_path(SID) / "vm-home/.cave-final").touch()
    final = report("bundle").read_bytes()
    result = companion(vm, "bundle")
    assert result.returncode == 1 and "final snapshot" in result.stderr
    assert report("bundle").read_bytes() == final and sandbox.finished(SID, "code")


def test_a_broken_repo_fails_alone(tmp_path, vm):
    result = companion(tmp_path / "gone", second=vm)
    assert result.returncode == 1
    assert not sandbox.finished(SID, "code")
    assert sandbox.finished(SID, "second")


def test_a_repo_created_in_the_vm_is_not_recorded_as_a_gitlink(vm):
    make_repo(vm / "newproj")
    (vm / "kept.txt").write_text("x\n")
    assert companion(vm).returncode == 0
    tree = git.query(vm, "ls-tree", "-r", BRANCH)
    assert "160000" not in tree and "kept.txt" in tree
    assert report("embedded").read_text() == "newproj/\n"
    (vm / "more.txt").write_text("x\n")
    companion(vm)
    assert report("embedded").read_text() == "newproj/\n"  # once, however many snapshots see it


def test_strays_are_files_outside_every_repo(vm):
    code = box_path(SID) / "code"
    nested = make_clone(vm, code / "in[side]", SID)
    (code / "notes.txt").write_text("x\n")
    (code / "dir").mkdir()
    (code / "dir/more.txt").write_text("x\n")
    assert companion(nested).returncode == 0
    assert sandbox.report_path(SID, "strays").read_text().split() == sorted(["notes.txt", "dir/more.txt"], reverse=True)


def test_strays_include_files_beside_the_workspace_but_not_cave_s_own(vm, tmp_path):
    volume = tmp_path / "volume"
    (volume / "code").mkdir(parents=True)
    nested = make_clone(vm, volume / "code/repo", SID)
    for name in ("code/notes.txt", "other/deep/f.txt", ".cave-config/settings.json", "lost+found/x"):
        (volume / name).parent.mkdir(parents=True, exist_ok=True)
        (volume / name).write_text("x\n")
    assert run_companion(SID, "--final", {"code": nested}, CAVE_VOLUME=str(volume)).returncode == 0
    assert sandbox.report_path(SID, "strays").read_text().split() == sorted(["code/notes.txt", "other/deep/f.txt"], reverse=True)


def test_a_workspace_that_is_a_repo_has_no_strays(vm):
    make_clone(vm, box_path(SID) / "code/sub", SID)
    assert companion(box_path(SID) / "code").returncode == 1  # the empty box clone is not a repo
    assert sandbox.report_path(SID, "strays").read_text() == ""


def test_a_repo_path_with_a_trailing_space_works(tmp_path, vm):
    spaced = make_repo(tmp_path / "with space ")
    run_git(spaced, "update-ref", "refs/cave/delivered", "HEAD")
    run_git(spaced, "checkout", "-q", "-b", BRANCH)
    (spaced / "new.txt").write_text("x\n")
    assert companion(spaced).returncode == 0
    assert subject(spaced) == "WIP"


def update_inbox(repo, *since):
    """The host moved main and replaced the bundle in the inbox."""
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "host")
    (box_path(SID) / "inbox").mkdir(exist_ok=True)
    git.create_bundle(repo, box_path(SID) / "inbox" / f"{sandbox.report_key('code')}.bundle", "main", *since)
    return git.query(repo, "rev-parse", "main")


def flag():
    return box_path(SID) / "vm-home/.cave-update"


def test_fetch_moves_origin_only_and_flags_the_update_once(vm, repo):
    base = git.query(repo, "rev-parse", "main")
    report("acked").write_text(base + "\n")
    tip = update_inbox(repo, base)
    branch, delivered = git.query(vm, "rev-parse", BRANCH), git.query(vm, "rev-parse", "refs/cave/delivered")
    assert companion(vm, "fetch").returncode == 0
    assert git.query(vm, "rev-parse", "refs/remotes/origin/main") == tip
    assert report("acked").read_text().strip() == tip
    assert flag().exists()
    assert git.query(vm, "rev-parse", BRANCH) == branch and git.query(vm, "rev-parse", "HEAD") == branch
    assert git.query(vm, "rev-parse", "refs/cave/delivered") == delivered
    assert git.query(vm, "rev-parse", "main") == base
    flag().unlink()
    companion(vm, "fetch")
    assert not flag().exists()


def test_a_bundle_this_clone_lacks_the_start_of_asks_for_all_of_main(vm, repo):
    update_inbox(repo)
    missing = git.query(repo, "rev-parse", "main")
    tip = update_inbox(repo, missing)
    companion(vm, "fetch")
    assert report("need-full").exists() and not flag().exists()
    assert git.query(vm, "rev-parse", "-q", "--verify", f"{tip}^{{commit}}") == ""


def test_the_watcher_counts_uncommitted_files_and_a_broken_repo_counts_as_unknown(vm, tmp_path):
    (vm / "new.txt").write_text("x\n")
    (vm / "file.txt").write_text("changed\n")
    assert companion(vm, "fetch", second=tmp_path / "gone").returncode == 0
    assert report("dirty").read_text() == "2\n"
    assert not report("dirty", "second").exists()
    assert sandbox.uncommitted(SID, sandbox.load_meta(SID)) == "2"


@pytest.mark.parametrize("words, asked", [(["review"], None), (["add-repo", "sub dir"], "sub dir")])
def test_a_request_reaches_the_host_in_the_format_it_reads(vm, words, asked):
    assert companion(vm, "request").returncode == 2
    assert run_companion(SID, "request", {}, *words).returncode == 0
    assert [request.repo for request in sandbox.requests(SID)] == [asked]


def test_a_repo_can_be_cloned_in_later_inside_another(vm, repo, tmp_path):
    other = make_repo(tmp_path / "other")
    (box_path(SID) / "inbox").mkdir()
    bundle = box_path(SID) / "inbox/new.bundle"
    git.create_bundle(other, bundle, "main")
    companion(vm, "bundle")
    entry = {"path": str(vm / "dir/new[1]"), "key": "newkey", "bundle": str(bundle), "base": "main", "origin": "",
             "exclude": []}
    env = dict(os.environ, CAVE_VM_HOME=str(box_path(SID) / "vm-home"), CAVE_OUTBOX=str(box_path(SID) / "outbox"))
    assert subprocess.run(["bash", str(COMPANION), "add", json.dumps(entry)], env=env).returncode == 0
    assert git.query(vm / "dir/new[1]", "branch", "--show-current") == BRANCH
    assert (box_path(SID) / "outbox/newkey.acked").read_text().strip() == git.query(other, "rev-parse", "main")
    assert "newkey" in (box_path(SID) / "vm-home/.cave-repos").read_text()
    assert (box_path(SID) / "vm-home/.cave-added").exists()
    assert git.query(vm, "status", "--porcelain") == ""  # the repo around it doesn't see it
    add = lambda: subprocess.run(["bash", str(COMPANION), "add", json.dumps(entry)], env=env).returncode
    assert add() == 0 and (box_path(SID) / "vm-home/.cave-repos").read_text().count("newkey") == 1  # a retry
    (box_path(SID) / "vm-home/.cave-repos").write_text("")  # one cut short after the clone, before it was listed
    assert add() == 0 and "newkey" in (box_path(SID) / "vm-home/.cave-repos").read_text()


def recover(clone, gone):
    entries = [{"path": str(path), "key": sandbox.report_key(key), "bundle": "/nowhere", "base": "main", "origin": "",
                "exclude": []} for key, path in (("code", clone), ("second", gone))]
    home = box_path(SID) / "vm-home"
    home.mkdir()
    (home / ".cave-repos").write_text("")  # a new VM's home holds nothing of the old one
    return run_companion(SID, "recover", {}, CAVE_ID=SID, CAVE_RESUME="1", CAVE_REPOS=json.dumps(entries))


def test_recovery_snapshots_from_nothing_but_the_volume(tmp_path, vm):
    (vm / ".git/index.lock").touch()
    (vm / "left.txt").write_text("x\n")
    report("finished", "second").touch()
    report("finished").write_text("stale")
    (tmp_path / "gone").mkdir()
    (tmp_path / "gone/mine.txt").write_text("x\n")
    assert recover(vm, tmp_path / "gone").returncode == 0
    assert (box_path(SID) / "vm-home/.cave-final").exists()  # a recovery VM is listed as running, so merge may exec here
    assert subject(vm) == "WIP"
    assert sandbox.finished(SID, "code") and report("finished").read_text() == ""
    assert not report("none", "second").exists()  # a repo gone from a used volume isn't a repo with no work


def fake_timeout(tmp_path, body):
    """A `timeout` first on PATH, as the image has and macOS lacks."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    (bin_dir / "timeout").write_text(f"#!/bin/sh\n{body}\n")
    (bin_dir / "timeout").chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


def test_a_stray_search_that_times_out_writes_no_strays_and_fails(vm, tmp_path):
    (box_path(SID) / "code/notes.txt").write_text("x\n")
    path = fake_timeout(tmp_path, "exit 124")  # what timeout says when it had to stop find
    result = run_companion(SID, "--final", {"code": vm}, PATH=path)
    assert result.returncode == 1
    assert not sandbox.report_path(SID, "strays").exists()
    assert not list((box_path(SID) / "outbox").glob(".strays.*"))
    assert report("finished").exists()  # the repos themselves were snapshotted


def test_the_stray_search_runs_under_timeout_with_a_limit_inside_the_stop_budget(vm, tmp_path):
    (box_path(SID) / "code/notes.txt").write_text("x\n")
    record = tmp_path / "args"
    path = fake_timeout(tmp_path, f'echo "$@" > {record}; shift 3; exec "$@"')
    result = run_companion(SID, "--final", {"code": vm}, PATH=path)
    assert result.returncode == 0, result.stderr
    words = record.read_text().split()
    assert words[:3] == ["-k", "1", "6"] and words[3] == "find"
    assert sandbox.report_path(SID, "strays").read_text().split() == ["notes.txt"]
