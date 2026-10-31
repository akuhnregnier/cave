"""Helpers for tests that boot real VMs."""
import fcntl
import hashlib
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from cave import container, image

HERE = Path(__file__).parent
LOCK_DIR = Path(tempfile.gettempdir())
CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_BUDGET_USD = "0.10"  # per run, the most any one Claude run here may spend
SPEND_TOKENS_VAR = "CAVE_TEST_SPEND_TOKENS"
PRUNABLE_IMAGE = "cave:20200101-000000000000"  # the one image a test launch may delete
LIVE = "/workspace/project/live.txt"  # the stub writes this once it's up


def vm_available():
    if shutil.which("container") is None:
        return False
    return subprocess.run(["container", "system", "status"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def max_vms():
    """How many test VMs this host can run at once: 2 vCPUs and 2G each, with room to spare."""
    cpus = os.cpu_count() or 2
    memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return max(1, min(cpus // 2, memory // (6 * 1024 ** 3)))


def max_workers():
    """pytest workers. Most tests only wait on subprocesses, so leave two CPUs free."""
    return max(2, (os.cpu_count() or 2) - 2)


@contextmanager
def _flock(path, flags=fcntl.LOCK_EX):
    """Hold an exclusive lock across pytest workers. The OS drops it if a worker dies."""
    with open(path, "w") as handle:
        fcntl.flock(handle, flags)
        yield


@contextmanager
def vm_slot():
    """Wait for one of max_vms() slots."""
    while True:
        for slot in range(max_vms()):
            try:
                with _flock(LOCK_DIR / f"cave-test-vm-{slot}.lock", fcntl.LOCK_EX | fcntl.LOCK_NB):
                    yield
                    return
            except BlockingIOError:
                continue
        time.sleep(0.2)


def build_once(harness, user_dockerfile):
    """Build an image unless it exists. Workers share images, so they build in turn."""
    digest = image.image_hash(user_dockerfile)
    with _flock(LOCK_DIR / "cave-test-build.lock"):
        if not any(name.endswith(f"-{digest}") for name in container.image_names()):
            harness.cave("build")


def image_guard():
    """A `container` that only a real VM test sees, and that keeps the user's images.

    Images are shared with the real HOME, but the sandboxes that keep them there aren't, so
    a launch in a test would delete any over a week old. Only PRUNABLE_IMAGE can go.
    """
    return (f'#!/bin/bash\n[[ "$1 $2" =~ ^image\\ (rm|delete)$ && "$3" != {PRUNABLE_IMAGE} ]] && exit 0\n'
            f'exec {shlex.quote(shutil.which("container"))} "$@"\n')


def install_stub_claude(harness):
    """Commit a config whose image layer swaps claude for stub_claude.sh."""
    config = harness.config
    harness.cave("init")
    stubs = [("stub_claude.sh", "stub"), ("slow_git", "slow_git")]
    for source, name in stubs:
        shutil.copy(HERE / source, config / name)
    # The image hash only covers the Dockerfile, so a changed stub must change it too.
    stub_hash = hashlib.sha256(b"".join((config / name).read_bytes() for _, name in stubs)).hexdigest()
    (config / "Dockerfile").write_text(f"{(HERE / 'stub.Dockerfile').read_text()}# stubs {stub_hash}\n")
    (config / "settings.json").write_text('{"enableAllProjectMcpServers": false, "model": "sonnet", '
                                          '"sandbox": {"enabled": true, "x": 1}}\n')
    (config / "CLAUDE.md").write_text("# test instructions\n")
    harness.git("add", "-A", repo=config)
    harness.git("commit", "-qm", "test config", repo=config)
    build_once(harness, (config / "Dockerfile").read_text())


def require_token_approval():
    """Fail unless `make test-integration-container` was acknowledged."""
    if os.environ.get(SPEND_TOKENS_VAR) != "1":
        pytest.fail(f"{SPEND_TOKENS_VAR}=1 is required; use `make test-integration-container`", pytrace=False)


def ask_claude(harness, prompt, *flags):
    """One real, budget-capped Claude run in a new sandbox. This spends tokens."""
    require_token_approval()
    return harness.cave("run", "--", "-p", prompt, "--model", CLAUDE_MODEL,
                        "--max-budget-usd", CLAUDE_BUDGET_USD, *flags, ok=False)


def wait_for_running(harness, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = harness.cave("ls").stdout.splitlines()
        if len(lines) > 1 and lines[1].split()[1] == "running":
            return lines[1].split()[0]
        time.sleep(1)
    raise AssertionError("sandbox never started")


def stop_timed(harness, sid):
    """`cave stop`, returning how many seconds it took."""
    started = time.time()
    harness.cave("stop", sid)
    return time.time() - started


def wait_until(condition, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.5)
    raise AssertionError("condition never became true")


def in_vm(sid, path):
    """Does path exist in a running VM? Its clones are out of the host's reach."""
    return subprocess.run(["container", "exec", sid, "test", "-e", path], capture_output=True).returncode == 0


@contextmanager
def session(harness, mode):
    """A stub session, up and working. Yields its cave process and sandbox id."""
    process = harness.start("run", "--", mode)
    try:
        sid = wait_for_running(harness)
        wait_until(lambda: in_vm(sid, LIVE))
        yield process, sid
    finally:
        # VMs first: killing cave leaves its `container run` holding the pipes until its VM goes, and a
        # failing test must not wait out the stub's 300 s sleep.
        for box in harness.boxes():
            subprocess.run(["container", "delete", "--force", box], capture_output=True, timeout=60)
        process.kill()
        process.communicate(timeout=60)


def _start_on_pty(harness):
    env = {**os.environ, **harness.env, "TERM": "xterm-256color"}
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(harness.repo))
        os.execve(sys.executable, [sys.executable, "-m", "cave"], env)
    return pid, fd


def _plain(screen):
    """Screen bytes as text. A TUI moves the cursor between words, so those become spaces."""
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", " ", screen.decode("utf8", "replace"))
    return re.sub(r" +", " ", text)


def _read_until(fd, until, deadline):
    screen = b""
    while time.time() < deadline and until not in _plain(screen):
        if select.select([fd], [], [], 0.5)[0]:
            try:
                screen += os.read(fd, 65536)
            except OSError:
                break
    return screen


def exit_interactive_session(harness, timeout=90):
    """Type /exit into a real session. True if it ended because of that."""
    pid, fd = _start_on_pty(harness)
    deadline = time.time() + timeout
    try:
        _read_until(fd, "bypass permissions", deadline)
        for key in "/exit":
            os.write(fd, key.encode())
            time.sleep(0.05)
        time.sleep(0.5)
        os.write(fd, b"\r")
        _read_until(fd, "will never appear", time.time() + 20)
        ended, _ = os.waitpid(pid, os.WNOHANG)
        if not ended:
            harness.cave("discard", harness.boxes()[-1], ok=False)
            os.waitpid(pid, 0)
        return ended != 0
    finally:
        os.close(fd)


def interactive_screen(harness, until, timeout=90):
    """Run `cave` on a pty until `until` appears, discard it, and return the screen text."""
    pid, fd = _start_on_pty(harness)
    try:
        screen = _read_until(fd, until, time.time() + timeout)
    finally:
        if harness.boxes():
            harness.cave("discard", harness.boxes()[-1], ok=False)
        os.close(fd)
        os.waitpid(pid, 0)
    return _plain(screen)
