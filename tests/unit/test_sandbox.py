import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from cave import git, sandbox
from cave._testing.repos import SID, repo_entry, run_git
from cave.errors import CaveError


def test_load_meta_survives_a_corrupt_file(box):
    (box / "meta.json").write_text("{")
    assert sandbox.load_meta(SID) == {}


def test_clones_leaves_out_merged_repos(box):
    sandbox.update_repo(SID, "code", merged=True)
    assert sandbox.clones(sandbox.load_meta(SID)) == []


def test_all_ids_ignores_other_directories(box):
    (box.parent / "stray").mkdir()
    assert sandbox.all_ids() == [SID]


@pytest.mark.parametrize("marker,active,expected", [
    (True, set(), "stopped"), (False, set(), "crashed"), (False, {SID}, "running"), (True, {SID}, "running"),
    (None, set(), "unknown"),  # no metadata at all
])
def test_state(box, marker, active, expected):
    if marker is None:
        (box / "meta.json").unlink()
    elif marker:
        sandbox.repo_report(SID, "code", "finished").touch()
    assert sandbox.state(SID, active) == expected


def test_rmtree_handles_read_only_dirs(tmp_path):
    target = tmp_path / "t/ro"
    target.mkdir(parents=True)
    (target / "f").touch()
    target.chmod(0o555)
    sandbox.rmtree(tmp_path / "t")
    assert not (tmp_path / "t").exists()


def test_rmtree_does_not_follow_symlinks(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious").write_text("x")
    keep.chmod(0o500)
    (tmp_path / "t").mkdir()
    (tmp_path / "t/link").symlink_to(keep)
    sandbox.rmtree(tmp_path / "t")
    assert (keep / "precious").exists()
    assert keep.stat().st_mode & 0o777 == 0o500
    keep.chmod(0o700)


def test_delete_removes_the_container_then_its_volume_then_the_rest(box, repo, container_calls):
    run_git(repo, "update-ref", f"refs/cave/{SID}/abc", "HEAD")
    sandbox.delete(SID)
    assert [call[:3] for call in container_calls.log[:2]] == [("delete", "--force", SID), ("volume", "delete", f"cave-{SID}")]
    assert not box.exists()
    assert git.sandbox_ref_ids(repo) == []


def test_a_volume_that_will_not_go_keeps_the_sandbox(box, repo, container_calls):
    run_git(repo, "update-ref", f"refs/cave/{SID}/abc", "HEAD")
    container_calls.fake.stdout = f"cave-{SID}\n"
    with pytest.raises(CaveError, match="could not delete volume"):
        sandbox.delete(SID)
    assert box.exists() and git.sandbox_ref_ids(repo) == [SID]


def test_delete_survives_a_missing_main_clone(box, tmp_path):
    sandbox.write_meta(SID, {"repos": [repo_entry("code", tmp_path / "gone")]})
    sandbox.delete(SID)
    assert not box.exists()


def test_update_repo_is_keyed_on_the_clone_not_the_name(box, repo):
    meta = sandbox.load_meta(SID)
    meta["repos"].append(repo_entry("config", repo, name="code"))
    sandbox.write_meta(SID, meta)
    sandbox.update_repo(SID, "config", merged=True)
    assert [bool(e.get("merged")) for e in sandbox.load_meta(SID)["repos"]] == [False, True]


def test_the_copy_out_command_mounts_read_only_and_runs_nothing_from_the_volume():
    command = sandbox.copy_out_command(SID, {"image": "cave:2024-abc"})
    assert f"-v cave-{SID}:/workspace:ro " in command and "--mount" not in command  # a comma in $HOME splits --mount
    assert "--entrypoint bash" in command and "cave:2024-abc -o pipefail -c " in command
    assert "-type f -o -type l" in command and "cp -P --parents" in command and "chmod ug-s" in command
    assert "<image>" in sandbox.copy_out_command(SID, {})


@pytest.mark.parametrize("plant", [os.mkfifo, lambda path: path.symlink_to("/etc/passwd"), os.mkdir])
def test_reports_must_be_regular_files(box, plant):
    for path in (sandbox.repo_report(SID, "code", "embedded"), sandbox.report_path(SID, "strays"),
                 sandbox.repo_report(SID, "code", "finished"), sandbox.repo_report(SID, "code", "busy")):
        plant(path)
    assert not sandbox.finished(SID, "code")
    assert not sandbox.mid_operation(SID, "code")
    assert sandbox.unlanded(SID, sandbox.load_meta(SID)) == ["anything outside the repos, as the VM never said"]


def test_unlanded_work_comes_from_the_reports(box):
    meta = sandbox.load_meta(SID)
    sandbox.repo_report(SID, "code", "embedded").write_text("new/\n")
    sandbox.report_path(SID, "strays").write_text("b.txt\na.txt\n")
    assert sandbox.unlanded(SID, meta) == ["code/new", "a.txt", "b.txt"]


@pytest.mark.parametrize("plant", [
    lambda path: path.write_text("pwned\x1b[2J\n"),
    lambda path: path.write_text("workingX\n"),
    lambda path: path.symlink_to("/etc/passwd"),
    lambda path: os.mkfifo(path),
])
def test_a_status_the_host_did_not_expect_is_unknown(box, plant):
    plant(box / "outbox/status")
    assert sandbox.activity(SID) == "unknown"


@pytest.mark.parametrize("word", sandbox.ACTIVITIES)
def test_status_is_one_of_these_words(box, word):
    (box / "outbox/status").write_text(f"{word}\n")
    assert sandbox.activity(SID) == word


@pytest.mark.parametrize("text, shown", [("3\n", "3"), ("-1\n", "?"), ("٣\n", "?"), ("9" * 99, "?"), (None, "?")])
def test_uncommitted_counts_only_a_plain_number(box, text, shown):
    if text is not None:
        sandbox.repo_report(SID, "code", "dirty").write_text(text)
    assert sandbox.uncommitted(SID, sandbox.load_meta(SID)) == shown


def test_ls_reports_only_on_a_running_sandbox(box, active):
    (box / "outbox/status").write_text("idle\n")
    sandbox.repo_report(SID, "code", "dirty").write_text("2\n")
    (box / "outbox/request-0123456789abcdef").write_text("review\n")
    assert " idle " not in sandbox.table()
    active.add(SID)
    assert sandbox.table().splitlines()[1].split()[1:5] == ["running", "idle", "2", "1"]


NAME = "request-0123456789abcdef"


@pytest.mark.parametrize("text, asked", [("review\n", None), ("add-repo sub dir\n", "sub dir"), ("review", None)])
def test_a_request_has_a_fixed_format(box, text, asked):
    (box / "outbox" / NAME).write_text(text)
    assert [request.repo for request in sandbox.requests(SID)] == [asked]


@pytest.mark.parametrize("name, data", [
    (NAME, b"delete everything\n"),
    (NAME, b"review please\n"),
    (NAME, b"add-repo \n"),
    (NAME, b"add-repo a\nadd-repo b\n"),
    (NAME, b"add-repo a\x00b\n"),
    (NAME, b"add-repo \x1b[2J\n"),
    (NAME, b"add-repo \xff\n"),
    (NAME, b"add-repo " + b"a" * sandbox.MAX_REQUEST + b"\n"),
    ("request-1", b"review\n"),
    ("request-0123456789ABCDEF", b"review\n"),
    ("../request-0123456789abcdef", b"review\n"),
    (".request-0123456789abcdef", b"review\n"),
])
def test_a_hostile_request_is_no_request(box, name, data):
    (box / "outbox" / name).write_bytes(data)
    assert sandbox.requests(SID) == []


def test_a_link_or_fifo_is_no_request(box, tmp_path):
    target = tmp_path / "target"
    target.write_text("review\n")
    (box / "outbox" / NAME).symlink_to(target)
    os.mkfifo(box / "outbox/request-0000000000000000")
    assert sandbox.requests(SID) == []


def test_requests_are_capped(box):
    for number in range(sandbox.MAX_REQUESTS + 5):
        (box / "outbox" / f"request-{number:016x}").write_text("review\n")
    assert len(sandbox.requests(SID)) == sandbox.MAX_REQUESTS


def test_concurrent_meta_changes_all_survive(box, repo):
    keys = [f"code{number}" for number in range(8)]
    meta = sandbox.load_meta(SID)
    meta["repos"] = [repo_entry(key, repo) for key in keys]
    sandbox.write_meta(SID, meta)

    def change_and_read(key):
        sandbox.update_repo(SID, key, landed=key)
        return bool(sandbox.load_meta(SID))  # lock-free, as sweeps and commands read it

    with ThreadPoolExecutor(len(keys)) as pool:
        assert all(pool.map(change_and_read, keys * 25))
    assert [entry.get("landed") for entry in sandbox.load_meta(SID)["repos"]] == keys


def test_a_repo_joins_ahead_of_the_config_repo(box, repo):
    meta = sandbox.load_meta(SID)
    meta["repos"].append(repo_entry("config", repo))
    sandbox.write_meta(SID, meta)
    sandbox.add_repo(SID, repo_entry("code/new", repo))
    assert [entry["clone"] for entry in sandbox.load_meta(SID)["repos"]] == ["code", "code/new", "config"]


def test_an_unreadable_outbox_does_not_break_the_listing(box, active):
    outbox = box / "outbox"
    outbox.chmod(0)
    try:
        assert " crashed " in sandbox.table()
    finally:
        outbox.chmod(0o700)
