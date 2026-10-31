import errno
import os
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from cave import prune as prune_module
from cave import cli, container, git, log, sandbox
from cave._testing.repos import age_id, make_box, make_repo, raises, repo_entry, run_git
from cave.errors import CaveError
from cave.paths import box_path, sandboxes_dir, sessions_dir, volume_name
from cave.prune import prune

pytestmark = pytest.mark.usefixtures("active", "vms")


@pytest.fixture
def vms(monkeypatch, active):
    """What `container` lists, and what prune has deleted. The state dir is there, as after any launch."""
    sandboxes_dir().mkdir(parents=True)
    found = SimpleNamespace(volumes=[], deleted=[])
    found.networks = []
    monkeypatch.setattr(container, "own_volumes", lambda: found.volumes)
    monkeypatch.setattr(container, "own_networks", lambda: found.networks)
    monkeypatch.setattr(container, "delete_network", lambda name: found.deleted.append(name) or True)
    monkeypatch.setattr(container, "force_delete", found.deleted.append)
    monkeypatch.setattr(container, "delete_volume", lambda name: found.deleted.append(name) or True)
    return found


def warned_long_ago(sid, days=2):
    sandbox.update_meta(sid, warned=time.time() - days * 86400)


def add_ref(repo, sid):
    run_git(repo, "update-ref", f"refs/cave/{sid}/abc", "HEAD")


def test_prunes_old_sandboxes_with_their_refs(repo):
    old, young = age_id(31), age_id(29)
    for sid in (old, young):
        make_box(sid, repo)
        add_ref(repo, sid)
    warned_long_ago(old)
    prune()
    assert sandbox.all_ids() == [young]
    assert git.sandbox_ref_ids(repo) == [young]


def test_spares_running_sandboxes(repo, active):
    sid = age_id(40)
    make_box(sid, repo)
    active.add(sid)
    prune()
    assert sandbox.all_ids() == [sid]


def test_prunes_orphan_refs_in_the_current_repo(repo, monkeypatch):
    orphan = age_id(40)
    add_ref(repo, orphan)
    for other in ("not-an-id", "99999999-999999-abcd"):  # the last has an id's shape, but no real date
        add_ref(repo, other)
    monkeypatch.chdir(repo)
    prune()
    assert git.sandbox_ref_ids(repo) == ["99999999-999999-abcd", "not-an-id"]


def test_prunes_sessions_by_mtime_and_keeps_memory():
    key = sessions_dir() / "p/-workspace-p"
    (key / "memory").mkdir(parents=True)
    stale_uuid, fresh_uuid = "2f75b9f7-cef5-417b-8933-cc966ac1cc27", "7585e220-db2a-4e4b-94d1-2bb9819c18aa"
    stale = time.time() - 31 * 86400
    stale_paths = [key / f"{stale_uuid}.jsonl", key / "memory/MEMORY.md"]
    for path in [*stale_paths, key / f"{fresh_uuid}.jsonl"]:
        path.write_text("{}")
    for path in [*stale_paths, key / "memory"]:
        os.utime(path, (stale, stale))
    prune()
    assert not (key / f"{stale_uuid}.jsonl").exists()
    assert (key / f"{fresh_uuid}.jsonl").exists()
    assert (key / "memory/MEMORY.md").exists()


def test_a_session_dir_with_a_fresh_file_stays():
    session = sessions_dir() / "p/-workspace-p/2f75b9f7-cef5-417b-8933-cc966ac1cc27"
    session.mkdir(parents=True)
    (session / "agent.jsonl").write_text("{}")
    stale = time.time() - 31 * 86400
    os.utime(session, (stale, stale))
    prune()
    assert (session / "agent.jsonl").exists()


def old_session(directory):
    directory.mkdir(parents=True, exist_ok=True)
    session = directory / "2f75b9f7-cef5-417b-8933-cc966ac1cc27.jsonl"
    session.write_text("{}")
    stale = time.time() - 31 * 86400
    os.utime(session, (stale, stale))
    return session


def test_symlinks_in_the_sessions_mount_are_not_followed(tmp_path):
    outside = old_session(tmp_path / "outside/key")
    project = sessions_dir() / "p"
    project.mkdir(parents=True)
    (project / "evil").symlink_to(tmp_path / "outside/key")
    (sessions_dir() / "q").symlink_to(tmp_path / "outside")
    prune()
    assert outside.exists()


def test_a_parent_swapped_for_a_symlink_mid_prune_deletes_nothing_outside(tmp_path, monkeypatch):
    outside = old_session(tmp_path / "outside")
    key = sessions_dir() / "p/key"
    old_session(key)
    real_open = prune_module._open_dir

    def swapping(name, parent=None):
        if name == "key":  # after the listing found it a real directory
            shutil.rmtree(key)
            key.symlink_to(tmp_path / "outside")
        return real_open(name, parent)

    monkeypatch.setattr(prune_module, "_open_dir", swapping)
    prune()
    assert outside.exists() and key.is_symlink()


def test_an_entry_swapped_for_a_symlink_is_unlinked_and_its_target_kept(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    stale = time.time() - 31 * 86400
    os.utime(outside, (stale, stale))
    key = sessions_dir() / "p/key"
    key.mkdir(parents=True)
    (key / "2f75b9f7-cef5-417b-8933-cc966ac1cc27").symlink_to(outside)
    fd = os.open(key, os.O_RDONLY)
    prune_module._remove("2f75b9f7-cef5-417b-8933-cc966ac1cc27", fd)
    os.close(fd)
    assert (outside / "keep").exists() and not os.path.lexists(key / "2f75b9f7-cef5-417b-8933-cc966ac1cc27")


def test_an_old_session_folder_with_read_only_parts_is_removed():
    key = sessions_dir() / "p/-workspace-p"
    folder = key / "2f75b9f7-cef5-417b-8933-cc966ac1cc27"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub/f").write_text("x")
    (folder / "sub").chmod(0o500)
    stale = (time.time() - 31 * 86400,) * 2
    for path in (folder / "sub/f", folder / "sub", folder):
        os.utime(path, stale)
    prune()
    assert not folder.exists()


def test_an_unreadable_session_entry_does_not_stop_prune(monkeypatch):
    key = sessions_dir() / "p/-workspace-p"
    session = old_session(key)
    bad = key / "7585e220-db2a-4e4b-94d1-2bb9819c18aa"
    bad.mkdir()
    (bad / "x").write_text("")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if "7585e220" in str(path):
            raise OSError(63, "File name too long")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    prune()
    assert not session.exists()


def test_a_sandbox_that_cannot_be_deleted_does_not_stop_prune(repo, monkeypatch):
    make_box(age_id(40), repo)
    session = old_session(sessions_dir() / "p/-workspace-p")

    monkeypatch.setattr(sandbox, "delete", raises(CaveError("could not delete")))
    prune()
    assert not session.exists()


def test_prunes_orphan_refs_in_nested_repos(repo, monkeypatch):
    nested = make_repo(repo / "sub")
    add_ref(nested, age_id(40))
    monkeypatch.chdir(repo)
    prune()
    assert git.sandbox_ref_ids(nested) == []


def test_an_orphan_goes_container_first_as_that_holds_its_volume(vms):
    sid = age_id(40)
    vms.volumes = [volume_name(sid)]
    prune()
    assert vms.deleted == [sid, volume_name(sid)]


def test_only_a_leaked_network_of_a_box_gone_from_disk_is_swept(vms, repo, active):
    leaked, kept, running = age_id(1), age_id(2), age_id(3)
    make_box(kept, repo)
    active.add(running)
    vms.networks = [f"cave-{sid}" for sid in (leaked, kept, running)] + ["default", "cave-not-an-id", "other-" + leaked]
    prune()
    assert vms.deleted == [f"cave-{leaked}"]  # young, as a failed delete leaves them


def test_a_sweep_past_its_deadline_leaves_the_rest_for_the_next_one(vms):
    vms.networks = [f"cave-{age_id(day)}" for day in (1, 2, 3)]
    prune_module.sweep_networks(set(), deadline=time.time() - 1)
    assert vms.deleted == []
    prune_module.sweep_networks(set(), deadline=time.time() + 60)
    assert vms.deleted == vms.networks


def test_a_network_that_will_not_go_warns_and_blocks_nothing(vms, monkeypatch, capsys):
    sid = age_id(1)
    vms.networks = [f"cave-{sid}"]
    monkeypatch.setattr(container, "delete_network", lambda name: False)
    session = old_session(sessions_dir() / "p/-workspace-p")
    prune()
    assert f"could not delete network cave-{sid}" in capsys.readouterr().err and not session.exists()


def test_only_a_box_gone_from_disk_makes_an_orphan(vms, repo, active, monkeypatch):
    kept, running, young = age_id(40), age_id(41), age_id(29)
    make_box(kept, repo)
    monkeypatch.setattr(sandbox, "delete", raises(CaveError("could not delete")))
    active.add(running)
    vms.volumes = [f"cave-{sid}" for sid in (kept, running, young, "not-an-id")] + [age_id(42)]  # no cave- on the last
    add_ref(repo, kept)
    monkeypatch.chdir(repo)
    prune()
    assert vms.deleted == []
    assert git.sandbox_ref_ids(repo) == [kept]


@pytest.mark.parametrize("break_it", ["no state dir", "unreadable"])
def test_a_box_that_cannot_be_ruled_out_is_no_orphan(vms, monkeypatch, repo, break_it):
    sid = age_id(40)
    vms.volumes = [volume_name(sid)]
    add_ref(repo, sid)
    monkeypatch.chdir(repo)
    if break_it == "no state dir":
        sandboxes_dir().rmdir()
    else:
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if str(path) == str(box_path(sid)):
                raise PermissionError(13, "denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
    prune()
    assert vms.deleted == []
    assert git.sandbox_ref_ids(repo) == [sid]


@pytest.mark.parametrize("error", [CaveError("no"), OSError(5, "no"), subprocess.TimeoutExpired("container", 1)])
def test_a_failed_listing_deletes_nothing_and_the_rest_of_prune_goes_on(vms, monkeypatch, repo, error, capsys):
    sid = age_id(40)
    add_ref(repo, sid)
    monkeypatch.chdir(repo)
    session = old_session(sessions_dir() / "p/-workspace-p")
    monkeypatch.setattr(container, "own_volumes", raises(error))
    prune()
    assert vms.deleted == [] and git.sandbox_ref_ids(repo) == []  # refs don't depend on this listing
    assert not session.exists() and "skipped a step" in capsys.readouterr().err


def test_with_running_unknown_nothing_of_a_sandbox_goes(vms, monkeypatch, repo, capsys):
    old = age_id(40)
    make_box(old, repo)
    vms.volumes = [volume_name(age_id(41))]
    session = old_session(sessions_dir() / "p/-workspace-p")
    monkeypatch.setattr(container, "running", raises(CaveError("no")))
    prune()
    assert sandbox.all_ids() == [old] and vms.deleted == []
    assert not session.exists() and "only sessions" in capsys.readouterr().err


def test_a_box_that_stays_loses_only_old_staging_and_landed_bundles(repo, active):
    sid, running = age_id(5), age_id(4)
    for each in (sid, running):
        box = make_box(each, repo)
        (box / "staging").mkdir()
        sandbox.write_meta(each, dict(sandbox.load_meta(each), repos=[
            dict(sandbox.load_meta(each)["repos"][0], merged=True),
            dict(sandbox.load_meta(each)["repos"][0], clone="open"),
        ]))
        for clone in ("code", "open"):
            sandbox.repo_report(each, clone, "bundle").write_text("x")
    active.add(running)
    stale, fresh = box_path(sid) / "staging/in-a", box_path(sid) / "staging/in-b"
    stale.write_text("x")
    fresh.write_text("x")
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    prune()
    assert not stale.exists() and fresh.exists()
    assert not sandbox.repo_report(sid, "code", "bundle").exists()
    assert sandbox.repo_report(sid, "open", "bundle").exists()  # all the host may ever get of it
    assert sandbox.repo_report(running, "code", "bundle").exists()


def test_a_link_in_place_of_a_landed_bundle_goes_and_its_target_stays(repo, tmp_path):
    sid = age_id(5)
    box = make_box(sid, repo)
    (box / "staging").mkdir()
    sandbox.write_meta(sid, dict(sandbox.load_meta(sid), repos=[dict(sandbox.load_meta(sid)["repos"][0], merged=True)]))
    target = tmp_path / "target"
    target.write_text("x")
    sandbox.repo_report(sid, "code", "bundle").symlink_to(target)
    prune()
    assert target.exists() and not os.path.lexists(sandbox.repo_report(sid, "code", "bundle"))


def test_a_box_that_cannot_be_tidied_does_not_stop_prune(repo):
    make_box(age_id(5), repo)  # no staging
    session = old_session(sessions_dir() / "p/-workspace-p")
    prune()
    assert not session.exists()


def test_a_stopped_sandbox_near_expiry_is_named_with_its_unlanded_work_on_every_command(repo, active, launched, capsys):
    soon, young, running, landed = age_id(24), age_id(22), age_id(25), age_id(26)
    for sid in (soon, young, running, landed):
        (make_box(sid, repo) / "staging").mkdir()
    sandbox.write_meta(soon, dict(sandbox.load_meta(soon), name="box\x1b[2J", repos=[repo_entry("code", repo, "evil\x07")]))
    sandbox.report_path(soon, "strays").write_text("".join(f"stray{n}\n" for n in range(7)))
    active.add(running)
    for kind in ("none", "finished"):
        sandbox.repo_report(landed, "code", kind).touch()
    sandbox.report_path(landed, "strays").touch()
    for command in (["ls"], ["prune"], ["init"], []):
        cli.dispatch(command)
        err = capsys.readouterr().err
        assert err.count("WARNING") == 1 and "\x1b" not in err and "\x07" not in err
        assert not {young, running, landed} & set(err.split())
        for words in (soon, r"box\x1b[2J", r"evil\x07", "stray4, 2 more", "UNLANDED", "cave merge", "--entrypoint bash", ":/workspace:ro"):
            assert words in err
    old = age_id(31)
    (make_box(old, repo) / "staging").mkdir()
    prune()
    assert sandbox.all_ids()[0] == old and "unlanded work" not in capsys.readouterr().err  # first warned just now
    warned_long_ago(old)
    prune()
    assert "its unlanded work with it: code, anything outside the repos" in capsys.readouterr().err


def test_an_expired_sandbox_with_unlanded_work_is_kept_a_day_after_its_first_warning(repo, capsys):
    never, recent, clean = age_id(40), age_id(41), age_id(42)
    for sid in (never, recent, clean):
        (make_box(sid, repo) / "staging").mkdir()
    sandbox.report_path(clean, "strays").touch()
    sandbox.repo_report(clean, "code", "none").touch()
    sandbox.repo_report(clean, "code", "finished").touch()
    warned_long_ago(recent, days=0.5)
    cli.dispatch(["prune"])
    assert set(sandbox.all_ids()) == {never, recent}  # nothing to lose in the third
    assert "WARNING" in capsys.readouterr().err and "warned" in sandbox.load_meta(never)
    warned_long_ago(recent, days=1.1)
    cli.dispatch(["prune"])
    assert sandbox.all_ids() == [never]


def test_a_deletion_the_muted_prune_thread_could_not_print_is_reported_by_the_next_command(repo, capsys):
    sid = age_id(40)
    make_box(sid, repo)
    warned_long_ago(sid)
    worker = threading.Thread(target=prune)
    log.mute(worker)
    worker.start()
    worker.join()
    assert sandbox.all_ids() == [] and capsys.readouterr().err == ""
    cli.dispatch(["ls"])
    assert f"pruning {sid}" in capsys.readouterr().err
    cli.dispatch(["ls"])
    assert "pruning" not in capsys.readouterr().err


def test_a_sandbox_warned_long_ago_and_started_again_gets_a_new_day_before_it_is_deleted(repo):
    sid = age_id(31)
    (make_box(sid, repo) / "staging").mkdir()
    sandbox.report_path(sid, "strays").write_text("new.txt\n")
    warned_long_ago(sid, days=7)
    sandbox.update_meta(sid, warned=None)  # what launch does when the sandbox starts
    prune()
    assert sandbox.all_ids() == [sid]  # warned just now, not a week ago
    assert sandbox.load_meta(sid)["warned"] > time.time() - 60


def deep_session(directory, depth):
    session = directory / "2f75b9f7-cef5-417b-8933-cc966ac1cc27"
    (session / "/".join(["d"] * depth)).mkdir(parents=True)
    stale = time.time() - 31 * 86400
    os.utime(session, (stale, stale))
    return session


def test_a_session_nested_deeper_than_the_walk_allows_is_kept_and_prune_carries_on(capsys):
    directory = sessions_dir() / "p/-workspace-p"
    deep = deep_session(directory, prune_module.MAX_DEPTH + 40)
    shallow = old_session(directory)
    prune()
    assert deep.exists() and not shallow.exists()


def test_a_session_whose_folder_will_not_open_is_not_taken_for_old(monkeypatch):
    directory = sessions_dir() / "p/-workspace-p"
    session = deep_session(directory, 3)
    real = prune_module._open_dir

    def open_dir(name, parent=None):
        if name == "d":
            raise OSError(errno.EMFILE, "too many open files")
        return real(name, parent)

    monkeypatch.setattr(prune_module, "_open_dir", open_dir)
    prune()
    assert session.exists()


def test_removing_a_tree_that_grew_too_deep_stops_with_an_error_not_a_crash(tmp_path):
    (tmp_path / "t/a/b/c").mkdir(parents=True)
    root = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(OSError):
            prune_module._remove("t", root, depth=prune_module.MAX_DEPTH - 1)
    finally:
        os.close(root)
