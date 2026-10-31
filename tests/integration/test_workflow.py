"""The CLI end to end with real git and a fake VM."""
import json
import os
import shutil
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from cave import git

COMMIT = "echo two >> file.txt && git add -A && git commit -q -m 'from claude'"
CONFIG_COMMIT = 'cd "$CONFIG" && echo hi >> CLAUDE.md && git add -A && git commit -qm cfg'
CONFLICT = "echo theirs > file.txt && git commit -qam sandbox"
MERGE_COMMIT = ("git checkout -q -b side && echo side > side.txt && git add -A && git commit -qm side && "
                "git checkout -q - && echo main > main.txt && git add -A && git commit -qm main && "
                "git merge -q --no-edit side")
RESOLVE = ("git fetch -q origin; git rebase origin/main > /dev/null 2>&1; echo resolved > file.txt; git add -A; "
           "GIT_EDITOR=true git rebase --continue > /dev/null")


def store_token(fake_vm):
    (Path(fake_vm.env["PATH"].split(":")[0]) / "security").write_text("#!/bin/bash\necho s3cret-token\n")


@pytest.mark.parametrize("running", [False, True])
def test_a_conflict_leaves_the_host_alone_keeps_the_work_and_still_merges_the_others(fake_vm, running):
    fake_vm.cave("init")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{CONFLICT} && {CONFIG_COMMIT}")
    if running:
        fake_vm.box_running(sid)
    fake_vm.host_commit_conflicting_with_sandbox()
    before = fake_vm.git("rev-parse", "main")
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "rebase conflict" in result.stderr
    assert fake_vm.git("rev-parse", "main") == before
    assert fake_vm.git("symbolic-ref", "--short", "HEAD") == "main"
    assert (fake_vm.repo / "file.txt").read_text() == "ours\n"
    assert not (fake_vm.repo / ".git/rebase-merge").exists()
    assert clone_of(fake_vm, sid).is_dir()
    assert fake_vm.held(sid)
    assert fake_vm.boxes() == [sid]
    assert "hi" in (fake_vm.config / "CLAUDE.md").read_text()
    assert fake_vm.refs(fake_vm.config) == ["main"]


@pytest.mark.parametrize("running", [False, True])
def test_history_with_a_merge_commit_is_held_back_not_dropped(fake_vm, running):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=MERGE_COMMIT)
    if running:
        fake_vm.box_running(sid)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "merge commit" in result.stderr
    assert fake_vm.boxes() == [sid]
    assert fake_vm.held(sid)
    assert not (fake_vm.repo / "side.txt").exists()


@pytest.mark.parametrize("running", [False, True])
def test_the_config_repo_diffs_and_merges_like_any_other(fake_vm, running):
    fake_vm.cave("init")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && {CONFIG_COMMIT}")
    if running:
        fake_vm.box_running(sid)
    assert "+hi" in fake_vm.cave("diff", "-p", sid).stdout
    fake_vm.cave("merge", sid)
    assert "hi" in (fake_vm.config / "CLAUDE.md").read_text()
    assert fake_vm.refs(fake_vm.config) == ["main"]
    assert fake_vm.boxes() == ([sid] if running else [])


def test_diff_fetches_into_the_cave_namespace(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    result = fake_vm.cave("diff", "-p", sid)
    assert "+two" in result.stdout
    assert fake_vm.held(sid)


def test_diff_recovers_what_a_crash_left(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT="echo left > left.txt", FAKE_CRASH="1")
    result = fake_vm.cave("diff", sid)
    assert "recovery VM" in result.stdout
    assert "left.txt" in result.stdout


def test_diff_warns_while_running(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.running.write_text(sid + "\n")
    assert "still running" in fake_vm.cave("diff", sid).stderr


def test_hooks_planted_in_the_clone_never_run(fake_vm, tmp_path):
    marker = tmp_path / "pwned"
    plant = (f"mkdir -p .git/hooks && for h in post-checkout post-rewrite post-merge; do "
             f"printf '#!/bin/sh\\ntouch {marker}\\n' > .git/hooks/$h; chmod +x .git/hooks/$h; done")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && {plant}")
    fake_vm.cave("diff", sid)
    fake_vm.cave("merge", sid)
    assert not marker.exists()


def test_main_clone_hooks_do_not_run_on_merge(fake_vm, tmp_path):
    marker = tmp_path / "pwned"
    hook = fake_vm.repo / ".git/hooks/post-rewrite"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "moved on")
    fake_vm.cave("merge", sid)
    assert not marker.exists()


def test_merge_is_linear_and_cleans_up(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && echo three >> file.txt && git commit -qam second")
    (fake_vm.repo / "other.txt").write_text("x\n")
    fake_vm.git("add", "-A")
    fake_vm.git("commit", "-q", "-m", "moved on")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["second", "from claude", "moved on", "first"]
    assert fake_vm.git("rev-list", "--merges", "--count", "main") == "0"
    assert fake_vm.git("symbolic-ref", "--short", "HEAD") == "main"
    assert fake_vm.boxes() == []
    assert fake_vm.refs() == ["main"]


def test_a_crash_is_recovered_by_a_vm_with_neither_claude_nor_root(fake_vm):
    store_token(fake_vm)
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && echo left > left.txt", FAKE_CRASH="1")
    (fake_vm.root / "token").unlink()
    fake_vm.log.write_text("")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["WIP", "from claude", "first"]
    assert (fake_vm.repo / "left.txt").read_text() == "left\n"
    assert fake_vm.boxes() == []
    assert not (fake_vm.root / "token").exists()
    run = next(line for line in fake_vm.log.read_text().splitlines() if line.startswith("run "))
    assert "--user claude --entrypoint cave-companion" in run and run.endswith(" recover")
    for absent in (".claude/projects", "--mount", "--tmpfs", "--rm", "CAVE_TOKEN_EXPECTED"):
        assert absent not in run
    calls = [line.split()[0] for line in fake_vm.log.read_text().splitlines()]
    assert calls.index("delete") < calls.index("run")  # a stopped container holds the volume
    assert "delete" in calls[calls.index("run"):]


@pytest.mark.parametrize("args", [(), ("--force",)])
def test_a_failed_recovery_holds_everything_back(fake_vm, args):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT, FAKE_CRASH="1")
    result = fake_vm.cave("merge", sid, *args, ok=False, FAKE_RECOVERY_FAILS="1")
    assert result.returncode == 1
    assert "final snapshot" in result.stderr and "the recovery went wrong" in result.stderr
    assert fake_vm.boxes() == [sid]
    assert (fake_vm.repo / "file.txt").read_text() == "one\n"
    fake_vm.cave("merge", sid)  # nothing was lost by trying
    assert "two" in (fake_vm.repo / "file.txt").read_text()


def test_a_broken_repo_fails_recovery_alone(fake_vm):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    script = "echo x > left.txt; cd sub && echo y > mine.txt && rm -rf .git"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script, FAKE_CRASH="1")
    result = fake_vm.cave("merge", sid, ok=False)
    assert "held back sub" in result.stderr
    assert (fake_vm.repo / "left.txt").exists()
    assert fake_vm.boxes() == [sid]
    assert not (other / "mine.txt").exists() and (clone_of(fake_vm, sid) / "sub/mine.txt").exists()
    assert "no conflict" not in fake_vm.cave("resolve", sid, ok=False).stderr


def test_a_missing_image_falls_back_with_a_warning(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT, FAKE_CRASH="1")
    (fake_vm.root / "images").write_text("")
    assert "has gone" in fake_vm.cave("merge", sid).stderr


def test_a_bundle_from_before_a_crash_is_not_taken_for_the_final_snapshot(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt")
    fake_vm.cave("diff", sid)
    for report in fake_vm.box(sid).glob("outbox/*.finished"):
        report.unlink()
    fake_vm.running.write_text("")
    assert fake_vm.cave("merge", sid, "--force", ok=False, FAKE_RECOVERY_FAILS="1").returncode == 1
    assert not (fake_vm.repo / "a.txt").exists()


def test_resolving_a_conflict_in_a_new_vm_goes_back_through_the_gate(fake_vm):
    store_token(fake_vm)
    sid = fake_vm.launch(FAKE_VM_SCRIPT=CONFLICT)
    fake_vm.host_commit_conflicting_with_sandbox()
    assert f"cave resolve {sid}" in fake_vm.cave("merge", sid, ok=False).stderr
    (fake_vm.root / "token").unlink()
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT=RESOLVE)
    assert (fake_vm.root / "token").read_text() == "s3cret-token\n"  # every session is sent it
    assert "s3cret-token" not in fake_vm.log.read_text()
    run = [line for line in fake_vm.log.read_text().splitlines() if line.startswith("run ")][-1]
    assert f"claude/{sid}" in run and "/workspace/project (onto main)" in run and "CAVE_RESUME=1" in run
    result = fake_vm.cave("merge", sid)
    assert "file.txt" in result.stdout and "host" not in result.stdout
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["sandbox", "host", "first"]
    assert (fake_vm.repo / "file.txt").read_text() == "resolved\n"
    assert fake_vm.boxes() == []


def test_resolving_a_conflict_in_a_live_sandbox_after_an_earlier_merge(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt")
    fake_vm.cave("merge", sid)
    claude_commits(fake_vm, sid, "file.txt", "theirs\n", "conflicting")
    fake_vm.host_commit_conflicting_with_sandbox()
    assert "rebase conflict" in fake_vm.cave("merge", sid, ok=False).stderr
    assert flagged(fake_vm, sid) == [True]
    with git.lock(fake_vm.repo):  # the update it sends must not collide with a merge or a sweep
        process = fake_vm.start("resolve", sid)
        watchdog = threading.Timer(60, process.kill)
        watchdog.start()
        assert "waiting for another cave command" in process.stderr.readline()
    watchdog.cancel()
    assert "delivered the current main" in process.communicate()[0]
    assert (fake_vm.root / f"vm-home/{sid}/.cave-note").exists()
    clone = clone_of(fake_vm, sid)
    fake_vm.git("fetch", "-q", "origin", repo=clone)
    fake_vm.git("reset", "-q", "--hard", "origin/main", repo=clone)
    claude_commits(fake_vm, sid, "file.txt", "resolved\n", "resolved")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["resolved", "host", "work", "first"]
    assert flagged(fake_vm, sid) == [False]


def test_a_conflict_that_went_away_needs_no_resolve(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "file.txt", "theirs\n", "conflicting")
    fake_vm.host_commit_conflicting_with_sandbox()
    fake_vm.cave("merge", sid, ok=False)
    fake_vm.git("reset", "-q", "--hard", "refs/cave/delivered", repo=clone_of(fake_vm, sid))
    fake_vm.cave("merge", sid)
    assert flagged(fake_vm, sid) == [False]


def test_a_resolve_reopens_repos_already_merged(fake_vm):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{CONFLICT} && cd sub && echo two >> file.txt && git commit -qam nested")
    fake_vm.host_commit_conflicting_with_sandbox()
    assert fake_vm.cave("merge", sid, ok=False).returncode == 1
    assert fake_vm.git("log", "-1", "--format=%s", repo=other) == "nested"
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT=f"{RESOLVE}; echo late > sub/late.txt")
    fake_vm.cave("merge", sid)
    assert (other / "late.txt").read_text() == "late\n"
    assert fake_vm.git("log", "--format=%s", repo=other).split("\n") == ["WIP", "nested", "first"]
    assert fake_vm.boxes() == []


@pytest.mark.parametrize("script, held, fix, landed", [
    (MERGE_COMMIT, "merge commit", "git fetch -q origin && git rebase -q origin/main", ["first", "main", "side"]),
    (f"{COMMIT} && git checkout -q main", "off its branch", "git checkout -q - && echo left > left.txt",
     ["WIP", "first", "from claude"]),
])
def test_resolve_gets_work_only_the_vm_can_fix_out_of_a_stopped_sandbox(fake_vm, script, held, fix, landed):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    result = fake_vm.cave("merge", sid, ok=False)
    assert held in result.stderr and f"cave resolve {sid}" in result.stderr
    assert fake_vm.git("log", "--format=%s", "main") == "first"
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT=fix)
    fake_vm.cave("merge", sid)
    assert sorted(fake_vm.git("log", "--format=%s", "main").split("\n")) == landed
    assert fake_vm.boxes() == []


def test_resolve_gets_rewritten_history_out_of_a_stopped_sandbox(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt", "a\n", "a")
    fake_vm.cave("merge", sid)
    fake_vm.git("commit", "-q", "--amend", "-m", "rewritten", repo=clone_of(fake_vm, sid))
    fake_vm.box_stopped(sid)
    assert f"cave resolve {sid}" in fake_vm.cave("merge", sid, ok=False).stderr
    fix = "git fetch -q origin && git reset -q --hard origin/main && echo b > b.txt"
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT=fix)
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["WIP", "a", "first"]
    assert fake_vm.boxes() == []


def test_resolve_finishes_a_merge_the_vm_stopped_in(fake_vm):
    script = ("git checkout -q -b side && echo side > file.txt && git commit -qam side && git checkout -q - && "
              "echo main > file.txt && git commit -qam work && git merge side > /dev/null")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT="git merge --abort && echo done >> file.txt")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["WIP", "work", "first"]


def test_resolve_with_nothing_to_resolve(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    assert "no conflict" in fake_vm.cave("resolve", sid, ok=False).stderr


def test_discard_deletes_without_merging(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.cave("diff", sid)
    fake_vm.cave("discard", sid)
    assert fake_vm.boxes() == []
    assert fake_vm.refs() == ["main"]
    assert (fake_vm.repo / "file.txt").read_text() == "one\n"


def networks(fake_vm):
    """The fake's networks, which are folders."""
    root = fake_vm.root / "networks"
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def test_a_sandbox_gets_a_network_of_its_own_that_goes_with_it(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    assert json.loads((fake_vm.box(sid) / "meta.json").read_text())["network"] == f"cave-{sid}"
    assert networks(fake_vm) == [f"cave-{sid}"]
    assert f"--network cave-{sid}" in fake_vm.log.read_text()
    fake_vm.cave("merge", sid)
    assert networks(fake_vm) == []
    second = fake_vm.launch()
    assert networks(fake_vm) == [f"cave-{second}"]
    fake_vm.cave("discard", second)
    assert networks(fake_vm) == []


def test_a_launch_sweeps_leaked_networks_and_retries_once_before_it_refuses(fake_vm):
    leaked = fake_vm.launch()
    fake_vm.cave("discard", leaked, FAKE_NETWORK_DELETE_FAILS="1")  # the discard still succeeds
    assert fake_vm.boxes() == [] and networks(fake_vm) == [f"cave-{leaked}"]
    fake_vm.cave("run", ok=False)  # whose prune sweeps it
    assert f"cave-{leaked}" not in networks(fake_vm)
    sid = fake_vm.boxes()[-1]
    fake_vm.cave("discard", sid)
    fake_vm.cave("run", ok=False, FAKE_NETWORK_CREATE_FAILS="1")  # the retry works
    sid = fake_vm.boxes()[-1]
    assert networks(fake_vm) == [f"cave-{sid}"]
    fake_vm.cave("discard", sid)
    before = fake_vm.log.read_text().count("--network")
    refused = fake_vm.cave("run", ok=False, FAKE_NETWORK_CREATE_FAILS="9")
    assert refused.returncode == 1 and "could not create network" in refused.stderr, refused.stderr
    assert "won't use the shared one" in refused.stderr
    assert fake_vm.log.read_text().count("--network") == before  # no VM started, on any network
    assert fake_vm.boxes() == [] and networks(fake_vm) == []
    assert not list((fake_vm.root / "volumes").glob("cave-*"))  # and no volume is left behind


def test_a_network_that_cannot_be_deleted_blocks_nothing_and_is_swept_later(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    merged = fake_vm.cave("merge", sid, FAKE_NETWORK_DELETE_FAILS="1")
    assert "could not delete network" in merged.stderr
    assert (fake_vm.repo / "file.txt").read_text() == "one\ntwo\n" and fake_vm.boxes() == []
    assert networks(fake_vm) == [f"cave-{sid}"]
    fake_vm.cave("prune")
    assert networks(fake_vm) == []


def test_prune_leaves_the_network_of_a_sandbox_that_is_still_there(fake_vm):
    sid = fake_vm.launch()
    fake_vm.cave("prune")
    assert networks(fake_vm) == [f"cave-{sid}"]


@pytest.mark.parametrize("old", [False, True])
def test_a_later_vm_uses_the_sandboxes_network_and_an_old_sandbox_the_default(fake_vm, old):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=CONFLICT, FAKE_CRASH="1")  # whose recovery VM is the first of the later ones
    fake_vm.host_commit_conflicting_with_sandbox()
    meta_file = fake_vm.box(sid) / "meta.json"
    meta = json.loads(meta_file.read_text())
    if old:
        del meta["network"]
        meta_file.write_text(json.dumps(meta))
    shutil.rmtree(fake_vm.root / "networks")  # lost, as with a `container` reinstall
    fake_vm.log.write_text("")
    fake_vm.cave("merge", sid, ok=False)  # recovers first, then finds the conflict
    fake_vm.cave("resolve", sid, ok=False, FAKE_VM_SCRIPT=RESOLVE)
    runs = [line for line in fake_vm.log.read_text().splitlines() if line.startswith("run ")]
    assert len(runs) == 2 and all(("--network" in run) != old for run in runs), runs
    assert networks(fake_vm) == ([] if old else [f"cave-{sid}"])


def test_recovery_never_falls_back_to_the_shared_network(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT="echo x > left.txt", FAKE_CRASH="1")
    shutil.rmtree(fake_vm.root / "networks")
    fake_vm.log.write_text("")
    result = fake_vm.cave("merge", sid, ok=False, FAKE_NETWORK_CREATE_FAILS="9")
    assert "could not create network" in result.stderr and not (fake_vm.repo / "left.txt").exists()
    assert "run " not in fake_vm.log.read_text() and fake_vm.boxes() == [sid]  # and nothing was lost


def test_discard_copes_with_a_hostile_outbox(fake_vm, tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious").write_text("x")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f'cd "$CAVE_OUTBOX" && ln -s {keep} link && mkdir ro && touch ro/f && chmod 555 ro')
    fake_vm.cave("discard", sid)
    assert fake_vm.boxes() == []
    assert (keep / "precious").exists()


def test_ids_cannot_escape_the_sandbox_dir(fake_vm):
    victim = Path(fake_vm.env["TMPDIR"]) / "victim"
    victim.mkdir()
    for bad in ("../victim", "..", "20260101-000000-abcd/../../victim"):
        result = fake_vm.cave("discard", bad, ok=False)
        assert result.returncode == 1
        assert "not a sandbox id" in result.stderr
    assert victim.exists()


def test_a_sandbox_gets_a_volume_that_goes_with_it(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    log = fake_vm.log.read_text()
    assert re.search(rf"volume create --label cave=[0-9a-f]{{12}} cave-{sid}", log)
    assert "--rm" not in log and f"-v cave-{sid}:/workspace " in log
    assert clone_of(fake_vm, sid).is_dir()
    fake_vm.cave("discard", sid)
    assert f"delete --force {sid}" in fake_vm.log.read_text()
    assert not clone_of(fake_vm, sid).exists()


def test_the_vm_gets_only_bundles_as_remotes(fake_vm):
    fake_vm.git("remote", "add", "origin", "https://example.com/a/b.git")
    sid = fake_vm.launch()
    clone = clone_of(fake_vm, sid)
    assert fake_vm.git("remote", "get-url", "upstream", repo=clone) == "https://example.com/a/b.git"
    assert fake_vm.git("remote", "get-url", "origin", repo=clone).endswith(".bundle")
    assert fake_vm.git("symbolic-ref", "--short", "HEAD", repo=clone) == f"claude/{sid}"
    assert fake_vm.git("rev-parse", "refs/cave/delivered", repo=clone) == fake_vm.git("rev-parse", "main")


def test_a_clone_that_fails_in_the_vm_is_reported_and_blocks_nothing(fake_vm):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    run = fake_vm.cave("run", ok=False, FAKE_VM_SCRIPT=COMMIT, FAKE_LOST_BUNDLE="sub")
    sid = fake_vm.boxes()[-1]
    assert "could not clone" in run.stderr
    fake_vm.cave("merge", sid)
    assert "two" in (fake_vm.repo / "file.txt").read_text()
    assert fake_vm.git("log", "--format=%s", repo=other) == "first"
    assert fake_vm.boxes() == []


def test_ls_shows_state(fake_vm):
    assert fake_vm.cave("ls").stdout == ""
    clean = fake_vm.launch()
    time.sleep(1.1)
    crashed = fake_vm.launch(FAKE_CRASH="1")
    lines = fake_vm.cave("ls").stdout.splitlines()
    assert {line.split()[0]: line.split()[1] for line in lines[1:]} == {clean: "stopped", crashed: "crashed"}


def test_name_reaches_meta_the_vm_and_the_listing(fake_vm):
    sid = fake_vm.launch("--name", "auth-fix", FAKE_VM_SCRIPT=COMMIT)
    name = json.loads((fake_vm.box(sid) / "meta.json").read_text())["name"]
    assert re.fullmatch(r"cave-project-[0-9a-f]{4}-auth-fix", name)
    assert f"CAVE_NAME={name}" in fake_vm.log.read_text()
    assert name in fake_vm.cave("ls").stdout
    assert "+two" in fake_vm.cave("diff", "-p", name[:-4]).stdout


def test_a_duplicate_name_is_refused_before_the_vm_starts(fake_vm):
    fake_vm.launch("--name", "x")
    result = fake_vm.cave("--name", "x", ok=False)
    assert result.returncode == 1 and "already exists" in result.stderr
    assert len(fake_vm.boxes()) == 1


def test_commands_find_the_sandbox_from_the_folder(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    (fake_vm.repo / "deep").mkdir()
    deep = fake_vm.cave("diff", "-p", cwd=fake_vm.repo / "deep")
    assert "+two" in deep.stdout and "using sandbox" in deep.stderr
    assert "using sandbox" not in fake_vm.cave("diff").stderr
    assert "no sandbox covers" in fake_vm.cave("diff", cwd=fake_vm.root, ok=False).stderr
    assert sid in fake_vm.boxes()
    assert "using sandbox" in fake_vm.cave("discard").stderr
    assert fake_vm.boxes() == []


def test_orphans_go_on_launch_unless_a_box_or_another_state_dir_has_them(fake_vm):
    orphan, foreign, kept = (f"20200101-000000-{tail}" for tail in ("aaaa", "bbbb", "cccc"))
    orphaned, others, own = fake_vm.orphan_volume(orphan), fake_vm.orphan_volume(foreign, "elsewhere"), fake_vm.orphan_volume(kept)
    fake_vm.running.write_text(f"{kept}\n")  # running, so even its age can't touch it
    fake_vm.box(kept).mkdir()
    fake_vm.launch()
    assert not orphaned.exists() and others.exists() and own.exists()


def test_a_listing_that_fails_never_reads_as_stopped(fake_vm):
    sid = fake_vm.launch()
    fake_vm.box_running(sid)
    result = fake_vm.cave("merge", sid, ok=False, FAKE_LISTINGS_FAIL="1")
    assert result.returncode == 1 and fake_vm.boxes() == [sid]
    assert "--entrypoint cave-companion" not in fake_vm.log.read_text()  # no recovery VM ran
    assert not any(line.startswith("delete") for line in fake_vm.log.read_text().splitlines())


def test_claude_args_pass_through_and_the_token_stays_out_of_argv(fake_vm):
    store_token(fake_vm)
    fake_vm.cave("run", "--", "-p", "hello")
    log = fake_vm.log.read_text()
    assert "-p hello" in log
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in log
    assert "s3cret-token" not in log
    assert (fake_vm.root / "token").read_text() == "s3cret-token\n"


def test_every_repo_under_a_plain_directory_round_trips(fake_vm):
    root = fake_vm.root / "work"
    fake_vm.add_repo(root / "a")
    fake_vm.add_repo(root / "b/c")
    fake_vm.add_repo(root / "untouched")
    script = ("for r in a b/c; do (cd $r && echo two >> file.txt && git commit -qam from-$(basename $r)); done")
    run = fake_vm.cave("run", ok=False, cwd=root, FAKE_VM_SCRIPT=script)
    assert "has 3 repos" in run.stderr
    for name in ("a", "b/c", "untouched"):
        assert str(root / name) in run.stderr
    sid = fake_vm.boxes()[-1]

    diff = fake_vm.cave("diff", "-p", sid, cwd=root)
    assert diff.stdout.count("+two") == 2
    assert "untouched" not in diff.stderr

    fake_vm.cave("merge", sid, cwd=root)
    assert fake_vm.git("log", "-1", "--format=%s", repo=root / "a") == "from-a"
    assert fake_vm.git("log", "-1", "--format=%s", repo=root / "b/c") == "from-c"
    assert fake_vm.git("log", "-1", "--format=%s", repo=root / "untouched") == "first"
    assert fake_vm.boxes() == []
    assert fake_vm.refs(root / "a") == ["main"]


def test_retrying_a_merge_after_a_conflict_skips_merged_repos(fake_vm):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    script = f"{CONFLICT} && cd sub && echo two >> file.txt && git commit -qam nested"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    fake_vm.host_commit_conflicting_with_sandbox()
    assert fake_vm.cave("merge", sid, ok=False).returncode == 1
    fake_vm.git("reset", "-q", "--hard", "HEAD~1")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", repo=other).split("\n") == ["nested", "first"]
    assert fake_vm.boxes() == []


def clone_of(fake_vm, sid, name="project"):
    """Where a clone sits on the fake volume."""
    return fake_vm.root / "volumes" / f"cave-{sid}" / name


def running_sandbox(fake_vm):
    sid = fake_vm.launch()
    fake_vm.box_running(sid)
    return sid


def claude_commits(fake_vm, sid, name, content="x\n", message="work", clone="project"):
    """Claude commits while the sandbox runs."""
    path = clone_of(fake_vm, sid, clone)
    (path / name).write_text(content)
    fake_vm.git("add", "-A", repo=path)
    fake_vm.git("commit", "-q", "-m", message, repo=path)


def landed_a(fake_vm):
    """A running sandbox whose first commit has landed."""
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt", message="a")
    fake_vm.cave("merge", sid)
    return sid


def vm_fetches(fake_vm, sid):
    """What the watcher in a VM does every few seconds."""
    subprocess.run(["container", "exec", "--user", "claude", sid, "cave-companion", "fetch"], check=True,
                   env={**os.environ, **fake_vm.env})


def origin_main(fake_vm, sid):
    return fake_vm.git("rev-parse", "refs/remotes/origin/main", repo=clone_of(fake_vm, sid))


def test_landing_updates_every_running_sandbox_that_holds_the_repo(fake_vm):
    first = running_sandbox(fake_vm)
    time.sleep(1.1)
    second = fake_vm.launch()
    fake_vm.running.write_text(f"{first}\n{second}\n")
    claude_commits(fake_vm, first, "a.txt", message="a")
    fake_vm.cave("merge", first)
    vm_fetches(fake_vm, second)
    assert origin_main(fake_vm, second) == fake_vm.git("rev-parse", "main")
    assert (fake_vm.root / f"vm-home/{second}/.cave-update").exists()
    clone = clone_of(fake_vm, second)
    assert fake_vm.git("branch", "--show-current", repo=clone) == f"claude/{second}"
    assert not (clone / "a.txt").exists()


def test_main_moved_outside_cave_goes_out_with_any_command_and_a_stopped_box_gets_none(fake_vm):
    stopped = fake_vm.launch()
    time.sleep(1.1)
    sid = running_sandbox(fake_vm)
    before = (fake_vm.box(stopped) / "inbox").stat().st_mtime_ns
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "by hand")
    fake_vm.cave("ls")
    vm_fetches(fake_vm, sid)
    assert origin_main(fake_vm, sid) == fake_vm.git("rev-parse", "main")
    assert (fake_vm.box(stopped) / "inbox").stat().st_mtime_ns == before


def test_a_rebase_onto_a_main_that_moved_on_lands_only_new_commits(fake_vm):
    sid = landed_a(fake_vm)
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "newer than what landed")
    fake_vm.cave("ls")
    vm_fetches(fake_vm, sid)
    clone = clone_of(fake_vm, sid)
    fake_vm.git("rebase", "-q", "origin/main", repo=clone)
    claude_commits(fake_vm, sid, "b.txt", message="b")
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "host moved on")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["b", "host moved on", "newer than what landed", "a", "first"]


def test_a_rebase_that_leaves_nothing_new_is_nothing_new(fake_vm):
    sid = landed_a(fake_vm)
    vm_fetches(fake_vm, sid)
    fake_vm.git("rebase", "-q", "origin/main", repo=clone_of(fake_vm, sid))
    assert "nothing new" in fake_vm.cave("merge", sid).stdout


def test_an_older_delivered_main_is_rebased_onto_after_a_second_update(fake_vm):
    sid = landed_a(fake_vm)
    vm_fetches(fake_vm, sid)
    fake_vm.git("rebase", "-q", "origin/main", repo=clone_of(fake_vm, sid))
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "newer")
    fake_vm.cave("ls")  # delivered, not yet fetched or rebased onto
    claude_commits(fake_vm, sid, "b.txt", message="b")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["b", "newer", "a", "first"]


def test_a_prerequisite_the_vm_lacks_falls_back_to_a_full_bundle(fake_vm):
    sid = running_sandbox(fake_vm)
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "one")
    report = next((fake_vm.box(sid) / "outbox").glob("*.acked"))
    report.write_text(fake_vm.git("rev-parse", "main") + "\n")  # a claim to have what it doesn't
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "two")
    fake_vm.cave("ls")
    vm_fetches(fake_vm, sid)
    assert list((fake_vm.box(sid) / "outbox").glob("*.need-full"))
    fake_vm.cave("ls")
    assert not list((fake_vm.box(sid) / "outbox").glob("*.need-full"))
    report.unlink()
    vm_fetches(fake_vm, sid)
    assert origin_main(fake_vm, sid) == fake_vm.git("rev-parse", "main")


def plain_root(fake_vm):
    """A root that's no repo, holding one, so another can turn up inside it later."""
    root = fake_vm.root / "work"
    fake_vm.add_repo(root / "first")
    sid = fake_vm.launch(cwd=root)
    fake_vm.box_running(sid)
    return root, sid


def in_sandbox(fake_vm, sid):
    return [entry["clone"] for entry in json.loads((fake_vm.box(sid) / "meta.json").read_text())["repos"]]


def flagged(fake_vm, sid):
    """Each repo: does cave resolve think it needs the VM?"""
    return [entry.get("conflict", False) for entry in json.loads((fake_vm.box(sid) / "meta.json").read_text())["repos"]]


def request(fake_vm, sid, *words):
    subprocess.run(["container", "exec", "--user", "claude", sid, "cave-companion", "request", *words], check=True,
                   env={**os.environ, **fake_vm.env}, stdout=subprocess.DEVNULL)


def test_a_repo_added_to_a_live_sandbox_lands_like_any_other(fake_vm):
    outside = fake_vm.add_repo(fake_vm.root / "elsewhere")
    sid = running_sandbox(fake_vm)
    fake_vm.cave("add", str(outside), sid)
    key = in_sandbox(fake_vm, sid)[-1]
    assert key.startswith("added/elsewhere-")
    clone = clone_of(fake_vm, sid, ".cave-added/" + key.split("/")[1])
    (clone / "new.txt").write_text("x\n")
    fake_vm.git("add", "-A", repo=clone)
    fake_vm.git("commit", "-q", "-m", "work", repo=clone)
    fake_vm.box_stopped(sid)
    fake_vm.cave("merge", sid)
    assert (fake_vm.git("show", "main:new.txt", repo=outside)) == "x"
    assert fake_vm.boxes() == []


def test_a_repo_inside_another_stays_out_of_it(fake_vm):
    sid = running_sandbox(fake_vm)
    fake_vm.add_repo(fake_vm.repo / "sub")
    fake_vm.cave("add", "sub", sid)
    assert in_sandbox(fake_vm, sid) == ["code", "code/sub"]
    assert fake_vm.git("status", "--porcelain", repo=clone_of(fake_vm, sid)) == ""
    fake_vm.box_stopped(sid)
    fake_vm.cave("merge", sid)
    assert fake_vm.boxes() == []  # nothing was left over as an embedded repo


def test_a_request_needs_a_yes(fake_vm):
    root, sid = plain_root(fake_vm)
    second = fake_vm.add_repo(root / "second")
    request(fake_vm, sid, "add-repo", "second")
    assert "has requests waiting" in fake_vm.cave("ls", cwd=root).stderr
    assert " 1 " in fake_vm.cave("ls", cwd=root).stdout
    piped = fake_vm.cave("requests", sid, ok=False)
    assert piped.returncode == 1 and "second" in piped.stdout and "nothing was done" in piped.stderr
    assert "asks for" in fake_vm.on_terminal("requests", sid, answer="n\n")
    assert "code/second" not in in_sandbox(fake_vm, sid)
    request(fake_vm, sid, "add-repo", "second")
    output = fake_vm.on_terminal("requests", sid, answer="y\n")
    assert str(second) in output and "added second" in output
    assert in_sandbox(fake_vm, sid)[-1] == "code/second"
    assert (clone_of(fake_vm, sid, "work/second") / "file.txt").exists()
    assert "no requests" in fake_vm.cave("requests", sid).stdout
    fake_vm.box_stopped(sid)
    fake_vm.cave("merge", sid)
    assert fake_vm.boxes() == []  # nothing is left over as a stray or an embedded repo


def test_a_review_request_goes_through_the_gate(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "new.txt")
    before = fake_vm.git("rev-parse", "main")
    request(fake_vm, sid, "review")
    assert "land this on main" not in fake_vm.on_terminal("requests", sid, answer="n\n")
    assert fake_vm.git("rev-parse", "main") == before
    request(fake_vm, sid, "review")
    output = fake_vm.on_terminal("requests", sid, answer="y\nn\n")
    assert "land this on main" in output and fake_vm.git("rev-parse", "main") == before
    request(fake_vm, sid, "review")
    fake_vm.on_terminal("requests", sid, answer="y\ny\n")
    assert fake_vm.git("rev-parse", "main") != before


def test_add_can_be_retried_when_the_vm_fails(fake_vm):
    outside = fake_vm.add_repo(fake_vm.root / "elsewhere")
    sid = running_sandbox(fake_vm)
    assert "could not clone" in fake_vm.cave("add", str(outside), sid, ok=False, FAKE_EXEC_FAILS="1").stderr
    assert in_sandbox(fake_vm, sid) == ["code"]
    fake_vm.cave("add", str(outside), sid)
    assert len(in_sandbox(fake_vm, sid)) == 2
    assert "already in this sandbox" in fake_vm.cave("add", str(outside), sid, ok=False).stderr


def test_add_refuses_a_stopped_sandbox_or_a_folder_that_is_no_repo(fake_vm):
    sid = fake_vm.launch()
    assert "not running" in fake_vm.cave("add", ".", sid, ok=False).stderr
    assert "not running" in fake_vm.cave("requests", sid, ok=False).stderr
    fake_vm.box_running(sid)
    (fake_vm.repo / "folder").mkdir()
    assert "not the top of a git repo" in fake_vm.cave("add", "folder", sid, ok=False).stderr


def test_a_cleanly_stopped_repo_mid_merge_needs_no_recovery(fake_vm):
    sid = fake_vm.launch()
    (clone_of(fake_vm, sid) / ".git/MERGE_HEAD").touch()
    for report in fake_vm.box(sid).glob("outbox/*.finished"):
        report.unlink()  # as a real VM's setup does
    subprocess.run(["container", "stop", sid], check=False, env={**os.environ, **fake_vm.env})
    assert " stopped " in fake_vm.cave("ls").stdout
    result = fake_vm.cave("merge", sid, ok=False)
    assert "under way" in result.stderr and "final snapshot" not in result.stdout
    assert "--entrypoint" not in fake_vm.log.read_text()


def test_merge_of_a_running_sandbox_takes_committed_work_and_keeps_it(fake_vm):
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt")
    (clone_of(fake_vm, sid) / "uncommitted.txt").write_text("x\n")
    fake_vm.cave("merge", sid)
    assert (fake_vm.repo / "a.txt").exists()
    assert not (fake_vm.repo / "uncommitted.txt").exists()
    assert fake_vm.boxes() == [sid]
    assert fake_vm.refs() == ["main"]
    assert f"exec --user claude {sid} cave-companion bundle" in fake_vm.log.read_text()
    assert (clone_of(fake_vm, sid) / "uncommitted.txt").exists()


def test_repeated_merges_land_only_new_work_on_a_moving_main(fake_vm):
    sid = landed_a(fake_vm)
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "host moved on")
    claude_commits(fake_vm, sid, "b.txt", message="b")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["b", "host moved on", "a", "first"]
    assert "nothing new" in fake_vm.cave("merge", sid).stdout


def test_merge_after_stopping_lands_only_the_rest(fake_vm):
    sid = landed_a(fake_vm)
    claude_commits(fake_vm, sid, "b.txt", message="last")
    fake_vm.box_stopped(sid)
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "main").split("\n") == ["last", "a", "first"]
    assert fake_vm.boxes() == []
    assert fake_vm.refs() == ["main"]


def test_running_merge_holds_back_only_a_dirty_repo(fake_vm):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    sid = running_sandbox(fake_vm)
    claude_commits(fake_vm, sid, "a.txt")
    claude_commits(fake_vm, sid, "b.txt", clone="project/sub")
    (other / "file.txt").write_text("dirty\n")
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "held back sub" in result.stderr and "uncommitted changes" in result.stderr
    assert (fake_vm.repo / "a.txt").exists()
    assert not (other / "b.txt").exists()
    assert (other / "file.txt").read_text() == "dirty\n"
    assert fake_vm.boxes() == [sid]
    (other / "file.txt").write_text("one\n")
    fake_vm.cave("merge", sid)
    assert (other / "b.txt").exists()


@pytest.mark.parametrize("crash", ["", "1"])
def test_a_repo_mid_merge_is_held_back_and_the_rest_land(fake_vm, crash):
    other = fake_vm.add_repo(fake_vm.repo / "sub")
    script = ("git checkout -q -b side && echo side > file.txt && git commit -qam side && git checkout -q - && "
              "echo main > file.txt && git commit -qam work && (git merge side > /dev/null; "
              "cd sub && echo two >> file.txt && git commit -qam nested)")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script, FAKE_CRASH=crash)
    before = fake_vm.git("rev-parse", "main")
    for args in ((), ("--force",)):
        result = fake_vm.cave("merge", sid, *args, ok=False)
        assert result.returncode == 1
        assert "merge or rebase is under way" in result.stderr
        assert "reported a failure" not in result.stderr  # a recovery that did all it could
        assert ("--force doesn't drop" in result.stderr) == bool(args)
        assert fake_vm.git("rev-parse", "main") == before
        assert fake_vm.boxes() == [sid]
    assert fake_vm.git("log", "-1", "--format=%s", repo=other) == "nested"


def test_diff_to_a_file_escapes_terminal_escapes(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT="printf 'a\\033]0;PWNED\\007b\\n' > new.txt && git add -A && git commit -qm x")
    result = fake_vm.cave("diff", "-p", sid)
    assert r"+a\x1b]0;PWNED\x07b" in result.stdout
    assert "\x1b" not in result.stdout + result.stderr
    assert "warning: escaped control characters" in result.stdout
    assert "warning: escaped control characters" in result.stderr


def test_merge_keeps_everything_if_the_base_branch_was_renamed(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.git("branch", "-m", "main", "trunk")
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "main" in result.stderr
    assert fake_vm.boxes() == [sid]


def test_an_emptied_outbox_is_an_error_and_loses_nothing(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.cave("diff", sid)
    for report in fake_vm.box(sid).glob("outbox/*.bundle"):
        report.unlink()
    assert "no bundle" in fake_vm.cave("diff", sid, ok=False).stderr
    assert fake_vm.cave("diff", sid, ok=False).returncode == 1
    result = fake_vm.cave("merge", sid, "--force", ok=False)
    assert result.returncode == 1
    assert "no bundle" in result.stderr
    assert fake_vm.held(sid)
    assert fake_vm.boxes() == [sid]


@pytest.mark.parametrize("script", ["rm -rf sub", "rm -rf sub && ln -s /tmp sub"])
def test_a_nested_clone_gone_from_the_vm_is_held_back(fake_vm, script):
    fake_vm.add_repo(fake_vm.repo / "sub")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "held back sub" in result.stderr
    assert fake_vm.boxes() == [sid]


def test_repos_with_the_same_name_keep_their_own_state(fake_vm):
    fake_vm.add_repo(fake_vm.repo / "config")
    fake_vm.cave("init")
    conflict = "cd config && echo theirs > file.txt && git commit -qam sandbox"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{conflict} && cd - >/dev/null && {CONFIG_COMMIT}")
    nested = fake_vm.repo / "config"
    (nested / "file.txt").write_text("ours\n")
    fake_vm.git("commit", "-qam", "host", repo=nested)
    assert fake_vm.cave("merge", sid, ok=False).returncode == 1
    assert fake_vm.cave("merge", sid, ok=False).returncode == 1
    assert fake_vm.boxes() == [sid]
    assert fake_vm.held(sid, nested)


EVIL = "echo evil > {0} && git add -f {0} && git commit -qm x"


@pytest.mark.parametrize("ignored, host_file, script, branch", [
    (".env", ".env", EVIL.format(".env"), "main"),
    (".env", ".env", EVIL.format(".env"), "feature"),
    (".env", ".env", "mkdir .env && echo evil > .env/x && git add -f .env && git commit -qm x", "main"),
    ("build/", "build/secret", "echo evil > build && git add -f build && git commit -qm x", "main"),
    (None, "new.txt", EVIL.format("new.txt"), "main"),  # untracked: named, and not called a conflict
])
def test_landing_cannot_overwrite_or_swap_a_host_file_in_its_way(fake_vm, ignored, host_file, script, branch):
    if ignored:
        (fake_vm.repo / ".gitignore").write_text(f"{ignored}\n")
        fake_vm.git("add", "-A")
        fake_vm.git("commit", "-qm", "ignore")
    secret = fake_vm.repo / host_file
    secret.parent.mkdir(exist_ok=True)
    secret.write_text("host secret\n")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    if branch != "main":
        fake_vm.git("checkout", "-q", "-b", branch)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "overwrite untracked or ignored" in result.stderr and "rebase conflict" not in result.stderr
    assert host_file.split("/")[0] in result.stderr
    assert secret.read_text() == "host secret\n"
    assert fake_vm.boxes() == [sid]


def test_an_ignored_host_file_beside_new_work_is_no_collision(fake_vm):
    (fake_vm.repo / "src").mkdir()
    (fake_vm.repo / "src/tracked").write_text("x\n")
    (fake_vm.repo / ".gitignore").write_text("*.pyc\n")
    fake_vm.git("add", "-A")
    fake_vm.git("commit", "-qm", "src")
    (fake_vm.repo / "src/cache.pyc").write_text("x\n")
    sid = fake_vm.launch(FAKE_VM_SCRIPT="echo y > src/new && git add -A && git commit -qm y")
    fake_vm.cave("merge", sid)
    assert (fake_vm.repo / "src/new").exists()


def test_a_paused_rebase_on_the_host_is_left_alone(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.git("checkout", "-q", "-b", "feature")
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "a")
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "b")
    fake_vm.git("-c", "sequence.editor=sed -i '' 1s/pick/edit/", "rebase", "-q", "-i", "--keep-empty", "HEAD~2")
    assert (fake_vm.repo / ".git/rebase-merge").exists()
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "in progress" in result.stderr
    assert (fake_vm.repo / ".git/rebase-merge").exists()


def test_concurrent_merges_serialise(fake_vm):
    first = fake_vm.launch(FAKE_VM_SCRIPT="echo a > a.txt && git add -A && git commit -qm a")
    time.sleep(1.1)
    second = fake_vm.launch(FAKE_VM_SCRIPT="echo b > b.txt && git add -A && git commit -qm b")
    processes = [fake_vm.start("merge", "--yes", sid) for sid in (first, second)]
    results = [(process.communicate(), process.returncode) for process in processes]
    assert [code for _, code in results] == [0, 0], results
    assert sorted(fake_vm.git("log", "--format=%s", "main").split("\n")) == ["a", "b", "first"]
    assert fake_vm.git("symbolic-ref", "--short", "HEAD") == "main"


@pytest.mark.parametrize("linked", [True, False])
def test_landing_while_on_another_branch_moves_base_where_it_is_checked_out_or_not(fake_vm, tmp_path, linked):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.git("checkout", "-q", "-b", "feature")
    worktree = tmp_path / "wt"
    if linked:
        fake_vm.git("worktree", "add", "-q", str(worktree), "main")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("symbolic-ref", "--short", "HEAD") == "feature"
    assert "two" in fake_vm.git("show", "main:file.txt")
    if linked:
        assert "two" in (worktree / "file.txt").read_text()


def test_a_tag_named_main_does_not_confuse_landing(fake_vm):
    fake_vm.git("tag", "main", "HEAD")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    fake_vm.git("commit", "-q", "--allow-empty", "-m", "moved on")
    fake_vm.cave("merge", sid)
    assert fake_vm.git("log", "--format=%s", "refs/heads/main").split("\n") == ["from claude", "moved on", "first"]


@pytest.mark.parametrize("rewrite", ["amend", "drop"])
def test_a_merge_after_a_rewrite_reports_divergence_rather_than_nothing_new(fake_vm, rewrite):
    sid = landed_a(fake_vm)
    clone = clone_of(fake_vm, sid)
    if rewrite == "amend":
        (clone / "a.txt").write_text("rewritten\n")
        fake_vm.git("add", "-A", repo=clone)
        fake_vm.git("commit", "-q", "--amend", "-m", "rewritten", repo=clone)
    else:
        fake_vm.git("reset", "-q", "--hard", "HEAD~1", repo=clone)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "rewritten" in result.stderr
    assert (fake_vm.repo / "a.txt").read_text() == "x\n"


def test_two_conflicts_get_one_set_of_instructions(fake_vm):
    nested = fake_vm.add_repo(fake_vm.repo / "my sub")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{CONFLICT} && cd 'my sub' && {CONFLICT}")
    fake_vm.host_commit_conflicting_with_sandbox()
    (nested / "file.txt").write_text("ours\n")
    fake_vm.git("commit", "-qam", "host", repo=nested)
    message = fake_vm.cave("merge", sid, ok=False).stderr
    assert message.count("rebase conflict") == 1
    assert message.count("CONFLICT") == 2
    assert "git rebase" not in message and "cave discard" not in message
    assert str(nested) in message


def test_a_repo_created_in_the_vm_keeps_the_sandbox(fake_vm):
    script = "mkdir newproj && cd newproj && git init -q && echo x > f && git add -A && git commit -qm x"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1
    assert "newproj" in result.stderr
    assert fake_vm.boxes() == [sid]
    assert f"-v cave-{sid}:/workspace:ro " in result.stderr and "--entrypoint bash" in result.stderr
    assert "newproj" in fake_vm.cave("merge", sid, "--force").stderr
    assert fake_vm.boxes() == []


def test_loose_files_at_a_plain_root_keep_the_sandbox(fake_vm):
    root = fake_vm.root / "work"
    fake_vm.add_repo(root / "a")
    fake_vm.cave("run", ok=False, cwd=root, FAKE_VM_SCRIPT="echo x > notes.txt")
    sid = fake_vm.boxes()[-1]
    result = fake_vm.cave("merge", sid, ok=False, cwd=root)
    assert result.returncode == 1
    assert "notes.txt" in result.stderr
    assert fake_vm.boxes() == [sid]


def test_a_deleted_host_repo_is_held_back_not_a_crash(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    shutil.rmtree(fake_vm.repo)
    result = fake_vm.cave("merge", sid, ok=False, cwd=fake_vm.root)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert fake_vm.boxes() == [sid]


def test_conflict_output_cannot_reach_the_terminal_raw(fake_vm):
    subject = "printf 'x\\033]0;PWNED\\007\\n' > msg"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{subject} && echo theirs > file.txt && git commit -qaF msg && rm msg")
    fake_vm.host_commit_conflicting_with_sandbox()
    result = fake_vm.cave("merge", sid, ok=False)
    assert "\x1b" not in result.stdout + result.stderr


def test_merge_without_a_terminal_or_yes_refuses_and_keeps_everything(fake_vm):
    fake_vm.cave("init")
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && {CONFIG_COMMIT}")
    result = fake_vm.cave("merge", sid, ok=False, approve=False)
    assert result.returncode == 1
    assert result.stderr.count("nothing here can approve it") == 2
    assert "(config repo)" in result.stdout  # the config repo gets the same gate, and CLAUDE.md is flagged there
    assert "CLAUDE.md  +" in result.stdout and "[agents]" in result.stdout
    assert (fake_vm.repo / "file.txt").read_text() == "one\n"
    assert "hi" not in (fake_vm.config / "CLAUDE.md").read_text()
    assert fake_vm.boxes() == [sid]
    assert fake_vm.held(sid) and fake_vm.held(sid, fake_vm.config)


def test_yes_lands_and_still_prints_the_report(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=f"{COMMIT} && echo x > .envrc && git add -A && git commit -qm env")
    result = fake_vm.cave("merge", sid)
    assert "from claude" in result.stdout and "rebased onto main" in result.stdout and "environment:" in result.stdout
    assert (fake_vm.repo / ".envrc").exists()


def test_the_prompt_decides(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    before = fake_vm.git("rev-parse", "main")
    output = fake_vm.on_terminal("merge", sid, answer="n\n")
    assert "from claude" in output and "[y/N]" in output and "rejected" in output
    assert fake_vm.git("rev-parse", "main") == before
    assert not fake_vm.held(sid)
    assert fake_vm.boxes() == [sid]
    assert "merged" in fake_vm.on_terminal("merge", sid, answer="y\n")
    assert "two" in (fake_vm.repo / "file.txt").read_text()
    assert fake_vm.boxes() == []


def test_the_review_comes_before_a_filter_in_host_config_can_run(fake_vm, tmp_path):
    """The fast-forward applies the sandbox's .gitattributes, so nothing may reach it unapproved."""
    marker = tmp_path / "pwned"
    fake_vm.git("config", "filter.pwn.smudge", f"touch {marker}; cat")
    fake_vm.git("config", "filter.pwn.clean", "cat")
    script = "echo '*.txt filter=pwn' > .gitattributes && echo x > new.txt && git add -A && git commit -qm attrs"
    sid = fake_vm.launch(FAKE_VM_SCRIPT=script)
    result = fake_vm.cave("merge", sid, ok=False, approve=False)
    assert "attributes, which apply on landing" in result.stdout and ".gitattributes" in result.stdout
    assert not marker.exists() and not (fake_vm.repo / "new.txt").exists()


def test_a_second_merge_says_it_is_waiting_for_the_lock(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=COMMIT)
    with git.lock(fake_vm.repo):
        process = fake_vm.start("merge", "--yes", sid)
        watchdog = threading.Timer(60, process.kill)
        watchdog.start()
        assert "waiting for another cave command" in process.stderr.readline()
    watchdog.cancel()
    process.communicate()
    assert process.returncode == 0


def test_stop_gives_the_vm_20_seconds_and_reports_what_went_wrong(fake_vm):
    sid = fake_vm.launch()
    assert "not running" in fake_vm.cave("stop", sid, ok=False).stderr
    fake_vm.box_running(sid)
    assert "could not stop" in fake_vm.cave("stop", sid, ok=False, FAKE_STOP_FAILS="1").stderr
    assert "cave merge" in fake_vm.cave("stop", sid).stdout
    assert f"stop --signal USR1 --time 20 {sid}" in fake_vm.log.read_text()


def test_unknown_sandbox(fake_vm):
    sid = "20260101-000000-abcd"
    for command in ("diff", "stop", "merge", "discard"):
        assert "no such sandbox" in fake_vm.cave(command, sid, ok=False).stderr
