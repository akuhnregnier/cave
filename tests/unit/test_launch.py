import re
import subprocess
import threading
import time

import pytest

from cave import container, git, launch, sandbox, transport
from cave._testing.harness import values_after
from cave._testing.repos import make_repo, raises, run_git
from cave.errors import CaveError
from cave.log import warn
from cave.paths import box_path, config_repo


def created(root):
    meta, skipped = launch._plan(str(root))
    launch._create_sandbox(meta, skipped)
    return meta


def bundle_of(meta, clone="code"):
    return box_path(meta["id"]) / "inbox" / transport.inbox_name(clone)


def heads(meta, clone="code"):
    entry = next(e for e in meta["repos"] if e["clone"] == clone)
    return git.query(entry["repo"], "bundle", "list-heads", str(bundle_of(meta, clone))).split()[1:]


def vm_repos(meta):
    return {entry["path"]: entry for entry in [launch._vm_repo(e, meta) for e in meta["repos"]]}


def test_create_sandbox_bundles_code_and_records_meta(repo):
    meta = created(repo)
    assert heads(meta) == ["refs/heads/main"]
    assert not list((box_path(meta["id"]) / "staging").iterdir())
    assert vm_repos(meta)["/workspace/project"]["bundle"] == f"/cave/inbox/{bundle_of(meta).name}"
    assert sandbox.load_meta(meta["id"]) == meta
    assert [(e["name"], e["base"]) for e in meta["repos"]] == [(".", "main")]


def test_nested_repos_are_recreated_at_the_same_paths(repo):
    make_repo(repo / "sub")
    meta = created(repo)
    assert heads(meta) == heads(meta, "code/sub") == ["refs/heads/main"]
    assert list(vm_repos(meta)) == ["/workspace/project", "/workspace/project/sub"]


def test_a_plain_directory_of_repos_works(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root / "a")
    make_repo(root / "b")
    meta = created(root)
    assert [e["vm"] for e in meta["repos"]] == ["/workspace/root/a", "/workspace/root/b"]
    assert heads(meta, "code/b") == ["refs/heads/main"]


def test_a_repo_named_config_is_told_apart_from_the_config_repo(repo):
    make_repo(repo / "config")
    run_git(repo, "clone", "-q", str(repo), str(config_repo()))
    entries = created(repo)["repos"]
    assert len({e["name"] for e in entries}) == len(entries)
    assert len({e["clone"] for e in entries}) == len(entries)


def test_config_repo_comes_last(repo):
    run_git(repo, "clone", "-q", str(repo), str(config_repo()))
    meta = created(repo)
    assert heads(meta, "config") == ["refs/heads/main"]
    assert meta["repos"][-1]["vm"] == launch.VM_CONFIG


def test_config_repo_under_the_root_is_not_bundled_twice(repo, monkeypatch):
    config = make_repo(repo / "config")
    monkeypatch.setenv("CAVE_CONFIG", str(config))
    assert [e["clone"] for e in created(repo)["repos"]] == ["code", "config"]


def test_nothing_usable_leaves_nothing_behind(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q", "-b", "main")
    with pytest.raises(CaveError, match="no git repos"):
        launch._plan(str(empty))
    assert sandbox.all_ids() == []


def test_project_name_is_sanitised(tmp_path):
    odd = tmp_path / "my proj:1"
    odd.mkdir()
    run_git(odd, "init", "-q", "-b", "main")
    run_git(odd, "commit", "-q", "--allow-empty", "-m", "x")
    assert created(odd)["project"] == "my_proj_1"


def test_parents_exclude_their_nested_repos_with_special_characters_escaped(repo):
    for name in ("sub", "sub/inner", "foo[1]", "a*b"):
        make_repo(repo / name)
    entries = vm_repos(created(repo))
    assert entries["/workspace/project"]["exclude"] == [r"/a\*b/", r"/foo\[1\]/", "/sub/", "/sub/inner/"]
    assert entries["/workspace/project/sub"]["exclude"] == ["/inner/"]
    assert entries["/workspace/project/sub/inner"]["exclude"] == []


def test_each_repo_keeps_its_own_public_origin(repo):
    run_git(repo, "remote", "add", "origin", "git@github.com:a/b.git")
    sub = make_repo(repo / "sub")
    run_git(sub, "remote", "add", "origin", "https://user:secret@example.com/c.git")
    entries = vm_repos(created(repo))
    assert entries["/workspace/project"]["origin"] == "https://github.com/a/b.git"
    assert entries["/workspace/project/sub"]["origin"] == ""


def test_startup_lists_every_repo_what_each_leaves_out_and_anything_skipped(repo, capfd):
    make_repo(repo / "sub")
    (repo / ".gitmodules").write_text("")
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "x")
    (repo / "file.txt").write_text("dirty\n")
    meta, skipped = launch._plan(str(repo))
    launch._announce(meta, [("old", "no commits on main or master")])
    err = capfd.readouterr().err
    assert f"sandbox {meta['name']} has 2 repos" in err
    assert str(repo / "sub") in err
    assert "(uncommitted changes not included) (submodules not included) (Git LFS files not included)" in err
    assert "skipped old: no commits on main or master" in err


def test_waiting_for_the_vm_to_leave_the_listing_ends_at_its_exit_a_timeout_or_a_failed_listing(monkeypatch):
    listings = [{"sid"}, {"sid"}, set()]
    monkeypatch.setattr(container, "running", lambda: listings.pop(0))
    launch._wait_until_gone("sid")
    assert listings == []
    monkeypatch.setattr(container, "running", lambda: {"sid"})
    launch._wait_until_gone("sid", timeout=0.2)

    monkeypatch.setattr(container, "running", raises(CaveError("no")))
    launch._wait_until_gone("sid")


def test_run_args_mount_stable_paths(repo):
    meta = created(repo)
    args = launch._run_args(meta, False)
    volumes = values_after(args, "-v")
    assert "--rm" not in args
    assert f"cave-{meta['id']}:/workspace" in volumes
    assert f"{box_path(meta['id'])}/outbox:{launch.VM_OUTBOX}" in volumes
    assert values_after(args, "--mount") == [f"type=bind,source={box_path(meta['id'])}/inbox,target=/cave/inbox,readonly"]
    assert any("cave/sessions/project-" in v and v.endswith(":" + launch.VM_PROJECTS) for v in volumes)
    assert not any(v.endswith(launch.VM_CONFIG) for v in volumes)
    assert values_after(args, "--name") == [meta["id"]]


def test_every_vm_of_a_sandbox_joins_its_network_and_an_old_sandbox_joins_none(repo):
    meta = created(repo)
    assert "--network" not in launch._run_args(meta, False)
    meta["network"] = f"cave-{meta['id']}"
    assert values_after(launch._run_args(meta, False), "--network") == [meta["network"]]
    assert values_after(launch._run_args(meta, True, resume=True), "--network") == [meta["network"]]
    assert values_after(launch._vm_args(meta, True), "--network") == [meta["network"]]  # as recovery's are


def test_a_recovery_without_its_network_stops_rather_than_join_the_shared_one(monkeypatch, isolated_env):
    ran = []
    monkeypatch.setattr(container, "ensure_network", raises(CaveError("could not create network n")))
    monkeypatch.setattr(container, "run_to_end", lambda *args: ran.append(args))
    meta = {"id": "20260101-000000-abcd", "name": "n", "workspace": "/w", "root": "/nowhere", "image": "cave:old",
            "network": "cave-20260101-000000-abcd", "repos": []}
    assert launch.recover(meta) == "could not create network n" and ran == []
    with pytest.raises(CaveError, match="could not create network"):
        launch.resume(meta, [])


def test_the_network_is_made_again_if_it_has_gone(monkeypatch):
    made = []
    monkeypatch.setattr(container, "ensure_network", made.append)
    launch._ensure_network({"id": "20260101-000000-abcd", "network": "something else"})  # only a flag
    launch._ensure_network({"id": "20260101-000000-abcd"})  # an old sandbox has none
    assert made == ["cave-20260101-000000-abcd"]


def test_the_network_name_comes_from_the_id_whatever_meta_says(repo):
    meta = created(repo)
    meta["network"] = "cave-tampered"
    assert values_after(launch._vm_args(meta, False), "--network") == [f"cave-{meta['id']}"]


def test_creating_a_network_sweeps_retries_once_and_then_refuses(monkeypatch):
    attempts, swept = [], []
    monkeypatch.setattr(container, "create_network", lambda name: attempts.append(name) or len(attempts) > 1)
    monkeypatch.setattr(container, "running", lambda strict=False: set())
    monkeypatch.setattr(launch, "sweep_networks", lambda active, deadline: swept.append(active))
    launch._create_network("20260101-000000-abcd")
    assert attempts == ["cave-20260101-000000-abcd"] * 2 and swept == [set()]
    attempts.clear()
    monkeypatch.setattr(container, "create_network", lambda name: attempts.append(name) and False)
    with pytest.raises(CaveError, match="won't use the shared one"):
        launch._create_network("20260101-000000-abcd")
    assert len(attempts) == 2
    monkeypatch.setattr(container, "running", raises(CaveError("unknown")))
    with pytest.raises(CaveError, match="could not create network"):  # an unknown sweep still ends in a refusal
        launch._create_network("20260101-000000-abcd")


def test_a_create_that_hangs_counts_as_failed_so_the_sweep_and_retry_still_happen(monkeypatch):
    attempts, swept = [], []

    def create(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise CaveError("container network did not answer within 30 seconds")
        return True

    monkeypatch.setattr(container, "create_network", create)
    monkeypatch.setattr(container, "running", lambda strict=False: set())
    monkeypatch.setattr(launch, "sweep_networks", lambda active, deadline: swept.append(active))
    launch._create_network("20260101-000000-abcd")
    assert len(attempts) == 2 and swept == [set()]
    monkeypatch.setattr(container, "create_network", raises(CaveError("hung")))
    with pytest.raises(CaveError, match="won't use the shared one"):
        launch._create_network("20260101-000000-abcd")


def test_a_sweep_that_drags_on_is_given_up_on_at_a_deadline_and_the_launch_goes_on(monkeypatch):
    results = iter([False, True])
    monkeypatch.setattr(container, "create_network", lambda name: next(results))
    monkeypatch.setattr(container, "running", lambda strict=False: set())
    monkeypatch.setattr(launch, "SWEEP_SECONDS", 0.3)
    release = threading.Event()
    monkeypatch.setattr(launch, "sweep_networks", lambda active, deadline: release.wait(10))
    started = time.time()
    launch._create_network("20260101-000000-abcd")
    release.set()
    assert time.time() - started < 5


def test_run_args_announce_a_token_but_never_hold_it(repo, monkeypatch):
    meta = created(repo)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "s3cret")
    expected = "CAVE_TOKEN_EXPECTED=1"
    assert expected in values_after(launch._run_args(meta, True), "-e")
    assert expected not in values_after(launch._run_args(meta, False), "-e")
    assert not [arg for arg in launch._run_args(meta, True) if "OAUTH" in arg or "s3cret" in arg]


def test_run_args_honour_resource_overrides(repo, monkeypatch):
    monkeypatch.setenv("CAVE_MEMORY", "9G")
    assert values_after(launch._run_args(created(repo), False), "--memory") == ["9G"]


def test_a_prune_that_hangs_never_blocks_a_launch_and_is_silent_behind_the_session(monkeypatch, capfd):
    release, spoke = threading.Event(), threading.Event()

    def slow_prune():
        release.wait()
        warn("into Claude's terminal")
        spoke.set()

    monkeypatch.setattr(launch, "prune", slow_prune)
    monkeypatch.setattr(launch, "HOUSEKEEPING_SECONDS", 0.01)
    launch._housekeep("prune", lambda: launch.prune())
    release.set()
    assert spoke.wait(5)
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("error", [OSError(5, "boom"), CaveError("boom"), subprocess.CalledProcessError(1, "git"),
                                   subprocess.TimeoutExpired("container", 1), KeyError("repo"), RecursionError()])
def test_a_failed_prune_never_blocks_a_launch(monkeypatch, capfd, error):
    monkeypatch.setattr(launch, "prune", raises(error))
    launch._housekeep("prune", lambda: launch.prune())
    assert "prune failed" in capfd.readouterr().err


def test_a_nested_repo_at_a_path_its_parent_tracks_is_skipped(repo):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "tracked.txt").write_text("x")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "tracks sub")
    run_git(sub, "init", "-q", "-b", "main")
    run_git(sub, "commit", "-q", "--allow-empty", "-m", "x")
    meta, skipped = launch._plan(str(repo))
    launch._create_sandbox(meta, skipped)
    assert [e["name"] for e in meta["repos"]] == ["."]
    assert sandbox.load_meta(meta["id"]) == meta
    assert [rel for rel, _ in skipped] == ["sub"]


def test_a_gitlink_at_a_nested_repo_is_no_reason_to_skip_it(repo):
    sub = make_repo(repo / "sub")
    commit = git.query(sub, "rev-parse", "HEAD")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},sub")
    run_git(repo, "commit", "-q", "-m", "gitlink")
    assert [e["name"] for e in created(repo)["repos"]] == [".", "sub"]


def test_a_failed_bundle_is_skipped_not_fatal(repo, monkeypatch):
    make_repo(repo / "sub")
    real_bundle = git.create_bundle

    def create_bundle(source, dest, base, since=None):
        if str(source).endswith("sub"):
            raise subprocess.CalledProcessError(128, "git bundle")
        real_bundle(source, dest, base)

    monkeypatch.setattr(git, "create_bundle", create_bundle)
    meta, skipped = launch._plan(str(repo))
    launch._create_sandbox(meta, skipped)
    assert [e["name"] for e in meta["repos"]] == ["."]
    assert [rel for rel, _ in skipped] == ["sub"]


def test_all_bundles_failing_is_an_error_and_leaves_nothing(repo, monkeypatch):
    def create_bundle(source, dest, base, since=None):
        raise subprocess.CalledProcessError(128, "git bundle")

    monkeypatch.setattr(git, "create_bundle", create_bundle)
    meta, skipped = launch._plan(str(repo))
    with pytest.raises(CaveError, match="could not be bundled"):
        launch._create_sandbox(meta, skipped)
    assert sandbox.all_ids() == []


def test_a_linked_worktree_gets_a_clear_error(repo, tmp_path):
    worktree = tmp_path / "wt"
    run_git(repo, "worktree", "add", "-q", "-b", "wt", str(worktree))
    with pytest.raises(CaveError, match="linked worktree"):
        launch._plan(str(worktree))


def test_nested_repos_are_not_reported_as_uncommitted_changes(repo, capfd):
    (repo / "deep").mkdir()
    make_repo(repo / "deep/sub")
    meta, skipped = launch._plan(str(repo))
    launch._announce(meta, skipped)
    assert "uncommitted" not in capfd.readouterr().err


def test_sessions_are_separate_for_projects_with_the_same_name(tmp_path):
    first, second = tmp_path / "x/app", tmp_path / "y/app"
    first.parent.mkdir()
    second.parent.mkdir()
    make_repo(first)
    make_repo(second)
    mounts = [next(v for v in values_after(launch._run_args(created(r), False), "-v") if launch.VM_PROJECTS in v)
              for r in (first, second)]
    assert mounts[0] != mounts[1]
    assert "/sessions/app-" in mounts[0]


def test_names_come_from_the_folder_and_the_label(repo):
    key = launch._project_key(str(repo))
    assert re.fullmatch(r"cave-project-[0-9a-f]{4}", key)
    meta, _ = launch._plan(str(repo))
    assert (meta["key"], meta["name"]) == (key, f"{key}-{meta['id'][-4:]}")
    assert launch._plan(str(repo), "auth-fix")[0]["name"] == f"{key}-auth-fix"


@pytest.mark.parametrize("label", ["", "-x", "a b", "a/b", "../x", "a\n", "a" * 41, "caf\u00e9"])
def test_a_bad_label_is_refused(repo, label):
    with pytest.raises(CaveError, match="bad --name"):
        launch._check_label(str(repo), label)


def test_a_duplicate_name_is_refused_only_in_the_same_project(repo, tmp_path):
    meta, skipped = launch._plan(str(repo), "x")
    launch._create_sandbox(meta, skipped)
    with pytest.raises(CaveError, match="already exists"):
        launch._check_label(str(repo), "x")
    launch._check_label(str(repo), "y")
    launch._check_label(str(tmp_path / "elsewhere/project"), "x")


def nested_warning(capfd, meta):
    capfd.readouterr()
    launch._warn_if_nested(meta)
    return capfd.readouterr().err


def test_warns_when_a_parent_sandbox_covers_these_repos(repo, capfd):
    sub = make_repo(repo / "sub")
    parent = created(repo)
    child = launch._plan(str(sub))[0]
    assert parent["name"] in nested_warning(capfd, child)
    sandbox.update_repo(parent["id"], "code/sub", merged=True)
    assert nested_warning(capfd, child) == ""


def test_siblings_and_children_are_not_nesting(repo, capfd):
    sub = make_repo(repo / "sub")
    child = created(sub)
    assert nested_warning(capfd, child) == ""
    assert nested_warning(capfd, launch._plan(str(repo))[0]) == ""
    assert nested_warning(capfd, launch._plan(str(sub))[0]) == ""


def test_the_shared_config_repo_is_not_nesting(repo, capfd):
    run_git(repo, "clone", "-q", str(repo), str(config_repo()))
    created(repo)
    child = launch._plan(str(make_repo(repo / "sub")))[0]
    assert child["repos"][-1]["name"] == launch.CONFIG_NAME
    assert nested_warning(capfd, child) == ""


def test_a_recovery_vm_that_hangs_is_given_up_on_and_deleted(monkeypatch, isolated_env):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("container", 1)

    deleted = []
    monkeypatch.setattr(container, "ensure_system", lambda: None)
    monkeypatch.setattr(container, "image_names", lambda: ["cave:old"])
    monkeypatch.setattr(container, "container", hang)
    monkeypatch.setattr(container, "force_delete", deleted.append)
    meta = {"id": "20260101-000000-abcd", "name": "n", "workspace": "/w", "root": "/nowhere", "image": "cave:old", "repos": []}
    assert "still running" in launch.recover(meta)
    assert deleted == [meta["id"]] * 2  # before, as it holds the volume, and after


def test_a_recovery_that_cannot_start_says_why_at_each_step(monkeypatch, isolated_env, capsys):
    monkeypatch.setattr(container, "ensure_system", lambda: None)
    monkeypatch.setattr(container, "image_names", lambda: ["cave:old"])
    monkeypatch.setattr(container, "force_delete", lambda name: "the volume is still held")
    monkeypatch.setattr(container, "run_to_end", lambda *args: "name already in use")
    meta = {"id": "20260101-000000-abcd", "name": "n", "workspace": "/w", "root": "/nowhere", "image": "cave:old", "repos": []}
    assert launch.recover(meta) == "name already in use"
    assert "could not clear the sandbox's old container: the volume is still held" in capsys.readouterr().err


@pytest.mark.parametrize("start", ["session", "resume"])
def test_starting_a_sandbox_clears_its_old_warning_so_new_work_gets_a_fresh_grace(repo, monkeypatch, start):
    meta = created(repo)
    meta["image"] = "cave:x"
    sandbox.update_meta(meta["id"], warned=123.0)
    vm = type("VM", (), {"wait": lambda self: 0})()
    monkeypatch.setattr(container, "ensure_system", lambda: None)
    monkeypatch.setattr(container, "image_names", lambda: ["cave:x"])
    monkeypatch.setattr(container, "force_delete", lambda name: "")
    monkeypatch.setattr(container, "boot", lambda *args: vm)
    monkeypatch.setattr(launch.auth, "read_token", lambda: None)
    monkeypatch.setattr(launch, "_run_args", lambda *args: [])
    monkeypatch.setattr(launch, "_wait_until_gone", lambda sid: None)
    if start == "resume":
        launch.resume(meta, [])
    else:
        launch._session(meta, "cave:x", [])
    assert not sandbox.load_meta(meta["id"]).get("warned")
