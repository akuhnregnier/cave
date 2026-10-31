"""Host git. Everything runs in a main clone or a scratch clone of one, never against a sandbox clone."""
import fcntl
import os
import re
import signal
import subprocess
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from cave.errors import CaveError
from cave.log import warn

NO_HOOKS = ["git", "-c", "core.hooksPath=/dev/null"]
MIN_VERSION = (2, 50, 1)
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
FETCH_TIMEOUT = 300  # seconds; a hostile pack can be slow to unpack
MAX_WRITTEN = 4 << 30  # bytes a rebase may write to scratch; a bundle's size says little of that
MAX_LISTING = 32 << 20  # bytes of change listing read, some quarter of a million entries: review holds it several times over
LISTING_TIMEOUT = 120  # seconds for git to produce that listing, and for sizing what it names
VERIFY_TIMEOUT = 600  # seconds for bundle verify and list-heads, which read the whole pack
REBASE_TIMEOUT = 1800  # seconds for the rebase, which checks out every new commit


class RebaseFailed(CaveError):
    def __init__(self, output):
        super().__init__("rebase conflict")
        self.output = output


def require_version():
    """Refuse a git older than the one cave's safety was tested on.

    This git parses bundles the VM wrote, so its handling of them is part of the
    security: the hostile bundle tests are what pins this version down.
    """
    try:
        output = subprocess.run(["git", "--version"], text=True, stdout=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise CaveError("git not found")
    match = re.match(r"git version (\d+)\.(\d+)\.(\d+)", output)
    if not match:
        raise CaveError(f"could not read the git version from: {output.strip()}")
    found = tuple(int(part) for part in match.groups())
    if found < MIN_VERSION:
        wanted = ".".join(map(str, MIN_VERSION))
        raise CaveError(f"git {wanted} or newer is required, found {'.'.join(map(str, found))}")


def git(repo, *args):
    return subprocess.run([*NO_HOOKS, "-C", str(repo), *args], check=True)


def _quiet(repo, *args):
    return subprocess.run([*NO_HOOKS, "-C", str(repo), *args], errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def query(repo, *args):
    """Run a git query, returning its output, or '' if it fails."""
    result = _quiet(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _checked(repo, *args, ok=(0,)):
    result = _quiet(repo, *args)
    if result.returncode not in ok:
        raise CaveError(f"git {args[0]} failed in {repo}")
    return result.stdout


def strict(repo, *args):
    """Run a git query that has to work. Use this when '' would mean something."""
    return _checked(repo, *args).strip()


def raw(repo, *args, limit=None):
    """strict() for review: the bytes as they are, so invalid UTF-8 reaches log.escape intact.

    With limit, reads one byte more than that and stops git there, so the caller can tell it was cut.
    """
    process = subprocess.Popen([*NO_HOOKS, "--no-pager", "-c", "core.quotePath=true", "-C", str(repo), *args],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = process.stdout.read(-1 if limit is None else limit + 1)
    if limit is not None and len(data) > limit:
        process.kill()
    if process.wait() and not (limit is not None and len(data) > limit):
        raise CaveError(f"git {args[0]} failed in {repo}")
    return data


def config_values(repo, key):
    """Every value of a config key, none if unset. Any other failure raises."""
    return _checked(repo, "config", "--get-all", key, ok=(0, 1)).splitlines()


@contextmanager
def lock(repo, wait=True):
    """Serialise work on a repo across concurrent sandboxes, yielding True once held.

    A merge holds this through its prompt, so without wait it yields False rather than queue.
    """
    path = Path(repo) / strict(repo, "rev-parse", "--git-common-dir") / "cave.lock"
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                yield False
                return
            warn(f"waiting for another cave command to finish with {repo}")
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield True


def in_progress(repo):
    """Is a rebase, merge, cherry-pick or revert under way in this worktree?"""
    git_dir = Path(strict(repo, "rev-parse", "--absolute-git-dir"))
    return any((git_dir / name).exists() for name in IN_PROGRESS)


def worktree_of(repo, branch):
    """The worktree that has branch checked out, if any."""
    path = None
    for line in strict(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def toplevel():
    return query(Path.cwd(), "rev-parse", "--show-toplevel")


def base_branch(repo):
    for name in ("main", "master"):
        if query(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}^{{commit}}"):
            return name
    raise CaveError(f"{repo} has no commits on main or master")


def public_origin(repo):
    """The origin as an unauthenticated HTTPS URL, or '' if there is none. A query or fragment may hold a secret."""
    url = query(repo, "config", "remote.origin.url")
    ssh = re.fullmatch(r"(?:ssh://)?git@([^:/]+)[:/](.+)", url)
    if ssh:
        url = f"https://{ssh.group(1)}/{ssh.group(2)}"
    return url if re.match(r"https://[^/@]+/[^?#]*$", url) else ""


def create_bundle(repo, dest, base, since=None):
    """Write a bundle of base alone for the VM: no other ref, tag or stray object.

    With since, a commit of base's the VM has, it holds only what comes after that.
    Replace refs are off, because they'd swap in objects from outside base's history.
    """
    exclude = [f"^{since}"] if since else []
    subprocess.run([*NO_HOOKS, "--no-replace-objects", "-C", str(repo), "bundle", "create", "-q", str(dest),
                    f"refs/heads/{base}", *exclude], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tracks(repo, base, path):
    """Does base hold anything at path? A gitlink doesn't count: its folder stays empty in a clone."""
    entry = strict(repo, "ls-tree", f"refs/heads/{base}", "--", path)
    return bool(entry) and not entry.startswith("160000")


def left_out(repo, base):
    """What base holds that a bundle can't carry."""
    ref = f"refs/heads/{base}"
    found = ["submodules"] if tracks(repo, base, ".gitmodules") else []
    if _quiet(repo, "grep", "-q", "filter=lfs", ref, "--", "*.gitattributes").returncode == 0:
        found.append("Git LFS files")
    return found


def _bounded(args, timeout, what, **kwargs):
    """Run git with its output captured, killing it and its children after timeout. Raises CaveError then."""
    process = subprocess.Popen(args, start_new_session=True, errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    try:
        output = process.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        output = None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # the whole group has already exited
        process.wait()
    if output is None:
        raise CaveError(f"{what} took over {timeout} seconds and was stopped. Everything is kept")
    return subprocess.CompletedProcess(args, process.returncode, output)


def bundle_heads(repo, bundle):
    """The refs a bundle lists. Raises if it's malformed or repo lacks its prerequisites."""
    def run(*args):
        return _bounded([*NO_HOOKS, "-C", str(repo), "bundle", *args, str(bundle)], VERIFY_TIMEOUT, "reading its bundle")

    if run("verify", "--quiet").returncode:
        raise CaveError("its bundle failed git bundle verify")
    listing = run("list-heads")
    if listing.returncode:
        raise CaveError(f"git bundle failed in {repo}")
    return listing.stdout.strip()


def bundle_tip(repo, bundle):
    """The commit a bundle of one branch holds, or '' if it can't be read."""
    return query(repo, "bundle", "list-heads", str(bundle)).split(" ")[0]


def fetch_bundle(repo, bundle, source, dest):
    """Import one ref of a bundle, fscking every object. False if git refused it."""
    args = ["fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
            "--no-auto-maintenance", str(bundle), f"+{source}:{dest}"]
    # Its own process group, because git leaves the unpacking to a child, and killing only
    # git would orphan it. Output isn't captured, because a child holding the pipe would
    # outlast the timeout.
    process = subprocess.Popen([*NO_HOOKS, "-c", "fetch.fsckObjects=true", "-C", str(repo), *args],
                               start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return process.wait(timeout=FETCH_TIMEOUT) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # the whole group has already exited
        process.wait()


def _isolated_env(repo):
    """The host's committer identity and none of its other git settings."""
    name, email = re.fullmatch(r"(.*) <(.*)> \S+ \S+", strict(repo, "var", "GIT_COMMITTER_IDENT")).groups()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def written_bytes(repo, start, tip):
    """What rebasing start..tip checks out: every file each commit adds or changes, counted per entry.

    A tiny bundle can point many entries at one huge blob, which is why this isn't the objects' size.
    """
    process = subprocess.Popen([*NO_HOOKS, "-C", str(repo), "log", "--no-merges", "--no-renames", "-r", "--raw", "-z",
                                "--format=", f"{start}..{tip}"], start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    timed_out = []

    def stop(late=False):
        if late:
            timed_out.append(True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # the whole group has already exited

    timer = threading.Timer(LISTING_TIMEOUT, stop, [True])
    timer.start()
    try:
        listing = process.stdout.read(MAX_LISTING + 1)  # bytes: a tree of millions of entries can't take the memory too
        if len(listing) > MAX_LISTING:
            stop()
            process.communicate()
            return MAX_WRITTEN + 1
        code = process.wait()
    finally:
        timer.cancel()
        stop()
        process.wait()
    if timed_out:  # a listing cut short says nothing, so it counts as over the limit
        return MAX_WRITTEN + 1
    if code:
        raise CaveError(f"git log failed in {repo}")
    shas = [sha.decode() for mode, sha in re.findall(rb":\d+ (\d+) [0-9a-f]+ ([0-9a-f]+) \w\0", listing)
            if mode != b"160000" and set(sha) != {ord("0")}]
    try:
        sizes = subprocess.run([*NO_HOOKS, "-C", str(repo), "cat-file", "--batch-check=%(objectsize)"],
                               input="".join(f"{sha}\n" for sha in shas), universal_newlines=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=LISTING_TIMEOUT)
    except subprocess.TimeoutExpired:
        return MAX_WRITTEN + 1
    if sizes.returncode:
        raise CaveError(f"git cat-file failed in {repo}")
    return sum(int(size) for size in sizes.stdout.split() if size.isdecimal())


def fold_collisions(repo, old, new):
    """Paths in commit new that one of macOS's filesystems would fold onto another path or folder,
    unless old already had that clash. Checking out such a pair leaves one file, with the later one's content."""
    def groups(commit):
        result = _quiet(repo, "ls-tree", "-r", "-z", "--name-only", commit)
        if result.returncode:
            raise CaveError(f"git ls-tree failed in {repo}")
        found = {}
        for name in filter(None, result.stdout.split("\0")):
            parts = name.split("/")
            for end in range(1, len(parts) + 1):
                path = "/".join(parts[:end])
                found.setdefault(unicodedata.normalize("NFC", path).casefold(), {})[path] = end == len(parts)
        return {fold: names for fold, names in found.items() if len(names) > 1 and any(names.values())}

    before = groups(old)
    return sorted(name for fold, names in groups(new).items() if set(before.get(fold, ())) != set(names)
                  for name in names)


def rebase_in_scratch(repo, branch, start, tip, dest):
    """Rebase start..tip onto branch in a disposable clone and fetch the result into ref dest.

    The commits are VM-authored and may bring a .gitattributes, so the clone must see no
    host config: with no filter or merge driver defined, there is none to name. It shares
    repo's objects, so it can reach tip unfetched. Every commit survives, even one left
    empty, so that none vanishes unseen. Returns (the commit rebased onto, the result).
    """
    env = _isolated_env(repo)
    with tempfile.TemporaryDirectory(prefix="cave-scratch-") as scratch:
        def run(*args):
            return _bounded([*NO_HOOKS, "-C", scratch, *args], REBASE_TIMEOUT, "rebasing it", env=env)

        if run("clone", "-q", "--shared", "--no-tags", "--single-branch", "--template=",
               "--branch", branch, str(repo), ".").returncode:
            raise CaveError(f"could not make a scratch clone of {repo}")
        onto = run("rev-parse", "HEAD").stdout.strip()
        # --force-rebase, or commits already on onto would keep the committer the VM gave them.
        result = run("rebase", "--force-rebase", "--reapply-cherry-picks", "--empty=keep", "--keep-empty",
                     "--onto", onto, start, tip)
        if result.returncode:
            raise RebaseFailed(result.stdout)
        strict(repo, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
               "--no-auto-maintenance", scratch, f"+HEAD:{dest}")
    return onto, strict(repo, "rev-parse", dest)


def compare_and_swap(repo, ref, new, old):
    """Move ref from old to new. False if it isn't at old any more."""
    return _quiet(repo, "update-ref", ref, new, old).returncode == 0


def is_ancestor(repo, ancestor, commit):
    return _quiet(repo, "merge-base", "--is-ancestor", ancestor, commit).returncode == 0


def sandbox_refs(repo, sid):
    return query(repo, "for-each-ref", "--format=%(refname)", f"refs/cave/{sid}/").split()


def delete_sandbox_refs(repo, sid):
    for ref in sandbox_refs(repo, sid):
        _quiet(repo, "update-ref", "-d", ref)  # prune does this, and may be behind a session by then


def sandbox_ref_ids(repo):
    refs = query(repo, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/cave/").split()
    return sorted({ref.split("/")[0] for ref in refs})
