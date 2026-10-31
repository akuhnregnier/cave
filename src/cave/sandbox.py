"""On-disk sandboxes.

Clones live on a named volume that outlives the container and that only a VM can read,
and the VM can write anything into them, including their .git. All the host can do with
one is delete it. What it knows of a sandbox comes from the outbox, as bundles that
transport.py checks and short reports that prove nothing beyond their own bytes.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

from cave import container, git
from cave.errors import CaveError
from cave.log import warn
from cave.paths import box_path, is_id, network_name, sandboxes_dir, volume_name

MAX_MARKER = 65536
MAX_REQUEST = 4096
MAX_REQUESTS = 20  # at a time: each is a prompt, and a VM can write as many as it likes
REQUEST_NAME = re.compile(r"request-[0-9a-f]{16}")
REQUEST_TEXT = re.compile(r"review|add-repo (?P<repo>[^\x00-\x1f\x7f]+)")
ACTIVITIES = ("working", "idle")

Clone = namedtuple("Clone", "key repo base name landed_tip landed conflict", defaults=(False,))
Request = namedtuple("Request", "file repo")  # with no repo, it's asking for a review


def all_ids():
    root = sandboxes_dir()
    return sorted(p.name for p in root.iterdir() if is_id(p.name)) if root.is_dir() else []


def write_meta(sid, meta):
    """Atomically, as commands and sweeps read this without a lock. Changes go through _meta_lock."""
    temp = box_path(sid) / f".meta.{os.getpid()}"
    temp.write_text(json.dumps(meta))
    os.replace(temp, box_path(sid) / "meta.json")


def load_meta(sid):
    try:
        return json.loads((box_path(sid) / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def require_meta(sid):
    meta = load_meta(sid)
    if not meta:
        raise CaveError(f"no such sandbox: {sid}")
    return meta


def clones(meta):
    """The repos not yet merged, with the config repo last."""
    return [Clone(e["clone"], Path(e["repo"]), e["base"], e["name"], e.get("landed_tip"), e.get("landed"),
                  e.get("conflict", False))
            for e in meta.get("repos", []) if not e.get("merged")]


@contextmanager
def _meta_lock(sid):
    """Held only to read, change and write, never over a prompt, so waiting for it costs nothing."""
    with open(box_path(sid) / ".meta.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def update_repo(sid, key, **fields):
    """Change one repo's entry. key is its `clone`: display names needn't be unique."""
    with _meta_lock(sid):
        meta = load_meta(sid)
        next(entry for entry in meta["repos"] if entry["clone"] == key).update(fields)
        write_meta(sid, meta)


def update_meta(sid, **fields):
    """Change the sandbox's own entries, unless its meta can't be read: writing then would erase it."""
    with _meta_lock(sid):
        meta = load_meta(sid)
        if meta:
            write_meta(sid, dict(meta, **fields))


def add_repo(sid, entry):
    """Join a repo to a sandbox, ahead of the config repo, which goes last."""
    with _meta_lock(sid):
        meta = load_meta(sid)
        repos = meta["repos"]
        repos.insert(next((at for at, other in enumerate(repos) if other["clone"] == "config"), len(repos)), entry)
        write_meta(sid, meta)


def report_key(clone):
    """Names a repo's files in the outbox. From the host, so never from a VM path."""
    return hashlib.sha256(clone.encode()).hexdigest()[:12]


def report_path(sid, name):
    return box_path(sid) / "outbox" / name


def repo_report(sid, clone, kind):
    return report_path(sid, f"{report_key(clone)}.{kind}")


def open_report(path):
    """Open a VM-written file, which might be a symlink or a FIFO, or become one at any time."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    return os.fdopen(fd, "rb")


def has_report(path):
    try:
        open_report(path).close()
        return True
    except OSError:
        return False


def read_report(path):
    try:
        with open_report(path) as handle:
            return handle.read(MAX_MARKER).decode(errors="replace")
    except OSError:
        return ""


def unlanded(sid, meta):
    """Work in the sandbox that no repo can carry to the host. No report counts as some."""
    found = []
    for entry in meta.get("repos", []):
        for line in read_report(repo_report(sid, entry["clone"], "embedded")).splitlines():
            found.append(os.path.normpath(os.path.join(entry["name"], line)))
    strays = report_path(sid, "strays")
    if not has_report(strays):
        return found + ["anything outside the repos, as the VM never said"]
    return found + sorted(read_report(strays).splitlines())


def copy_out_command(sid, meta):
    """A shell command that copies the volume to ~/cave-rescue/<id>, for work no repo can carry.

    The volume is hostile. It is mounted read-only (-v, as --mount splits on a comma in $HOME), the
    image's entrypoint is bypassed so nothing in it runs, and only find, xargs, mkdir, cp and chmod
    from the image run. Only regular files, folders and symlinks (kept as links) are copied, so no
    FIFO or device node reaches the host's folder, and no setuid or setgid bit survives. Root is kept
    for reading, as the VM can leave folders that nobody else can enter.
    """
    dest = f'"$HOME/cave-rescue/{sid}"'
    script = ("cd /workspace && find . -type d -empty -print0 | xargs -0 -r -I{} mkdir -p -- /out/{} "
              "&& find . \\( -type f -o -type l \\) -print0 | xargs -0 -r cp -P --parents -t /out -- "
              "&& find /out ! -type l -perm /6000 -exec chmod ug-s -- {} +")
    return (f"mkdir -p {dest} && container run --rm --user root --entrypoint bash "
            f"-v {volume_name(sid)}:/workspace:ro -v {dest}:/out "
            f"{shlex.quote(meta.get('image') or '<image>')} -o pipefail -c {shlex.quote(script)}")


def finished(sid, clone):
    return has_report(repo_report(sid, clone, "finished"))


def mid_operation(sid, clone):
    """Did the VM say a merge or rebase is under way in this repo?"""
    return has_report(repo_report(sid, clone, "busy"))


def settled(sid, clone):
    """Has a final snapshot done all it can here? It skips a repo mid-merge, and writes strays last."""
    return finished(sid, clone) or (mid_operation(sid, clone) and has_report(report_path(sid, "strays")))


def holds_work(sid, clone):
    """Might this repo hold what the host lacks? Only a finished snapshot with nothing new says no."""
    return not (finished(sid, clone) and has_report(repo_report(sid, clone, "none")))


def activity(sid):
    text = read_report(report_path(sid, "status")).strip()
    return text if text in ACTIVITIES else "unknown"


def uncommitted(sid, meta):
    """How many files the VM says are uncommitted, or ? unless every repo says so."""
    counts = [read_report(repo_report(sid, clone.key, "dirty")).strip() for clone in clones(meta)]
    return str(sum(map(int, counts))) if all(re.fullmatch("[0-9]{1,9}", count) for count in counts) else "?"


def _read_request(path):
    try:
        with open_report(path) as handle:
            data = handle.read(MAX_REQUEST + 1)
        text = data.decode().rstrip("\n")
    except (OSError, ValueError):
        return None
    match = REQUEST_TEXT.fullmatch(text) if len(data) <= MAX_REQUEST else None
    return match and Request(path, match["repo"])


def requests(sid):
    """The well-formed requests waiting in the outbox. Anything else there is ignored, and none has any effect."""
    try:
        names = sorted(name for name in os.listdir(box_path(sid) / "outbox") if REQUEST_NAME.fullmatch(name))
    except OSError:
        return []
    return [found for found in (_read_request(report_path(sid, name)) for name in names[:MAX_REQUESTS]) if found]


def rmtree(path):
    # The VM can leave read-only directories, and any depth, which shutil.rmtree would die of.
    # Neither of these follows symlinks.
    subprocess.run(["chmod", "-R", "u+rwx", str(path)], check=False, stderr=subprocess.DEVNULL)
    subprocess.run(["rm", "-rf", str(path)], check=False, stderr=subprocess.DEVNULL)
    if path.exists():
        raise CaveError(f"could not delete {path}")


def delete(sid, extra_repos=()):
    repos = {entry["repo"] for entry in load_meta(sid).get("repos", [])}
    container.force_delete(sid)
    if not container.delete_volume(volume_name(sid)):  # the box is what a retry goes by
        raise CaveError(f"could not delete volume {volume_name(sid)}, so sandbox {sid} is kept")
    for repo in repos | {str(repo) for repo in extra_repos}:
        if Path(repo).is_dir():
            git.delete_sandbox_refs(repo, sid)
    if not container.delete_network(network_name(sid)):  # prune sweeps it later: no work depends on it
        warn(f"could not delete network {network_name(sid)}")
    if box_path(sid).exists():
        rmtree(box_path(sid))


def state(sid, active):
    meta = load_meta(sid)
    if sid in active:
        return "running"
    if not meta:
        return "unknown"
    return "stopped" if all(settled(sid, clone.key) for clone in clones(meta)) else "crashed"


def table():
    active = container.running()
    rows = []
    for sid in all_ids():
        meta = load_meta(sid)
        live = (activity(sid), uncommitted(sid, meta), str(len(requests(sid)))) if sid in active else ("-", "-", "-")
        rows.append((sid, state(sid, active), *live, meta.get("name", "?"), meta.get("root", "?")))
    if not rows:
        return ""
    rows.insert(0, ("ID", "STATE", "ACTIVITY", "UNCOMMITTED", "REQUESTS", "NAME", "ROOT"))
    width = max(len(row[5]) for row in rows)
    return "\n".join(f"{sid:<22} {status:<8} {doing:<8} {dirty:<11} {asks:<8} {name:<{width}} {root}"
                     for sid, status, doing, dirty, asks, name, root in rows)
