"""The Apple `container` CLI."""
import json
import os
import shutil
import subprocess
import tempfile
import time

from cave.errors import CaveError
from cave.paths import state_key

STOP_SECONDS = 20  # 5 for Claude to exit, and the rest for slow git commands
EXEC_SECONDS = 300  # a hostile VM can hang an exec forever
CLEANUP_SECONDS = 30  # deleting runs during prune, which must never block a launch
TOKEN_SECONDS = 30  # the entrypoint waits a little longer than this
QUERY_SECONDS = 30  # for anything that only asks `container` a question
START_SECONDS = 120  # `container system start` may have a lot to bring up
MAX_ERROR = 2000  # of the end of what a failed VM said


def container(*args, **kwargs):
    return subprocess.run(["container", *args], **kwargs)


def _bounded(*args, timeout=QUERY_SECONDS, **kwargs):
    """`container`, where a hung service raises CaveError instead of leaving us waiting for good."""
    try:
        return container(*args, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise CaveError(f"container {args[0]} did not answer within {timeout} seconds: is the container service stuck?")


def _succeeds(*args, timeout=QUERY_SECONDS):
    result = _bounded(*args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    return result.returncode == 0


def _listing(*args, timeout=QUERY_SECONDS):
    """What a listing prints. A failed one raises, as it must never read as an empty list."""
    result = _bounded(*args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise CaveError(f"container {' '.join(args)} failed")
    return result.stdout


def ensure_system():
    if shutil.which("container") is None:
        raise CaveError("Apple `container` not found (brew install container)")
    if not _succeeds("system", "status"):
        _bounded("system", "start", check=True, timeout=START_SECONDS)


def running(timeout=QUERY_SECONDS, strict=False):
    """Ids of the running VMs. Raises if unknown, unless the system is down, so that none can be.

    strict is for what deletes: there, a system that merely seems down is unknown too.
    """
    try:
        return set(_listing("ls", "--quiet", timeout=timeout).split())
    except CaveError:
        if strict or _succeeds("system", "status", timeout=timeout):
            raise
        return set()


def own_volumes():
    """The names of the volumes this state dir created, going by their label. Raises if unknown."""
    return _own("volume")


def _own(kind):
    text = _listing(kind, "ls", "--format", "json", timeout=CLEANUP_SECONDS)
    try:
        return [entry["configuration"]["name"] for entry in json.loads(text)
                if entry["configuration"]["labels"].get("cave") == state_key()]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise CaveError(f"could not read the {kind} listing")


def own_networks():
    """The names of the networks this state dir created, going by their label. Raises if unknown."""
    return _own("network")


def image_names():
    result = _bounded("image", "ls", "--quiet", text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.split()


def images_in_use():
    """Short names of the images running containers use. Raises if that's unknown."""
    try:
        entries = json.loads(_listing("ls", "--format", "json"))
        return {entry["configuration"]["image"]["reference"].rsplit("/", 1)[-1] for entry in entries}
    except (AttributeError, KeyError, TypeError, ValueError):
        raise CaveError("could not read the container listing")


def delete_image(name):
    return _succeeds("image", "rm", name)


def build(*args):
    # Not -q: `container build -q` hangs in container 1.1.0.
    if container("build", "--progress", "plain", *args).returncode:
        raise CaveError("image build failed")


def tag(source, target):
    container("image", "tag", source, target, check=True)


def stop(name):
    """Stop a VM, allowing the time its entrypoint needs for the final commit.

    USR1 tells the entrypoint this stop has STOP_SECONDS, so Claude gets its grace period.
    """
    return _bounded("stop", "--signal", "USR1", "--time", str(STOP_SECONDS), name, stdout=subprocess.DEVNULL,
                    timeout=STOP_SECONDS + 10).returncode == 0


def run_companion(name, *args):
    """Ask a running VM's companion to act. Its answer arrives as files: output isn't read."""
    return not run_to_end("exec", "--user", "claude", name, "cave-companion", *args)


def run_to_end(*args):
    """`container run` for a VM that only leaves files, bounded as it might hang. Returns why it failed, or ''.

    What it says goes to a file, not a pipe held in memory, as the VM decides how much that is.
    """
    with tempfile.TemporaryFile() as said:
        try:
            status = container(*args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=said,
                               timeout=EXEC_SECONDS).returncode
        except subprocess.TimeoutExpired:
            return f"it was still running after {EXEC_SECONDS} seconds"
        if not status:
            return ""
        said.seek(max(0, said.seek(0, os.SEEK_END) - MAX_ERROR))
        return said.read().decode(errors="replace").strip() or f"container run exited with status {status}"


def boot(*args):
    """`container run`, left running: a token can only follow once the VM is up."""
    return subprocess.Popen(["container", *args])


def send_token(name, token, vm):
    """Hand a booting VM its token over stdin, which keeps it out of argv and the stored config."""
    deadline = time.time() + TOKEN_SECONDS
    while time.time() < deadline:
        try:
            result = container("exec", "-i", "--user", "claude", name, "cave-companion", "token", input=f"{token}\n",
                               text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=deadline - time.time())
        except subprocess.TimeoutExpired:
            return False
        if result.returncode == 0:
            return True
        if vm.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def create_volume(name):
    return _succeeds("volume", "create", "--label", f"cave={state_key()}", name)


def force_delete(name):
    """Returns why the container may still be there, or ''. Those that need it gone go by the volume or `run`."""
    try:
        result = container("delete", "--force", name, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                           errors="replace", timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        return f"container delete was still running after {CLEANUP_SECONDS} seconds"
    return "" if result.returncode == 0 or "not found" in result.stderr else result.stderr.strip()


def delete_volume(name):
    """True once the volume is gone, as a missing one already is. Unknown counts as still there."""
    try:
        container("volume", "delete", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  timeout=CLEANUP_SECONDS)
        listing = container("volume", "ls", "--quiet", text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return listing.returncode == 0 and name not in listing.stdout.split()


def create_network(name):
    return _succeeds("network", "create", "--label", f"cave={state_key()}", name, timeout=CLEANUP_SECONDS)


def ensure_network(name):
    """Make sure this sandbox's network exists, or raise: a VM must never fall back to a shared one."""
    if name not in own_networks() and not create_network(name):
        raise CaveError(f"could not create network {name}: sandbox VMs each get one so that they can't reach each other")


def delete_network(name):
    """True once the network is gone, as a missing one already is. Unknown counts as still there.

    Only a network with this state dir's label is ever deleted. `container` refuses while any
    container, even a stopped one, refers to it.
    """
    try:
        if name not in own_networks():
            return True
        container("network", "delete", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  timeout=CLEANUP_SECONDS)
        return name not in own_networks()
    except (subprocess.TimeoutExpired, CaveError):
        return False
