"""Bundles from the VM, with real git. Every bundle here is hostile input."""
import os
import subprocess

import pytest

from cave import git, sandbox, transport
from cave._testing.repos import SID, make_clone, run_git
from cave.errors import CaveError
from cave.paths import box_path

BRANCH = f"refs/heads/claude/{SID}"


@pytest.fixture
def vm(repo, box, tmp_path):
    """A clone one commit ahead of the host, with nothing in the outbox yet."""
    return make_clone(repo, tmp_path / "clone", SID)


@pytest.fixture
def entry():
    return sandbox.clones(sandbox.load_meta(SID))[0]


def outbox(kind="bundle"):
    return sandbox.repo_report(SID, "code", kind)


def bundle(clone, *revs, dest=None):
    run_git(clone, "bundle", "create", "-q", str(dest or outbox()), *(revs or (BRANCH, "^refs/cave/delivered")))


def host_state(repo):
    return git.query(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def refused(repo, entry, match):
    """fetch must raise, leaving every host ref alone and nothing in staging."""
    before = host_state(repo)
    with pytest.raises(CaveError, match=match):
        transport.fetch(entry, SID)
    assert host_state(repo) == before
    assert not list((box_path(SID) / "staging").glob("*"))


@pytest.fixture
def staged(box):
    (box / "inbox").mkdir()
    (box / "staging").mkdir()
    path = box / "staging/x.bundle"
    path.write_text("new")
    return path


def test_delivering_moves_a_file_into_the_inbox(box, staged):
    transport.deliver(SID, "x.bundle", staged)
    assert (box / "inbox/x.bundle").read_text() == "new"
    assert not staged.exists()


@pytest.mark.parametrize("plant", [lambda path: path.write_text("old"), lambda path: path.symlink_to("/nowhere")])
def test_delivering_refuses_an_existing_target_without_following_it(box, staged, plant):
    plant(box / "inbox/x.bundle")
    with pytest.raises(CaveError, match="already exists"):
        transport.deliver(SID, "x.bundle", staged)
    assert os.path.lexists(box / "inbox/x.bundle") and not staged.exists()


def test_only_an_update_replaces_what_is_in_the_inbox(box, staged):
    (box / "inbox/x.bundle").symlink_to("/nowhere")
    transport.deliver(SID, "x.bundle", staged, update=True)
    target = box / "inbox/x.bundle"
    assert target.read_text() == "new" and not target.is_symlink()


def test_a_good_bundle_lands_in_the_cave_namespace_only(repo, vm, entry):
    before = host_state(repo)
    bundle(vm)
    tip = git.query(vm, "rev-parse", BRANCH)
    assert transport.fetch(entry, SID) == tip
    assert git.query(repo, "rev-parse", transport.ref(SID, "code")) == tip
    assert set(host_state(repo).splitlines()) - set(before.splitlines()) == {f"{transport.ref(SID, 'code')} {tip}"}
    assert not (repo / ".git/FETCH_HEAD").exists()
    assert not list((box_path(SID) / "staging").iterdir())


def test_a_bundle_replaces_what_an_earlier_one_left(repo, vm, entry):
    bundle(vm)
    transport.fetch(entry, SID)
    run_git(vm, "commit", "-q", "--amend", "--allow-empty", "-m", "amended")
    bundle(vm)
    assert transport.fetch(entry, SID) == git.query(vm, "rev-parse", BRANCH)


def test_the_none_report_means_nothing_new(repo, vm, entry):
    outbox("none").touch()
    assert transport.fetch(entry, SID) is None


def test_no_report_at_all_is_an_error_never_no_changes(repo, vm, entry):
    refused(repo, entry, "no bundle")


@pytest.mark.parametrize("plant", [os.mkfifo, os.mkdir, lambda path: path.symlink_to("/etc/passwd")])
def test_a_none_report_must_be_a_regular_file(repo, vm, entry, plant):
    plant(outbox("none"))
    refused(repo, entry, "no bundle")


def test_a_symlink_to_a_good_bundle_is_refused(repo, vm, entry, tmp_path):
    bundle(vm, dest=tmp_path / "good.bundle")
    outbox().symlink_to(tmp_path / "good.bundle")
    refused(repo, entry, "could not read")


@pytest.mark.parametrize("plant", [os.mkfifo, os.mkdir])
def test_special_files_are_refused_without_blocking(repo, vm, entry, plant):
    plant(outbox())
    outbox("none").touch()  # a bundle slot in use is never taken for "nothing new"
    refused(repo, entry, "could not read")


def test_over_the_cap_is_refused_before_git_sees_it(repo, vm, entry, monkeypatch):
    bundle(vm)
    monkeypatch.setenv("CAVE_MAX_BUNDLE", str(outbox().stat().st_size - 1))
    refused(repo, entry, "over the")
    monkeypatch.setenv("CAVE_MAX_BUNDLE", str(outbox().stat().st_size))
    assert transport.fetch(entry, SID)


def test_a_bad_cap_is_an_error(repo, vm, entry, monkeypatch):
    bundle(vm)
    monkeypatch.setenv("CAVE_MAX_BUNDLE", "lots")
    refused(repo, entry, "CAVE_MAX_BUNDLE")


def test_a_truncated_bundle_is_refused(repo, vm, entry):
    bundle(vm)
    outbox().write_bytes(outbox().read_bytes()[:-30])
    refused(repo, entry, "refused|verify")


@pytest.mark.parametrize("content", [b"", b"garbage\n", b"# v2 git bundle\n", b"\x00\xff" * 1000])
def test_garbage_is_refused(repo, vm, entry, content):
    outbox().write_bytes(content)
    refused(repo, entry, "verify|nothing else")


@pytest.mark.parametrize("make, revs", [
    (["branch", "extra", BRANCH], [BRANCH, "refs/heads/extra"]),  # an extra ref
    (["tag", "v1", BRANCH], [BRANCH, "refs/tags/v1"]),  # a tag
    (["branch", "other", BRANCH], ["refs/heads/other"]),  # the wrong branch
    (["branch", "claude/20260101-000000-ffff", BRANCH], ["refs/heads/claude/20260101-000000-ffff"]),  # another sandbox's
])
def test_anything_but_exactly_its_branch_is_refused(repo, vm, entry, make, revs):
    run_git(vm, *make)
    bundle(vm, *revs, "^refs/cave/delivered")
    refused(repo, entry, "nothing else")


@pytest.mark.parametrize("name", [BRANCH.encode() + b"\x1b[31m", BRANCH.encode() + b"/../../heads/main", b"HEAD"])
def test_a_ref_name_edited_in_the_header_is_refused_and_not_printed(repo, vm, entry, name):
    bundle(vm)
    outbox().write_bytes(outbox().read_bytes().replace(BRANCH.encode(), name, 1))
    with pytest.raises(CaveError) as caught:
        transport.fetch(entry, SID)
    assert "\x1b" not in str(caught.value)
    assert git.query(repo, "for-each-ref", "refs/cave/") == ""


def test_a_missing_prerequisite_is_refused(repo, vm, entry):
    run_git(vm, "commit", "-q", "--allow-empty", "-m", "later")
    bundle(vm, BRANCH, f"^{BRANCH}~1")  # the host never had the commit this builds on
    refused(repo, entry, "verify")


def test_a_filtered_bundle_is_refused(repo, vm, entry):
    bundle(vm, "--filter=blob:none", BRANCH, "^refs/cave/delivered")
    refused(repo, entry, "refused|verify")


def write_object(clone, kind, data):
    result = subprocess.run(["git", "-C", str(clone), "hash-object", "-t", kind, "-w", "--stdin", "--literally"],
                            input=data, stdout=subprocess.PIPE, check=True)
    return result.stdout.decode().strip()


def test_a_commit_failing_fsck_is_refused(repo, vm, entry):
    tree = git.query(vm, "rev-parse", f"{BRANCH}^{{tree}}")
    parent = git.query(vm, "rev-parse", BRANCH)
    bad = write_object(vm, "commit", f"tree {tree}\nparent {parent}\nauthor no email\ncommitter x\n\nbad\n".encode())
    run_git(vm, "update-ref", BRANCH, bad)
    bundle(vm)
    refused(repo, entry, "refused")


@pytest.mark.parametrize("name", [b".git", b".GIT", b"..", b"a/b"])
def test_a_tree_with_a_hostile_entry_is_refused(repo, vm, entry, name):
    blob = write_object(vm, "blob", b"evil\n")
    tree = write_object(vm, "tree", b"100644 " + name + b"\0" + bytes.fromhex(blob))
    parent = git.query(vm, "rev-parse", BRANCH)
    who = "a <a@example.com> 1 +0000"
    bad = write_object(vm, "commit", f"tree {tree}\nparent {parent}\nauthor {who}\ncommitter {who}\n\nbad\n".encode())
    run_git(vm, "update-ref", BRANCH, bad)
    bundle(vm)
    refused(repo, entry, "refused")


def host_commit(repo, name="host.txt"):
    (repo / name).write_text("x\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", name)
    return git.query(repo, "rev-parse", "HEAD")


@pytest.fixture
def inbox(repo, box):
    """The inbox as launch left it, the VM having reported the clone it made."""
    for name in ("inbox", "staging"):
        (box / name).mkdir()
    entry = sandbox.clones(sandbox.load_meta(SID))[0]
    transport.send_repo(SID, entry.key, repo, "main")
    outbox("acked").write_text(git.query(repo, "rev-parse", "main") + "\n")
    return box / "inbox" / transport.inbox_name(entry.key)


def prerequisites(repo, path):
    return subprocess.run(["git", "-C", str(repo), "bundle", "verify", str(path)], capture_output=True, text=True).stdout


def test_an_update_holds_only_what_the_vm_lacks(repo, inbox, entry):
    start = git.query(repo, "rev-parse", "main")
    tip = host_commit(repo)
    transport.update(SID, entry)
    assert git.bundle_tip(repo, inbox) == tip
    assert start in prerequisites(repo, inbox)
    assert not list(inbox.parent.parent.glob("staging/*"))


def test_an_update_with_nothing_new_leaves_the_bundle_alone(repo, inbox, entry):
    before = inbox.stat().st_ino
    transport.update(SID, entry)
    assert inbox.stat().st_ino == before


FOREIGN = "a" * 40


def forge_ack(value):
    outbox("acked").write_text(value)


@pytest.mark.parametrize("ack", ["", "HEAD", "--upload-pack=x", "abc123", FOREIGN, "not hex\n" * 100])
def test_an_ack_that_proves_nothing_gets_a_full_bundle(repo, inbox, entry, ack):
    host_commit(repo)
    forge_ack(ack)
    transport.update(SID, entry)
    assert "complete history" in prerequisites(repo, inbox)


def test_an_ack_of_a_commit_off_the_branch_gets_a_full_bundle(repo, inbox, entry):
    run_git(repo, "checkout", "-q", "-b", "private")
    private = host_commit(repo, "private.txt")
    run_git(repo, "checkout", "-q", "main")
    host_commit(repo)
    forge_ack(private)
    transport.update(SID, entry)
    assert "complete history" in prerequisites(repo, inbox)


@pytest.mark.parametrize("plant", [lambda path, target: path.symlink_to(target), lambda path, target: os.mkfifo(path)])
def test_an_ack_is_only_read_from_a_regular_file(repo, inbox, entry, tmp_path, plant):
    host_commit(repo)
    (tmp_path / "secret").write_text(git.query(repo, "rev-parse", "main~1"))
    outbox("acked").unlink()
    plant(outbox("acked"), tmp_path / "secret")
    transport.update(SID, entry)
    assert "complete history" in prerequisites(repo, inbox)


def test_main_rewound_past_the_ack_gets_a_full_bundle(repo, inbox, entry):
    tip = host_commit(repo)
    outbox("acked").write_text(tip)
    run_git(repo, "reset", "-q", "--hard", "HEAD~1")
    transport.update(SID, entry)
    assert "complete history" in prerequisites(repo, inbox)


def test_asking_for_all_of_main_gets_it_once(repo, inbox, entry):
    outbox("need-full").touch()
    before = inbox.stat().st_ino
    transport.update(SID, entry)
    assert inbox.stat().st_ino != before and "complete history" in prerequisites(repo, inbox)
    assert not outbox("need-full").exists()


def test_a_planted_link_never_asks_and_is_never_followed(repo, inbox, entry, tmp_path):
    (tmp_path / "kept").touch()
    outbox("need-full").symlink_to(tmp_path / "kept")
    before = inbox.stat().st_ino
    transport.update(SID, entry)
    assert inbox.stat().st_ino == before and (tmp_path / "kept").exists()
