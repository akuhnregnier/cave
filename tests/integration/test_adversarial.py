"""A hostile VM, end to end. test_transport, test_review and test_log take each input apart;
this is what only the whole path shows: main stays put, the sandbox stays, and nothing leaks."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

TWO_COMMITS = "for n in 1 2; do echo $n >> file.txt; git commit -qam $n; done"


def only_bundle(vm, sid):
    return next((vm.box(sid) / "outbox").glob("*.bundle"))


def in_clone(vm, sid, *args):
    vm.git(*args, repo=vm.root / "volumes" / f"cave-{sid}/project")


def truncated(vm, sid, bundle):
    bundle.write_bytes(bundle.read_bytes()[:-30])


def garbage(vm, sid, bundle):
    bundle.write_bytes(b"\x1b]0;PWNED\x07 not a bundle\n" * 50)


def link_to_a_good_bundle(vm, sid, bundle):
    outside = vm.root / "outside.bundle"
    bundle.rename(outside)
    bundle.symlink_to(outside)


def fifo(vm, sid, bundle):
    bundle.unlink()
    os.mkfifo(bundle)


def folder(vm, sid, bundle):
    bundle.unlink()
    bundle.mkdir()


def another_ref(vm, sid, bundle):
    in_clone(vm, sid, "branch", "extra")
    in_clone(vm, sid, "bundle", "create", "-q", str(bundle), f"claude/{sid}", "extra", "^refs/cave/delivered")


def missing_prerequisite(vm, sid, bundle):
    in_clone(vm, sid, "bundle", "create", "-q", str(bundle), f"claude/{sid}", f"^claude/{sid}~1")


def ref_with_escapes(vm, sid, bundle):
    name = f"refs/heads/claude/{sid}".encode()
    bundle.write_bytes(bundle.read_bytes().replace(name, name + b"\x1b[2J", 1))


def blob_for_a_tip(vm, sid, bundle):
    in_clone(vm, sid, "tag", "blob", f"claude/{sid}:file.txt")
    in_clone(vm, sid, "bundle", "create", "-q", str(bundle), "refs/tags/blob")
    bundle.write_bytes(bundle.read_bytes().replace(b"refs/tags/blob", f"refs/heads/claude/{sid}".encode(), 1))


def sha256_bundle(vm, sid, bundle):
    head, pack = bundle.read_bytes().split(b"\n\n", 1)
    bundle.write_bytes(head.replace(b"# v2 git bundle\n", b"# v3 git bundle\n@object-format=sha256\n") + b"\n\n" + pack)


@pytest.mark.parametrize("tamper", [truncated, garbage, link_to_a_good_bundle, fifo, folder, another_ref,
                                    missing_prerequisite, ref_with_escapes, blob_for_a_tip, sha256_bundle])
def test_a_bad_bundle_never_moves_main_or_loses_the_sandbox(fake_vm, tamper):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=TWO_COMMITS)
    tamper(fake_vm, sid, only_bundle(fake_vm, sid))
    main = fake_vm.git("rev-parse", "main")
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1 and "held back" in result.stderr
    assert fake_vm.git("rev-parse", "main") == main and fake_vm.boxes() == [sid]
    assert "\x1b" not in result.stdout + result.stderr and "Traceback" not in result.stderr
    shown = fake_vm.cave("diff", sid, ok=False)
    assert shown.returncode == 1 and "\x1b" not in shown.stdout + shown.stderr and "commits," not in shown.stdout


def test_an_oversized_bundle_never_moves_main(fake_vm):
    sid = fake_vm.launch(FAKE_VM_SCRIPT=TWO_COMMITS)
    result = fake_vm.cave("merge", sid, ok=False, CAVE_MAX_BUNDLE="10")
    assert "byte cap" in result.stderr and fake_vm.boxes() == [sid]
    assert fake_vm.git("log", "--format=%s") == "first"


@pytest.mark.parametrize("report, text", [("strays", b"a\x1b]52;c;AAAA\x07\nb\xff\n"), ("embedded", b"x\x1b[2J/\n")])
def test_reports_of_what_stays_behind_reach_the_terminal_escaped(fake_vm, report, text):
    sid = fake_vm.launch(FAKE_VM_SCRIPT="git log -1 > /dev/null")
    outbox = fake_vm.box(sid) / "outbox"
    (outbox / report if report == "strays" else next(outbox.glob("*.none")).with_suffix(".embedded")).write_bytes(text)
    result = fake_vm.cave("merge", sid, ok=False)
    assert result.returncode == 1 and fake_vm.boxes() == [sid]
    assert r"\x1b" in result.stderr and "\x1b" not in result.stderr


def deep(path):
    """Deeper than Python's recursion limit, which a VM has no reason to respect."""
    script = "import os\nfor _ in range(1200):\n    os.mkdir('d')\n    os.chdir('d')"
    subprocess.run([sys.executable, "-c", script], cwd=str(path), check=True)


def test_a_folder_too_deep_for_python_cannot_keep_a_sandbox(fake_vm):
    sid = fake_vm.launch()
    deep(fake_vm.box(sid) / "outbox")
    fake_vm.cave("discard", sid)
    assert fake_vm.boxes() == []


def test_a_session_too_deep_for_python_cannot_stop_prune(fake_vm):
    sessions = Path(fake_vm.env["HOME"]) / "Library/Application Support/cave/sessions/p/key"
    session = sessions / "2f75b9f7-cef5-417b-8933-cc966ac1cc27"
    session.mkdir(parents=True)
    deep(session)
    other = sessions / "7585e220-db2a-4e4b-94d1-2bb9819c18aa.jsonl"
    other.write_text("{}")
    os.utime(other, (0, 0))
    result = fake_vm.cave("prune")
    assert not other.exists() and "Traceback" not in result.stderr
    subprocess.run(["rm", "-rf", str(session)], check=True)


def test_a_folder_name_with_escapes_reaches_the_terminal_escaped(fake_vm):
    root = fake_vm.add_repo(fake_vm.root / "evil\x1b[2J")
    start = fake_vm.cave("run", ok=False, cwd=root)
    listing = fake_vm.cave("ls", cwd=root)
    assert "\x1b" not in start.stderr + start.stdout + listing.stdout
    assert r"evil\x1b[2J" in listing.stdout
